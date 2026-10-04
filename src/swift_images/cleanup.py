"""Recoverable catalog cleanup: consolidate duplicates, archive proven absences."""
import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from .core import normalize, HTTPFailure
from .firebase import Firebase

ABSENT_REASONS = {'brand_not_in_official_catalog', 'no_verified_match'}

def duplicate_plan(products):
    groups = defaultdict(list)
    for product in products:
        code = str(product.get('codigo','')).removesuffix('.0').strip()
        if not code or not product.get('nome'): continue
        # Keep different names under the same code for manual review.
        brand = str((product.get('dadosOriginais') or {}).get('Marca','')).strip().casefold()
        groups[(code, normalize(product['nome']), brand)].append(product)
    actions = []
    for rows in groups.values():
        if len(rows) < 2: continue
        # Preserve admin changes first; then availability, photo and individual unit.
        rows.sort(key=lambda p: (bool(p.get('atualizadoPor')), p.get('disponivelNoJogo') is True,
                                bool((p.get('imagemSwift') or {}).get('url')),
                                (p.get('dadosOriginais') or {}).get('Unidade Medida') == 'PC',
                                p.get('_updateTime','') if p.get('atualizadoPor') else '',
                                str(p['id'])), reverse=True)
        canonical = rows[0]
        for duplicate in rows[1:]:
            actions.append(dict(id=duplicate['id'], canonicalId=canonical['id'],
                                updateTime=duplicate.get('_updateTime'), canonicalUpdateTime=canonical.get('_updateTime'),
                                reason='duplicate'))
    return sorted(actions,key=lambda a:a['id'])

def absence_allowed(report, candidates):
    return (report.get('pages',0) >= 100 and report.get('pageErrors',0) == 0
            and report.get('metadataSkipped',0) == 0 and len(candidates) >= 100
            and report.get('pages') == report.get('expectedPages')
            and report.get('fullCatalog') is True and not report.get('blocked'))

def archive_action(firebase, product, action, dry_run=False):
    if not product.get('_updateTime'): raise ValueError('Missing document version')
    if action.get('updateTime') and product['_updateTime'] != action['updateTime']:
        raise ValueError('Product changed since cleanup plan')
    if dry_run: return 'would_archive'
    return firebase.archive_product(product['id'], product['_updateTime'], action['reason'],
                                    action.get('canonicalId'), action.get('canonicalUpdateTime'))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--restore',default='',help='Archive ID to restore to its original product ID')
    parser.add_argument('--report',default='reports/cleanup.json')
    args=parser.parse_args()
    raw=os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON')
    if not raw: raw=Path(os.environ['FIREBASE_SERVICE_ACCOUNT_FILE']).read_text(encoding='utf-8-sig')
    firebase=Firebase(json.loads(raw))
    report=dict(dryRun=not args.apply,productsBefore=0,duplicateCandidates=0,archived=0,conflicts=0,errors=0)
    try:
        if args.restore:
            if not args.apply: raise ValueError('Restoration requires --apply')
            firebase.restore_product(args.restore);report['restored']=1
        else:
            fields=['nome','codigo','disponivelNoJogo','imagemSwift','dadosOriginais.Marca',
                    'dadosOriginais.Unidade Medida','atualizadoPor']
            products=firebase.list('produtos_swift',fields=fields)
            plan=duplicate_plan(products);report.update(productsBefore=len(products),duplicateCandidates=len(plan))
            by_id={p['id']:p for p in products}
            for action in plan:
                try:
                    outcome=archive_action(firebase,by_id[action['id']],action,not args.apply)
                    report['archived']+=args.apply and outcome=='archived'
                except HTTPFailure as error:
                    if error.status in (409,412):report['conflicts']+=1;continue
                    raise
    except HTTPFailure as error:
        report.update(errors=1,blocked='firebase_quota_exceeded' if error.status==429 else 'firebase_unavailable')
    Path(args.report).parent.mkdir(parents=True,exist_ok=True)
    Path(args.report).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)
    if report['errors']:raise SystemExit(1)

if __name__=='__main__':main()
