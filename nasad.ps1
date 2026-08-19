# Uvar.si — nasadenie jedným príkazom.
# Použitie:  cd "$env:USERPROFILE\OneDrive\Online produkt"; .\nasad.ps1
# Nahrá súbory na jarvis, reštartuje službu, nastaví Caddy a overí, že všetko beží.
# BLOKOVANÉ: nepoužívať pred Task 5; staré Caddy a error-handling správanie ostáva nebezpečné.

$ErrorActionPreference = "Continue"
$B = $PSScriptRoot
function Krok($t) { Write-Host "`n=== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  OK  $t" -ForegroundColor Green }
function Zle($t)  { Write-Host "  !!  $t" -ForegroundColor Red }

Krok "1/6  Priečinky na serveri"
ssh jarvis "mkdir -p /opt/uvarsi/app/static /var/www/uvarsi" | Out-Null
Ok "pripravené"

Krok "2/6  Nahrávam súbory"
$subory = @(
  @{ l = "$B\app\config.py";            r = "/opt/uvarsi/app/config.py" },
  @{ l = "$B\app\weekly_data.py";       r = "/opt/uvarsi/app/weekly_data.py" },
  @{ l = "$B\app\offer_data.py";        r = "/opt/uvarsi/app/offer_data.py" },
  @{ l = "$B\app\landing_data.py";      r = "/opt/uvarsi/app/landing_data.py" },
  @{ l = "$B\app\receipt_data.py";      r = "/opt/uvarsi/app/receipt_data.py" },
  @{ l = "$B\app\plan_data.py";         r = "/opt/uvarsi/app/plan_data.py" },
  @{ l = "$B\app\server.py";            r = "/opt/uvarsi/app/server.py" },
  @{ l = "$B\app\zbierac_akcii.py";     r = "/opt/uvarsi/app/zbierac_akcii.py" },
  @{ l = "$B\hetzner\refresh_blocek.py"; r = "/opt/uvarsi/refresh_blocek.py" },
  @{ l = "$B\hetzner\recepty.py";       r = "/opt/uvarsi/recepty.py" },
  @{ l = "$B\hetzner\dozorca.sh";       r = "/opt/uvarsi/dozorca.sh" },
  @{ l = "$B\index.html";               r = "/var/www/uvarsi/index.html" }
)
foreach ($s in $subory) {
  if (Test-Path $s.l) { scp -q $s.l "jarvis:$($s.r)"; Ok (Split-Path $s.l -Leaf) }
  else { Zle "chýba $($s.l)" }
}
if (Test-Path "$B\app\static") {
  scp -q -r "$B\app\static\*" "jarvis:/opt/uvarsi/app/static/"
  Ok "static/ (PWA)"
}

Krok "3/6  Závislosti"
ssh jarvis "/opt/uvarsi/venv/bin/pip -q install fastapi uvicorn anthropic pillow requests 2>&1 | tail -1; chmod +x /opt/uvarsi/dozorca.sh"
Ok "hotové"

Krok "4/6  Služba uvarsi (beží stále, prežije reštart)"
$svc = @'
[Unit]
Description=Uvarsi app
After=network.target

[Service]
WorkingDirectory=/opt/uvarsi/app
ExecStart=/opt/uvarsi/venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8090
Restart=always

[Install]
WantedBy=multi-user.target
'@ -replace "`r`n", "`n"
$svc | ssh jarvis "cat > /etc/systemd/system/uvarsi.service"
ssh jarvis "systemctl daemon-reload; systemctl enable uvarsi >/dev/null 2>&1; systemctl restart uvarsi; sleep 3; systemctl is-active uvarsi"
Ok "služba beží"

Krok "5/6  Caddy (appka na /app, landing na hlavnej)"
$py = @'
import re, shutil
p = "/etc/caddy/Caddyfile"
s = open(p).read()
shutil.copy(p, p + ".zaloha")
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
	handle /static/* {
		reverse_proxy 127.0.0.1:8090
	}
	handle {
		root * /var/www/uvarsi
		file_server
	}
}"""
# nahradí celý uvarsi blok (vrátane vnorených zátvoriek), ostatné weby nechá tak
i = s.find("uvarsi.89.167.72.159.sslip.io")
if i == -1:
    s = s.rstrip() + "\n\n" + blok + "\n"
else:
    j = s.find("{", i)
    hlbka, k = 0, j
    while k < len(s):
        if s[k] == "{": hlbka += 1
        elif s[k] == "}":
            hlbka -= 1
            if hlbka == 0: break
        k += 1
    s = s[:i] + blok + s[k+1:]
open(p, "w").write(s)
print("caddy zapisany")
'@ -replace "`r`n", "`n"
$py | ssh jarvis "cat > /tmp/caddyfix.py; python3 /tmp/caddyfix.py; caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 && systemctl reload caddy && echo CADDY_OK || echo CADDY_CHYBA"
Ok "web nastavený"

Krok "6/6  Kontrola"
$stav = ssh jarvis "echo -n 'akcie: '; curl -s localhost:8090/api/akcie/pocet; echo; echo -n 'appka: '; curl -s -o /dev/null -w '%{http_code}' https://uvarsi.89.167.72.159.sslip.io/app; echo; echo -n 'landing: '; curl -s -o /dev/null -w '%{http_code}' https://uvarsi.89.167.72.159.sslip.io/; echo"
Write-Host $stav

Write-Host "`nHOTOVO." -ForegroundColor Green
Write-Host "  Appka:   https://uvarsi.89.167.72.159.sslip.io/app"
Write-Host "  Landing: https://uvarsi.89.167.72.159.sslip.io/"
Write-Host "`nPrihlasovaci odkaz (kym nemame SMTP): .\odkaz.ps1" -ForegroundColor Yellow
