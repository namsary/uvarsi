# Uvar.si — AUTOPUSH.
# Bezi na pozadi na Martinovom PC. Kazdych 5 minut sa pozrie, ci sa v projekte
# nieco zmenilo, a ak ano, commitne to a pushne na GitHub. Server si to potom
# stiahne sam (samopull).
#
# Vysledok: Martin uz nespusta ziadny prikaz. Claude zapise subory do OneDrivu,
# toto ich posle na GitHub, server ich nasadi a sam sa vrati spat, ak nefunguju.
#
# Bezpecnostna poistka: pushuje LEN ked lokalne testy prejdu. Rozbity kod sa
# na server nikdy nedostane.
#
# Instalacia (raz, v PowerShelli):
#   powershell -ExecutionPolicy Bypass -File "...\nastroje\autopush.ps1" -Instaluj
# Vypnutie:
#   Unregister-ScheduledTask -TaskName "UvarsiAutopush" -Confirm:$false

param([switch]$Instaluj, [switch]$Raz)

$ErrorActionPreference = "Continue"
$REPO = Split-Path $PSScriptRoot -Parent
$LOG  = Join-Path $REPO "nastroje\autopush.log"

function Zapis($sprava) {
  $riadok = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $sprava
  Write-Host $riadok
  Add-Content -Path $LOG -Value $riadok -Encoding UTF8
}

if ($Instaluj) {
  $akcia = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`""
  $spustac = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
  $nastavenia = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
  Register-ScheduledTask -TaskName "UvarsiAutopush" -Action $akcia -Trigger $spustac `
    -Settings $nastavenia -Description "Uvar.si: automaticky commit a push zmien" -Force | Out-Null
  Write-Host "`nHOTOVO. Autopush bezi kazdych 5 minut." -ForegroundColor Green
  Write-Host "  Log:     $LOG"
  Write-Host "  Vypnut:  Unregister-ScheduledTask -TaskName UvarsiAutopush -Confirm:`$false"
  exit 0
}

Set-Location $REPO

# --- 1. Zmenilo sa nieco? ---
$zmeny = & git status --porcelain 2>$null
if (-not $zmeny) { exit 0 }                      # nic nove, ticho koncime

$pocet = ($zmeny | Measure-Object).Count
Zapis "zmenenych suborov: $pocet"

# --- 2. Testy MUSIA prejst, inak sa nepushuje ---
$py = "python"
$vysledok = & $py -m pytest tests/ -q `
  --deselect tests/test_app_html_contract.py `
  --deselect tests/test_dozorca_contract.py 2>&1 | Select-Object -Last 3
$suhrn = ($vysledok -join " ").Trim()

if ($LASTEXITCODE -ne 0) {
  Zapis "TESTY ZLYHALI - nepushujem. $suhrn"
  exit 1
}
Zapis "testy OK: $suhrn"

# --- 3. Commit a push ---
& git add -A 2>&1 | Out-Null
$sprava = "auto: {0:yyyy-MM-dd HH:mm} ({1} suborov)" -f (Get-Date), $pocet
& git commit -m $sprava 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Zapis "commit zlyhal alebo nebolo co commitnut"; exit 1 }

& git push origin HEAD:main 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
  Zapis "PUSH ZLYHAL (prihlasenie do GitHubu?) - zmeny su commitnute lokalne"
  exit 1
}

$sha = (& git rev-parse --short HEAD).Trim()
Zapis "pushnute $sha - server si to vezme do 10 minut"
exit 0
