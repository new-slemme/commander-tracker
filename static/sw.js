// EDH Son PWA Service Worker
const CACHE_NAME = "edh-son-v2";
const STATIC_ASSETS = [
  "/static/bootstrap.min.css",
  "/static/css/base.css",
  "/static/bootstrap.bundle.min.js",
  "/static/icons/mtg-192.png",
  "/static/icons/mtg-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS);
    }).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  // Dynamic pages (especially the home dashboard) must always go to the
  // network first so recent games / last winning deck do not get stuck behind
  // the PWA cache. Fall back to cache only when offline.
  if (event.request.mode === "navigate" || (event.request.headers.get("accept") || "").includes("text/html")) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).catch(() => cached);
    })
  );
});
