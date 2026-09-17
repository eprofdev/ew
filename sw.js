/* عامل الخدمة: يخزّن ملفات الموقع ليعمل بسرعة وبلا إنترنت.
   عند تعديل أي ملف، ارفع رقم CACHE لتحديث نسخة الزوار. */
const CACHE = "labina-v1";
const ASSETS = [
  "./", "./index.html", "./portfolio.html",
  "./assets/styles.css", "./assets/brand.js", "./assets/channels.js",
  "./assets/icon.svg", "./manifest.webmanifest"
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* الشبكة أولاً ثم الذاكرة، حتى يرى الزائر أحدث نسخة وهو متصل */
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET" || new URL(e.request.url).origin !== location.origin) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request).then((hit) => hit || caches.match("./index.html")))
  );
});
