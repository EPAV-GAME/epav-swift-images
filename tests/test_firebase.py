import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from swift_images.firebase import Firebase, field_path, quote_field


class FieldMaskTest(unittest.TestCase):
    def setUp(self):
        self.firebase = Firebase({'project_id': 'epav-game'})
        self.firebase.headers = Mock(return_value={})

    @patch('swift_images.firebase.request')
    def test_nested_field_with_spaces_is_escaped_on_every_page(self, request):
        request.side_effect = [
            (200, {}, json.dumps({'nextPageToken': 'page-2'}).encode()),
            (200, {}, json.dumps({'documents': [{
                'name': self.firebase.document_name('produtos_swift', 'item'),
                'updateTime': 'version',
                'fields': {'dadosOriginais': {'mapValue': {'fields': {
                    'Unidade Medida': {'stringValue': 'PC'}
                }}}}
            }]}).encode()),
        ]
        documents = self.firebase.list('produtos_swift', ['nome', 'dadosOriginais.Unidade Medida'])
        self.assertEqual(documents[0]['dadosOriginais']['Unidade Medida'], 'PC')
        for call in request.call_args_list:
            query = parse_qs(urlsplit(call.args[0]).query)
            self.assertEqual(query['mask.fieldPaths'], ['nome', 'dadosOriginais.`Unidade Medida`'])
        self.assertEqual(query['pageToken'], ['page-2'])

    @patch('swift_images.firebase.request')
    def test_patch_preserves_literal_field_names_and_version_precondition(self, request):
        self.firebase.patch('produtos_swift', 'item', {'nome': 'A', 'campo.com espaço': 'B'}, 'version')
        query = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query['updateMask.fieldPaths'], ['nome', '`campo.com espaço`'])
        self.assertEqual(query['currentDocument.updateTime'], ['version'])
        self.assertEqual(set(json.loads(request.call_args.args[3])['fields']), {'nome', 'campo.com espaço'})
        self.assertTrue(self.firebase.catalog_changed)

    def test_special_characters_follow_firestore_quoting(self):
        self.assertEqual(field_path('dadosOriginais.Unidade Medida'), 'dadosOriginais.`Unidade Medida`')
        self.assertEqual(quote_field('ação'), '`ação`')
        self.assertEqual(quote_field('a`b\\c'), '`a\\`b\\\\c`')


if __name__ == '__main__':
    unittest.main()
