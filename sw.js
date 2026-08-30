// Uvar.si — service worker.
//
// Býva v KORENI webu (/sw.js), nie v /static/. Scope workera je vždy priečinok,
// z ktorého sa načíta: /static/sw.js dostane scope /static/ a na /app nedosiahne
// — appka teda mala „offline škrupinu", ktorá nikdy nikoho neobslúžila.
//
// Pravidlá:
//   • /api/*        — VŽDY zo siete, nikdy z cache. Výnimkou sú iba HTML
//                     auth stránky pod /api/auth/pages/*, ktoré worker zachytí
//                     network-first, ale pri offline stave nič nenahrádza.
//   • /app a auth   — network-first, aby prepnutie auth flagu nikdy neostalo
//                     skryté starou škrupinou; offline fallback má iba /app.
//   • ?query        — count.json?v=<čas> je zakaždým iná adresa, cache by rástla.
//   • /static/fonts — nemenné (v názve je odtlačok obsahu), teda rovno z cache.
//   • zvyšok        — z cache hneď, na pozadí sa obnoví (stale-while-revalidate):
//                     škrupina je na obrazovke okamžite a ďalšie otvorenie ju má
//                     už aktuálnu.
const CACHE = 'uvarsi-v4';
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

async function networkFirstShell(request, pathname) {
  try {
    const response = await fetch(request);
    if (pathname === '/app' && response && response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then(c => c.put('/app', copy)).catch(() => {});
    }
    return response;
  } catch (_error) {
    if (pathname !== '/app') throw _error;
    return (await caches.match(request)) || caches.match('/app');
  }
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;
  const authApiPage = url.pathname.startsWith('/api/auth/pages/');
  if (url.pathname.startsWith('/api/') && !authApiPage) return; // dáta vždy zo siete
  if (url.pathname === '/co-varit-tento-tyzden') return;    // týždenné ceny nikdy zo zásoby
  if (url.pathname === '/robots.txt') return;               // crawler pravidlá vždy čerstvé
  if (url.pathname === '/sitemap.xml') return;              // sitemap musí sedieť s aktuálnymi URL
  if (url.search) return;                                   // ?v=… nikdy do cache

  const authAlias = ['/prihlasenie', '/potvrdenie', '/heslo'].includes(url.pathname);
  if (url.pathname === '/app' || authAlias || authApiPage) {
    e.respondWith(networkFirstShell(e.request, url.pathname));
    return;
  }

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
