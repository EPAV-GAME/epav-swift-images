import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from swift_images.core import product_records, SwiftClient, HTTPFailure, decode_html
from swift_images.matching import ProductMatcher
from swift_images.sync import DownloadCache, sync_one, index_pages, main
from test_sync import photo, PRODUCT_HTML

def item(name, image='a', code='', brand=''):
    return dict(name=name,image=image,code=code,brand=brand,sku='1',page='https://www.swift.com.br/example/p')

class ContextMatchTest(unittest.TestCase):
    def test_abbreviations_and_implied_species_without_product_code(self):
        cases=[('FILE PEITO SWIFT 1KG','Filé de peito de frango Swift 1000 g'),
               ('FILE MIGNON BOV CONG SWIFT KG','Filé Mignon Swift kg'),
               ('CAMARAO 7 BARBAS DESC SWIFT 400GR','Camarão sete barbas descascado Swift 400g'),
               ('AGUA CRYSTAL SG 1,5L','Água Crystal sem gás 1500ml')]
        for raw, official in cases:
            with self.subTest(raw=raw):
                selected,reason=ProductMatcher([item(official)]).find({'nome':raw})
                self.assertIsNotNone(selected);self.assertEqual(reason,'context')

    def test_brand_weight_species_cut_and_preparation_are_not_interchangeable(self):
        pairs=[('PICANHA BOV FRIBOI KG','Picanha Swift kg'),
               ('FILE PEITO SWIFT 1KG','Filé de peito de frango Swift 800g'),
               ('LINGUICA FR SWIFT 700G','Linguiça suína Swift 700g'),
               ('FILE COXA SWIFT 1KG','Filé de peito de frango Swift 1kg'),
               ('FILE PEITO TEMP SWIFT 1KG','Filé de peito de frango Swift 1kg'),
               ('PICANHA SWIFT BLACK KG','Picanha Swift Argentina kg'),
               ('AGUA CRYSTAL SG 500ML','Água Crystal com gás 500ml'),
               ('FILE PEITO RESF SWIFT 1KG','Filé de peito de frango congelado Swift 1kg')]
        for raw, official in pairs:
            with self.subTest(raw=raw): self.assertIsNone(ProductMatcher([item(official)]).find({'nome':raw})[0])

    def test_ambiguity_never_becomes_an_automatic_assignment(self):
        matcher=ProductMatcher([item('Filé de peito de frango Swift 1kg','a'),item('Filé peito frango Swift 1000g','b')])
        self.assertEqual(matcher.find({'nome':'FILE PEITO SWIFT 1KG'})[1],'ambiguous_context')
        matcher=ProductMatcher([item('A','a','123456'),item('A','b','123456')])
        self.assertEqual(matcher.find({'nome':'A','codigo':'123456'})[1],'ambiguous_code')

    def test_small_spelling_error_requires_same_brand_and_variant(self):
        matcher=ProductMatcher([item('Chocolate branco Swift 150g')])
        self.assertEqual(matcher.find({'nome':'CHOCOLATEE BRANCO SWIFT 150G'})[1],'spelling')
        self.assertIsNone(matcher.find({'nome':'CHOCOLATEE AMARGO SWIFT 150G'})[0])

    def test_manufacturer_in_name_takes_precedence_over_retailer_metadata(self):
        matcher=ProductMatcher([item('Água Crystal sem gás 1500ml',brand='Swift')])
        self.assertEqual(matcher.find({'nome':'AGUA CRYSTAL SG 1,5L','dadosOriginais':{'Marca':'TERCEIRO'}})[1],'context')

    def test_same_vtex_image_version_does_not_create_false_ambiguity(self):
        matcher=ProductMatcher([item('A','https://www.swift.com.br/x.jpg?v=1','123456'),item('A','https://www.swift.com.br/x.jpg?v=2','123456')])
        self.assertEqual(matcher.find({'nome':'A','codigo':'123456'})[1],'code')

    def test_pack_reusing_original_code_keeps_single_unit_image(self):
        matcher=ProductMatcher([item('Água sem gás Crystal 500ml','single','123456'),
                                item('Pack 6 Águas sem gás Crystal 500ml','pack','123456')])
        candidate,reason=matcher.find({'nome':'AGUA CRYSTAL SG PT 500ML','codigo':'123456'})
        self.assertEqual(candidate['image'],'single');self.assertEqual(reason,'code_packaging')
        self.assertEqual(matcher.find({'nome':'Pack 6 Águas sem gás Crystal 500ml','codigo':'123456'})[0]['image'],'pack')

class PublicCatalogTest(unittest.TestCase):
    def test_declared_or_legacy_encoding_preserves_accents(self):
        self.assertEqual(decode_html('Água'.encode(),{'Content-Type':'text/html; charset=utf-8'}),'Água')
        self.assertEqual(decode_html('Água'.encode('windows-1252'),{}),'Água')
    def test_all_variants_and_valid_product_coded_image_are_extracted(self):
        html='<script type="application/ld+json">'+json.dumps({'@type':'ProductGroup','hasVariant':[
            {'@type':'Product','name':'A','image':['https://evil.com/banner.jpg',{'contentUrl':'https://swiftbr.vteximg.com.br/arquivos/ids/1/pack%2D123456%2Da.jpg'}]},
            {'@type':'Product','name':'B','image':'https://swiftbr.vteximg.com.br/arquivos/123457-b.jpg'}]})+'</script>'
        records=product_records(html,'https://www.swift.com.br/a/p')
        self.assertEqual([p['code'] for p in records],['123456','123457'])
        self.assertNotIn('evil.com',str(records))

    def test_variant_without_photo_blocks_pruning_even_when_other_variant_has_photo(self):
        html='<script type="application/ld+json">'+json.dumps({'@type':'ProductGroup','hasVariant':[
            {'@type':'Product','name':'Missing image','image':None},
            {'@type':'Product','name':'Usable','image':'https://swiftbr.vteximg.com.br/arquivos/123456-a.jpg'}]})+'</script>'
        client=Mock();client.get.return_value=(200,{'ETag':'tag'},html.encode())
        cache={};report=dict(pages=0,pagesSkipped=0,pageErrors=0,metadataSkipped=0)
        self.assertEqual(len(index_pages(client,['url'],report,cache=cache)),1)
        self.assertEqual(report['metadataSkipped'],1)
        client.get.return_value=(304,{},b'')
        index_pages(client,['url'],report,cache=cache)
        self.assertEqual(report['metadataSkipped'],2)

    def test_nested_sitemaps_and_new_detail_urls(self):
        client=SwiftClient();client.get=Mock(side_effect=[
            (200,{},b'<sitemapindex><sitemap><loc>https://www.swift.com.br/nested.xml</loc></sitemap></sitemapindex>'),
            (200,{},b'<urlset><url><loc>https://www.swift.com.br/a/p</loc></url><url><loc>https://www.swift.com.br/detail/b</loc></url><url><loc>https://www.swift.com.br/categoria</loc></url></urlset>')])
        self.assertEqual(client.sitemap(),['https://www.swift.com.br/a/p','https://www.swift.com.br/detail/b'])

    def test_unchanged_page_uses_metadata_and_removed_page_is_dropped(self):
        cache={};report=dict(pages=0,pagesSkipped=0,pageErrors=0)
        client=Mock();client.get.return_value=(200,{'ETag':'tag'},PRODUCT_HTML.encode())
        first=index_pages(client,['url'],report,cache=cache)
        client.get.return_value=(304,{},b'')
        self.assertEqual(index_pages(client,['url'],report,cache=cache),first)
        self.assertEqual(client.get.call_args.args[1],{'If-None-Match':'tag'})
        client.get.return_value=(404,{},b'')
        with patch('builtins.print'): self.assertEqual(index_pages(client,['url'],report,cache=cache),[])
        self.assertNotIn('url',cache)

class DuplicateAndQuotaTest(unittest.TestCase):
    @patch('swift_images.sync.request',return_value=(201,{},b''))
    def test_duplicate_food_rows_download_and_upload_once(self, request):
        client=Mock();client.get.return_value=(200,{},photo())
        firebase=Mock();cache=DownloadCache()
        candidate=item('A','https://swiftbr.vteximg.com.br/arquivos/123456-a.jpg','123456')
        for doc in ['p1','p2']:
            self.assertEqual(sync_one(dict(id=doc,nome='A',_updateTime='old'),candidate,{},client,firebase,
                                     'https://epav-swift-images.test.workers.dev','secret',downloads=cache),'updated')
        self.assertEqual(client.get.call_count,1);self.assertEqual(request.call_count,1)
        self.assertEqual(firebase.patch.call_count,4)
        self.assertFalse(cache.responses[next(iter(cache.responses))][2])

    def test_firestore_quota_stops_before_crawling_and_leaves_a_report(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'latest.json'
            env=dict(FIREBASE_SERVICE_ACCOUNT_JSON='{}',IMAGE_SYNC_TOKEN='test',IMAGE_SERVICE_URL='https://epav-swift-images.test.workers.dev')
            with patch.dict(os.environ,env),patch('sys.argv',['sync','--report',str(target)]),patch('swift_images.sync.Firebase') as firebase,patch('swift_images.sync.SwiftClient') as swift,patch('builtins.print'):
                firebase.return_value.list.side_effect=HTTPFailure(429,'firestore.googleapis.com')
                with self.assertRaises(SystemExit):main()
                swift.assert_not_called()
            self.assertEqual(json.loads(target.read_text())['blocked'],'firebase_quota_exceeded')

    def test_missing_game_products_are_processed_before_existing_or_non_game(self):
        with tempfile.TemporaryDirectory() as directory:
            env=dict(FIREBASE_SERVICE_ACCOUNT_JSON='{}',IMAGE_SYNC_TOKEN='test',IMAGE_SERVICE_URL='https://epav-swift-images.test.workers.dev')
            products=[dict(id='a',nome='A',disponivelNoJogo=False),dict(id='b',nome='A',disponivelNoJogo=True,imagemSwift={'url':'existing'}),dict(id='c',nome='A',disponivelNoJogo=True)]
            with patch.dict(os.environ,env),patch('sys.argv',['sync','--limit','1','--report',str(Path(directory)/'latest.json'),'--catalog-cache',str(Path(directory)/'cache.json')]),patch('swift_images.sync.Firebase') as firebase,patch('swift_images.sync.SwiftClient') as swift,patch('swift_images.sync.sync_one',return_value='updated') as sync,patch('builtins.print'):
                firebase.return_value.list.side_effect=[products,[]]
                swift.return_value.sitemap.return_value=['valid'];swift.return_value.get.return_value=(200,{},PRODUCT_HTML.encode())
                main();self.assertEqual(sync.call_args.args[0]['id'],'c')
