# Stabilný Cloudflare API token a serverová oprava Tesco bridge

Dátum: 2026-09-21

## Cieľ

Odstrániť závislosť opravy Tesco bridge od lokálneho notebooku, interaktívneho
Cloudflare OAuth prihlásenia a hodinového OAuth tokenu. Pravidelná obnova cien
aj bezpečná oprava bridge musia byť vykonateľné na Hetzneri bez otvoreného
prehliadača alebo spusteného Windows počítača.

## Aktuálny problém

`ops/repair_tesco_bridge.ps1` používa lokálny Node a Wrangler. Pred každou
opravou volá `wrangler whoami`; Wrangler pritom používa používateľský OAuth
token uložený na Windows. Token sa obnovuje približne každú hodinu a natívny
proces pri obnove opakovane skončil kódom `0xC0000409`. Bezpečnostná brána preto
správne zastavila opravu ešte pred serverovou alebo Cloudflare mutáciou, ale
prevádzková oprava zostala závislá od notebooku.

Samotný štvorhodinový zber na Hetzneri Cloudflare administračný token
nepotrebuje. Worker volá cez samostatný `UVARSI_TESCO_BRIDGE_SECRET`. Nový API
token sa preto nesmie dostať do runtime prostredia aplikácie ani zberača; slúži
iba úzko ohraničenej údržbe existujúceho Workera.

## Zvolená architektúra

### Cloudflare oprávnenie

V Cloudflare vznikne account-owned API token s rolou `Editor` obmedzenou iba na
existujúci Worker `uvarsi-tesco-bridge` v účte
`0510a19c8c69e8354378d3198e10302f`.

Token nesmie dostať:

- oprávnenie `Admin`,
- prístup k iným Workerom,
- DNS alebo Workers Routes oprávnenia,
- prístup k fakturácii, používateľom, zónam, R2, KV alebo D1,
- oprávnenie vytvárať alebo mazať Workery či ďalšie tokeny.

Takýto token môže čítať, aktualizovať a nasadiť existujúci Worker, ale nemôže ho
zmazať. Ak Cloudflare pri vytváraní tokenu neponúkne resource scope na jeden
Worker, migrácia sa zastaví; bez výslovného schválenia sa nesmie potichu použiť
účetný rozsah na všetky Workery.

### Uloženie tajomstva

Token sa jednorazovo vloží na Hetzner do samostatného root-only súboru:

```text
/etc/uvarsi/secrets/cloudflare-worker-token
```

Požadované vlastníctvo a práva:

```text
root:root 0600
```

Token sa nesmie uložiť do:

- Git repozitára alebo pracovného stromu,
- `/opt/uvarsi/uvarsi.env`,
- systemd unit súboru alebo crontabu,
- shell histórie, argumentov procesu, dočasného súboru na Windows,
- logu, chybového hlásenia, reportu alebo test fixture.

Hodnota sa načíta iba do prostredia dedikovaného údržbového procesu a po jeho
skončení zanikne. Bežná aplikácia, plánovací worker, dozorca ani webový proces k
súboru nesmú mať prístup.

### Serverová údržba

Vznikne root-only serverová operácia spravovaná cez
`/opt/uvarsi/uvarsi-deploy-state.sh`. Lokálny PowerShell už nebude komunikovať
s Cloudflare ani overovať používateľský e-mail. Môže nanajvýš bezpečne spustiť
pevnú serverovú operáciu cez SSH; rovnakú operáciu bude možné spustiť priamo na
Hetzneri alebo z kontrolovaného systemd procesu.

Údržbová operácia:

1. získa globálny zámok, aby nemohli bežať dve opravy,
2. overí `PLATBY_ZAPNUTE=0` aj `UVARSI_PAYMENTS_ENABLED=0` v súbore a procese,
3. vykoná read-only diagnostiku bridge,
4. opraví iba stavy `auth_mismatch`, `identity_mismatch` alebo jednoznačne
   neplatnú bridge konfiguráciu,
5. pri DNS, sieti, Cloudflare 5xx, neplatnom obsahu alebo nejasnom stave nič
   nemení,
6. overí token požiadavkou proti presnému account ID a presnému názvu Workera,
7. vytvorí nový release-bound `BRIDGE_SECRET` a novú Worker verziu,
8. aktivuje presne vytvorenú verziu na 100 % trafficu,
9. atomicky synchronizuje nové `release`, `version_id` a `BRIDGE_SECRET` do
   `/opt/uvarsi/uvarsi.env`, pričom platby vynúti na nule,
10. overí podpísanú identitu bridge a spustí ohraničený supervisor,
11. úspech potvrdí až po čerstvých dátach Kaufland, Tesco a Lidl a po
    `check-readiness`,
12. pri chybe zachová posledný overený bloček a vykoná ohraničený rollback
    bridge konfigurácie/verzie bez zásahu do používateľských dát.

Cloudflare API alebo serverový Wrangler nesmie dostať token v argumente
príkazového riadka. Ak sa použije Wrangler, musí mať presne zamknutú verziu a
kontrolné súčty rovnako ako súčasný lokálny nástroj. Preferovaná implementácia
je malý serverový klient nad oficiálnym Cloudflare API, aby oprava nebola
závislá od lokálneho OAuth, prehliadača ani Windows Node runtime.

## Automatizácia a hranice samoopravy

Denný/štvorhodinový zber zostáva bez Cloudflare administračného tokenu.
Automatická mutácia Cloudflare sa nesmie spustiť iba preto, že chýbajú nové
letákové dáta. Najprv sa musí jednoznačne potvrdiť opraviteľný bridge stav.

Samooprava bude mať tieto obmedzenia:

- najviac jeden pokus za 24 hodín,
- žiadna mutácia pri externom výpadku alebo nejednoznačnej diagnostike,
- posledný overený bloček zostane publikovaný,
- stav sa zaznamená bez tajomstiev a pošle sa existujúcim notifikačným kanálom,
- opakovaný neúspech vyžaduje manuálnu kontrolu, nie ďalšie rotácie kľúčov.

Prvá verzia môže serverovú opravu vyžadovať manuálne spustenie. Automatické
spustenie dozorcom sa zapne až po úspešnom staging teste, kontrolovanom živom
teste a samostatnom schválení.

## Migrácia

1. Pridať offline testy pre tokenové hranice, presné Cloudflare identity,
   redakciu výstupov, zámok, rate limit a rozhodovanie o opraviteľnosti.
2. Implementovať serverového Cloudflare klienta a údržbovú operáciu bez
   uloženia tokenu v repozitári.
3. Pridať bezpečný jednorazový bootstrap, ktorý token prijme bez echo a uloží
   ho priamo na Hetzner s právami `0600`.
4. Overiť token read-only volaním voči presnému účtu a Workeru.
5. Vykonať staging/simulovaný repair s falošným Cloudflare endpointom.
6. Vykonať jeden kontrolovaný živý repair so stále vypnutými platbami.
7. Až po úspechu odstrániť lokálne `wrangler whoami` a OAuth závislosť z
   produkčného repair flow.
8. Odstrániť alebo znefunkčniť staré lokálne OAuth prihlasovacie vetvy tak, aby
   sa pri chybe nemohli použiť ako tichý fallback.

Počas migrácie nesmie existovať stav, v ktorom by sa oba mechanizmy pokúsili
súčasne meniť Worker. Globálny zámok a explicitný feature gate povolia vždy iba
jeden repair backend.

## Testovanie

Povinné testy:

- jednotkové testy parsovania Cloudflare odpovedí a presnej identity,
- contract testy s lokálnym falošným API pre úspech, 401, 403, 404, 409, 429,
  5xx, timeout a neplatný JSON,
- dôkaz, že token sa neobjaví v argv, stdout, stderr, journale ani backupoch,
- dôkaz, že aplikácia a zberač nevedia čítať tokenový súbor,
- test súbehu a 24-hodinového rate limitu,
- test zachovania posledného overeného bločka pri každom zlyhaní,
- test atomickej synchronizácie a rollbacku bridge konfigurácie,
- plný existujúci offline suite,
- kontrolovaný produkčný smoke so stále vypnutými platbami.

## Akceptačné podmienky

Riešenie je hotové až keď:

- oprava nevolá používateľský Cloudflare OAuth ani `wrangler login`,
- notebook a prehliadač môžu byť vypnuté,
- token je obmedzený na presný Worker a uložený iba root-only na Hetzneri,
- bežné Uvar.si služby token nemajú vo svojom prostredí,
- server rozlíši opraviteľný auth/identity problém od výpadku siete alebo dát,
- zlyhanie nikdy nezmaže posledný platný bloček a nezapne platby,
- všetky testy a produkčný smoke prejdú,
- dokumentácia obsahuje bezpečnú rotáciu a odvolanie tokenu bez vypísania jeho
  hodnoty.

## Mimo rozsahu

- zapnutie platieb,
- zmena receptov, cien alebo zdrojov letákov,
- automatická aktivácia samoopravy bez samostatného schválenia,
- rozšírenie tokenu na ďalšie Cloudflare služby alebo Workery.
