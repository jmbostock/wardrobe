/* Clueless Closet service worker — app-shell cache, API stays network-only.
   Only activates on a secure context (HTTPS or localhost); over plain LAN HTTP
   the browser ignores it and the app works exactly as before. */
const CACHE = 'closet-v10'; // bump on every JS/CSS release so phones fetch fresh assets
const SHELL = [
  '/suggest', '/tryon', '/wardrobe', '/outfits', '/account', '/login',
  '/static/css/app.css',
  '/static/js/common.js', '/static/js/auth.js', '/static/js/suggest.js',
  '/static/js/tryon.js', '/static/js/wardrobe.js', '/static/js/outfits.js',
  '/static/js/account.js',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.png', '/static/icons/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return; // API is always network-only
  if (e.request.mode === 'navigate') {
    // navigation: network-first, fall back to the cached shell when offline
    e.respondWith(
      fetch(e.request).catch(() => caches.match('/suggest').then((r) => r || caches.match(e.request)))
    );
    return;
  }
  // static assets: cache-first, populate cache as you go
  e.respondWith(
    caches.match(e.request).then((cached) => {
      if (cached) return cached;
      return fetch(e.request).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      });
    })
  );
});
