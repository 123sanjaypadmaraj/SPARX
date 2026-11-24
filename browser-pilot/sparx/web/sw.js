// Caches only the static shell so the app opens (and says "offline") without the server.
// API responses and the WebSocket are never touched: your data stays on the server.
const CACHE = 'sparx-shell-v2';
const SHELL = ['/', '/app.js', '/orb.js', '/style.css', '/manifest.json', '/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || !SHELL.includes(url.pathname)) return;
  // network first, so a new version shows up at once; the cached copy is only for when the server is unreachable
  e.respondWith(fetch(e.request).then((resp) => {
    if (resp.ok) {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(url.pathname, copy));
    }
    return resp;
  }).catch(() => caches.match(url.pathname).then((hit) => hit || Response.error())));
});
