// CyberRig PWA shell cache.
// Never cache /api/* or /ws — live CAT state must always hit the server.
const CACHE_NAME = "cyberrig-shell-v18";
const SHELL_ASSETS = [
  "/",
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
  "/static/icons/favicon-32.png",
  // 100 KB of base64 faces. Worth precaching precisely because it is big:
  // re-fetching it on every SSTV page load is the one asset here that would
  // be noticeable, and the SSTV overlay renderer needs it before it can draw.
  "/static/fonts.css",
  "/sstv",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Live control paths — never intercept
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws")) {
    return;
  }
  if (event.request.method !== "GET") {
    return;
  }

  // Network-first for the app shell so UI updates land on hard refresh;
  // fall back to cache only when offline / radio host unreachable.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res.ok && (url.pathname === "/" || url.pathname.startsWith("/static/"))) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match("/")))
  );
});
