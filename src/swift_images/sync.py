import argparse
import datetime
import json
import os
from pathlib import Path
from .core import SwiftClient, VERSION, match_product, optimize, product_data, request
from .firebase import Firebase

def sync_one(product, candidate, state, client, firebase, service, token, dry_run=False):
    old = product.get('imagemSwift', {})
    source = candidate['image']
    exists = False
    if old.get('key'):
        status, _, _ = request(service + '/images/' + old['key'], 'HEAD')
        exists = status == 200
    conditional = {}
    if exists and state.get('sourceUrl') == source and old.get('transformVersion') == VERSION:
        if state.get('etag'):
            conditional['If-None-Match'] = state['etag']
        if state.get('lastModified'):
            conditional['If-Modified-Since'] = state['lastModified']
    status, headers, body = client.get(source, conditional)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    new_state = {k: v for k, v in state.items() if k not in ('id', '_updateTime')}
    new_state.update(sourceUrl=source, checkedAt=now, status='unchanged')
    if status == 304:
        if not exists:
            raise RuntimeError('Unexpected 304 for missing image')
        outcome = 'unchanged'
    elif status == 200:
        optimized, pixels, sha = optimize(body)
        new_state.update(etag=headers.get('ETag', ''), lastModified=headers.get('Last-Modified', ''), pixelHash=pixels)
        if exists and old.get('pixelHash') == pixels and old.get('transformVersion') == VERSION:
            outcome = 'unchanged'
        else:
            key = 'swift/' + sha + '.webp'
            image = dict(bucket='epav-swift-images', key=key, url=service + '/images/' + key,
                         format='webp', width=512, height=512, bytes=len(optimized), sha256=sha,
                         pixelHash=pixels, transformVersion=VERSION, sourceUrl=source,
                         sourcePage=candidate['page'], sourceName=candidate['name'],
                         sourceSku=candidate['sku'], updatedAt=now)
            if not dry_run:
                request(service + '/images/' + key, 'PUT', {'Authorization': 'Bearer ' + token, 'Content-Type': 'image/webp'}, optimized)
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
    parser.add_argument('--product-code', default='')
    parser.add_argument('--source-page', default='')
    args = parser.parse_args()
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
    products = firebase.list('produtos_swift')
    if args.product_code:
        products = [p for p in products if str(p.get('codigo', '')).removesuffix('.0') == args.product_code]
        if not products:
            raise ValueError('Product code not found in Firebase')
    states = {state['id']: state for state in firebase.list('sincronizacao_imagens_swift')}
    client = SwiftClient()
    report = dict(products=len(products), pages=0, pageErrors=0, updated=0, unchanged=0, unmatched=0, errors=0, dryRun=args.dry_run)
    index = []
    urls = client.sitemap()
    if args.source_page:
        if args.source_page not in urls:
            raise ValueError('Selected product page is not in the official Swift sitemap')
        urls = [args.source_page]
    for url in urls:
        try:
            status, _, body = client.get(url)
            candidate = product_data(body.decode('utf-8', 'replace'), url) if status == 200 else None
            if candidate:
                if args.product_code and candidate['code'] != args.product_code:
                    raise ValueError('Selected page does not carry the requested original Swift code')
                index.append(candidate)
            else:
                report['pageErrors'] += 1
        except (RuntimeError, ValueError, OSError):
            report['pageErrors'] += 1
        report['pages'] += 1
        if report['pages'] % 50 == 0:
            print(json.dumps({'indexedPages': report['pages'], 'usableProducts': len(index)}), flush=True)
    if not index:
        raise RuntimeError('No usable Swift products; existing images preserved')
    products.sort(key=lambda p: (bool(p.get('imagemSwift')), states.get(p['id'], {}).get('checkedAt', ''), p['id']))
    for product in products[:args.limit or None]:
        candidate = match_product(product, index)
        if not candidate:
            report['unmatched'] += 1
            continue
        try:
            report[sync_one(product, candidate, states.get(product['id'], {}), client, firebase, service, token, args.dry_run)] += 1
        except (RuntimeError, ValueError, OSError):
            # Never log credentials, private catalog values or authenticated request objects.
            report['errors'] += 1
        if sum(report[k] for k in ('updated', 'unchanged', 'errors')) % 50 == 0:
            print(json.dumps(report), flush=True)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
    if report['errors'] or report['pageErrors']:
        raise SystemExit(1)

if __name__ == '__main__':
    main()
