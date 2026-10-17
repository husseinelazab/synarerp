const CACHE='synar-field-shell-v2';
const FILES=['/static/field/index.html','/static/field/style.css','/static/field/app.js','/static/field/manifest.json'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(FILES)));self.skipWaiting()});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('synar-field-shell-')&&k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(u.origin!==location.origin||e.request.method!=='GET')return;if(e.request.mode==='navigate'&&u.pathname==='/work/field/'){e.respondWith(fetch(e.request).catch(()=>caches.match('/static/field/index.html')));return}if(FILES.includes(u.pathname))e.respondWith(caches.match(e.request).then(hit=>hit||fetch(e.request)))});
