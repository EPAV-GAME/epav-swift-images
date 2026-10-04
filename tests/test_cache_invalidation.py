import os
import unittest
from unittest.mock import patch
from swift_images.firebase import Firebase
from swift_images.core import HTTPFailure


class InvalidationTest(unittest.TestCase):
    @patch('swift_images.firebase.request')
    def test_partial_failure_still_invalidates_successfully_changed_catalog(self, request):
        firebase=Firebase({'project_id':'epav-game'})
        with patch.dict(os.environ,{'CACHE_INVALIDATION_TOKEN':'test-token'}), patch('builtins.print'):
            with self.assertRaises(RuntimeError):
                with firebase:
                    firebase.catalog_changed=True
                    raise RuntimeError('Failed after successful changes')
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0],
            'https://epav-product-evaluator.kevinernandes2012.workers.dev/v1/cache/invalidate')

    @patch('swift_images.firebase.request')
    def test_no_changes_do_not_invalidate(self, request):
        with Firebase({'project_id':'epav-game'}): pass
        request.assert_not_called()

    @patch('swift_images.firebase.request',side_effect=HTTPFailure(503,'cache'))
    def test_invalidation_failure_preserves_database_success_without_leaking_token(self, request):
        firebase=Firebase({'project_id':'epav-game'})
        with patch.dict(os.environ,{'CACHE_INVALIDATION_TOKEN':'secret-example'}), patch('builtins.print') as logs:
            with firebase: firebase.catalog_changed=True
        self.assertNotIn('secret-example',str(logs.call_args))
        self.assertIn('game_cache_invalidation_failed',str(logs.call_args))
