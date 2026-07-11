/* JARVIS Remote — minimal PWA service worker.
 * Caches only the static app shell (HTML/icons/crypto.js) for fast, installable,
 * offline-tolerant loading. It MUST NEVER cache dynamic endpoints — /api/*, /ws*,
 * /uploads/*, and /login always go straight to the network so auth, commands, and
 * live data are never served stale. */
const CACHE = 'jarvis-shell-v1';
const SHELL = [
  '/',
  '/static/crypto.js',
  '/manifest.webmanifest',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Never intercept live/auth/dynamic endpoints.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/ws') ||
    url.pathname.startsWith('/uploads/') ||
    url.pathname.startsWith('/login') ||
    url.pathname.startsWith('/auto-login')
  ) {
    return;
  }

  // Static assets → cache-first (they rarely change; SW version bump refreshes them).
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest') {
    e.respondWith(
      caches.match(req).then((hit) =>
        hit ||
        fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        })
      )
    );
    return;
  }

  // App shell navigations → network-first, fall back to cached shell when offline.
  if (req.mode === 'navigate' || url.pathname === '/') {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put('/', copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match('/').then((hit) => hit || caches.match(req)))
    );
  }
});
