import os,unittest
from unittest.mock import patch
from swift_images.firebase import Firebase
class MutationTest(unittest.TestCase):
    def test_no_changes_do_not_call_api(self):
        with patch.dict(os.environ,{'DATA_SYNC_TOKEN':'test'}),patch('swift_images.firebase.request') as request:
            with Firebase():pass
        request.assert_not_called()
    def test_successful_writes_are_immediate_even_if_later_sync_fails(self):
        with patch.dict(os.environ,{'DATA_SYNC_TOKEN':'test'}):catalog=Firebase()
        with patch.object(catalog,'call',return_value={'revision':'new'}) as call:
            with self.assertRaises(RuntimeError):
                with catalog:
                    catalog.patch('produtos_swift','a',{'imagemSwift':{}},'old')
                    raise RuntimeError('Later failure')
        call.assert_called_once();self.assertTrue(catalog.catalog_changed)
