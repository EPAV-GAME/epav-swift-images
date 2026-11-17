import json,os,unittest
from unittest.mock import patch
from swift_images.firebase import Firebase
class CatalogAdapterTest(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'DATA_SYNC_TOKEN':'test-secret'});self.env.start();self.addCleanup(self.env.stop)
        self.catalog=Firebase()
    @patch('swift_images.firebase.request')
    def test_pagination_preserves_versions_and_original_fields(self,request):
        doc=dict(id='item',revision='v1',updatedAt='2026-11-17T12:00:00Z',fields={'dadosOriginais':{'mapValue':{'fields':{'Unidade Medida':{'stringValue':'PC'}}}}})
        request.side_effect=[(200,{},json.dumps({'documents':[doc],'next':'item'}).encode()),
            (200,{},b'{"documents":[],"next":null}')]
        result=self.catalog.list('produtos_swift',fields=['dadosOriginais.Unidade Medida'])
        self.assertEqual(result[0]['_updateTime'],'v1');self.assertEqual(result[0]['_updatedAt'],'2026-11-17T12:00:00Z')
        self.assertEqual(result[0]['dadosOriginais']['Unidade Medida'],'PC')
        self.assertIn('after=item',request.call_args.args[0])
        self.assertTrue(all('firestore' not in c.args[0] for c in request.call_args_list))
    @patch('swift_images.firebase.request',return_value=(200,{},b'{"revision":"new"}'))
    def test_photo_patch_keeps_optimistic_version(self,request):
        self.catalog.patch('produtos_swift','id',{'imagemSwift':{'url':'image'}},'version')
        body=json.loads(request.call_args.args[3]);self.assertEqual(body['version'],'version')
        self.assertEqual(body['fields']['imagemSwift']['mapValue']['fields']['url'],{'stringValue':'image'})
        self.assertTrue(self.catalog.catalog_changed)
    def test_missing_token_fails_closed(self):
        with patch.dict(os.environ,{'DATA_SYNC_TOKEN':''}):
            with self.assertRaises(ValueError):Firebase()
