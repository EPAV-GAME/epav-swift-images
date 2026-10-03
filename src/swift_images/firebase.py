import base64
import json
import time
import urllib.parse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from .core import request

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
