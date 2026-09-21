$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Write-Host 'Uvar.si: opravu Tesco mosta vykona zabezpecene server.' -ForegroundColor Cyan
Write-Host 'Na tomto pocitaci sa Cloudflare kluc ani nastroje nepouzivaju.' -ForegroundColor DarkGray

& ssh jarvis 'sudo /opt/uvarsi/uvarsi-deploy-state.sh repair-tesco-bridge'
if ($LASTEXITCODE -ne 0) {
    throw 'Serverova oprava zlyhala. Platby zostali vypnute.'
}

Write-Host 'Hotovo. Server opravil most a overil aktualny blocek.' -ForegroundColor Green
