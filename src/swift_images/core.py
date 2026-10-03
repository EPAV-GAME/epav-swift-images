"""Public Swift catalog, conservative matching and deterministic image conversion."""
import hashlib
import io
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from PIL import Image, ImageOps

AGENT = 'EPAV-Swift-Images/1.0 (+https://github.com/EPAV-GAME/epav-swift-images)'
HOSTS = {'www.swift.com.br', 'swiftbr.vteximg.com.br'}
MAX_BYTES = 100 * 1024
VERSION = 'webp-512-white-v1'
Image.MAX_IMAGE_PIXELS = 20_000_000

def safe_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Source URL outside allowed hosts')
    return url

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class HTTPFailure(RuntimeError):
    def __init__(self, status, host=''):
        self.status, self.host = status, host
        super().__init__(f'HTTP {status}')

class Robots(urllib.robotparser.RobotFileParser):
    def can_fetch(self, agent, url):
        # Python's standard parser uses the first match; RFC 9309 uses the
        # longest matching rule. VTEX explicitly allows /arquivos under Disallow: /.
        entry = next((e for e in self.entries if e.applies_to(agent)), self.default_entry)
        if not entry:
            return True
        parsed = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parsed.path + ('?' + parsed.query if parsed.query else ''))
        matches = []
        for rule in entry.rulelines:
            pattern = urllib.parse.unquote(rule.path)
            expression = '^' + re.escape(pattern).replace(r'\*', '.*')
            if pattern.endswith('$'):
                expression = expression[:-2] + '$'
            if re.search(expression, path):
                matches.append((len(pattern.replace('*','').rstrip('$')), rule.allowance))
        return max(matches, default=(0, True))[1]

def request(url, method='GET', headers=None, data=None, limit=10_000_000, public_redirect=False):
    req = urllib.request.Request(url, data=data, method=method, headers={'User-Agent': AGENT, **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=40) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError('Response exceeds size limit')
            return response.status, response.headers, body
    except urllib.error.HTTPError as error:
        if error.code in (304, 404) or public_redirect and error.code in (301,302,303,307,308):
            return error.code, error.headers, b''
        raise HTTPFailure(error.code, urllib.parse.urlsplit(url).hostname) from None

class SwiftClient:
    def __init__(self, delay=1):
        self.delay, self.last, self.robots = delay, {}, {}

    def get(self, url, headers=None, redirects=0):
        safe_url(url)
        p = urllib.parse.urlsplit(url)
        if p.hostname not in self.robots:
            robots_url = f'https://{p.hostname}/robots.txt'
            status, _, body = request(robots_url, headers={'User-Agent': AGENT}, limit=500_000)
            rules = Robots()
            rules.parse(body.decode('utf-8', 'replace').splitlines() if status == 200 else [])
            self.robots[p.hostname] = rules
        rules = self.robots[p.hostname]
        if not rules.can_fetch(AGENT, url):
            raise ValueError('robots.txt forbids this URL')
        delay = max(self.delay, rules.crawl_delay(AGENT) or 0)
        time.sleep(max(0, delay - (time.monotonic() - self.last.get(p.hostname, 0))))
        self.last[p.hostname] = time.monotonic()
        result = request(url, headers={'User-Agent': AGENT, **(headers or {})}, public_redirect=True)
        if result[0] in (301,302,303,307,308):
            if redirects >= 5:
                raise ValueError('Too many public redirects')
            target = urllib.parse.urljoin(url, result[1].get('Location', ''))
            safe_url(target)
            return self.get(target, headers, redirects + 1)
        return result

    def sitemap(self):
        pending, visited, urls = ['https://www.swift.com.br/sitemap.xml'], set(), set()
        while pending:
            url = pending.pop(0)
            if url in visited: continue
            if len(visited) >= 50: raise ValueError('Sitemap traversal exceeds limit')
            visited.add(url)
            _, _, body = self.get(safe_url(url))
            root = ET.fromstring(body)
            locations = [safe_url(node.text.strip()) for node in root.findall('.//{*}loc') if node.text]
            if root.tag.rsplit('}',1)[-1] == 'sitemapindex': pending.extend(locations)
            else:
                urls.update(loc for loc in locations if re.fullmatch(r'/(?:[^/]+/p|detail/[^/]+)/?', urllib.parse.urlsplit(loc).path))
        return sorted(urls)

class StructuredData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.documents = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('type', '').lower() == 'application/ld+json':
            self.active, self.parts = True, []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            self.active = False
            try:
                self.documents.append(json.loads(''.join(self.parts)))
            except json.JSONDecodeError:
                pass

def product_records(html, page):
    parser = StructuredData()
    parser.feed(html)
    queue = list(parser.documents)
    products = []
    while queue:
        node = queue.pop(0)
        if isinstance(node, list):
            queue.extend(node)
        elif isinstance(node, dict):
            graph = node.get('@graph', [])
            queue.extend(graph if isinstance(graph, list) else [graph])
            variants = node.get('hasVariant', [])
            queue.extend(variants if isinstance(variants, list) else [variants])
            types = node.get('@type', [])
            if types == 'Product' or isinstance(types, list) and 'Product' in types:
                images = node.get('image', [])
                images = images if isinstance(images, list) else [images]
                choices = []
                for image in images:
                    image = (image.get('url') or image.get('contentUrl')) if isinstance(image, dict) else image
                    if not isinstance(image,str): continue
                    try: safe_url(image)
                    except ValueError: continue
                    filename = urllib.parse.unquote(urllib.parse.urlsplit(image).path.rsplit('/', 1)[-1])
                    code = re.search(r'(?:^|[-_])(\d{6})(?=[-_.])', filename)
                    choices.append((image, code.group(1) if code else ''))
                # Prefer an official product-coded photo to a generic first image.
                if choices and node.get('name'):
                    image, code = next((item for item in choices if item[1]), choices[0])
                    brand = node.get('brand', '')
                    brand = brand.get('name', '') if isinstance(brand,dict) else brand
                    products.append({'name': str(node['name']), 'image': image, 'page': page,
                                     'code': code, 'sku': str(node.get('sku', '')), 'brand':str(brand)})
    return products

def product_data(html, page):
    products = product_records(html, page)
    return products[0] if products else None

def normalize(name):
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    name = re.sub(r'^\s*\(in\)\s*', '', name)
    name = re.sub(r'(\d+(?:[.,]\d+)?)\s*(kg|g)\b',
                  lambda m: f' {round(float(m[1].replace(",", ".")) * (1000 if m[2] == "kg" else 1))}g ', name)
    return tuple(sorted(word for word in re.findall(r'[a-z0-9]+', name)
                        if word not in {'swift', 'de', 'da', 'do', 'das', 'dos', 'e'}))

def match_product(product, candidates):
    from .matching import ProductMatcher
    return ProductMatcher(candidates).find(product)[0]

def optimize(body):
    if len(body) > 10_000_000:
        raise ValueError('Image exceeds download limit')
    with Image.open(io.BytesIO(body)) as source:
        if source.format not in {'JPEG', 'PNG', 'WEBP'} or source.width * source.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError('Unsupported or oversized image')
        source.load()
        image = ImageOps.exif_transpose(source).convert('RGBA')
        white = Image.new('RGBA', image.size, 'white')
        white.alpha_composite(image)
        image = white.convert('RGB')
    pixel_hash = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()
    canvas = Image.new('RGB', (512, 512), 'white')
    resized = ImageOps.contain(image, (512, 512), Image.Resampling.LANCZOS)
    canvas.paste(resized, ((512 - resized.width) // 2, (512 - resized.height) // 2))
    for quality in (82, 76, 68, 60, 50, 40, 30, 20):
        output = io.BytesIO()
        canvas.save(output, format='WEBP', quality=quality, method=6)
        result = output.getvalue()
        if len(result) <= MAX_BYTES:
            return result, pixel_hash, hashlib.sha256(result).hexdigest()
    raise ValueError('Cannot meet image size budget')
