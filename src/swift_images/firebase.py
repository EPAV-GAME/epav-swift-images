import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
from .core import request, HTTPFailure

def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=')

def quote_field(name):
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        return name
    return '`' + name.replace('\\', '\\\\').replace('`', '\\`') + '`'

def field_path(path):
    return '.'.join(quote_field(part) for part in path.split('.'))

def value(data):
    if data is None:
        return {'nullValue': None}
    if isinstance(data, bool):
        return {'booleanValue': data}
    if isinstance(data, int):
        return {'integerValue': str(data)}
    if isinstance(data, float):
        return {'doubleValue': data}
    if isinstance(data, dict):
        return {'mapValue': {'fields': {k: value(v) for k, v in data.items()}}}
    if isinstance(data, list):
        return {'arrayValue': {'values': [value(v) for v in data]}}
    return {'stringValue': str(data)}

def decode(data):
    if 'mapValue' in data:
        return {k: decode(v) for k, v in data['mapValue'].get('fields', {}).items()}
    if 'arrayValue' in data:
        return [decode(v) for v in data['arrayValue'].get('values', [])]
    if 'integerValue' in data:
        return int(data['integerValue'])
    return next(iter(data.values()))

class Firebase:
    """Compatibility name for the PostgreSQL catalog API adapter. No Firebase data access."""
    def __init__(self, account=None):
        self.base = os.environ.get('DATA_API_URL','https://epav-product-evaluator.kevinernandes2012.workers.dev').rstrip('/')
        if self.base != 'https://epav-product-evaluator.kevinernandes2012.workers.dev':
            raise ValueError('Expected EPAV catalog API')
        self.token = os.environ.get('DATA_SYNC_TOKEN','')
        if not self.token: raise ValueError('DATA_SYNC_TOKEN required')
        self.catalog_changed = False

    def __enter__(self): return self
    def __exit__(self,*error): return False
    def headers(self): return {'Authorization':'Bearer '+self.token,'Content-Type':'application/json'}
    def call(self,path,payload=None):
        _,_,body=request(self.base+path,'GET' if payload is None else 'POST',self.headers(),
                         None if payload is None else json.dumps(payload).encode())
        return json.loads(body)
    def list(self,collection,fields=None):
        documents=[];after='';seen=set()
        while True:
            result=self.call('/v1/sync/dados/'+collection+'?limit=100&after='+urllib.parse.quote(after,safe=''))
            for doc in result['documents']:
                data={k:decode(v) for k,v in doc['fields'].items()}
                if fields:
                    projected={}
                    for field in fields:
                        parts=field.split('.');source=data;target=projected
                        for part in parts[:-1]:
                            if not isinstance(source,dict) or part not in source:break
                            source=source[part];target=target.setdefault(part,{})
                        else:
                            if isinstance(source,dict) and parts[-1] in source:target[parts[-1]]=source[parts[-1]]
                    data=projected
                documents.append(dict(data,id=doc['id'],_updateTime=doc['revision'],_updatedAt=doc.get('updatedAt','')))
            after=result.get('next')
            if not after:return documents
            if after in seen:raise ValueError('Repeated catalog cursor')
            seen.add(after)
    def patch(self,collection,doc_id,fields,update_time=None):
        self.call('/v1/sync/patch',dict(collection=collection,id=doc_id,
            fields={k:value(v) for k,v in fields.items()},version=update_time))
        if collection=='produtos_swift':self.catalog_changed=True
    def archive_product(self,doc_id,update_time,reason,canonical_id=None,canonical_time=None):
        result=self.call('/v1/sync/archive',dict(id=doc_id,version=update_time,reason=reason,
            canonical_id=canonical_id,canonical_version=canonical_time))
        if result['status']=='archived':self.catalog_changed=True
        return result['status']
    def restore_product(self,archive_id):
        self.call('/v1/sync/restore',dict(id=archive_id));self.catalog_changed=True
