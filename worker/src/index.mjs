const LIMIT = 100 * 1024;
const pathPattern = /^\/images\/(swift\/[a-f0-9]{64}\.webp)$/;
const json = (body, status = 200) => new Response(JSON.stringify(body), {status, headers:{'Content-Type':'application/json'}});

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

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === '/health' && request.method === 'GET') return json({service:'epav-swift-images',ready:!!env.IMAGE_SYNC_TOKEN});
    const match = path.match(pathPattern);
    if (!match) return json({error:'Not found'},404);
    const key = match[1];
    if (request.method === 'PUT') {
      if (!env.IMAGE_SYNC_TOKEN || request.headers.get('Authorization') !== `Bearer ${env.IMAGE_SYNC_TOKEN}`) return json({error:'Unauthorized'},401);
      if (request.headers.get('Content-Type') !== 'image/webp') return json({error:'WebP required'},415);
      if (Number(request.headers.get('Content-Length')) > LIMIT) return json({error:'Image too large'},413);
      const reader = request.body?.getReader();
      if (!reader) return json({error:'Empty body'},400);
      const chunks = []; let size = 0;
      while (true) {
        const {done,value} = await reader.read();
        if (done) break;
        size += value.length;
        if (size > LIMIT) {await reader.cancel(); return json({error:'Image too large'},413);}
        chunks.push(value);
      }
      const bytes = new Uint8Array(size); let cursor = 0;
      for (const chunk of chunks) {bytes.set(chunk,cursor); cursor += chunk.length;}
      const dims = dimensions(bytes);
      if (!dims || dims[0] !== 512 || dims[1] !== 512) return json({error:'512x512 WebP required'},422);
      const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
      if (key !== `swift/${digest}.webp`) return json({error:'Hash mismatch'},422);
      if (await env.IMAGES.head(key)) return json({changed:false,key});
      await env.IMAGES.put(key,bytes,{httpMetadata:{contentType:'image/webp',cacheControl:'public, max-age=31536000, immutable'}});
      return json({changed:true,key},201);
    }
    if (!['GET','HEAD'].includes(request.method)) return json({error:'Method not allowed'},405);
    const object = request.method === 'HEAD' ? await env.IMAGES.head(key) : await env.IMAGES.get(key);
    if (!object) return json({error:'Not found'},404);
    const headers = new Headers({'Content-Type':'image/webp','Cache-Control':'public, max-age=31536000, immutable','Access-Control-Allow-Origin':'*','X-Content-Type-Options':'nosniff','ETag':object.httpEtag});
    if (request.headers.get('If-None-Match') === object.httpEtag) return new Response(null,{status:304,headers});
    headers.set('Content-Length',String(object.size));
    return new Response(request.method === 'HEAD' ? null : object.body,{headers});
  }
};
