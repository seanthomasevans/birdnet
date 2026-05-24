// Minimal SW: cache the app shell only. Audio/API requests bypass the cache.
const CACHE = "birdnet-v19";
const SHELL = ["./", "./index.html", "./styles.css", "./app.js", "./manifest.json", "./log.html", "./wall.html"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // never cache API calls or audio uploads
  if (e.request.method !== "GET") return;
  if (url.pathname.startsWith("/analyze") || url.pathname.startsWith("/enrich") ||
      url.pathname.startsWith("/history") || url.pathname.startsWith("/audio") ||
      url.pathname.startsWith("/healthz")) return;
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((resp) => {
      const copy = resp.clone();
      if (resp.ok && url.origin === location.origin) {
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return resp;
    }).catch(() => caches.match("./index.html")))
  );
});
