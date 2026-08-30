#Requires -Version 5.1

<#
.SYNOPSIS
Safely verifies the Auth v3 rollout without persisting credentials or cookies.

.DESCRIPTION
The default mode performs only health and anonymous capability checks. Password
login, session creation, and logout require -AllowMutation. Registration is an
additional opt-in probe and requires a separate disposable credential.

.EXAMPLE
.\nastroje\over_auth_v3.ps1 -BaseUrl https://uvar.si -ExpectedOrigin https://uvar.si

.EXAMPLE
$credential = Get-Credential
.\nastroje\over_auth_v3.ps1 -BaseUrl https://uvar.si -ExpectedOrigin https://uvar.si `
  -AllowMutation -Credential $credential
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$BaseUrl,

  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$ExpectedOrigin,

  [System.Management.Automation.PSCredential]$Credential,

  [switch]$AllowMutation,

  [switch]$AllowDisposableRegistrationProbe,

  [System.Management.Automation.PSCredential]$DisposableRegistrationCredential,

  [ValidateRange(2, 120)]
  [int]$TimeoutSec = 15
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$script:CurrentPhase = "origin-validation"
$script:ServiceOrigin = $null
$script:ExpectedOriginValue = $null

function Fail-Phase {
  throw [System.InvalidOperationException]::new("smoke phase failed")
}

function Complete-Phase([string]$Name) {
  Write-Host ("OK [{0}]" -f $Name) -ForegroundColor Green
}

function Get-ValidatedOrigin {
  param([string]$CandidateBaseUrl, [string]$CandidateExpectedOrigin)

  try {
    $base = [System.Uri]$CandidateBaseUrl
    $expected = [System.Uri]$CandidateExpectedOrigin
  } catch {
    throw [System.InvalidOperationException]::new("invalid origin")
  }

  if (-not $base.IsAbsoluteUri -or -not $expected.IsAbsoluteUri) { Fail-Phase }
  if ($base.Scheme -notin @("http", "https")) { Fail-Phase }
  if ($expected.Scheme -notin @("http", "https")) { Fail-Phase }
  if ($base.UserInfo -or $expected.UserInfo) { Fail-Phase }
  if ($base.AbsolutePath -ne "/" -or $base.Query -or $base.Fragment) { Fail-Phase }
  if ($expected.AbsolutePath -ne "/" -or $expected.Query -or $expected.Fragment) { Fail-Phase }

  $baseOrigin = $base.GetLeftPart([System.UriPartial]::Authority).TrimEnd("/")
  $expectedOrigin = $expected.GetLeftPart([System.UriPartial]::Authority).TrimEnd("/")
  if ($base.Scheme -ne $expected.Scheme -or $base.Host -ne $expected.Host -or
      $base.Port -ne $expected.Port -or $baseOrigin -ne $expectedOrigin) {
    Fail-Phase
  }

  $loopbackHosts = @("localhost", "127.0.0.1", "::1")
  if ($base.Scheme -eq "http" -and $base.Host -notin $loopbackHosts) { Fail-Phase }

  return @($baseOrigin, $expectedOrigin)
}

function Invoke-SafeJsonRequest {
  param(
    [Parameter(Mandatory = $true)][string]$Method,
    [Parameter(Mandatory = $true)][string]$Path,
    [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
    [hashtable]$Headers,
    [string]$JsonBody
  )

  $parameters = @{
    Uri = $script:ServiceOrigin + $Path
    Method = $Method
    TimeoutSec = $TimeoutSec
    ErrorAction = "Stop"
  }
  if ($null -ne $Session) { $parameters["WebSession"] = $Session }
  if ($null -ne $Headers) { $parameters["Headers"] = $Headers }
  if ($PSBoundParameters.ContainsKey("JsonBody")) {
    $parameters["Body"] = $JsonBody
    $parameters["ContentType"] = "application/json"
  }

  try {
    return Invoke-RestMethod @parameters
  } catch {
    throw [System.InvalidOperationException]::new("request failed")
  }
}

function Invoke-CredentialPost {
  param(
    [Parameter(Mandatory = $true)]
    [System.Management.Automation.PSCredential]$RequestCredential,
    [Parameter(Mandatory = $true)][string]$Path,
    [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
    [hashtable]$Headers,
    [string]$DeviceName
  )

  $bstr = [System.IntPtr]::Zero
  $secret = $null
  $payload = $null
  $json = $null
  try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
      $RequestCredential.Password
    )
    $secret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $payload = @{
      email = $RequestCredential.UserName
      password = $secret
    }
    if ($DeviceName) { $payload["device_name"] = $DeviceName }
    $json = $payload | ConvertTo-Json -Compress
    return Invoke-SafeJsonRequest -Method "POST" -Path $Path -Session $Session `
      -Headers $Headers -JsonBody $json
  } finally {
    if ($bstr -ne [System.IntPtr]::Zero) {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $secret = $null
    $json = $null
    $payload = $null
  }
}

function Assert-HealthShape($Response) {
  if ($null -eq $Response) { Fail-Phase }
  if (-not ($Response.vydanie -is [string]) -or
      [string]::IsNullOrWhiteSpace($Response.vydanie)) { Fail-Phase }
  if (-not ($Response.tyzden -is [string]) -or
      [string]::IsNullOrWhiteSpace($Response.tyzden)) { Fail-Phase }
  if ($null -eq $Response.plan_queue) { Fail-Phase }
}

function Assert-AnonymousCapabilityShape($Response) {
  if ($null -eq $Response -or -not ($Response.prihlaseny -is [bool]) -or
      $Response.prihlaseny -ne $false) { Fail-Phase }
  $authProperty = $Response.PSObject.Properties["auth_v3"]
  if ($null -ne $authProperty -and -not ($Response.auth_v3 -is [bool])) { Fail-Phase }
}

function Assert-RegistrationResponseShape($Response) {
  if ($null -eq $Response) { Fail-Phase }
  $propertyNames = @(
    $Response.PSObject.Properties | ForEach-Object { $_.Name } | Sort-Object
  )
  if (($propertyNames -join ",") -ne "message,ok") { Fail-Phase }
  if ($Response.ok -ne $true -or -not ($Response.message -is [string]) -or
      [string]::IsNullOrWhiteSpace($Response.message)) { Fail-Phase }
}

function Assert-LoginShape($Response) {
  if ($null -eq $Response -or $Response.ok -ne $true -or
      -not ($Response.redirect -is [string]) -or $Response.redirect -ne "/app") {
    Fail-Phase
  }
}

function Assert-AuthenticatedIdentity {
  param($Response, [string]$ExpectedEmail, $ExpectedId)

  if ($null -eq $Response -or $Response.prihlaseny -ne $true) { Fail-Phase }
  if ($Response.auth_v3 -ne $true) { Fail-Phase }
  if ($Response.password_configured -ne $true) { Fail-Phase }
  if ($null -eq $Response.id -or -not ($Response.email -is [string])) { Fail-Phase }
  if ($Response.email -ne $ExpectedEmail) { Fail-Phase }
  if ($null -ne $ExpectedId -and $Response.id -ne $ExpectedId) { Fail-Phase }
  return $Response.id
}

function Assert-LogoutShape($Response) {
  if ($null -eq $Response -or $Response.ok -ne $true) { Fail-Phase }
}

function Invoke-BestEffortLogout {
  param([Microsoft.PowerShell.Commands.WebRequestSession]$Session, [hashtable]$Headers)
  try {
    $null = Invoke-SafeJsonRequest -Method "POST" -Path "/api/auth/logout" `
      -Session $Session -Headers $Headers -JsonBody "{}"
  } catch {
    # Cleanup must not replace the original concise phase failure.
  }
}

try {
  $origins = Get-ValidatedOrigin -CandidateBaseUrl $BaseUrl `
    -CandidateExpectedOrigin $ExpectedOrigin
  $script:ServiceOrigin = $origins[0]
  $script:ExpectedOriginValue = $origins[1]
  $originHeaders = @{ Origin = $script:ExpectedOriginValue }
  Complete-Phase "origin-validation"

  $script:CurrentPhase = "health"
  $readOnlySession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
  $health = Invoke-SafeJsonRequest -Method "GET" -Path "/api/health" `
    -Session $readOnlySession -Headers $originHeaders
  Assert-HealthShape $health
  Complete-Phase "health"

  $script:CurrentPhase = "capability-preflight"
  $anonymous = Invoke-SafeJsonRequest -Method "GET" -Path "/api/me" `
    -Session $readOnlySession -Headers $originHeaders
  Assert-AnonymousCapabilityShape $anonymous
  Complete-Phase "capability-preflight"

  if (-not $AllowMutation) {
    Write-Host "OK [read-only-complete]" -ForegroundColor Green
    exit 0
  }

  Write-Warning "MUTATING auth smoke authorized: login/logout sessions will be created and removed."
  if ($null -eq $Credential) {
    $Credential = Get-Credential -Message "Auth v3 test account (secure prompt)"
  }
  if ($null -eq $Credential -or [string]::IsNullOrWhiteSpace($Credential.UserName) -or
      $Credential.Password.Length -eq 0) {
    $script:CurrentPhase = "credential-preflight"
    Fail-Phase
  }
  $expectedEmail = $Credential.UserName.Trim().ToLowerInvariant()

  if ($AllowDisposableRegistrationProbe) {
    Write-Warning "MUTATING disposable registration probe authorized: it may send one test message."
    if ($null -eq $DisposableRegistrationCredential) {
      $DisposableRegistrationCredential = Get-Credential `
        -Message "Disposable registration account only (secure prompt)"
    }
    if ($null -eq $DisposableRegistrationCredential -or
        [string]::IsNullOrWhiteSpace($DisposableRegistrationCredential.UserName) -or
        $DisposableRegistrationCredential.Password.Length -eq 0 -or
        $DisposableRegistrationCredential.UserName.Trim().ToLowerInvariant() -eq $expectedEmail) {
      $script:CurrentPhase = "registration-credential-preflight"
      Fail-Phase
    }
    $script:CurrentPhase = "registration-response-shape"
    $registration = Invoke-CredentialPost `
      -RequestCredential $DisposableRegistrationCredential `
      -Path "/api/auth/register" -Headers $originHeaders
    Assert-RegistrationResponseShape $registration
    Complete-Phase "registration-response-shape"
  }

  $sessionA = New-Object Microsoft.PowerShell.Commands.WebRequestSession
  $sessionB = New-Object Microsoft.PowerShell.Commands.WebRequestSession
  $sessionAActive = $false
  $sessionBActive = $false

  try {
    $script:CurrentPhase = "login-session-a"
    $loginA = Invoke-CredentialPost -RequestCredential $Credential `
      -Path "/api/auth/login" -Session $sessionA -Headers $originHeaders `
      -DeviceName "Auth v3 smoke A"
    Assert-LoginShape $loginA
    $sessionAActive = $true
    Complete-Phase "login-session-a"

    $script:CurrentPhase = "identity-session-a"
    $meA = Invoke-SafeJsonRequest -Method "GET" -Path "/api/me" `
      -Session $sessionA -Headers $originHeaders
    $identityId = Assert-AuthenticatedIdentity $meA $expectedEmail $null
    Complete-Phase "identity-session-a"

    $script:CurrentPhase = "login-session-b"
    $loginB = Invoke-CredentialPost -RequestCredential $Credential `
      -Path "/api/auth/login" -Session $sessionB -Headers $originHeaders `
      -DeviceName "Auth v3 smoke B"
    Assert-LoginShape $loginB
    $sessionBActive = $true
    Complete-Phase "login-session-b"

    $script:CurrentPhase = "identity-session-b"
    $meB = Invoke-SafeJsonRequest -Method "GET" -Path "/api/me" `
      -Session $sessionB -Headers $originHeaders
    $null = Assert-AuthenticatedIdentity $meB $expectedEmail $identityId
    Complete-Phase "identity-session-b"

    $script:CurrentPhase = "logout-current-session"
    $logoutA = Invoke-SafeJsonRequest -Method "POST" -Path "/api/auth/logout" `
      -Session $sessionA -Headers $originHeaders -JsonBody "{}"
    Assert-LogoutShape $logoutA
    $sessionAActive = $false
    $signedOutA = Invoke-SafeJsonRequest -Method "GET" -Path "/api/me" `
      -Session $sessionA -Headers $originHeaders
    Assert-AnonymousCapabilityShape $signedOutA
    Complete-Phase "logout-current-session"

    $script:CurrentPhase = "other-session-survives"
    $survivingB = Invoke-SafeJsonRequest -Method "GET" -Path "/api/me" `
      -Session $sessionB -Headers $originHeaders
    $null = Assert-AuthenticatedIdentity $survivingB $expectedEmail $identityId
    Complete-Phase "other-session-survives"

    $script:CurrentPhase = "password-fallback"
    $fallbackLogin = Invoke-CredentialPost -RequestCredential $Credential `
      -Path "/api/auth/login" -Session $sessionA -Headers $originHeaders `
      -DeviceName "Auth v3 password fallback"
    Assert-LoginShape $fallbackLogin
    $sessionAActive = $true
    $fallbackMe = Invoke-SafeJsonRequest -Method "GET" -Path "/api/me" `
      -Session $sessionA -Headers $originHeaders
    $null = Assert-AuthenticatedIdentity $fallbackMe $expectedEmail $identityId
    Complete-Phase "password-fallback"
  } finally {
    if ($sessionAActive) { Invoke-BestEffortLogout $sessionA $originHeaders }
    if ($sessionBActive) { Invoke-BestEffortLogout $sessionB $originHeaders }
  }

  Write-Host "OK [mutating-smoke-complete]" -ForegroundColor Green
  exit 0
} catch {
  Write-Host ("FAIL [{0}]" -f $script:CurrentPhase) -ForegroundColor Red
  exit 1
}
