import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker, {dimensions, authenticateAdmin, storageSnapshot, createWorker} from './index.mjs';

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

test('admin authorization checks fresh Firebase claims and rejects ordinary or disabled accounts',async()=>{
  const env = {FIREBASE_WEB_API_KEY:'public'};
  const request = new Request('https://example.com/admin/storage',{headers:{Authorization:'Bearer '+'x'.repeat(30)}});
  let calls = 0;
  const transport = user => async (url, options) => {
    calls++; assert.equal(new URL(url).host,'identitytoolkit.googleapis.com');
    assert.equal(JSON.parse(options.body).idToken,'x'.repeat(30));
    assert.equal(options.redirect,'manual');
    return Response.json({users:[user]});
  };
  assert.equal((await authenticateAdmin(new Request(request.url),env,transport({}))).status,401);
  assert.equal((await authenticateAdmin(request,env,transport({localId:'u',customAttributes:'{}'}))).status,403);
  assert.equal((await authenticateAdmin(request,env,transport({localId:'u',disabled:true,customAttributes:'{"admin":true}'}))).status,403);
  assert.deepEqual(await authenticateAdmin(request,env,transport({localId:'u',customAttributes:'{"admin":true}'})),{uid:'u'});
  assert.equal(calls,3);
});

test('storage count sums every paginated unique image object, not product references',async()=>{
  const key = c => 'swift/'+c.repeat(64)+'.webp';
  let calls = 0;
  const snapshot = await storageSnapshot({list:async options=>{
    calls++; assert.equal(options.prefix,'swift/');
    if (!options.cursor) return {objects:[{key:key('a'),size:100},{key:'swift/not-an-image.txt',size:999}],truncated:true,cursor:'next'};
    assert.equal(options.cursor,'next');
    return {objects:[{key:key('b'),size:300}],truncated:false};
  }});
  assert.equal(calls,2); assert.equal(snapshot.image_count,2); assert.equal(snapshot.total_bytes,400);
  assert.equal(snapshot.average_bytes,200); assert.equal(snapshot.largest_bytes,300);
});

test('default authentication transport retains the Workers global fetch receiver',async()=>{
  const original = globalThis.fetch;
  globalThis.fetch = async function() {
    assert.equal(this,globalThis,'Workers fetch requires its original receiver');
    return Response.json({users:[{localId:'u',customAttributes:'{"admin":true}'}]});
  };
  try {
    const result = await authenticateAdmin(new Request('https://example.com/admin/storage',{
      headers:{Authorization:'Bearer '+'x'.repeat(30)}}),{FIREBASE_WEB_API_KEY:'public'});
    assert.deepEqual(result,{uid:'u'});
  } finally {globalThis.fetch=original;}
});

test('empty storage returns zero totals and malformed pagination cannot report partial totals',async()=>{
  assert.equal((await storageSnapshot({list:async()=>({objects:[],truncated:false})})).image_count,0);
  await assert.rejects(storageSnapshot({list:async()=>({objects:[],truncated:true})}));
});

test('private storage authenticates on every request even when statistics are cached',async()=>{
  let auths = 0, lists = 0, allowed = true, now = 100;
  const handler = createWorker({authenticate:async()=>{auths++;return allowed ? {uid:'u'} : Response.json({}, {status:403});},clock:()=>now});
  const env = {IMAGES:{list:async()=>{lists++;return {objects:[],truncated:false};}}};
  const req = () => new Request('https://example.com/admin/storage',{headers:{Origin:'https://epav-game.github.io'}});
  assert.equal((await handler.fetch(req(),env)).status,200);
  const cached = await handler.fetch(req(),env);
  assert.equal(cached.headers.get('Access-Control-Allow-Origin'),'https://epav-game.github.io');
  assert.equal(cached.headers.get('Cache-Control'),'no-store');
  assert.equal(auths,2); assert.equal(lists,1);
  allowed = false;
  assert.equal((await handler.fetch(req(),env)).status,403); assert.equal(lists,1);
  allowed = true; now += 60001;
  assert.equal((await handler.fetch(req(),env)).status,200); assert.equal(lists,2);
});

test('admin CORS excludes other origins and accepts the authenticated upload preflight',async()=>{
  let auths = 0;
  const handler = createWorker({authenticate:async()=>{auths++;return {uid:'u'};}});
  const denied = await handler.fetch(new Request('https://example.com/admin/storage',{headers:{Origin:'https://evil.example'}}),{});
  assert.equal(denied.status,403); assert.equal(denied.headers.get('Access-Control-Allow-Origin'),null); assert.equal(auths,0);
  const preflight = await handler.fetch(new Request('https://example.com/admin/images/swift/x.webp',{method:'OPTIONS',headers:{Origin:'https://epav-game.github.io'}}),{});
  assert.equal(preflight.status,204); assert.equal(auths,0);
  assert.match(preflight.headers.get('Access-Control-Allow-Headers'),/Authorization/);
});

test('admin upload verifies content hash and normalization, returns manual metadata, and reuses identical files',async()=>{
  const body = fixture();
  const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',body))].map(x=>x.toString(16).padStart(2,'0')).join('');
  const handler = createWorker({authenticate:async()=>({uid:'u'})});
  let stored = false, puts = 0;
  const env = {IMAGES:{head:async()=>stored?{}:null,put:async()=>{stored=true;puts++;}}};
  const req = (key = hash, data = body) => new Request('https://example.com/admin/images/swift/'+key+'.webp',{
    method:'PUT',headers:{'Content-Type':'image/webp',Origin:'https://epav-game.github.io'},body:data});
  assert.equal((await handler.fetch(req('a'.repeat(64)),env)).status,422);
  const first = await handler.fetch(req(),env), data = await first.json();
  assert.equal(first.status,201); assert.equal(data.image.manual,true); assert.equal(data.image.bytes,body.length);
  assert.equal(data.image.width,512); assert.equal(data.image.sha256,hash);
  assert.equal((await handler.fetch(req(),env)).status,200); assert.equal(puts,1);
  assert.equal((await handler.fetch(req(hash,new Uint8Array(102401)),env)).status,413);
});

test('statistics and uploads never reach R2 when admin authentication fails',async()=>{
  const handler = createWorker({authenticate:async()=>Response.json({}, {status:403})});
  const env = {IMAGES:{list:async()=>assert.fail('R2 list'),head:async()=>assert.fail('R2 head')}};
  assert.equal((await handler.fetch(new Request('https://example.com/admin/storage'),env)).status,403);
  assert.equal((await handler.fetch(new Request('https://example.com/admin/images/swift/'+ 'a'.repeat(64)+'.webp',{method:'PUT',body:fixture()}),env)).status,403);
});
