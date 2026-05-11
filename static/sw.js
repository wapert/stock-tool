// Service Worker — caches the shell for offline launch screen
const CACHE = "stock-tool-v1";
const SHELL = ["/", "/static/manifest.json", "/static/icon-192.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Network-first: always fetch live data, fall back to cache for shell only
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  // API calls — always network, never cache
  if (url.pathname.startsWith("/analyze") || url.pathname.startsWith("/profiles")) return;
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
