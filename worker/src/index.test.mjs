import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker, {dimensions} from './index.mjs';

function fixture() {
  const b = new Uint8Array(30), v = new DataView(b.buffer), t = new TextEncoder();
  b.set(t.encode('RIFF')); v.setUint32(4,22,true); b.set(t.encode('WEBPVP8 '),8);
  v.setUint32(16,10,true); b.set([157,1,42],23); v.setUint16(26,512,true); v.setUint16(28,512,true);
  return b;
}
test('dimensions reject malformed and accept 512 WebP frame header',()=>{
  assert.equal(dimensions(new Uint8Array(30)),null);
  assert.deepEqual(dimensions(fixture()),[512,512]);
});
test('private writes require authentication and reject paths',async()=>{
  const key = 'swift/'+ 'a'.repeat(64)+'.webp';
  assert.equal((await worker.fetch(new Request('https://example.com/images/'+key,{method:'PUT'}),{IMAGE_SYNC_TOKEN:'secret'})).status,401);
  assert.equal((await worker.fetch(new Request('https://example.com/images/other.webp'),{})).status,404);
});
test('correct hash upload is idempotent; bad hash is rejected',async()=>{
  const body = fixture();
  const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',body))].map(x=>x.toString(16).padStart(2,'0')).join('');
  let stored = false, puts = 0;
  const env = {IMAGE_SYNC_TOKEN:'secret',IMAGES:{head:async()=>stored?{}:null,put:async()=>{stored=true;puts++;}}};
  const req = key => new Request('https://example.com/images/swift/'+key+'.webp',{method:'PUT',headers:{Authorization:'Bearer secret','Content-Type':'image/webp'},body});
  assert.equal((await worker.fetch(req('a'.repeat(64)),env)).status,422);
  assert.equal((await worker.fetch(req(hash),env)).status,201);
  assert.equal((await worker.fetch(req(hash),env)).status,200);
  assert.equal(puts,1);
});
test('read serves immutable image and handles conditional requests',async()=>{
  const env = {IMAGES:{get:async()=>({httpEtag:'"etag"',size:3,body:new Uint8Array([1,2,3])})}};
  const url = 'https://example.com/images/swift/'+ 'a'.repeat(64)+'.webp';
  const response = await worker.fetch(new Request(url),env);
  assert.equal(response.status,200);
  assert.equal(response.headers.get('Content-Type'),'image/webp');
  assert.equal((await worker.fetch(new Request(url,{headers:{'If-None-Match':'"etag"'}}),env)).status,304);
});
