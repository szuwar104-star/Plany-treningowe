// Pamięć podręczna tylko dla plików aplikacji. Zapytania do GitHuba zawsze idą do sieci.
const CACHE = "plan-garmin-v1";
const PLIKI = ["./", "./index.html", "./manifest.webmanifest",
               "./icon-192.png", "./icon-512.png", "./icon-maskable-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PLIKI)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(k => Promise.all(k.filter(n => n !== CACHE).map(n => caches.delete(n))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // najpierw sieć (świeża wersja aplikacji), bez sieci – kopia
  e.respondWith(
    fetch(e.request)
      .then(r => { const k = r.clone(); caches.open(CACHE).then(c => c.put(e.request, k)); return r; })
      .catch(() => caches.match(e.request, {ignoreSearch: true})
        .then(r => r || caches.match("./index.html")))
  );
});
