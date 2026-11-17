import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from swift_images.cleanup import duplicate_plan, absence_allowed, archive_action
from swift_images.firebase import Firebase, value
from swift_images.core import HTTPFailure
from swift_images.sync import main

class CleanupPlanTest(unittest.TestCase):
    def test_same_code_name_brand_is_duplicate_but_variants_are_retained(self):
        products=[dict(id='a',codigo=123,nome='Filé Swift 1KG',dadosOriginais={'Marca':'Swift','Unidade Medida':'PC'}),
                  dict(id='b',codigo=123,nome='FILE DE SWIFT 1000G',dadosOriginais={'Marca':'Swift','Unidade Medida':'ST'}),
                  dict(id='c',codigo=123,nome='Filé Swift 800G',dadosOriginais={'Marca':'Swift'}),
                  dict(id='d',codigo=456,nome='Filé Swift 1KG',dadosOriginais={'Marca':'Swift'})]
        plan=duplicate_plan(products)
        self.assertEqual([(p['id'],p['canonicalId']) for p in plan],[('b','a')])

    def test_admin_change_then_photo_is_preserved_and_missing_code_is_not_merged(self):
        base=dict(codigo=123,nome='A',disponivelNoJogo=True)
        products=[dict(base,id='a',imagemSwift={'url':'photo'}),dict(base,id='b',atualizadoPor='admin'),dict(id='c',nome='A')]
        self.assertEqual(duplicate_plan(products)[0]['canonicalId'],'b')
        self.assertEqual(len(duplicate_plan(products)),1)

    def test_pruning_requires_full_successful_catalog_and_product_metadata(self):
        report=dict(pages=1000,expectedPages=1000,fullCatalog=True,pageErrors=0,metadataSkipped=0)
        self.assertTrue(absence_allowed(report,[{}]*900))
        for change in [dict(pageErrors=1),dict(metadataSkipped=1),dict(fullCatalog=False),dict(pages=999),dict(blocked='quota')]:
            self.assertFalse(absence_allowed(dict(report,**change),[{}]*900))
        self.assertFalse(absence_allowed(report,[]))

    def test_dry_run_and_changed_versions_never_remove_records(self):
        firebase=Mock();product=dict(id='a',_updateTime='old')
        self.assertEqual(archive_action(firebase,product,dict(reason='duplicate'),True),'would_archive')
        firebase.archive_product.assert_not_called()
        with self.assertRaises(ValueError):archive_action(firebase,product,dict(reason='duplicate',updateTime='new'))

class ArchiveCommitTest(unittest.TestCase):
    def test_archive_sends_both_versions_to_atomic_api(self):
        with patch.dict(os.environ,{'DATA_SYNC_TOKEN':'test'}):firebase=Firebase()
        with patch.object(firebase,'call',return_value={'status':'archived'}) as call:
            self.assertEqual(firebase.archive_product('a','old','duplicate','b','keep'),'archived')
        self.assertEqual(call.call_args.args,('/v1/sync/archive',dict(id='a',version='old',reason='duplicate',canonical_id='b',canonical_version='keep')))
    def test_restore_uses_server_side_archive(self):
        with patch.dict(os.environ,{'DATA_SYNC_TOKEN':'test'}):firebase=Firebase()
        with patch.object(firebase,'call',return_value={'restored':True}) as call:firebase.restore_product('archive-id')
        call.assert_called_once_with('/v1/sync/restore',{'id':'archive-id'})

class CleanupIntegrationTest(unittest.TestCase):
    def run_sync(self,directory,page_failure=False,metadata_missing=False,dry_run=False,ambiguous=False,targeted=False):
        report=Path(directory)/'latest.json'
        argv=['sync','--prune-unmatched','--deduplicate','--report',str(report),'--catalog-cache',str(Path(directory)/'cache.json')]
        if dry_run:argv.append('--dry-run')
        if targeted:argv+=['--product-code','999999','--source-page','https://www.swift.com.br/example/p']
        product=dict(id='missing',nome='PRODUTO INEXISTENTE',codigo='999999',_updateTime='old')
        duplicate=dict(product,id='duplicate')
        candidates=[dict(name='Produto oficial '+str(i),code=str(i),image='image-'+str(i),page='page',sku=str(i)) for i in range(100)]
        if ambiguous:
            candidates+= [dict(candidates[0],code='999999'),dict(candidates[1],code='999999')]
        def indexed(client,urls,current,*args):
            current.update(pages=len(urls),pageErrors=int(page_failure),metadataSkipped=int(metadata_missing))
            return candidates
        env=dict(FIREBASE_SERVICE_ACCOUNT_JSON='{}',IMAGE_SYNC_TOKEN='test',IMAGE_SERVICE_URL='https://epav-swift-images.test.workers.dev')
        with patch.dict(os.environ,env),patch('sys.argv',argv),patch('swift_images.sync.Firebase') as firebase,patch('swift_images.sync.SwiftClient') as swift,patch('swift_images.sync.index_pages',side_effect=indexed),patch('builtins.print'):
            firebase.return_value.list.side_effect=[[product,duplicate],[]]
            firebase.return_value.archive_product.return_value='archived'
            swift.return_value.sitemap.return_value=['https://www.swift.com.br/example/p']+['page-'+str(i) for i in range(99)]
            if page_failure:
                with self.assertRaises(SystemExit):main()
            else:main()
            calls=firebase.return_value.archive_product.call_args_list
        return json.loads(report.read_text()),calls

    def test_healthy_catalog_consolidates_and_archives_missing_with_distinct_reasons(self):
        with tempfile.TemporaryDirectory() as directory:report,calls=self.run_sync(directory)
        self.assertEqual(report['deduplicated'],1)
        self.assertEqual(report['archivedMissing'],1)
        self.assertEqual([call.args[2] for call in calls],['duplicate','not_found_in_swift'])

    def test_page_errors_metadata_missing_ambiguity_and_targeted_lookup_prevent_absence_deletion(self):
        for option in ['page_failure','metadata_missing','ambiguous','targeted']:
            with self.subTest(option=option),tempfile.TemporaryDirectory() as directory:
                report,calls=self.run_sync(directory,**{option:True})
                self.assertEqual(report['archivedMissing'],0)
                self.assertEqual([call.args[2] for call in calls],['duplicate'])

    def test_dry_run_reports_candidates_without_any_archive_write(self):
        with tempfile.TemporaryDirectory() as directory:report,calls=self.run_sync(directory,dry_run=True)
        self.assertEqual(calls,[])
        self.assertEqual(report['duplicateCandidates'],1)
        self.assertEqual(report['pruneCandidates'],1)
        self.assertEqual(report['deduplicated'],0)
        self.assertEqual(report['archivedMissing'],0)
