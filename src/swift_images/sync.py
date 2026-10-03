import argparse
import datetime
import json
import os
from pathlib import Path
from .core import SwiftClient, VERSION, HTTPFailure, optimize, product_records, request
from .firebase import Firebase
from .matching import ProductMatcher, MATCH_VERSION

def read_cache(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data['pages'] if data.get('version') == 2 and isinstance(data.get('pages'), dict) else {}
    except (OSError, ValueError, TypeError): return {}

def write_cache(path, pages):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({'version':2,'pages':pages}), encoding='utf-8')

def write_report(path, report, unmatched):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=2), encoding='utf-8')
    Path(path).with_name('unmatched.json').write_text(json.dumps(unmatched, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)

def index_pages(client, urls, report, expected_code='', cache=None):
    index = []
    for url in urls:
        try:
            cached = cache.get(url, {}) if cache is not None else {}
            conditional = {}
            if cached.get('etag'): conditional['If-None-Match'] = cached['etag']
            if cached.get('lastModified'): conditional['If-Modified-Since'] = cached['lastModified']
            status, headers, body = client.get(url, conditional)
            if status == 404:
                if cache is not None: cache.pop(url, None)
                report['pagesSkipped'] += 1
                print(json.dumps({'event': 'page_skipped', 'reason': 'not_found', 'page': url}), flush=True)
            elif status not in (200, 304):
                raise RuntimeError('Unexpected page response')
            else:
                candidates = cached.get('products', []) if status == 304 else product_records(body.decode('utf-8', 'replace'), url)
                if status == 304 and 'products' not in cached: raise ValueError('Uncached 304')
                if cache is not None and status == 200:
                    cache[url] = dict(products=candidates, etag=headers.get('ETag',''), lastModified=headers.get('Last-Modified',''))
                if candidates:
                    if expected_code and not any(c['code'] == expected_code for c in candidates):
                        raise ValueError('Selected page does not carry the requested original Swift code')
                    index.extend(c for c in candidates if not expected_code or c['code'] == expected_code)
                else:
                    report['pagesSkipped'] += 1
                    print(json.dumps({'event': 'page_skipped', 'reason': 'no_product_image_metadata', 'page': url}), flush=True)
        except (RuntimeError, ValueError, OSError) as error:
            report['pageErrors'] += 1
            # Only public source URLs and exception classes; omit potentially sensitive diagnostics.
            print(json.dumps({'event': 'page_error', 'reason': type(error).__name__, 'page': url}), flush=True)
        report['pages'] += 1
        if report['pages'] % 50 == 0:
            print(json.dumps({'indexedPages': report['pages'], 'usableProducts': len(index),
                              'pagesSkipped': report['pagesSkipped'], 'pageErrors': report['pageErrors']}), flush=True)
    return index

class DownloadCache:
    """Per-run memo: identical sources and R2 objects are verified only once."""
    def __init__(self):
        self.heads, self.responses, self.optimized, self.uploaded = {}, {}, {}, set()

def sync_one(product, candidate, state, client, firebase, service, token, dry_run=False, downloads=None, match_method='code'):
    old = product.get('imagemSwift', {})
    source = candidate['image']
    exists = False
    if old.get('key'):
        head_url = service + '/images/' + old['key']
        if downloads is not None and head_url in downloads.heads: status = downloads.heads[head_url]
        else:
            status, _, _ = request(head_url, 'HEAD')
            if downloads is not None: downloads.heads[head_url] = status
        exists = status == 200
    conditional = {}
    if exists and state.get('sourceUrl') == source and old.get('transformVersion') == VERSION:
        if state.get('etag'):
            conditional['If-None-Match'] = state['etag']
        if state.get('lastModified'):
            conditional['If-Modified-Since'] = state['lastModified']
    response_key = (source, tuple(sorted(conditional.items())))
    if downloads is not None and response_key in downloads.responses: status, headers, body = downloads.responses[response_key]
    else:
        status, headers, body = client.get(source, conditional)
        # Retain the compact WebP after conversion, not every original multi-MB file.
        if downloads is not None: downloads.responses[response_key] = (status, headers, b'')
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    new_state = {k: v for k, v in state.items() if k not in ('id', '_updateTime')}
    new_state.update(sourceUrl=source, checkedAt=now, status='unchanged')
    if status == 304:
        if not exists:
            raise RuntimeError('Unexpected 304 for missing image')
        outcome = 'unchanged'
    elif status == 200:
        if downloads is not None and response_key in downloads.optimized: optimized, pixels, sha = downloads.optimized[response_key]
        else:
            optimized, pixels, sha = optimize(body)
            if downloads is not None: downloads.optimized[response_key] = (optimized, pixels, sha)
        new_state.update(etag=headers.get('ETag', ''), lastModified=headers.get('Last-Modified', ''), pixelHash=pixels)
        if exists and old.get('pixelHash') == pixels and old.get('transformVersion') == VERSION:
            outcome = 'unchanged'
        else:
            key = 'swift/' + sha + '.webp'
            image = dict(bucket='epav-swift-images', key=key, url=service + '/images/' + key,
                         format='webp', width=512, height=512, bytes=len(optimized), sha256=sha,
                         pixelHash=pixels, transformVersion=VERSION, sourceUrl=source,
                         sourcePage=candidate['page'], sourceName=candidate['name'],
                         sourceSku=candidate['sku'], updatedAt=now, matchMethod=match_method, matchVersion=MATCH_VERSION)
            if not dry_run:
                if downloads is None or key not in downloads.uploaded:
                    request(service + '/images/' + key, 'PUT', {'Authorization': 'Bearer ' + token, 'Content-Type': 'image/webp'}, optimized)
                    if downloads is not None: downloads.uploaded.add(key)
                # Refuse a stale match if an administrator renamed this product concurrently.
                firebase.patch('produtos_swift', product['id'], {'imagemSwift': image}, product['_updateTime'])
            outcome = 'updated'
            new_state['status'] = outcome
    else:
        raise RuntimeError(f'Image HTTP {status}')
    if not dry_run:
        firebase.patch('sincronizacao_imagens_swift', product['id'], new_state)
    return outcome

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--report', default='reports/latest.json')
    parser.add_argument('--catalog-cache', default='reports/swift-public-catalog.json')
    parser.add_argument('--missing-only', action='store_true', help='Only game products without an image')
    parser.add_argument('--product-code', default='')
    parser.add_argument('--source-page', default='')
    args = parser.parse_args()
    if args.limit < 0: parser.error('--limit must be non-negative')
    if bool(args.product_code) != bool(args.source_page):
        parser.error('--product-code and --source-page must be provided together')
    raw = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON')
    if not raw:
        raw = Path(os.environ['FIREBASE_SERVICE_ACCOUNT_FILE']).read_text(encoding='utf-8-sig')
    firebase = Firebase(json.loads(raw))
    service = os.environ['IMAGE_SERVICE_URL'].rstrip('/')
    if not service.startswith('https://epav-swift-images.') or not service.endswith('.workers.dev'):
        raise ValueError('Expected epav-swift-images Worker HTTPS URL')
    token = os.environ.get('IMAGE_SYNC_TOKEN', '')
    if not args.dry_run and not token:
        raise ValueError('IMAGE_SYNC_TOKEN required')
    report = dict(products=0, availableProducts=0, availableWithoutImage=0, pages=0, pagesSkipped=0,
                  pageErrors=0, updated=0, unchanged=0, unmatched=0, unmatchedAvailable=0, errors=0,
                  processed=0, matchesByMethod={}, unmatchedReasons={}, dryRun=args.dry_run)
    unmatched, cache = [], read_cache(args.catalog_cache)
    try:
        products = firebase.list('produtos_swift', fields=['nome','codigo','disponivelNoJogo','imagemSwift','dadosOriginais.Marca'])
        states = {state['id']: state for state in firebase.list('sincronizacao_imagens_swift')}
    except HTTPFailure as error:
        report.update(errors=1, blocked='firebase_quota_exceeded' if error.status == 429 else 'firebase_unavailable')
        write_report(args.report, report, unmatched)
        raise SystemExit(1) from None
    report['availableProducts'] = sum(p.get('disponivelNoJogo') is True for p in products)
    report['availableWithoutImage'] = sum(p.get('disponivelNoJogo') is True and not (p.get('imagemSwift') or {}).get('url') for p in products)
    if args.missing_only: products = [p for p in products if p.get('disponivelNoJogo') is True and not (p.get('imagemSwift') or {}).get('url')]
    if args.product_code:
        products = [p for p in products if str(p.get('codigo', '')).removesuffix('.0') == args.product_code]
        if not products:
            raise ValueError('Product code not found in Firebase')
    client = SwiftClient()
    report['products'] = len(products)
    try:
        urls = client.sitemap()
        if args.source_page:
            if args.source_page not in urls:
                raise ValueError('Selected product page is not in the official Swift sitemap')
            urls = [args.source_page]
        index = index_pages(client, urls, report, args.product_code, cache)
    except (RuntimeError,ValueError,OSError):
        report.update(errors=report['errors']+1, blocked='swift_catalog_unavailable')
        write_report(args.report, report, unmatched)
        raise SystemExit(1) from None
    write_cache(args.catalog_cache, cache)
    if not index:
        report.update(errors=report['errors']+1, blocked='swift_catalog_empty')
        write_report(args.report, report, unmatched)
        raise SystemExit(1)
    matcher, downloads = ProductMatcher(index), DownloadCache()
    products.sort(key=lambda p: (p.get('disponivelNoJogo') is not True, bool((p.get('imagemSwift') or {}).get('url')), states.get(p['id'], {}).get('checkedAt', ''), p['id']))
    for product in products[:args.limit or None]:
        report['processed'] += 1
        candidate, reason = matcher.find(product)
        if not candidate:
            report['unmatched'] += 1
            report['unmatchedAvailable'] += product.get('disponivelNoJogo') is True
            report['unmatchedReasons'][reason] = report['unmatchedReasons'].get(reason, 0) + 1
            unmatched.append(dict(id=product['id'], codigo=str(product.get('codigo','')), available=product.get('disponivelNoJogo') is True, reason=reason))
            continue
        report['matchesByMethod'][reason] = report['matchesByMethod'].get(reason, 0) + 1
        try:
            report[sync_one(product, candidate, states.get(product['id'], {}), client, firebase, service, token, args.dry_run, downloads, reason)] += 1
        except HTTPFailure as error:
            report['errors'] += 1
            if error.host == 'firestore.googleapis.com' and error.status == 429:
                report['blocked'] = 'firebase_quota_exceeded'
                break
        except (RuntimeError, ValueError, OSError):
            # Never log credentials, private catalog values or authenticated request objects.
            report['errors'] += 1
        if report['processed'] % 50 == 0:
            print(json.dumps(report), flush=True)
    write_report(args.report, report, unmatched)
    if report['errors'] or report['pageErrors']:
        raise SystemExit(1)

if __name__ == '__main__':
    main()
