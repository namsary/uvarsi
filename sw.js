// Uvar.si — service worker.
//
// Býva v KORENI webu (/sw.js), nie v /static/. Scope workera je vždy priečinok,
// z ktorého sa načíta: /static/sw.js dostane scope /static/ a na /app nedosiahne
// — appka teda mala „offline škrupinu", ktorá nikdy nikoho neobslúžila.
//
// Pravidlá:
//   • /api/*        — VŽDY zo siete, nikdy z cache. Ceny z letákov sa nesmú
//                     podávať zastarané, to je celý produkt.
//   • /prihlasenie  — jednorazový odkaz, cachovať ho nedáva zmysel.
//   • ?query        — count.json?v=<čas> je zakaždým iná adresa, cache by rástla.
//   • /static/fonts — nemenné (v názve je odtlačok obsahu), teda rovno z cache.
//   • zvyšok        — z cache hneď, na pozadí sa obnoví (stale-while-revalidate):
//                     škrupina je na obrazovke okamžite a ďalšie otvorenie ju má
//                     už aktuálnu.
const CACHE = 'uvarsi-v2';
const FONTS = '/static/fonts/';
const SHELL = [
  '/app',
  '/static/manifest.json',
  '/static/fonts/manrope-400-800.7101939e.woff2',
  '/static/fonts/anton-400.6d2997e3.woff2',
  '/static/fonts/ibmplexmono-400.c55d055f.woff2',
  '/static/fonts/ibmplexmono-600.f4faf2fe.woff2'
];

self.addEventListener('install', e => {
  // Jedna nedostupná položka nesmie zhodiť celú inštaláciu, inak zostane
  // appka bez škrupiny kvôli jednému fontu.
  e.waitUntil(caches.open(CACHE)
    .then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks =>
    Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

function refresh(request) {
  return fetch(request).then(r => {
    if (r && r.ok) {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(request, copy)).catch(() => {});
    }
    return r;
  });
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;            // dáta vždy zo siete
  if (url.pathname.startsWith('/prihlasenie')) return;      // jednorazový odkaz
  if (url.search) return;                                   // ?v=… nikdy do cache

  if (url.pathname.startsWith(FONTS)) {
    e.respondWith(caches.match(e.request).then(hit => hit || refresh(e.request)));
    return;
  }

  e.respondWith(caches.match(e.request).then(hit => {
    if (!hit) return refresh(e.request);
    refresh(e.request).catch(() => {});
    return hit;
  }));
});
