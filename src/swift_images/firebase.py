import base64
import hashlib
import json
import time
import urllib.parse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from .core import request, HTTPFailure

def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=')

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
    def __init__(self, account):
        if account.get('project_id') != 'epav-game':
            raise ValueError('Expected epav-game Firebase project')
        self.account = account
        self.base = 'https://firestore.googleapis.com/v1/projects/epav-game/databases/(default)/documents'
        self.token, self.expires = '', 0

    def headers(self):
        if time.time() > self.expires - 60:
            now = int(time.time())
            payload = {'iss': self.account['client_email'], 'scope': 'https://www.googleapis.com/auth/datastore',
                       'aud': 'https://oauth2.googleapis.com/token', 'iat': now, 'exp': now + 3600}
            message = encode(b'{"alg":"RS256","typ":"JWT"}') + b'.' + encode(json.dumps(payload).encode())
            key = serialization.load_pem_private_key(self.account['private_key'].encode(), password=None)
            jwt = message + b'.' + encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256()))
            body = urllib.parse.urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': jwt.decode()}).encode()
            _, _, response = request('https://oauth2.googleapis.com/token', 'POST', {'Content-Type': 'application/x-www-form-urlencoded'}, body)
            result = json.loads(response)
            self.token, self.expires = result['access_token'], now + int(result['expires_in'])
        return {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}

    def list(self, collection, fields=None):
        documents, token = [], ''
        while True:
            params = [('pageSize',500)] + ([('pageToken', token)] if token else [])
            params += [('mask.fieldPaths',field) for field in fields or []]
            query = urllib.parse.urlencode(params)
            _, _, body = request(self.base + '/' + collection + '?' + query, headers=self.headers())
            result = json.loads(body)
            for doc in result.get('documents', []):
                documents.append({'id': doc['name'].rsplit('/', 1)[-1], '_updateTime': doc['updateTime'],
                                  **{k: decode(v) for k, v in doc.get('fields', {}).items()}})
            token = result.get('nextPageToken')
            if not token:
                return documents

    def patch(self, collection, doc_id, fields, update_time=None):
        query = [('updateMask.fieldPaths', field) for field in fields]
        if update_time:
            query.append(('currentDocument.updateTime', update_time))
        url = self.base + '/' + collection + '/' + urllib.parse.quote(doc_id, safe='') + '?' + urllib.parse.urlencode(query)
        body = json.dumps({'fields': {k: value(v) for k, v in fields.items()}}).encode()
        request(url, 'PATCH', self.headers(), body)

    def document_name(self, collection, doc_id):
        if not doc_id or '/' in doc_id or doc_id in ('.','..'):raise ValueError('Invalid document ID')
        return self.base.removeprefix('https://firestore.googleapis.com/v1/')+'/'+collection+'/'+doc_id

    def raw_document(self, collection, doc_id, transaction=None):
        self.document_name(collection,doc_id)
        query='?'+urllib.parse.urlencode({'transaction':transaction}) if transaction else ''
        status,_,body=request(self.base+'/'+collection+'/'+urllib.parse.quote(doc_id,safe='')+query,headers=self.headers())
        return json.loads(body) if status==200 else None

    def begin_transaction(self):
        _,_,body=request(self.base+':beginTransaction','POST',self.headers(),b'{}')
        return json.loads(body)['transaction']

    def rollback(self, transaction):
        request(self.base+':rollback','POST',self.headers(),json.dumps({'transaction':transaction}).encode())

    def commit(self, writes, transaction=None):
        payload={'writes':writes}
        if transaction:payload['transaction']=transaction
        _,_,body=request(self.base+':commit','POST',self.headers(),json.dumps(payload).encode())
        return json.loads(body)

    def archive_product(self, doc_id, update_time, reason, canonical_id=None, canonical_time=None):
        if reason not in ('duplicate','not_found_in_swift'):raise ValueError('Invalid archive reason')
        transaction=self.begin_transaction() if canonical_id else None
        try:
            result=self._archive_product(doc_id,update_time,reason,canonical_id,canonical_time,transaction)
            if transaction and result=='already_absent':self.rollback(transaction)
            return result
        except BaseException:
            if transaction:
                try:self.rollback(transaction)
                except (RuntimeError,OSError):pass
            raise

    def _archive_product(self,doc_id,update_time,reason,canonical_id,canonical_time,transaction):
        original=self.raw_document('produtos_swift',doc_id,transaction)
        if not original:return 'already_absent'
        if original.get('updateTime')!=update_time:raise HTTPFailure(412,'firestore.googleapis.com')
        archive_id=hashlib.sha256((doc_id+'|'+update_time).encode()).hexdigest()
        meta={'produtoId':doc_id,'motivo':reason,'versaoOriginal':update_time,'duplicadoDe':canonical_id,
              'arquivadoEm':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
        fields={k:value(v) for k,v in meta.items()}
        # Preserve exact Firestore types, including timestamps and references.
        fields['dados']={'mapValue':{'fields':original.get('fields',{})}}
        writes=[]
        if canonical_id:
            canonical=self.raw_document('produtos_swift',canonical_id,transaction)
            if not canonical or canonical.get('updateTime')!=canonical_time:raise HTTPFailure(412,'firestore.googleapis.com')
        writes.extend([
            {'update':{'name':self.document_name('arquivo_produtos_swift',archive_id),'fields':fields},'currentDocument':{'exists':False}},
            {'delete':original['name'],'currentDocument':{'updateTime':update_time}}
        ])
        self.commit(writes,transaction)
        return 'archived'

    def restore_product(self, archive_id):
        archive=self.raw_document('arquivo_produtos_swift',archive_id)
        if not archive:raise ValueError('Archive not found')
        fields=archive['fields'];doc_id=decode(fields['produtoId'])
        self.commit([{'update':{'name':self.document_name('produtos_swift',doc_id),
                               'fields':fields['dados']['mapValue']['fields']},'currentDocument':{'exists':False}}])
