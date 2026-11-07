// Keeps the website's own files on this device, so the app opens instantly and works with no network or
// Tailscale off. Only the app's files are stored here; the data is kept by web-shim.js, and data.json and
// /api/* always go to the server.
// web.py fills in both values. VERSION is a fingerprint of the files, so any change to them installs a
// new copy of everything at once, never a mix of old and new files.
const VERSION = 'jaf-__VERSION__';
const FILES = "__FILES__";

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSION)
    .then(c => c.addAll(FILES.map(f => new Request(f, { cache: 'reload' }))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  const file = url.pathname.slice(1) || 'dashboard.html';
  if (!FILES.includes(file)) return;
  e.respondWith(caches.open(VERSION).then(c => c.match(file)).then(hit => hit || fetch(e.request)));
});
