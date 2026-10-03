"""Explainable matching of abbreviated catalog names to official product pages."""
import re
import unicodedata
import urllib.parse
from .core import normalize

MATCH_VERSION = 'context-v2'
ALIASES = {'bov': 'bovino', 'suin': 'suino', 'fr': 'frango', 'cong': 'congelado',
           'resf': 'resfriado', 'temp': 'temperado', 'desos': 'desossado',
           'desoss': 'desossado', 'desc': 'descascado', 'trad': 'tradicional',
           'choc': 'chocolate', 'verm': 'vermelho', 'porc': 'porcionado',
           'sg': 'sem gas', 'cg': 'com gas', 'sete': '7', 'filet':'file',
           'gr':'g', 'gramas':'g', 'grama':'g', 'litro':'l', 'litros':'l'}
BRANDS = ('swift', 'seara', 'friboi', 'maturatta', 'sulita', 'bem brasil', 'crystal',
          'schweppes', 'coca cola', 'santa helena', 'heinz', 'sadia', 'perdigao',
          'aurora', 'kibon', 'nestle', 'tramontina', 'marba', 'massa leve', '1953')
VARIANTS = {'black', 'argentina', 'argentino', 'chileno', 'zero', 'light', 'organico',
            'temperado', 'empanado', 'defumado', 'recheado', 'descascado', 'desossado',
            'pele', 'osso', 'integral', 'branco', 'amargo', 'leite', 'lactose',
            'com', 'sem', 'mini', 'skinpack', 'tradicional', 'gourmet'}
SPECIES = {'bovino', 'suino', 'frango', 'peru', 'cordeiro', 'pato'}
CUTS = {'picanha', 'alcatra', 'acem', 'patinho', 'maminha', 'fraldinha', 'costela',
        'contrafile', 'mignon', 'peito', 'coxa', 'sobrecoxa', 'asa', 'lombo', 'pernil',
        'bisteca', 'toscana', 'tilapia', 'salmao', 'merluza', 'bacalhau', 'camarao',
        'lagarto', 'cupim', 'coxao'}

def plain(value):
    return unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode().lower()

def context_tokens(name):
    text = re.sub(r'^\s*\(in\)\s*', '', plain(name))
    text = re.sub(r'\bjbs\s+mall\b', ' ', text)  # Internal sales channel, not a variant.
    text = re.sub(r'\bc\s*/', ' com ', text)
    text = re.sub(r'\bs\s*/', ' sem ', text)
    words = re.findall(r'[a-z]+|\d+(?:[.,]\d+)?|/', text)
    text = ' '.join(ALIASES.get(word, word) for word in words)
    # Expand species when the cut unambiguously implies it; keep explicit species.
    tokens = set(text.split())
    if not tokens & SPECIES:
        if tokens & {'peito', 'coxa', 'sobrecoxa', 'asa', 'sassami'}: text += ' frango'
        elif tokens & {'picanha', 'alcatra', 'acem', 'patinho', 'maminha', 'fraldinha', 'contrafile','mignon','lagarto','cupim','coxao'}: text += ' bovino'
    text = re.sub(r'(\d+(?:[.,]\d+)?)\s*(l|ml)\b',
                  lambda m: f' {round(float(m[1].replace(",", ".")) * (1000 if m[2] == "l" else 1))}ml ', text)
    return tuple(sorted(normalize(text)))

def brand_signals(name, brand=''):
    def signals(value):
        text = ' ' + re.sub(r'[^a-z0-9]+', ' ', plain(value)) + ' '
        return frozenset(value for value in BRANDS if ' ' + value + ' ' in text)
    named = signals(name)
    # A manufacturer in the name is more specific than a retailer's generic brand.
    return (named - {'swift'} or named) if named else signals(brand)

def profile(name, brand=''):
    tokens = context_tokens(name)
    words = set(tokens)
    return dict(tokens=tokens, brands=brand_signals(name, brand),
                weights=frozenset(t for t in tokens if re.fullmatch(r'\d+(g|ml)', t)),
                variants=frozenset(words & VARIANTS), species=frozenset(words & SPECIES),
                cuts=frozenset(words & CUTS), storage=frozenset(words & {'congelado', 'resfriado'}))

def image_identity(url):
    parsed = urllib.parse.urlsplit(url)
    # VTEX's v parameter versions the same image; other transforms remain distinct.
    query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parsed.query) if k != 'v'])
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ''))

def unique(candidates):
    by_image = {}
    for candidate in candidates: by_image.setdefault(image_identity(candidate['image']), candidate)
    return list(by_image.values())

def compatible(left, right):
    if left['brands'] and right['brands'] and left['brands'] != right['brands']: return False
    # A package of a different size or variant is not the same product.
    for key in ('weights', 'variants', 'species', 'cuts'):
        if left[key] != right[key]: return False
    if left['storage'] and right['storage'] and left['storage'] != right['storage']: return False
    return True

def one_typo(left, right):
    a, b = list(left), list(right)
    for token in left:
        if token in b: a.remove(token); b.remove(token)
    if len(a) != 1 or len(b) != 1 or min(len(a[0]), len(b[0])) < 5: return False
    a, b = a[0], b[0]
    if abs(len(a) - len(b)) > 1: return False
    if len(a) == len(b): return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b): a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))

class ProductMatcher:
    def __init__(self, candidates):
        self.candidates = list(candidates)
        self.codes, self.names, self.profiles = {}, {}, []
        for candidate in self.candidates:
            if candidate.get('code'): self.codes.setdefault(candidate['code'], []).append(candidate)
            self.names.setdefault(normalize(candidate['name']), []).append(candidate)
            self.profiles.append((candidate, profile(candidate['name'], candidate.get('brand', ''))))
        self.known_brands = set().union(*(p['brands'] for _,p in self.profiles))

    def find(self, product):
        code = str(product.get('codigo', '')).removesuffix('.0')
        matches = unique(self.codes.get(code, []))
        if matches: return (matches[0], 'code') if len(matches) == 1 else (None, 'ambiguous_code')
        wanted = profile(product['nome'], (product.get('dadosOriginais') or {}).get('Marca', ''))
        matches = unique(c for c in self.names.get(normalize(product['nome']), [])
                         if not wanted['brands'] or not profile(c['name'], c.get('brand',''))['brands']
                         or wanted['brands'] == profile(c['name'], c.get('brand',''))['brands'])
        if matches: return (matches[0], 'name') if len(matches) == 1 else (None, 'ambiguous_name')
        exact, spelling = [], []
        tokens = tuple(t for t in wanted['tokens'] if t not in {'congelado','resfriado'})
        for candidate, other in self.profiles:
            if not compatible(wanted, other): continue
            other_tokens = tuple(t for t in other['tokens'] if t not in {'congelado','resfriado'})
            if tokens == other_tokens: exact.append(candidate)
            elif len(tokens) >= 3 and wanted['brands'] and wanted['brands'] == other['brands'] and one_typo(tokens, other_tokens):
                spelling.append(candidate)
        matches = unique(exact or spelling)
        if len(matches) == 1: return matches[0], 'context' if exact else 'spelling'
        if matches: return None, 'ambiguous_context'
        if wanted['brands'] and not wanted['brands'] <= self.known_brands:
            return None, 'brand_not_in_official_catalog'
        return None, 'no_verified_match'
