// Pliki aplikacji: najpierw sieć, bez sieci – kopia. Zapytania do GitHuba idą zawsze do sieci.
// Udostępnienie z innej aplikacji (plik albo tekst) trafia tu jako POST ./udostepnij.
const CACHE = "plan-garmin-v2";
const SCHOWEK = "plan-garmin-udostepnione";
const PLIKI = ["./", "./index.html", "./manifest.webmanifest",
               "./icon-192.png", "./icon-512.png", "./icon-maskable-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PLIKI)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(k => Promise.all(k.filter(n => n !== CACHE && n !== SCHOWEK).map(n => caches.delete(n))))
    .then(() => self.clients.claim()));
});

async function przyjmijUdostepnienie(request) {
  let tekst = "", nazwa = "";
  try {
    const dane = await request.formData();
    const plik = dane.get("plik");
    if (plik && typeof plik !== "string" && plik.size) {
      tekst = await plik.text();
      nazwa = plik.name || "";
    } else {
      tekst = [dane.get("text"), dane.get("title")].filter(Boolean).join("\n");
    }
  } catch (e) { /* pusty wynik obsłuży strona */ }
  const c = await caches.open(SCHOWEK);
  await c.put("./__udostepniony", new Response(JSON.stringify({ tekst, nazwa }),
    { headers: { "Content-Type": "application/json" } }));
  return Response.redirect("./?udostepniony=1", 303);
}

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (e.request.method === "POST" && url.pathname.endsWith("/udostepnij")) {
    e.respondWith(przyjmijUdostepnienie(e.request));
    return;
  }
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request)
      .then(r => { const k = r.clone(); caches.open(CACHE).then(c => c.put(e.request, k)); return r; })
      .catch(() => caches.match(e.request, { ignoreSearch: true })
        .then(r => r || caches.match("./index.html")))
  );
});
