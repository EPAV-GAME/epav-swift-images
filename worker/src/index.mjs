const LIMIT = 100 * 1024;
const pathPattern = /^\/images\/(swift\/[a-f0-9]{64}\.webp)$/;
const json = (body, status = 200) => new Response(JSON.stringify(body), {status, headers:{'Content-Type':'application/json'}});
const adminOrigin = 'https://epav-game.github.io';
const objectPattern = /^swift\/[a-f0-9]{64}\.webp$/;

export async function authenticateAdmin(request, env, transport = (...args) => globalThis.fetch(...args)) {
  const authorization = request.headers.get('Authorization') || '';
  if (!/^Bearer [^\s]{20,4096}$/.test(authorization)) return json({error:'AUTH_REQUIRED'},401);
  if (!env.FIREBASE_WEB_API_KEY) return json({error:'AUTH_UNAVAILABLE'},503);
  let response, data;
  try {
    response = await transport('https://identitytoolkit.googleapis.com/v1/accounts:lookup?key='+encodeURIComponent(env.FIREBASE_WEB_API_KEY), {
      method:'POST', redirect:'manual', signal:AbortSignal.timeout(8000),
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({idToken:authorization.slice(7)})
    });
    if ([400,401,403].includes(response.status)) return json({error:'AUTH_INVALID'},401);
    if (!response.ok) return json({error:'AUTH_UNAVAILABLE'},503);
    data = await response.json();
  } catch { return json({error:'AUTH_UNAVAILABLE'},503); }
  try {
    const users = data.users;
    if (users?.length !== 1 || users[0].disabled || !users[0].localId ||
        JSON.parse(users[0].customAttributes || '{}').admin !== true) return json({error:'ADMIN_REQUIRED'},403);
    return {uid:users[0].localId};
  } catch { return json({error:'ADMIN_REQUIRED'},403); }
}

export async function storageSnapshot(bucket) {
  let cursor, imageCount = 0, totalBytes = 0, largestBytes = 0;
  do {
    const page = await bucket.list({prefix:'swift/',limit:1000,...(cursor ? {cursor} : {})});
    for (const object of page.objects) {
      if (!objectPattern.test(object.key)) continue;
      imageCount++; totalBytes += object.size; largestBytes = Math.max(largestBytes,object.size);
    }
    if (page.truncated && (!page.cursor || page.cursor === cursor)) throw new Error('Invalid pagination');
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return {image_count:imageCount,total_bytes:totalBytes,average_bytes:imageCount ? Math.round(totalBytes/imageCount) : 0,
    largest_bytes:largestBytes,updated_at:new Date().toISOString(),cache_seconds:60};
}

export function dimensions(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const tag = (offset, length) => new TextDecoder().decode(bytes.slice(offset, offset + length));
  if (bytes.length < 30 || tag(0,4) !== 'RIFF' || tag(8,4) !== 'WEBP' || view.getUint32(4,true) + 8 !== bytes.length) return null;
  for (let offset = 12; offset + 8 <= bytes.length;) {
    const type = tag(offset,4), length = view.getUint32(offset + 4,true), start = offset + 8;
    if (start + length > bytes.length) return null;
    if (type === 'VP8X' && length >= 10) {
      if (bytes[start] & 2) return null; // No animated images.
      const uint24 = i => bytes[i] | bytes[i+1] << 8 | bytes[i+2] << 16;
      return [uint24(start+4)+1,uint24(start+7)+1];
    }
    if (type === 'VP8 ' && length >= 10 && bytes[start+3] === 157 && bytes[start+4] === 1 && bytes[start+5] === 42) return [view.getUint16(start+6,true)&16383, view.getUint16(start+8,true)&16383];
    if (type === 'VP8L' && length >= 5 && bytes[start] === 47) {
      const bits = view.getUint32(start+1,true);
      return [(bits&16383)+1,((bits>>>14)&16383)+1];
    }
    offset = start + length + (length % 2);
  }
  return null;
}

export function createWorker({authenticate = authenticateAdmin, clock = Date.now} = {}) {
  let snapshot, expires = 0, pending;
  const cors = (response, origin) => {
    const headers = new Headers(response.headers);
    headers.set('Cache-Control','no-store'); headers.set('Vary','Origin');
    headers.set('X-Content-Type-Options','nosniff');
    if (origin === adminOrigin) headers.set('Access-Control-Allow-Origin',origin);
    return new Response(response.body,{status:response.status,headers});
  };
  return {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === '/health' && request.method === 'GET') return json({service:'epav-swift-images',ready:!!env.IMAGE_SYNC_TOKEN});
    const admin = path.startsWith('/admin/');
    const origin = request.headers.get('Origin');
    if (admin) {
      if (origin && origin !== adminOrigin) return cors(json({error:'ORIGIN_NOT_ALLOWED'},403),origin);
      if (request.method === 'OPTIONS') {
        const headers = {'Access-Control-Allow-Origin':adminOrigin,'Access-Control-Allow-Methods':'GET, PUT, OPTIONS',
          'Access-Control-Allow-Headers':'Authorization, Content-Type','Access-Control-Max-Age':'3600','Vary':'Origin'};
        return new Response(null,{status:204,headers});
      }
      if (env.ADMIN_IP_LIMIT && !(await env.ADMIN_IP_LIMIT.limit({key:request.headers.get('cf-connecting-ip') || 'unknown'})).success)
        return cors(json({error:'RATE_LIMITED'},429),origin);
      const identity = await authenticate(request,env);
      if (identity instanceof Response) return cors(identity,origin);
      if (env.ADMIN_USER_LIMIT && !(await env.ADMIN_USER_LIMIT.limit({key:identity.uid})).success)
        return cors(json({error:'RATE_LIMITED'},429),origin);
      if (path === '/admin/storage' && request.method === 'GET') {
        try {
          if (!snapshot || expires <= clock()) {
            if (!pending) pending = storageSnapshot(env.IMAGES).then(value=>{snapshot=value;expires=clock()+60000;return value;}).finally(()=>{pending=null;});
            await pending;
          }
          return cors(json(snapshot),origin);
        } catch { return cors(json({error:'STORAGE_UNAVAILABLE'},503),origin); }
      }
    }
    const match = (admin ? path.slice('/admin'.length) : path).match(pathPattern);
    if (!match) return json({error:'Not found'},404);
    const key = match[1];
    if (request.method === 'PUT') {
      const reply = response => admin ? cors(response,origin) : response;
      if (!admin && (!env.IMAGE_SYNC_TOKEN || request.headers.get('Authorization') !== `Bearer ${env.IMAGE_SYNC_TOKEN}`)) return json({error:'Unauthorized'},401);
      if (request.headers.get('Content-Type') !== 'image/webp') return reply(json({error:'WebP required'},415));
      if (Number(request.headers.get('Content-Length')) > LIMIT) return reply(json({error:'Image too large'},413));
      const reader = request.body?.getReader();
      if (!reader) return reply(json({error:'Empty body'},400));
      const chunks = []; let size = 0;
      while (true) {
        const {done,value} = await reader.read();
        if (done) break;
        size += value.length;
        if (size > LIMIT) {await reader.cancel(); return reply(json({error:'Image too large'},413));}
        chunks.push(value);
      }
      const bytes = new Uint8Array(size); let cursor = 0;
      for (const chunk of chunks) {bytes.set(chunk,cursor); cursor += chunk.length;}
      const dims = dimensions(bytes);
      if (!dims || dims[0] !== 512 || dims[1] !== 512) return reply(json({error:'512x512 WebP required'},422));
      const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
      if (key !== `swift/${digest}.webp`) return reply(json({error:'Hash mismatch'},422));
      try {
        const exists = await env.IMAGES.head(key);
        if (!exists) {
          await env.IMAGES.put(key,bytes,{httpMetadata:{contentType:'image/webp',cacheControl:'public, max-age=31536000, immutable'}});
          snapshot = null; expires = 0;
        }
        const image = admin ? {bucket:'epav-swift-images',key,url:'https://epav-swift-images.kevinernandes2012.workers.dev/images/'+key,
          format:'webp',width:512,height:512,bytes:size,sha256:digest,manual:true,source:'admin',
          transformVersion:'admin-webp-v1',updatedAt:new Date().toISOString()} : undefined;
        return reply(json({changed:!exists,key,...(image ? {image} : {})},exists ? 200 : 201));
      } catch { return reply(json({error:'STORAGE_UNAVAILABLE'},503)); }
    }
    if (admin) return cors(json({error:'Method not allowed'},405),origin);
    if (!['GET','HEAD'].includes(request.method)) return json({error:'Method not allowed'},405);
    const object = request.method === 'HEAD' ? await env.IMAGES.head(key) : await env.IMAGES.get(key);
    if (!object) return json({error:'Not found'},404);
    const headers = new Headers({'Content-Type':'image/webp','Cache-Control':'public, max-age=31536000, immutable','Access-Control-Allow-Origin':'*','X-Content-Type-Options':'nosniff','ETag':object.httpEtag});
    if (request.headers.get('If-None-Match') === object.httpEtag) return new Response(null,{status:304,headers});
    headers.set('Content-Length',String(object.size));
    return new Response(request.method === 'HEAD' ? null : object.body,{headers});
  }
  };
}
export default createWorker();
