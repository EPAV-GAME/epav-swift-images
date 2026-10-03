import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from PIL import Image
from swift_images.core import optimize, match_product, normalize, safe_url, product_data, VERSION, Robots
from swift_images.sync import sync_one, index_pages, main

PRODUCT_HTML = '<script type="application/ld+json">{"@type":"Product","name":"A","image":["https://swiftbr.vteximg.com.br/arquivos/616920-a.jpg"]}</script>'

class PageFailuresTest(unittest.TestCase):
    def test_missing_metadata_and_stale_sitemap_are_skipped(self):
        client = Mock()
        client.get.side_effect = [(200, {}, b'<html>No product metadata</html>'),
                                  (404, {}, b''), (200, {}, PRODUCT_HTML.encode())]
        report = dict(pages=0, pagesSkipped=0, pageErrors=0)
        with patch('builtins.print'):
            products = index_pages(client, ['missing', 'removed', 'valid'], report)
        self.assertEqual(len(products), 1)
        self.assertEqual(report, dict(pages=3, pagesSkipped=2, pageErrors=0))

    def test_network_failures_remain_errors_and_do_not_expose_details(self):
        client = Mock()
        client.get.side_effect = RuntimeError('sensitive credential content')
        report = dict(pages=0, pagesSkipped=0, pageErrors=0)
        with patch('builtins.print') as log:
            self.assertEqual(index_pages(client, ['public-page'], report), [])
        self.assertEqual(report['pageErrors'], 1)
        self.assertNotIn('sensitive', str(log.call_args_list))

    def test_complete_run_succeeds_with_one_unusable_page(self):
        with tempfile.TemporaryDirectory() as directory:
            report_file = str(Path(directory) / 'report.json')
            env = dict(FIREBASE_SERVICE_ACCOUNT_JSON='{}', IMAGE_SERVICE_URL='https://epav-swift-images.test.workers.dev', IMAGE_SYNC_TOKEN='test')
            with patch.dict(os.environ, env), patch('sys.argv', ['sync', '--report', report_file]), \
                 patch('swift_images.sync.Firebase') as firebase, patch('swift_images.sync.SwiftClient') as swift, \
                 patch('swift_images.sync.sync_one', return_value='unchanged'), patch('builtins.print'):
                firebase.return_value.list.side_effect = [[dict(id='p',nome='A')], []]
                swift.return_value.sitemap.return_value = ['unusable', 'valid']
                swift.return_value.get.side_effect = [(200,{},b'<html>no JSON-LD</html>'), (200,{},PRODUCT_HTML.encode())]
                main()
            report = json.loads(Path(report_file).read_text())
            self.assertEqual(report['pagesSkipped'], 1)
            self.assertEqual(report['pageErrors'], 0)
            self.assertEqual(report['unchanged'], 1)

def photo(color='red'):
    output = io.BytesIO()
    Image.new('RGB', (900,600), color).save(output, 'PNG')
    return output.getvalue()

class ImagesTest(unittest.TestCase):
    def test_robots_longest_rule(self):
        rules = Robots()
        rules.parse(['User-agent: *','Disallow: /','Allow: /arquivos'])
        self.assertTrue(rules.can_fetch('EPAV','https://swiftbr.vteximg.com.br/arquivos/photo.jpg'))
        self.assertFalse(rules.can_fetch('EPAV','https://swiftbr.vteximg.com.br/account'))
    def test_size_and_determinism(self):
        output, pixels, sha = optimize(photo())
        self.assertLessEqual(len(output), 102400)
        self.assertEqual(Image.open(io.BytesIO(output)).size, (512,512))
        self.assertEqual(optimize(photo()), (output,pixels,sha))

    def test_invalid_image(self):
        with self.assertRaises(OSError):
            optimize(b'not an image')

    def test_weights_and_variants(self):
        self.assertEqual(normalize('(IN) FILE DE PEITO SWIFT 1KG'), normalize('Filé de peito Swift 1000 g'))
        candidates = [dict(name='Linguiça Swift 700g',code='',image='a')]
        self.assertIsNone(match_product(dict(nome='Linguiça Swift 400g'),candidates))
        self.assertIsNone(match_product(dict(nome='Linguiça de frango Swift 700g'),candidates))
        self.assertIsNone(match_product(dict(nome='Picanha Swift Black KG'),[dict(name='Picanha Swift Argentina KG',code='',image='a')]))

    def test_original_code_and_ambiguity(self):
        candidates = [dict(name='Filé Swift',code='616920',image='a')]
        self.assertEqual(match_product(dict(nome='FILE SWIFT',codigo=616920),candidates),candidates[0])
        self.assertIsNone(match_product(dict(nome='Filé Swift'), candidates+[dict(name='Filé Swift',code='',image='b')]))

    def test_source_validation(self):
        for url in ('https://evil.com/x','http://www.swift.com.br/x','https://user@www.swift.com.br/x','https://www.swift.com.br:444/x'):
            with self.assertRaises(ValueError): safe_url(url)

    def test_json_ld(self):
        html = '<script type="application/ld+json">{"@graph":[{"@type":"Product","name":"Filé","sku":"265","image":["https://swiftbr.vteximg.com.br/arquivos/ids/1/616920-file.jpg"]}]}</script>'
        self.assertEqual(product_data(html,'https://www.swift.com.br/file/p')['code'],'616920')

class SyncTest(unittest.TestCase):
    def setUp(self):
        self.candidate = dict(image='https://swiftbr.vteximg.com.br/a.jpg', page='https://www.swift.com.br/a/p', name='A',sku='1')
        self.product = dict(id='p', nome='A', _updateTime='old')
        self.firebase, self.client = Mock(), Mock()
        self.service = 'https://epav-swift-images.test.workers.dev'

    @patch('swift_images.sync.request')
    def test_new_then_304(self, request):
        self.client.get.return_value = (200, {'ETag':'source'}, photo())
        request.return_value = (201,{},b'')
        self.assertEqual(sync_one(self.product,self.candidate,{},self.client,self.firebase,self.service,'secret'),'updated')
        image = self.firebase.patch.call_args_list[0].args[2]['imagemSwift']
        self.product['imagemSwift'] = image
        self.firebase.reset_mock(); request.reset_mock()
        request.return_value = (200,{},b'')
        self.client.get.return_value = (304,{},b'')
        self.assertEqual(sync_one(self.product,self.candidate,dict(sourceUrl=self.candidate['image'],etag='source'),self.client,self.firebase,self.service,'secret'),'unchanged')
        self.assertEqual(request.call_count,1)
        self.assertEqual(self.firebase.patch.call_args.args[0],'sincronizacao_imagens_swift')
        self.assertEqual(self.client.get.call_args.args[1],{'If-None-Match':'source'})

    @patch('swift_images.sync.request')
    def test_missing_object_and_dry_run(self, request):
        self.product['imagemSwift'] = dict(key='swift/old.webp',transformVersion=VERSION)
        request.return_value = (404,{},b'')
        self.client.get.return_value = (200,{},photo())
        self.assertEqual(sync_one(self.product,self.candidate,dict(sourceUrl=self.candidate['image'],etag='old'),self.client,self.firebase,self.service,'',True),'updated')
        self.assertEqual(self.client.get.call_args.args[1],{})
        self.firebase.patch.assert_not_called()
        self.assertEqual(request.call_count,1)

    @patch('swift_images.sync.request')
    def test_same_pixels_no_upload(self, request):
        _, pixels, _ = optimize(photo())
        self.product['imagemSwift'] = dict(key='swift/old.webp',transformVersion=VERSION,pixelHash=pixels)
        request.return_value = (200,{},b'')
        self.client.get.return_value = (200,{},photo())
        self.assertEqual(sync_one(self.product,self.candidate,{},self.client,self.firebase,self.service,'secret'),'unchanged')
        self.assertEqual(request.call_count,1)

if __name__ == '__main__': unittest.main()
