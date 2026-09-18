[CmdletBinding()]
param(
    [switch]$SelfTest,
    [switch]$LocalPreflight
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$BridgeRelease = 'f320e6b58243b9f06ef5f368907ffd12750533df'
$BridgeUrl = 'https://uvarsi-tesco-bridge.pumaragency.workers.dev'
$BridgeHost = 'uvarsi-tesco-bridge.pumaragency.workers.dev'
$ExpectedAppCommit = 'b151002a62b4804df478c4748ca64f1a8dd9a407'
$ExpectedAppVersion = '2026.09.18.2'
$ExpectedWranglerVersion = '4.131.1'
$ExpectedWranglerFiles = [ordered]@{
    'node_modules\.pnpm\wrangler@4.131.1\node_modules\wrangler\bin\wrangler.js' = '780661A508810F3B65786895B1CA9AACBC4F55D329AE6B8C1E49EC8433569F77'
    'node_modules\.pnpm\wrangler@4.131.1\node_modules\wrangler\wrangler-dist\cli.js' = 'DADBD16D62D6B104D8F711FF91D6B5BF08B58CF01986209C9A06C00F0631C786'
    'node_modules\.pnpm\wrangler@4.131.1\node_modules\wrangler\package.json' = 'F8C0028333405B155B088117DDA5181E7B2927D74E558AC9BAAE0FCDDA05DFA8'
}
$ExpectedNodeVersion = 'v24.19.0'
$ExpectedNodeHash = '3602F2BB1A10F2CBAB4C36886218A33C1AB3DB87290E73B033C46C77147D0237'
$ExpectedCloudflareAccountId = '0510a19c8c69e8354378d3198e10302f'

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '')
    }
    finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function New-BridgeSecret {
    param([Parameter(Mandatory = $true)][string]$Release)

    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    $random = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    return "$Release.$random"
}

function Assert-BridgeCoordinates {
    param(
        [Parameter(Mandatory = $true)][string]$Release,
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][string]$VersionId,
        [Parameter(Mandatory = $true)][string]$Secret
    )

    if ($Release -notmatch '^[0-9a-f]{12,64}$') {
        throw 'Neplatny release Tesco bridge.'
    }
    if ($VersionId -notmatch '^[A-Za-z0-9._-]{8,128}$') {
        throw 'Neplatne ID nasadenej Worker verzie.'
    }
    if ($HostName -cne $HostName.ToLowerInvariant() -or
        -not $HostName.StartsWith('uvarsi-tesco-bridge.') -or
        -not $HostName.EndsWith('.workers.dev')) {
        throw 'Neplatny zamknuty Worker host.'
    }

    $uri = [Uri]$Url
    if ($uri.Scheme -cne 'https' -or $uri.Host -cne $HostName -or
        -not $uri.IsDefaultPort -or $uri.AbsolutePath -ne '/' -or
        $uri.Query -ne '' -or $uri.Fragment -ne '' -or $uri.UserInfo -ne '') {
        throw 'Neplatna HTTPS adresa Tesco bridge.'
    }

    $escapedRelease = [Regex]::Escape($Release)
    if ($Secret -notmatch "^$escapedRelease\.[A-Za-z0-9._~-]{32,}$") {
        throw 'Neplatny release-bound bridge secret.'
    }
}

function Get-ActiveWorkerVersionFromJson {
    param([Parameter(Mandatory = $true)][string]$Json)

    # Windows PowerShell 5.1 keeps a top-level JSON array as one pipeline
    # object. Assign first, then array-wrap, so each deployment is inspected.
    $parsed = ConvertFrom-Json -InputObject $Json
    $deployments = @($parsed)
    $eligible = @(
        foreach ($deployment in $deployments) {
            $versions = @($deployment.versions)
            if ($versions.Count -eq 1 -and [int]$versions[0].percentage -eq 100) {
                [pscustomobject]@{
                    VersionId = [string]$versions[0].version_id
                    CreatedOn = [DateTimeOffset]$deployment.created_on
                }
            }
        }
    )
    $active = @($eligible | Sort-Object CreatedOn -Descending | Select-Object -First 1)
    if ($active.Count -ne 1 -or $active[0].VersionId -notmatch '^[A-Za-z0-9._-]{8,128}$') {
        throw 'Cloudflare nevratil jednoznacnu aktivnu Worker verziu.'
    }
    return $active[0].VersionId
}

function Get-WorkerVersionsFromJson {
    param([Parameter(Mandatory = $true)][string]$Json)

    $parsed = ConvertFrom-Json -InputObject $Json
    return @(
        foreach ($version in @($parsed)) {
            if ([string]$version.id -notmatch '^[0-9a-f-]{36}$') {
                throw 'Cloudflare vratil neplatne Worker version ID.'
            }
            [pscustomobject]@{
                Id = [string]$version.id
                Number = [int]$version.number
            }
        }
    )
}

function Test-BridgeStateRepairable {
    param([Parameter(Mandatory = $true)][string]$State)

    # Only rotate after the server-side probe proves a configuration, bearer,
    # or immutable Worker identity mismatch. Network/5xx/content failures do
    # not become a reason to rotate credentials.
    return $State -in @('config_invalid', 'auth_mismatch', 'identity_mismatch')
}

function Get-CreatedWorkerVersionFromOutput {
    param([Parameter(Mandatory = $true)][string]$Output)

    $matches = [Regex]::Matches(
        $Output,
        '(?im)Created version\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
    )
    if ($matches.Count -ne 1) {
        throw 'Cloudflare nevratil jednoznacne ID vytvorenej Worker verzie.'
    }
    return $matches[0].Groups[1].Value
}

function Invoke-OfflineSelfTest {
    $sampleRelease = '0123456789abcdef0123456789abcdef01234567'
    $sampleSecret = New-BridgeSecret -Release $sampleRelease
    Assert-BridgeCoordinates `
        -Release $sampleRelease `
        -Url 'https://uvarsi-tesco-bridge.example.workers.dev' `
        -HostName 'uvarsi-tesco-bridge.example.workers.dev' `
        -VersionId '22222222-2222-4222-8222-222222222222' `
        -Secret $sampleSecret

    $sampleJson = @'
[
  {"created_on":"2026-09-14T22:06:01Z","versions":[{"version_id":"11111111-1111-4111-8111-111111111111","percentage":100}]},
  {"created_on":"2026-09-14T22:16:45Z","versions":[{"version_id":"22222222-2222-4222-8222-222222222222","percentage":100}]}
]
'@
    $selected = Get-ActiveWorkerVersionFromJson -Json $sampleJson
    if ($selected -ne '22222222-2222-4222-8222-222222222222') {
        throw 'Self-test nevybral najnovsi 100-percentny deployment.'
    }

    $invalidRejected = $false
    try {
        Assert-BridgeCoordinates `
            -Release $sampleRelease `
            -Url 'http://uvarsi-tesco-bridge.example.workers.dev' `
            -HostName 'uvarsi-tesco-bridge.example.workers.dev' `
            -VersionId $selected `
            -Secret $sampleSecret
    }
    catch {
        $invalidRejected = $true
    }
    if (-not $invalidRejected) {
        throw 'Self-test prijal nezabezpecenu Worker adresu.'
    }

    foreach ($repairable in @('config_invalid', 'auth_mismatch', 'identity_mismatch')) {
        if (-not (Test-BridgeStateRepairable -State $repairable)) {
            throw "Self-test odmietol opravitelny stav $repairable."
        }
    }
    foreach ($blocked in @('request_failed', 'response_invalid', 'external_failure', 'local_error')) {
        if (Test-BridgeStateRepairable -State $blocked) {
            throw "Self-test prijal stav $blocked ako dovod na rotaciu."
        }
    }
    $createdVersion = Get-CreatedWorkerVersionFromOutput -Output (
        'Success! Created version 33333333-3333-4333-8333-333333333333 with 2 secrets.'
    )
    if ($createdVersion -ne '33333333-3333-4333-8333-333333333333') {
        throw 'Self-test neprecital presne ID vytvorenej Worker verzie.'
    }
    $versionsJson = @'
[
  {"id":"11111111-1111-4111-8111-111111111111","number":1},
  {"id":"22222222-2222-4222-8222-222222222222","number":2}
]
'@
    $versions = @(Get-WorkerVersionsFromJson -Json $versionsJson)
    $latest = @($versions | Sort-Object Number -Descending | Select-Object -First 1)
    if ($latest.Count -ne 1 -or $latest[0].Id -ne '22222222-2222-4222-8222-222222222222') {
        throw 'Self-test neodhalil najnovsi Cloudflare draft zaklad.'
    }

    $sampleSecret = $null
    Write-Output 'RECOVERY_STATES_OK'
    Write-Output 'VERSION_BASE_OK'
    Write-Output 'SELFTEST_OK'
}

if ($SelfTest) {
    Invoke-OfflineSelfTest
    exit 0
}

function Invoke-SshScript {
    param([Parameter(Mandatory = $true)][string]$Script)

    $normalized = $Script -replace "`r`n", "`n"
    $output = $normalized | & ssh jarvis 'tr -d ''\r'' | bash -s' 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Serverovy krok zlyhal s kodom $exitCode. $($output -join ' ')"
    }
    return ($output -join "`n")
}

function Get-ActiveWorkerVersion {
    $json = & $script:VerifiedNodeExecutable $script:VerifiedWranglerEntry `
        deployments list --json 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw 'Neda sa nacitat aktivna Cloudflare Worker verzia.'
    }
    return Get-ActiveWorkerVersionFromJson -Json $json
}

function Get-WorkerVersions {
    $json = & $script:VerifiedNodeExecutable $script:VerifiedWranglerEntry `
        versions list --json 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw 'Neda sa nacitat zoznam Cloudflare Worker verzii.'
    }
    return @(Get-WorkerVersionsFromJson -Json $json)
}

function Get-LatestWorkerVersionInfo {
    $versions = @(Get-WorkerVersions)
    $latest = @($versions | Sort-Object Number -Descending | Select-Object -First 1)
    if ($latest.Count -ne 1 -or $latest[0].Number -lt 1) {
        throw 'Cloudflare nema jednoznacnu latest Worker verziu.'
    }
    return $latest[0]
}

function Get-WorkerVersionInfo {
    param([Parameter(Mandatory = $true)][string]$VersionId)

    $matches = @(Get-WorkerVersions | Where-Object { $_.Id -eq $VersionId })
    if ($matches.Count -ne 1) {
        throw 'Vytvorena Worker verzia sa neda jednoznacne najst.'
    }
    return $matches[0]
}

function New-WorkerVersionWithSecrets {
    param(
        [Parameter(Mandatory = $true)][string]$Release,
        [Parameter(Mandatory = $true)][string]$Secret,
        [Parameter(Mandatory = $true)][string]$Tag
    )

    $bulk = [pscustomobject]@{
        WORKER_RELEASE = $Release
        BRIDGE_SECRET = $Secret
    } | ConvertTo-Json -Compress
    $output = $bulk | & $script:VerifiedNodeExecutable $script:VerifiedWranglerEntry `
        versions secret bulk `
        --tag $Tag `
        --message 'Atomic Uvar.si Tesco bridge credential repair' 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw 'Cloudflare odmietol atomicku aktualizaciu bridge udajov.'
    }
    return Get-CreatedWorkerVersionFromOutput -Output $output
}

function Deploy-ExactWorkerVersion {
    param([Parameter(Mandatory = $true)][string]$VersionId)

    $null = & $script:VerifiedNodeExecutable $script:VerifiedWranglerEntry `
        versions deploy "$VersionId@100" --yes `
        --message 'Activate exact Uvar.si Tesco bridge repair version' 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw 'Cloudflare odmietol nasadenie presnej opravnej Worker verzie.'
    }
}

function Wait-ForExactActiveWorkerVersion {
    param([Parameter(Mandatory = $true)][string]$ExpectedVersion)

    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    do {
        $current = Get-ActiveWorkerVersion
        if ($current -eq $ExpectedVersion) {
            return $current
        }
        Start-Sleep -Seconds 3
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Cloudflare nepotvrdil presnu opravnu Worker verziu do dvoch minut.'
}

function Wait-ForReceiptHealth {
    param([Parameter(Mandatory = $true)][DateTimeOffset]$NotBefore)

    $deadline = [DateTime]::UtcNow.AddMinutes(20)
    $lastCounts = 'Kaufland 0, Tesco 0, Lidl 0'
    do {
        try {
            $health = Invoke-RestMethod -Uri 'https://uvar.si/api/health' -TimeoutSec 15
            if ($health.vydanie -ne $ExpectedAppVersion) {
                throw 'Produkcia nema ocakavanu verziu Uvar.si.'
            }
            if ($health.recipe_engine.payments_enabled -ne $false) {
                throw 'Platobna poistka nie je vypnuta.'
            }
            $kaufland = [int]$health.ponuky_podla_obchodu.Kaufland
            $tesco = [int]$health.ponuky_podla_obchodu.Tesco
            $lidl = [int]$health.ponuky_podla_obchodu.Lidl
            $lastCounts = "Kaufland $kaufland, Tesco $tesco, Lidl $lidl"
            $lastSuccess = [DateTimeOffset]::MinValue
            if ($null -ne $health.dozorca.last_success_at) {
                $lastSuccess = [DateTimeOffset]::Parse([string]$health.dozorca.last_success_at)
            }
            if ($kaufland -gt 0 -and $tesco -gt 0 -and $lidl -gt 0 -and
                $health.dozorca.fresh -eq $true -and
                $lastSuccess -ge $NotBefore.AddSeconds(-5)) {
                return $lastCounts
            }
        }
        catch {
            if ($_.Exception.Message -match 'Platobna poistka|ocakavanu verziu') {
                throw
            }
        }
        Write-Host "Zber este bezi: $lastCounts" -ForegroundColor DarkGray
        Start-Sleep -Seconds 15
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Zber sa do 20 minut nepotvrdil vo vsetkych troch obchodoch. Posledny stav: $lastCounts"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$workerDir = Join-Path $repoRoot 'cloudflare\tesco-bridge'
$script:VerifiedWranglerEntry = Join-Path $workerDir `
    'node_modules\.pnpm\wrangler@4.131.1\node_modules\wrangler\bin\wrangler.js'
$script:VerifiedNodeExecutable = Join-Path $env:USERPROFILE `
    '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
$repairMutex = New-Object System.Threading.Mutex($false, 'Local\UvarsiTescoBridgeRepair')
if (-not $repairMutex.WaitOne(0)) {
    $repairMutex.Dispose()
    throw 'Ina oprava Tesco bridge uz bezi. Druhy subezny beh sa nespusti.'
}

Write-Host 'Uvar.si: opravujem Tesco most a obnovu blocka.' -ForegroundColor Cyan
Write-Host 'Platby zostanu vypnute. Tajny kluc sa nikde nevypise ani neulozi na PC.' -ForegroundColor DarkGray
Invoke-OfflineSelfTest | Out-Null
Write-Host '0/4 Povinne offline testy presli.' -ForegroundColor Green

foreach ($entry in $ExpectedWranglerFiles.GetEnumerator()) {
    $toolPath = Join-Path $workerDir $entry.Key
    if (-not (Test-Path -LiteralPath $toolPath -PathType Leaf)) {
        throw "Chyba overovany subor Wrangleru: $($entry.Key)"
    }
    $actualHash = Get-Sha256Hex -Path $toolPath
    if ($actualHash -cne $entry.Value) {
        throw "Wrangler nepresiel kontrolnym suctom: $($entry.Key)"
    }
}
foreach ($unsafeNodeEnvironment in @(
    'NODE_OPTIONS',
    'NODE_PATH',
    'NODE_EXTRA_CA_CERTS',
    'NODE_TLS_REJECT_UNAUTHORIZED',
    'CLOUDFLARE_API_BASE_URL',
    'CF_API_BASE_URL',
    'CLOUDFLARE_API_TOKEN',
    'CLOUDFLARE_API_KEY',
    'CLOUDFLARE_EMAIL',
    'CLOUDFLARE_ACCOUNT_ID',
    'CF_API_TOKEN',
    'CF_API_KEY',
    'CF_EMAIL',
    'CF_ACCOUNT_ID',
    'WRANGLER_LOG',
    'WRANGLER_LOG_PATH',
    'WRANGLER_LOG_SANITIZE',
    'WRANGLER_WRITE_LOGS'
)) {
    Remove-Item -LiteralPath "Env:$unsafeNodeEnvironment" -ErrorAction SilentlyContinue
}
$env:WRANGLER_LOG_SANITIZE = 'true'
$env:WRANGLER_WRITE_LOGS = 'false'
$env:CLOUDFLARE_ACCOUNT_ID = $ExpectedCloudflareAccountId
if (-not (Test-Path -LiteralPath $script:VerifiedNodeExecutable -PathType Leaf)) {
    throw 'Chyba presny overovany Node runtime.'
}
$nodeHash = Get-Sha256Hex -Path $script:VerifiedNodeExecutable
$nodeVersion = (& $script:VerifiedNodeExecutable --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $nodeHash -cne $ExpectedNodeHash -or $nodeVersion -cne $ExpectedNodeVersion) {
    throw 'Node runtime pre Wrangler nepresiel kontrolou verzie a kontrolneho suctu.'
}
$wranglerVersionOutput = & $script:VerifiedNodeExecutable $script:VerifiedWranglerEntry `
    --version 2>&1 | Out-String
$wranglerVersionLines = @(
    $wranglerVersionOutput -split "`r?`n" |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -match '^\d+\.\d+\.\d+$' }
)
if ($LASTEXITCODE -ne 0 -or $wranglerVersionLines.Count -ne 1 -or
    $wranglerVersionLines[0] -cne $ExpectedWranglerVersion) {
    throw 'Lokalny Wrangler nema presnu overenu verziu.'
}
Write-Host 'Overena verzia a kontrolne sucty Wrangleru suhlasia.' -ForegroundColor DarkGray

Push-Location $repoRoot
try {
    & git cat-file -e "$BridgeRelease^{commit}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Chyba skontrolovany Worker release.'
    }
    $workerPaths = @(
        'cloudflare/tesco-bridge/src',
        'cloudflare/tesco-bridge/wrangler.jsonc',
        'cloudflare/tesco-bridge/package.json'
    )
    & git diff --quiet $BridgeRelease -- $workerPaths
    if ($LASTEXITCODE -ne 0) {
        throw 'Worker kod sa od skontrolovaneho release zmenil. Oprava sa zastavila.'
    }
    $workerStatus = & git status --porcelain --untracked-files=all -- $workerPaths
    if ($LASTEXITCODE -ne 0 -or @($workerStatus).Count -ne 0) {
        throw 'Worker ma lokalne staged, unstaged alebo nezname zmeny. Oprava sa zastavila.'
    }
}
finally {
    Pop-Location
}

if ($LocalPreflight) {
    Write-Output 'LOCAL_PREFLIGHT_OK'
    $repairMutex.ReleaseMutex()
    $repairMutex.Dispose()
    exit 0
}

$whoamiOutput = & $script:VerifiedNodeExecutable $script:VerifiedWranglerEntry `
    whoami 2>&1 | Out-String
if ($LASTEXITCODE -ne 0 -or
    $whoamiOutput -notmatch "(?i)(?<![0-9a-f])$ExpectedCloudflareAccountId(?![0-9a-f])") {
    throw 'Cloudflare konto nie je presne schvalene PUMAR konto. Nic sa nezmenilo.'
}
Write-Host 'Cloudflare konto PUMAR je overene.' -ForegroundColor DarkGray

$serverPreflight = @'
set +x
set -Eeu
DIR=/opt/uvarsi
EXPECTED_COMMIT='b151002a62b4804df478c4748ca64f1a8dd9a407'
EXPECTED_VERSION='2026.09.18.2'
[ -f "$DIR/uvarsi.env" ]
[ -x "$DIR/venv/bin/python" ]
[ -x "$DIR/uvarsi-deploy-state.sh" ]
[ "$(cat "$DIR/.nasadene_sha" 2>/dev/null || true)" = "$EXPECTED_COMMIT" ]
[ "$(cat "$DIR/VERSION" 2>/dev/null || true)" = "$EXPECTED_VERSION" ]
mkdir -p "$DIR/backups"
[ -w "$DIR/backups" ]
command -v base64 >/dev/null 2>&1
. "$DIR/uvarsi-deploy-state.sh"
uvarsi_require_payments_off
if _uvarsi_require_tesco_bridge_transport; then
  printf '%s\n' 'SERVER_PREFLIGHT_OK' 'BRIDGE_STATE=OK'
else
  original_reason=$UVARSI_BRIDGE_FAILURE_REASON
  diagnosed=$original_reason
  if [ "$original_reason" = request_failed ] || [ "$original_reason" = response_invalid ]; then
    bridge_url=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_URL) || diagnosed=config_invalid
    bridge_release=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_RELEASE) || diagnosed=config_invalid
    bridge_version=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_VERSION_ID) || diagnosed=config_invalid
    bridge_secret=$(_uvarsi_env_value UVARSI_TESCO_BRIDGE_SECRET) || diagnosed=config_invalid
    if [ "$diagnosed" != config_invalid ]; then
      today=$(_uvarsi_today)
      response=$(mktemp "${TMPDIR:-/tmp}/uvarsi-bridge-diagnose.XXXXXX")
      chmod 600 "$response"
      request=$(printf '{"date":"%s","format":"HM"}' "$today")
      curl_exit=0
      http_code=$({
        printf 'header = "Accept: application/json"\n'
        printf 'header = "Content-Type: application/json"\n'
        printf 'header = "Authorization: Bearer %s"\n' "$bridge_secret"
      } | "$UVARSI_CURL" --disable --config - --silent --show-error \
          --max-time 30 --request POST --data-binary "$request" \
          --output "$response" --write-out '%{http_code}' \
          "$bridge_url/v1/tesco/leaflets" 2>/dev/null) || curl_exit=$?
      if [ "$curl_exit" -ne 0 ]; then
        diagnosed=external_failure
      elif [ "$http_code" = 401 ] || [ "$http_code" = 403 ]; then
        diagnosed=auth_mismatch
      elif [ "$http_code" = 200 ]; then
        if "$UVARSI_HEALTH_PY" -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
    bridge = payload["bridge"]
    valid_identity = (
        isinstance(bridge, dict)
        and set(bridge) == {"release", "version_id", "attestation"}
        and isinstance(bridge.get("release"), str) and bool(bridge["release"])
        and isinstance(bridge.get("version_id"), str) and bool(bridge["version_id"])
        and isinstance(bridge.get("attestation"), str) and bool(bridge["attestation"])
    )
except (OSError, ValueError, KeyError, TypeError, AttributeError):
    raise SystemExit(1)
if not valid_identity:
    raise SystemExit(1)
mismatch = (
    bridge.get("release") != sys.argv[2]
    or bridge.get("version_id") != sys.argv[3]
)
raise SystemExit(0 if mismatch else 1)
' "$response" "$bridge_release" "$bridge_version" >/dev/null 2>&1; then
          diagnosed=identity_mismatch
        else
          diagnosed=external_failure
        fi
      else
        diagnosed=external_failure
      fi
      rm -f "$response"
      unset bridge_secret
    fi
  fi
  printf '%s\n' 'SERVER_PREFLIGHT_OK' "BRIDGE_STATE=$diagnosed"
fi
'@

$preflightOutput = Invoke-SshScript -Script $serverPreflight
if ($preflightOutput -notmatch '(?m)^SERVER_PREFLIGHT_OK$') {
    throw 'Server nepotvrdil bezpecny preflight.'
}

$bridgeStateMatch = [Regex]::Match($preflightOutput, '(?m)^BRIDGE_STATE=([^\r\n]+)$')
$bridgeState = $bridgeStateMatch.Groups[1].Value
$supervisorNotBefore = [DateTimeOffset]::MinValue

if ($bridgeState -eq 'OK') {
    Write-Host '1/4 Tesco most je uz spravne zosuladeny.' -ForegroundColor Green
}
elseif (-not (Test-BridgeStateRepairable -State $bridgeState)) {
    throw "Tesco most ma neopravitelnu lokalnu chybu ($bridgeState). Rotacia kluca sa neurobila."
}
else {
    Write-Host '1/4 Server je pripraveny; platby su zamknute na nule.' -ForegroundColor Green

    Push-Location $workerDir
    $bridgeSecret = $null
    $payloadJson = $null
    $payloadBase64 = $null
    try {
        $baseActiveVersion = Get-ActiveWorkerVersion
        $baseLatestVersion = Get-LatestWorkerVersionInfo
        if ($baseLatestVersion.Id -ne $baseActiveVersion) {
            throw 'Cloudflare ma nenasadenu draft verziu. Oprava ju odmietla nasadit.'
        }
        $expectedCreatedNumber = $baseLatestVersion.Number + 1

        $bridgeSecret = New-BridgeSecret -Release $BridgeRelease
        $repairTag = 'uvarsi-bridge-repair-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' +
            ([Guid]::NewGuid().ToString('N').Substring(0, 8))
        $activeVersion = New-WorkerVersionWithSecrets `
            -Release $BridgeRelease `
            -Secret $bridgeSecret `
            -Tag $repairTag
        $createdVersionInfo = Get-WorkerVersionInfo `
            -VersionId $activeVersion
        if ($createdVersionInfo.Number -ne $expectedCreatedNumber) {
            throw 'Pocas opravy vznikla ina Cloudflare draft verzia. Nic sa nenasadilo.'
        }
        $stillActiveVersion = Get-ActiveWorkerVersion
        if ($stillActiveVersion -ne $baseActiveVersion) {
            throw 'Pocas opravy prebehol iny Cloudflare deploy. Nic dalsie sa nezmenilo.'
        }
        Deploy-ExactWorkerVersion -VersionId $activeVersion
        $null = Wait-ForExactActiveWorkerVersion `
            -ExpectedVersion $activeVersion

        Assert-BridgeCoordinates `
            -Release $BridgeRelease `
            -Url $BridgeUrl `
            -HostName $BridgeHost `
            -VersionId $activeVersion `
            -Secret $bridgeSecret

        Write-Host '2/4 Cloudflare ma novy bezpecny kluc a aktivnu verziu.' -ForegroundColor Green

        $payloadJson = [pscustomobject]@{
            environment = 'production'
            url = $BridgeUrl
            host_name = $BridgeHost
            release = $BridgeRelease
            version_id = $activeVersion
            secret = $bridgeSecret
        } | ConvertTo-Json -Compress
        $payloadBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payloadJson))

        $serverUpdateTemplate = @'
set +x
set -Eeu
umask 077
DIR=/opt/uvarsi
ENV_FILE="$DIR/uvarsi.env"
PAYLOAD_FILE=$(mktemp "${TMPDIR:-/tmp}/uvarsi-bridge-config.XXXXXX")
cleanup() { rm -f "$PAYLOAD_FILE"; }
trap cleanup EXIT HUP INT TERM
printf '%s' '__PAYLOAD_BASE64__' | base64 -d > "$PAYLOAD_FILE"

"$DIR/venv/bin/python" - "$ENV_FILE" "$PAYLOAD_FILE" "$DIR/backups" <<'PY'
import json
import os
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import urlsplit

env_path, payload_path, backup_dir = sys.argv[1:4]
managed = {
    "UVARSI_ENV",
    "UVARSI_TESCO_BRIDGE_URL",
    "UVARSI_TESCO_BRIDGE_WORKER_HOST",
    "UVARSI_TESCO_BRIDGE_RELEASE",
    "UVARSI_TESCO_BRIDGE_VERSION_ID",
    "UVARSI_TESCO_BRIDGE_SECRET",
    "PLATBY_ZAPNUTE",
    "UVARSI_PAYMENTS_ENABLED",
}
assignment = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
with open(payload_path, encoding="utf-8") as handle:
    payload = json.load(handle)

release = payload.get("release")
version_id = payload.get("version_id")
secret = payload.get("secret")
url = payload.get("url")
host = payload.get("host_name")
try:
    parsed = urlsplit(url)
    port = parsed.port
except (TypeError, ValueError):
    raise SystemExit("invalid bridge URL")
valid = (
    payload.get("environment") == "production"
    and isinstance(release, str) and re.fullmatch(r"[0-9a-f]{12,64}", release)
    and isinstance(version_id, str) and re.fullmatch(r"[A-Za-z0-9._-]{8,128}", version_id)
    and isinstance(secret, str) and secret.startswith(release + ".")
    and len(secret.partition(".")[2]) >= 32
    and re.fullmatch(r"[A-Za-z0-9._~-]+", secret)
    and isinstance(host, str) and host == host.lower()
    and host.startswith("uvarsi-tesco-bridge.") and host.endswith(".workers.dev")
    and parsed.scheme == "https" and parsed.hostname == host and port is None
    and parsed.username is None and parsed.password is None
    and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
)
if not valid:
    raise SystemExit("invalid bridge coordinates")
if os.path.islink(env_path) or not os.path.isfile(env_path):
    raise SystemExit("invalid env target")

metadata = os.stat(env_path, follow_symlinks=False)
with open(env_path, encoding="utf-8") as handle:
    original = handle.read()
if "\x00" in original or len(original) > 1_000_000:
    raise SystemExit("invalid env payload")

os.makedirs(backup_dir, mode=0o700, exist_ok=True)
stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
backup = os.path.join(backup_dir, "uvarsi-env-before-bridge-sync-" + stamp)
shutil.copy2(env_path, backup, follow_symlinks=False)
os.chmod(backup, 0o600)

kept = []
for line in original.splitlines():
    match = assignment.match(line)
    if not line.lstrip().startswith("#") and match and match.group(1) in managed:
        continue
    kept.append(line)
while kept and not kept[-1].strip():
    kept.pop()
values = {
    "UVARSI_ENV": "production",
    "UVARSI_TESCO_BRIDGE_URL": url,
    "UVARSI_TESCO_BRIDGE_WORKER_HOST": host,
    "UVARSI_TESCO_BRIDGE_RELEASE": release,
    "UVARSI_TESCO_BRIDGE_VERSION_ID": version_id,
    "UVARSI_TESCO_BRIDGE_SECRET": secret,
    "PLATBY_ZAPNUTE": "0",
    "UVARSI_PAYMENTS_ENABLED": "0",
}
kept.append("")
kept.extend(f"{key}={values[key]}" for key in (
    "UVARSI_ENV",
    "UVARSI_TESCO_BRIDGE_URL",
    "UVARSI_TESCO_BRIDGE_WORKER_HOST",
    "UVARSI_TESCO_BRIDGE_RELEASE",
    "UVARSI_TESCO_BRIDGE_VERSION_ID",
    "UVARSI_TESCO_BRIDGE_SECRET",
    "PLATBY_ZAPNUTE",
    "UVARSI_PAYMENTS_ENABLED",
))
candidate = "\n".join(kept) + "\n"

fd, temporary = tempfile.mkstemp(prefix=".uvarsi.env.", dir=os.path.dirname(env_path), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(candidate)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    if hasattr(os, "chown"):
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
    os.replace(temporary, env_path)
    if os.name != "nt":
        directory_fd = os.open(os.path.dirname(env_path), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print("SERVER_ENV_SYNCED")
PY

rm -f "$PAYLOAD_FILE"
trap - EXIT HUP INT TERM
chmod 600 "$ENV_FILE"
. "$DIR/uvarsi-deploy-state.sh"
uvarsi_require_payments_off
attempt=1
while [ "$attempt" -le 8 ]; do
  if _uvarsi_require_tesco_bridge_transport; then
    printf '%s\n' 'BRIDGE_CHECK_OK'
    break
  fi
  if [ "$attempt" -eq 8 ]; then
    printf 'BRIDGE_CHECK_FAILED=%s\n' "$UVARSI_BRIDGE_FAILURE_REASON" >&2
    exit 1
  fi
  sleep 5
  attempt=$((attempt + 1))
done
nohup "$DIR/uvarsi-deploy-state.sh" run-supervisor >> /var/log/uvarsi.log 2>&1 </dev/null &
printf 'SUPERVISOR_STARTED=%s\n' "$!"
'@
        $serverUpdate = $serverUpdateTemplate.Replace('__PAYLOAD_BASE64__', $payloadBase64)
        $confirmedVersion = Get-ActiveWorkerVersion
        if ($confirmedVersion -ne $activeVersion) {
            throw 'Cloudflare medzicasom nasadil inu verziu. Server sa nezmenil.'
        }
        $supervisorNotBefore = [DateTimeOffset]::UtcNow
        $serverOutput = Invoke-SshScript -Script $serverUpdate
        if ($serverOutput -notmatch '(?m)^SERVER_ENV_SYNCED$' -or
            $serverOutput -notmatch '(?m)^BRIDGE_CHECK_OK$' -or
            $serverOutput -notmatch '(?m)^SUPERVISOR_STARTED=\d+$') {
            throw 'Server nepotvrdil uplne zosuladenie bridge a start zberu.'
        }
        Write-Host '3/4 Hetzner ma rovnaky kluc; autentifikovany test presiel.' -ForegroundColor Green
    }
    finally {
        $bridgeSecret = $null
        $payloadJson = $null
        $payloadBase64 = $null
        Pop-Location
    }
}

if ($bridgeState -eq 'OK') {
    $startSupervisor = @'
set +x
set -Eeu
DIR=/opt/uvarsi
. "$DIR/uvarsi-deploy-state.sh"
uvarsi_require_payments_off
nohup "$DIR/uvarsi-deploy-state.sh" run-supervisor >> /var/log/uvarsi.log 2>&1 </dev/null &
printf 'SUPERVISOR_STARTED=%s\n' "$!"
'@
    $supervisorNotBefore = [DateTimeOffset]::UtcNow
    $serverOutput = Invoke-SshScript -Script $startSupervisor
    if ($serverOutput -notmatch '(?m)^SUPERVISOR_STARTED=\d+$') {
        throw 'Server nepotvrdil start zberu.'
    }
    Write-Host '2/4 Cloudflare a Hetzner uz boli zosuladene.' -ForegroundColor Green
    Write-Host '3/4 Obnova blocka bola spustena.' -ForegroundColor Green
}

$counts = Wait-ForReceiptHealth -NotBefore $supervisorNotBefore
Write-Host "4/4 LIVE: $counts; dozorca je cerstvy; platby su OFF." -ForegroundColor Green
Write-Host 'HOTOVO: blocek je obnoveny z realnych aktualnych dat.' -ForegroundColor Green
$repairMutex.ReleaseMutex()
$repairMutex.Dispose()
