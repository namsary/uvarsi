// Uvar.si — zrušený service worker.
//
// Ostrý worker je teraz /sw.js v koreni webu. Worker načítaný odtiaľto mal
// scope /static/, takže /app nikdy neovládal — PWA nemala offline škrupinu.
//
// Tento súbor tu ostáva len ako cesta von: prehliadače, ktoré starú registráciu
// ešte majú, si ju pri kontrole aktualizácie stiahnu, zmažú po nej cache
// a registráciu samy zrušia. Cache /sw.js (uvarsi-v2) sa nedotkne.
self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', e => e.waitUntil(
  caches.delete('uvarsi-v1')
    .catch(() => {})
    .then(() => self.registration.unregister())
    .catch(() => {})
));

self.addEventListener('message', e => {
  if (!e.data || e.data.type !== 'CLEAR_ACCOUNT_DATA') return;
  e.waitUntil(caches.keys().then(names =>
    Promise.all(names.map(name => caches.delete(name)))
  ));
});

// Kým sa registrácia zruší, nesmie nič podržať: všetko ide priamo zo siete.
self.addEventListener('fetch', () => {});
