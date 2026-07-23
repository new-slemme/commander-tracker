// EDH Son PWA Service Worker
const CACHE_NAME = "edh-son-v4";

// Immutable vendored and icon assets — cache-first, long-lived.
const STATIC_ASSETS = [
  "/static/bootstrap.min.css",
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

  const url = new URL(event.request.url);

  // Dynamic pages must always go to the network first so the dashboard
  // does not get stuck behind the PWA cache. Fall back to cache offline.
  if (event.request.mode === "navigate" || (event.request.headers.get("accept") || "").includes("text/html")) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
    return;
  }

  // First-party mutable CSS and JS — stale-while-revalidate so users see
  // updates after a normal reload without manual cache clearing.
  if (url.origin === self.location.origin &&
      (url.pathname.startsWith("/static/css/") || url.pathname.startsWith("/static/js/"))) {
    event.respondWith(
      caches.open(CACHE_NAME).then(function (cache) {
        return cache.match(event.request).then(function (cached) {
          var networkFetch = fetch(event.request).then(function (resp) {
            if (resp && resp.status === 200) cache.put(event.request, resp.clone());
            return resp;
          }).catch(function () { return cached; });
          return cached || networkFetch;
        });
      })
    );
    return;
  }

  // Everything else (vendored assets, icons, fonts) — cache-first.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).catch(() => cached);
    })
  );
});
