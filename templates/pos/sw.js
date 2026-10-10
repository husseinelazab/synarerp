/* عامل خدمة نقطة البيع — سِينار
   يخزن شاشة الكاشير وملفاتها وقائمة الأصناف لتعمل الشاشة عند انقطاع الإنترنت.
   المبيعات دون اتصال تُحفظ في الجهاز (pos.js) وتُزامن عند عودة الاتصال. */
const CACHE = "synar-pos-{{ version }}";
const ASSETS = [{% for a in assets %}"{{ a|escapejs }}"{% if not forloop.last %}, {% endif %}{% endfor %}];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("synar-pos-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function networkFirst(request) {
  return fetch(request)
    .then((response) => {
      // لا تُخزن صفحة الدخول أو إعادة التوجيه بدل الشاشة
      if (response.ok && !response.redirected) {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
      }
      return response;
    })
    .catch(() => caches.match(request).then((hit) => hit || new Response(
      "<h1 dir='rtl' style='font-family:sans-serif'>لا يوجد اتصال، ولم تُفتح شاشة نقطة البيع على هذا الجهاز من قبل.</h1>",
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } })));
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;  // البيع والمزامنة لا تمر بالتخزين
  const url = new URL(request.url);
  if (url.origin !== location.origin) return;
  if (url.pathname === "/pos/" || url.pathname === "/pos/api/items/") {
    event.respondWith(networkFirst(request));
  } else if (ASSETS.includes(url.pathname)) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
  }
});
