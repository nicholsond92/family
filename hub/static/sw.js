// Family Hub service worker — network-first with cache fallback.
// Always serves fresh content when online; falls back to the last
// successful response (pages and static assets) when offline.
const CACHE = "family-hub-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  // Never cache the ics feeds or auth flows.
  if (url.pathname.startsWith("/ics/") || url.pathname.startsWith("/invite/")) return;
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      try {
        const resp = await fetch(event.request);
        if (resp.ok && (url.pathname.startsWith("/static/") || resp.headers.get("content-type")?.includes("text/html"))) {
          cache.put(event.request, resp.clone());
        }
        return resp;
      } catch (err) {
        const hit = await cache.match(event.request);
        if (hit) return hit;
        throw err;
      }
    })
  );
});
