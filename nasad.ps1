# Uvar.si - nasadenie jednym prikazom.
# Pouzitie:  cd "$env:USERPROFILE\OneDrive\Online produkt"; .\nasad.ps1
# Nahra subory na jarvis, restartuje sluzbu, nastavi Caddy a overi, ze vsetko bezi.
#
# PRAVIDLO: nasadenie MUSI vediet zlyhat. Kazde vzdialene volanie kontroluje
# navratovy kod a pri prvej chybe sa konci `exit 1`. Na serveri bezi aj druha
# appka (taktik-mapa, Caddy site mapa.89.167.72.159.sslip.io) - jej site blok
# sa kontroluje pred aj po uprave Caddyfile a nic sa nezapise, kym config
# neprejde validaciou.

$ErrorActionPreference = "Stop"
$B = $PSScriptRoot
function Krok($t) { Write-Host "`n=== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  OK  $t" -ForegroundColor Green }
function Zle($t)  { Write-Host "  !!  $t" -ForegroundColor Red }
function Zlyhaj($t) {
  Zle $t
  Write-Host "`nNASADENIE ZLYHALO - na serveri sa nic dalsie nemenilo." -ForegroundColor Red
  exit 1
}
function Vyzaduj($popis) {
  if ($LASTEXITCODE -ne 0) { Zlyhaj "$popis (navratovy kod $LASTEXITCODE)" }
}

Krok "1/8  Priecinky na serveri"
ssh jarvis "mkdir -p /opt/uvarsi/app/static /var/www/uvarsi /var/lib/uvarsi"
Vyzaduj "priecinky na serveri sa nepodarilo vytvorit"
Ok "pripravene"

Krok "2/8  Kontrola uvarsi.env (hodnoty klucov sa NIKDY nevypisuju ani nenahravaju)"
$envCheck = @'
set -u
F=/opt/uvarsi/uvarsi.env
if [ ! -f "$F" ]; then
  echo "CHYBA: subor uvarsi.env na serveri neexistuje"
  exit 1
fi
CHYBA=0
for k in ANTHROPIC_API_KEY RESEND_API_KEY; do
  if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${k}=[^[:space:]]" "$F"; then
    echo "  $k: pritomny"
  else
    echo "  $k: CHYBA"
    CHYBA=1
  fi
done
exit $CHYBA
'@ -replace "`r`n", "`n"
$envCheck | ssh jarvis "tr -d '\r' > /tmp/uvarsi_env_check.sh; bash /tmp/uvarsi_env_check.sh"
Vyzaduj "uvarsi.env na serveri chyba alebo v nom nie su oba kluce"
Ok "env ma oba kluce"

Krok "3/8  Nahravam subory"
$subory = @(
  @{ l = "$B\app\config.py";            r = "/opt/uvarsi/app/config.py" },
  @{ l = "$B\app\db_rezim.py";          r = "/opt/uvarsi/app/db_rezim.py" },
  @{ l = "$B\app\naklady.py";           r = "/opt/uvarsi/app/naklady.py" },
  @{ l = "$B\app\auth_data.py";         r = "/opt/uvarsi/app/auth_data.py" },
  @{ l = "$B\app\weekly_data.py";       r = "/opt/uvarsi/app/weekly_data.py" },
  @{ l = "$B\app\offer_data.py";        r = "/opt/uvarsi/app/offer_data.py" },
  @{ l = "$B\app\landing_data.py";      r = "/opt/uvarsi/app/landing_data.py" },
  @{ l = "$B\app\receipt_data.py";      r = "/opt/uvarsi/app/receipt_data.py" },
  @{ l = "$B\app\plan_data.py";         r = "/opt/uvarsi/app/plan_data.py" },
  @{ l = "$B\app\predpocet.py";         r = "/opt/uvarsi/app/predpocet.py" },
  @{ l = "$B\app\server.py";            r = "/opt/uvarsi/app/server.py" },
  @{ l = "$B\app\platby.py";            r = "/opt/uvarsi/app/platby.py" },
  @{ l = "$B\app\premium_cli.py";       r = "/opt/uvarsi/app/premium_cli.py" },
  @{ l = "$B\app\zbierac_akcii.py";     r = "/opt/uvarsi/app/zbierac_akcii.py" },
  @{ l = "$B\hetzner\refresh_blocek.py"; r = "/opt/uvarsi/refresh_blocek.py" },
  @{ l = "$B\hetzner\recepty.py";       r = "/opt/uvarsi/recepty.py" },
  @{ l = "$B\hetzner\dozorca.sh";       r = "/opt/uvarsi/dozorca.sh" },
  @{ l = "$B\hetzner\zaloha.sh";        r = "/opt/uvarsi/zaloha.sh" },
  @{ l = "$B\VERSION";                  r = "/opt/uvarsi/VERSION" },
  @{ l = "$B\index.html";               r = "/var/www/uvarsi/index.html" },
  @{ l = "$B\sw.js";                    r = "/var/www/uvarsi/sw.js" }
)
foreach ($s in $subory) {
  if (-not (Test-Path $s.l)) { Zlyhaj "chyba lokalny subor $($s.l)" }
  scp -q $s.l "jarvis:$($s.r)"
  Vyzaduj "prenos zlyhal: $($s.l)"
  Ok (Split-Path $s.l -Leaf)
}
if (-not (Test-Path "$B\app\static")) { Zlyhaj "chyba lokalny priecinok $B\app\static" }
scp -q -r "$B\app\static\*" "jarvis:/opt/uvarsi/app/static/"
Vyzaduj "prenos zlyhal: app\static"
Ok "static/ (PWA)"

# Windows uklada .sh s CRLF; po binarnom scp je shebang "#!/bin/bash\r" a Linux
# hlada interpret /bin/bash\r -> "cannot execute: required file not found".
ssh jarvis "sed -i 's/\r//' /opt/uvarsi/*.sh; chmod +x /opt/uvarsi/*.sh"
Vyzaduj "normalizacia koncov riadkov v shell skriptoch zlyhala"
Ok "shell skripty maju LF konce riadkov"

Krok "4/8  Python venv a zavislosti"
# Na cerstvom serveri /opt/uvarsi/venv neexistuje - bez neho pip zlyha a systemd
# sa toci v 203/EXEC. Vytvorime ho, ak chyba.
ssh jarvis "set -eu; [ -x /opt/uvarsi/venv/bin/python ] || python3 -m venv /opt/uvarsi/venv; /opt/uvarsi/venv/bin/pip -q install fastapi uvicorn anthropic pillow requests; command -v sqlite3 >/dev/null || apt-get install -y sqlite3 >/dev/null 2>&1; command -v sqlite3 >/dev/null; chmod +x /opt/uvarsi/dozorca.sh"
Vyzaduj "venv alebo zavislosti sa nepodarilo pripravit"
Ok "venv, zavislosti aj sqlite3 (pre dozorcu)"

Krok "5/8  Sluzba uvarsi (bezi stale, prezije restart)"
$svc = @'
[Unit]
Description=Uvarsi app
After=network.target

[Service]
WorkingDirectory=/opt/uvarsi/app
Environment=UVARSI_URL=https://uvar.si
Environment=UVARSI_LANDING_DATA=/var/lib/uvarsi/landing_data.json
Environment=UVARSI_VERSION_FILE=/opt/uvarsi/VERSION
ExecStart=/opt/uvarsi/venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8090
Restart=always

[Install]
WantedBy=multi-user.target
'@ -replace "`r`n", "`n"
$svc | ssh jarvis "tr -d '\r' > /etc/systemd/system/uvarsi.service"
Vyzaduj "systemd jednotku sa nepodarilo zapisat"
ssh jarvis "set -eu; systemctl daemon-reload; systemctl enable uvarsi >/dev/null 2>&1; systemctl restart uvarsi; sleep 3; systemctl is-active uvarsi >/dev/null"
Vyzaduj "sluzba uvarsi po restarte nebezi"
Ok "sluzba bezi"

Krok "6/8  Caddy (appka na /app, landing na hlavnej)"
# Navrh configu vznika VEDLA ostreho suboru. Ostry Caddyfile sa nedotkne, kym
# navrh neprejde `caddy validate` ako samostatny prikaz (nie clanok rury -
# v rure je navratovy kod posledneho clanku, teda vzdy 0).
$py = @'
import sys
p = "/etc/caddy/Caddyfile"
novy = p + ".nove"
MAPA = "mapa.89.167.72.159.sslip.io"
s = open(p).read()
if MAPA not in s:
    sys.exit("CHYBA: v Caddyfile chyba site blok " + MAPA + " uz pred upravou - nerobim nic")
blok = """uvar.si, www.uvar.si, uvarsi.sk, www.uvarsi.sk, uvarsi.89.167.72.159.sslip.io {
	encode gzip
	handle /api/* {
		reverse_proxy 127.0.0.1:8090
	}
	handle /app* {
		reverse_proxy 127.0.0.1:8090
	}
	handle /prihlasenie* {
		reverse_proxy 127.0.0.1:8090
	}
	handle /static/fonts/* {
		header Cache-Control "public, max-age=31536000, immutable"
		reverse_proxy 127.0.0.1:8090
	}
	handle /static/* {
		reverse_proxy 127.0.0.1:8090
	}
	handle {
		root * /var/www/uvarsi
		file_server
	}
}"""
# Odstrani VSETKY bloky, ktore spominaju uvar.si / uvarsi.sk / uvarsi.<ip>.sslip.io
# (aj stare presmerovanie), inak Caddy hlasi "ambiguous site definition".
# Ostatne weby na serveri (napr. mapa.*) ostavaju nedotknute.
def bloky(text):
    """Vrati zoznam (zaciatok, koniec) top-level blokov { }."""
    out, i = [], 0
    while True:
        j = text.find("{", i)
        if j == -1:
            return out
        zac = text.rfind("\n", 0, j) + 1
        hlbka, k = 0, j
        while k < len(text):
            if text[k] == "{":
                hlbka += 1
            elif text[k] == "}":
                hlbka -= 1
                if hlbka == 0:
                    break
            k += 1
        if k >= len(text):
            return out
        out.append((zac, k + 1))
        i = k + 1

odstranene = 0
for zac, kon in reversed(bloky(s)):
    hlavicka = s[zac:s.find("{", zac)]
    if "uvar.si" in hlavicka or "uvarsi." in hlavicka:
        s = s[:zac] + s[kon:]
        odstranene += 1

s = s.rstrip() + "\n\n" + blok + "\n"
if MAPA not in s:
    sys.exit("CHYBA: uprava by odstranila site blok " + MAPA + " - nezapisujem nic")
open(novy, "w").write(s)
print("navrh caddy configu: %s (odstranenych starych blokov: %d)" % (novy, odstranene))
'@ -replace "`r`n", "`n"
$py | ssh jarvis "tr -d '\r' > /tmp/caddyfix.py"
Vyzaduj "skript na upravu Caddyfile sa nepodarilo preniest"

$caddy = @'
set -eu
OSTRY=/etc/caddy/Caddyfile
NOVY=/etc/caddy/Caddyfile.nove
rm -f "$NOVY"
python3 /tmp/caddyfix.py
test -s "$NOVY"
grep -q "mapa.89.167.72.159.sslip.io" "$NOVY"
caddy validate --config /etc/caddy/Caddyfile.nove --adapter caddyfile
chmod --reference="$OSTRY" "$NOVY"
chown --reference="$OSTRY" "$NOVY"
ZALOHA="$OSTRY.zaloha-$(date +%Y%m%d-%H%M%S)"
cp -a "$OSTRY" "$ZALOHA"
mv "$NOVY" "$OSTRY"
if ! systemctl reload caddy; then
  echo "CHYBA: reload caddy zlyhal - vraciam poslednu funkcnu zalohu"
  cp -a "$ZALOHA" "$OSTRY"
  systemctl reload caddy || true
  exit 1
fi
echo "caddy OK (zaloha: $ZALOHA)"
'@ -replace "`r`n", "`n"
$caddy | ssh jarvis "tr -d '\r' > /tmp/caddy_nasad.sh; bash /tmp/caddy_nasad.sh"
Vyzaduj "Caddy config nepresiel validaciou alebo reload zlyhal - ostry Caddyfile ostal nezmeneny"
Ok "web nastaveny (obe appky na serveri overene)"

Krok "7/8  Cron: dozorca (akcie) a nocna zaloha databazy"
# Tabulka `naroky` je jediny zaznam o tom, kto zaplatil - bez nocnej zalohy by
# ju strata disku zmazala nenavratne. Cron sa NEPREPISUJE naslepo: berie sa
# existujuci crontab a vyhadzuju sa z neho len nase dva riadky, takze zaznamy
# druhej appky na serveri (taktik-mapa) ostavaju nedotknute.
$cron = @'
set -eu
RIADOK='0 5-21 * * * /opt/uvarsi/dozorca.sh >> /var/log/uvarsi.log 2>&1'
RIADOK_ZALOHA='30 3 * * * /opt/uvarsi/zaloha.sh >> /var/log/uvarsi-zaloha.log 2>&1'
touch /var/log/uvarsi.log /var/log/uvarsi-zaloha.log
mkdir -p /var/backups/uvarsi
crontab -l 2>/dev/null | grep -v 'dozorca.sh' | grep -v 'zaloha.sh' > /tmp/uvarsi_cron.txt || true
printf '%s\n' "$RIADOK" >> /tmp/uvarsi_cron.txt
printf '%s\n' "$RIADOK_ZALOHA" >> /tmp/uvarsi_cron.txt
crontab /tmp/uvarsi_cron.txt
rm -f /tmp/uvarsi_cron.txt
POCET=$(crontab -l 2>/dev/null | grep -c 'dozorca.sh' || true)
if [ "${POCET:-0}" -ne 1 ]; then
  echo "CHYBA: v crontabe je $POCET riadkov s dozorcom, ocakavam presne 1"
  exit 1
fi
POCET_ZALOH=$(crontab -l 2>/dev/null | grep -c 'zaloha.sh' || true)
if [ "${POCET_ZALOH:-0}" -ne 1 ]; then
  echo "CHYBA: v crontabe je $POCET_ZALOH riadkov so zalohou, ocakavam presne 1"
  exit 1
fi
crontab -l | grep -E 'dozorca.sh|zaloha.sh'
'@ -replace "`r`n", "`n"
$cron | ssh jarvis "tr -d '\r' > /tmp/uvarsi_cron.sh; bash /tmp/uvarsi_cron.sh"
Vyzaduj "cron pre dozorcu a zalohu sa nepodarilo nainstalovat"
Ok "dozorca aj nocna zaloha v crone (po jednom riadku)"

# Prva zaloha hned pri nasadeni - nema zmysel cakat do 03:30 na overenie, ze to
# vobec funguje. Zaroven je to jediny okamih, kedy o pripadnom zlyhani vieme.
ssh jarvis "/opt/uvarsi/zaloha.sh"
Vyzaduj "prva zaloha databazy zlyhala - tabulka naroky by ostala bez zalohy"
Ok "prva zaloha databazy overena"

Krok "8/8  Kontrola (caka na sluzbu; 500 a 502 su chyba, nie uspech)"
$verzia = (Get-Content "$B\VERSION" -Raw).Trim()
$check = @'
set -u
OCAKAVANE="${1:-}"
PRAH=30                     # rovnaky prah ako v hetzner/dozorca.sh
CHYBY=0
zle() { echo "  !! $*"; CHYBY=1; }

for i in $(seq 1 30); do
  curl -sf localhost:8090/api/health >/dev/null 2>&1 && break
  sleep 1
done

STAV=$(systemctl is-active uvarsi || true)
echo "sluzba: $STAV"
[ "$STAV" = "active" ] || zle "sluzba uvarsi nebezi"

skontroluj() {
  KOD=$(curl -s -o /dev/null -w "%{http_code}" "$1" || true)
  echo "$2: $KOD"
  [ "$KOD" = "200" ] || zle "$2 vratilo $KOD, ocakavam 200"
}
skontroluj https://uvar.si/app "appka"
skontroluj https://uvar.si/ "landing"
skontroluj https://uvar.si/api/public/landing "landing JSON"
skontroluj https://uvar.si/api/health "health"

HEALTH=$(curl -s https://uvar.si/api/health || true)

POCET=$(printf '%s' "$HEALTH" | sed -n 's/.*"pocet"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')
echo "akcie: ${POCET:-0} (prah $PRAH)"
if [ "${POCET:-0}" -lt "$PRAH" ]; then
  zle "akcii je len ${POCET:-0}, dozorca ich vyzaduje aspon $PRAH - landing by hlasil obnovujeme"
fi

VYDANIE=$(printf '%s' "$HEALTH" | sed -n 's/.*"vydanie"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
echo "vydanie: ${VYDANIE:-?} (ocakavam ${OCAKAVANE:-?})"
if [ -n "$OCAKAVANE" ] && [ "$VYDANIE" != "$OCAKAVANE" ]; then
  zle "zive vydanie sa nezhoduje s lokalnym VERSION - prenos je len ciastocny"
fi

exit $CHYBY
'@ -replace "`r`n", "`n"
$stav = $check | ssh jarvis "tr -d '\r' > /tmp/check.sh; bash /tmp/check.sh '$verzia'"
$kodKontroly = $LASTEXITCODE
$stav | ForEach-Object { Write-Host "  $_" }
if ($kodKontroly -ne 0) {
  Zlyhaj "kontrola po nasadeni nepresla - diagnostika na serveri: journalctl -u uvarsi -n 40 --no-pager"
}

Write-Host "`nHOTOVO - vydanie $verzia." -ForegroundColor Green
Write-Host "  Appka:   https://uvarsi.89.167.72.159.sslip.io/app"
Write-Host "  Landing: https://uvarsi.89.167.72.159.sslip.io/"
Write-Host "`nPrihlasovaci odkaz (kym nemame SMTP): .\odkaz.ps1" -ForegroundColor Yellow
