/*
 * Cardr's intentionally small offline layer.
 *
 * It caches only the public app shell and static presentation assets. Vault
 * data, market responses, uploads, and every mutation stay network-only so a
 * shared device never receives a cached copy of private collection data.
 */
const CACHE_NAME = "cardr-shell-v1";
const APP_SHELL = [
  "/",
  "/static/manifest.webmanifest",
  "/static/icons/cardr-icon.svg",
  "/static/icons/cardr-icon-maskable.svg",
];
const STATIC_ASSET = /\.(?:css|js|mjs|webmanifest|svg|png|webp|ico|woff2?)$/i;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key.startsWith("cardr-shell-") && key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

function isPrivateOrLiveData(url) {
  return url.pathname.startsWith("/api/") || url.pathname.startsWith("/uploads/");
}

function isCacheableStaticAsset(url) {
  return url.pathname.startsWith("/static/") && STATIC_ASSET.test(url.pathname);
}

async function cacheFirstStatic(request) {
  const cached = await caches.match(request);
  if (cached) return cached;

  const response = await fetch(request);
  if (response.ok && response.type === "basic") {
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || isPrivateOrLiveData(url)) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match("/"))
    );
    return;
  }

  if (isCacheableStaticAsset(url)) {
    event.respondWith(cacheFirstStatic(request));
  }
});
