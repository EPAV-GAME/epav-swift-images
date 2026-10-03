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
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=40) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError('Response exceeds size limit')
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as error:
        if error.code in (304, 404) or public_redirect and error.code in (301,302,303,307,308):
            return error.code, dict(error.headers), b''
        raise RuntimeError(f'HTTP {error.code}') from None

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
        _, _, body = self.get('https://www.swift.com.br/sitemap.xml')
        root = ET.fromstring(body)
        urls = []
        for loc in root.findall('.//{*}loc'):
            if '/sitemap/product-' in loc.text:
                _, _, data = self.get(loc.text)
                urls.extend(node.text for node in ET.fromstring(data).findall('.//{*}loc'))
        return sorted(set(safe_url(url) for url in urls if url.endswith('/p')))

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

def product_data(html, page):
    parser = StructuredData()
    parser.feed(html)
    queue = list(parser.documents)
    while queue:
        node = queue.pop(0)
        if isinstance(node, list):
            queue.extend(node)
        elif isinstance(node, dict):
            queue.extend(node.get('@graph', []))
            types = node.get('@type', [])
            if types == 'Product' or isinstance(types, list) and 'Product' in types:
                images = node.get('image', [])
                images = [images] if isinstance(images, (str, dict)) else images
                image = images[0] if images else None
                image = image.get('url') if isinstance(image, dict) else image
                if image and node.get('name'):
                    safe_url(image)
                    code = re.match(r'(\d{6,})[-_]', urllib.parse.urlsplit(image).path.rsplit('/', 1)[-1])
                    return {'name': node['name'], 'image': image, 'page': page,
                            'code': code.group(1) if code else '', 'sku': str(node.get('sku', ''))}
    return None

def normalize(name):
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    name = re.sub(r'^\s*\(in\)\s*', '', name)
    name = re.sub(r'(\d+(?:[.,]\d+)?)\s*(kg|g)\b',
                  lambda m: f' {round(float(m[1].replace(",", ".")) * (1000 if m[2] == "kg" else 1))}g ', name)
    return tuple(sorted(word for word in re.findall(r'[a-z0-9]+', name)
                        if word not in {'swift', 'de', 'da', 'do', 'das', 'dos', 'e'}))

def match_product(product, candidates):
    code = str(product.get('codigo', '')).removesuffix('.0')
    exact_code = [item for item in candidates if item['code'] and item['code'] == code]
    # The public image filename carries Swift's original product code.
    if len({item['image'] for item in exact_code}) == 1:
        return exact_code[0]
    exact_name = [item for item in candidates if normalize(item['name']) == normalize(product['nome'])]
    if len({item['image'] for item in exact_name}) == 1:
        return exact_name[0]
    return None

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
