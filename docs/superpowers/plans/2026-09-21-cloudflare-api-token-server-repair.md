# Cloudflare API Token Server Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nahradiť lokálny hodinový Cloudflare OAuth stabilným API tokenom obmedzeným na Worker `uvarsi-tesco-bridge` a presunúť celú opravu Tesco bridge na Hetzner.

**Architecture:** Čistý Python klient nad oficiálnym Cloudflare REST API bude bežať iba ako root na Hetzneri, čítať token zo samostatného súboru `0600` a vykonávať transakčné vytvorenie Worker verzie, deploy a rollback. `uvarsi-deploy-state.sh` bude rozhodovať, či je stav opraviteľný, zamkne platby na nule, spustí klienta, overí bridge a bloček a pri chybe vráti pôvodnú verziu aj konfiguráciu. Lokálny PowerShell ostane iba tenkým SSH spúšťačom bez Node, Wrangleru alebo Cloudflare prihlasovania.

**Tech Stack:** Python 3 štandardná knižnica (`urllib`, `json`, `dataclasses`, `tempfile`), Bash, PowerShell 5.1, pytest, Cloudflare Workers REST API.

**Spec:** `docs/superpowers/specs/2026-09-21-cloudflare-api-token-server-repair-design.md`

## Global Constraints

- Cloudflare account ID musí byť presne `0510a19c8c69e8354378d3198e10302f`.
- Worker musí byť presne `uvarsi-tesco-bridge`; token má rolu `Editor` iba pre tento existujúci Worker.
- Token je iba v `/etc/uvarsi/secrets/cloudflare-worker-token`, vlastník `root:root`, režim `0600`.
- Token nesmie byť v Gite, `/opt/uvarsi/uvarsi.env`, argv, stdout, stderr, journale, backupe ani Windows súbore.
- Bežná aplikácia, plánovací worker, dozorca a zberač token nedostanú.
- Každá mutácia vyžaduje `PLATBY_ZAPNUTE=0` aj `UVARSI_PAYMENTS_ENABLED=0` v súbore aj bežiacom procese.
- Opraviteľné sú iba `auth_mismatch`, `identity_mismatch` a jednoznačná neplatná bridge konfigurácia; sieť, DNS, 429, Cloudflare 5xx a neplatný obsah sú fail-closed.
- Posledný overený bloček a používateľské dáta sa pri chybe nemenia.
- Automatické volanie opravy dozorcom je mimo tohto plánu; prvé vydanie bude manuálne spustiteľné na serveri alebo cez SSH wrapper.

## Review Focus

- Tokenový súbor je symlink, má nesprávneho vlastníka alebo príliš široké práva: oprava musí skončiť pred HTTP požiadavkou.
- Cloudflare odpovie 429, 5xx, timeoutom alebo neplatným JSON: nič sa nesmie nasadiť ani prepísať v `uvarsi.env`.
- Medzi načítaním a deployom vznikne iná aktívna alebo draft verzia: oprava musí detegovať súbeh a zastaviť sa bez aktivácie nesprávnej verzie.
- Proces skončí po Cloudflare deployi, ale pred potvrdením bridge: nasledujúci beh musí nájsť nedokončenú transakciu a najprv obnoviť pôvodnú verziu aj env.
- Chýbajúce nové letákové dáta pri zdravom bridge: stav nesmie byť zamenený za auth chybu a nesmie spustiť rotáciu tajomstva.

---

### Task 1: Striktný Cloudflare Workers API klient

**Files:**
- Create: `hetzner/uvarsi_cloudflare_worker.py`
- Create: `tests/test_cloudflare_worker_client.py`

**Interfaces:**
- Consumes: API token ako text načítaný volajúcim procesom; presný account ID a názov Workera z konštánt.
- Produces: `CloudflareWorkerClient`, `CloudflareApiError`, `VersionInfo`, `get_active_version_id() -> str`, `upload_version(worker_source: bytes, release: str, bridge_secret: str, base_version_id: str, tag: str) -> VersionInfo`, `deploy_version(version_id: str, expected_active_version: str) -> None`.

- [ ] **Step 1: Napíš zlyhávajúce contract testy transportu a schémy**

```python
def test_client_sends_token_only_in_authorization_header():
    calls = []
    transport = RecordingTransport(calls, response(200, deployments_payload()))
    client = CloudflareWorkerClient("unit-secret", transport=transport)
    client.get_active_version_id()
    method, url, headers, body = calls[0]
    assert method == "GET"
    assert url.endswith("/accounts/0510a19c8c69e8354378d3198e10302f/workers/scripts/uvarsi-tesco-bridge/deployments")
    assert headers["Authorization"] == "Bearer unit-secret"
    assert b"unit-secret" not in (body or b"")


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 500, 503])
def test_client_rejects_cloudflare_failures_without_leaking_token(status, capsys):
    client = CloudflareWorkerClient("unit-secret", transport=StaticTransport(status, b"provider body"))
    with pytest.raises(CloudflareApiError) as error:
        client.get_active_version_id()
    assert error.value.reason == f"http_{status}"
    assert "unit-secret" not in str(error.value)
    assert "provider body" not in str(error.value)
    assert "unit-secret" not in capsys.readouterr().out


def test_client_rejects_invalid_success_schema():
    client = CloudflareWorkerClient("unit-secret", transport=StaticTransport(200, b'{"success":true,"result":{}}'))
    with pytest.raises(CloudflareApiError, match="invalid_response"):
        client.get_active_version_id()
```

- [ ] **Step 2: Spusť test a potvrď RED**

Run: `python -m pytest tests/test_cloudflare_worker_client.py -q`

Expected: FAIL, pretože modul `hetzner.uvarsi_cloudflare_worker` ešte neexistuje.

- [ ] **Step 3: Implementuj klienta s injektovateľným transportom**

```python
ACCOUNT_ID = "0510a19c8c69e8354378d3198e10302f"
SCRIPT_NAME = "uvarsi-tesco-bridge"
API_ORIGIN = "https://api.cloudflare.com/client/v4"


@dataclass(frozen=True)
class VersionInfo:
    id: str
    number: int


class CloudflareApiError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class CloudflareWorkerClient:
    def __init__(self, token: str, transport: Transport = urllib_transport):
        if not token or any(ord(char) < 0x21 or ord(char) > 0x7E for char in token):
            raise CloudflareApiError("invalid_token")
        self._token = token
        self._transport = transport

    def _request(self, method: str, suffix: str, payload: dict | None = None) -> object:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, API_ORIGIN + suffix, headers, body)
        if status < 200 or status >= 300:
            raise CloudflareApiError(f"http_{status}")
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            raise CloudflareApiError("invalid_response") from None
        if not isinstance(envelope, dict) or envelope.get("success") is not True:
            raise CloudflareApiError("invalid_response")
        return envelope.get("result")
```

Implementuj presné validátory UUID, čísla verzie, deployment poľa a Cloudflare envelope. `upload_version` musí použiť dokumentovaný `POST /accounts/{account}/workers/scripts/{script}/versions?bindings_inherit=strict` a multipart upload. Metadata musia obsahovať presný `main_module`, `compatibility_date`, `BRIDGE_SECRET` a `WORKER_RELEASE` ako `secret_text`, `TOKEN_SECRET` ako `inherit` z presného `base_version_id` a `CF_VERSION_METADATA` ako `version_metadata`. Odpoveď musí priamo obsahovať novú version ID. `deploy_version` musí pred POST znovu načítať aktívnu verziu a vyžadovať `expected_active_version`.

- [ ] **Step 4: Pridaj testy bulk secret payloadu, súbehu a timeoutu**

```python
def test_upload_pins_source_and_inherits_only_token_secret():
    transport = ScriptedTransport([response(200, created_version_payload("22222222-2222-4222-8222-222222222222", 2))])
    client = CloudflareWorkerClient("unit-secret", transport=transport)
    result = client.upload_version(WORKER_SOURCE, RELEASE, BRIDGE_SECRET, BASE_VERSION, "repair-20260921")
    metadata, modules = decode_multipart(transport.calls[0])
    assert modules == {"worker.js": WORKER_SOURCE}
    assert metadata["main_module"] == "worker.js"
    assert metadata["compatibility_date"] == "2026-09-11"
    assert metadata["bindings"] == [
        {"name": "BRIDGE_SECRET", "type": "secret_text", "text": BRIDGE_SECRET},
        {"name": "WORKER_RELEASE", "type": "secret_text", "text": RELEASE},
        {"name": "TOKEN_SECRET", "type": "inherit", "version_id": BASE_VERSION},
        {"name": "CF_VERSION_METADATA", "type": "version_metadata"},
    ]
    assert result.number == 2


def test_deploy_refuses_changed_active_version():
    client = CloudflareWorkerClient("unit-secret", transport=deployment_transport(active=OTHER_VERSION))
    with pytest.raises(CloudflareApiError, match="concurrent_deploy"):
        client.deploy_version(NEW_VERSION, expected_active_version=BASE_VERSION)
```

- [ ] **Step 5: Spusť testy klienta**

Run: `python -m pytest tests/test_cloudflare_worker_client.py -q`

Expected: PASS.

- [ ] **Step 6: Commitni klienta**

```powershell
git add hetzner/uvarsi_cloudflare_worker.py tests/test_cloudflare_worker_client.py
git commit -m "feat: add strict Cloudflare Worker API client"
```

### Task 2: Obnoviteľná serverová repair transakcia

**Files:**
- Create: `hetzner/uvarsi_tesco_bridge_repair.py`
- Create: `tests/test_server_tesco_bridge_repair.py`

**Interfaces:**
- Consumes: `CloudflareWorkerClient`; token path, env path, backup dir a transaction path z `RepairPaths`.
- Produces: CLI `verify-token`, `begin`, `commit`, `rollback`, `recover`; `begin_repair(paths: RepairPaths, client: CloudflareWorkerClient, release: str) -> str` vracia iba nové version ID.

- [ ] **Step 1: Napíš zlyhávajúce testy tokenového súboru**

```python
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666])
def test_token_file_requires_root_only_mode(mode):
    metadata = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=0)
    with pytest.raises(RepairError, match="token_permissions"):
        validate_token_metadata(metadata, expected_uid=0)


def test_token_file_rejects_symlink():
    metadata = SimpleNamespace(st_mode=stat.S_IFLNK | 0o600, st_uid=0)
    with pytest.raises(RepairError, match="token_target"):
        validate_token_metadata(metadata, expected_uid=0)
```

- [ ] **Step 2: Napíš zlyhávajúci test transakcie a recovery**

```python
def test_recover_restores_version_and_env_after_interrupted_begin(repair_fixture):
    new_version = begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)
    assert new_version == NEW_VERSION
    assert repair_fixture.paths.transaction.exists()
    assert f"UVARSI_TESCO_BRIDGE_VERSION_ID={NEW_VERSION}" in repair_fixture.env_text()

    recover_repair(repair_fixture.paths, repair_fixture.client)

    assert repair_fixture.client.deployed_versions[-1] == BASE_VERSION
    assert repair_fixture.env_text() == repair_fixture.original_env
    assert not repair_fixture.paths.transaction.exists()
```

- [ ] **Step 3: Spusť testy a potvrď RED**

Run: `python -m pytest tests/test_server_tesco_bridge_repair.py -q`

Expected: FAIL, pretože repair modul ešte neexistuje.

- [ ] **Step 4: Implementuj bezpečné načítanie tokenu a atómové súbory**

```python
def validate_token_metadata(metadata: os.stat_result, expected_uid: int = 0) -> None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RepairError("token_target")
    if metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RepairError("token_permissions")


def read_token(path: Path, expected_uid: int = 0) -> str:
    validate_token_metadata(path.lstat(), expected_uid)
    value = path.read_text(encoding="ascii").rstrip("\n")
    if len(value) < 32 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        raise RepairError("token_format")
    return value


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
```

Token načítaj až po validácii ciest. Chybové správy musia obsahovať iba stabilné reason enumy, nikdy URL response body, hlavičky, env obsah alebo token.

- [ ] **Step 5: Implementuj `begin`/`commit`/`rollback`/`recover`**

`begin` musí v tomto poradí: odmietnuť existujúcu transakciu; zazálohovať env do root-only backupu; načítať presnú aktívnu verziu; overiť SHA-256 serverovej kópie `worker.js` proti `de485d6776e311877b8e7602ec6c4a0aa0dd962175b75fd64424b2cc37472f4d`; zapísať transaction JSON vo fáze `prepared`; vygenerovať `release + "." + secrets.token_urlsafe(32)`; uploadnúť presný Worker zdroj s novými bridge secrets a zdediť iba `TOKEN_SECRET` z pôvodnej aktívnej verzie; zapísať nové version ID do transakcie; znovu overiť pôvodnú aktívnu verziu; nasadiť novú verziu; nastaviť fázu `deployed`; atomicky prepísať iba bridge kľúče a oba payment flagy na `0`; nastaviť fázu `env_synced`.

`rollback` smie nasadiť pôvodnú version ID iba ak je stále aktívna kandidátska verzia vytvorená touto transakciou; potom obnoví env zo zálohy a odstráni transaction JSON. Ak medzitým nasadil niekto inú verziu, vráti `concurrent_deploy` a nič ďalšie nemení. `recover` podľa fázy buď iba odstráni bezpečne nedokončený `prepared` stav bez Cloudflare mutácie, alebo vykoná rovnaký kontrolovaný rollback. `commit` odstráni iba transaction JSON a jej env backup po tom, čo shell už potvrdil bridge a readiness.

- [ ] **Step 6: Pridaj testy rollbacku pre každý bod zlyhania**

```python
@pytest.mark.parametrize("failure", [
    "active_version", "upload_version", "predeploy_recheck", "deploy", "env_replace"
])
def test_begin_failure_never_leaves_untracked_partial_state(repair_fixture, failure):
    repair_fixture.client.fail_at = failure
    with pytest.raises(RepairError):
        begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)
    assert "unit-token" not in repair_fixture.captured_text()
    if repair_fixture.client.mutation_started:
        assert repair_fixture.paths.transaction.exists()
    else:
        assert repair_fixture.env_text() == repair_fixture.original_env
```

- [ ] **Step 7: Spusť repair testy**

Run: `python -m pytest tests/test_cloudflare_worker_client.py tests/test_server_tesco_bridge_repair.py -q`

Expected: PASS.

- [ ] **Step 8: Commitni transakciu**

```powershell
git add hetzner/uvarsi_tesco_bridge_repair.py tests/test_server_tesco_bridge_repair.py
git commit -m "feat: make Tesco bridge repair transactional"
```

### Task 3: Fail-closed orchestration v deploy-state

**Files:**
- Modify: `hetzner/uvarsi-deploy-state.sh:1-230,1193-1348,1919-1934`
- Modify: `tests/test_tesco_bridge_deployment.py`
- Modify: `tests/test_deploy_safety.py`

**Interfaces:**
- Consumes: repair CLI z Task 2 na `/opt/uvarsi/uvarsi_tesco_bridge_repair.py`.
- Produces: `_uvarsi_diagnose_tesco_bridge()`, `uvarsi_repair_tesco_bridge()`, CLI príkazy `verify-cloudflare-token`, `install-cloudflare-token`, `repair-tesco-bridge`.

- [ ] **Step 1: Napíš zlyhávajúce shell contract testy**

```python
def test_bridge_repair_requires_both_file_and_runtime_payment_gates():
    script = DEPLOY_STATE.read_text(encoding="utf-8")
    body = function_body(script, "uvarsi_repair_tesco_bridge")
    assert "uvarsi_require_payments_off" in body
    assert "uvarsi_require_runtime_payments_off" in body
    assert body.index("uvarsi_require_runtime_payments_off") < body.index('"$UVARSI_REPAIR_PY" begin')


def test_bridge_repair_mutates_only_repairable_states():
    result = run_deploy_state(
        "repair-tesco-bridge",
        env=fake_runtime(bridge_reason="external_failure"),
    )
    assert result.returncode != 0
    assert not fake_cloudflare_calls()


def test_bridge_repair_rolls_back_when_readiness_fails():
    result = run_deploy_state(
        "repair-tesco-bridge",
        env=fake_runtime(bridge_reason="auth_mismatch", readiness_exit=1),
    )
    assert result.returncode != 0
    assert repair_cli_calls()[-1] == "rollback"


def test_missing_fresh_data_with_healthy_bridge_never_rotates_credentials():
    result = run_deploy_state(
        "repair-tesco-bridge",
        env=fake_runtime(bridge_reason="OK", supervisor_exit=1),
    )
    assert result.returncode != 0
    assert "begin" not in repair_cli_calls()
```

- [ ] **Step 2: Spusť cielené testy a potvrď RED**

Run: `python -m pytest tests/test_tesco_bridge_deployment.py tests/test_deploy_safety.py -q`

Expected: FAIL na chýbajúcich repair funkciách.

- [ ] **Step 3: Pridaj serverové cesty a presnú diagnostiku**

```bash
UVARSI_CLOUDFLARE_TOKEN_FILE="${UVARSI_CLOUDFLARE_TOKEN_FILE:-/etc/uvarsi/secrets/cloudflare-worker-token}"
UVARSI_REPAIR_PY="${UVARSI_REPAIR_PY:-$UVARSI_DIR/uvarsi_tesco_bridge_repair.py}"
UVARSI_REPAIR_LOCK="${UVARSI_REPAIR_LOCK:-/run/lock/uvarsi-tesco-bridge-repair.lock}"

_uvarsi_bridge_state_repairable() {
  case "$1" in
    auth_mismatch|identity_mismatch|config_invalid) return 0 ;;
    *) return 1 ;;
  esac
}
```

Presuň read-only diagnostiku z vloženého PowerShell server preflightu do `_uvarsi_diagnose_tesco_bridge`. Funkcia smie vypísať iba jeden enum na stdout; response body, URL a tajomstvá ostávajú v dočasnom súbore `0600`, ktorý trap vždy odstráni.

- [ ] **Step 4: Implementuj zamknutú repair sekvenciu**

```bash
uvarsi_repair_tesco_bridge() (
  set +x
  set -Eeu
  exec 9>"$UVARSI_REPAIR_LOCK"
  "$UVARSI_FLOCK" -n 9 || exit 75
  uvarsi_require_payments_off
  uvarsi_require_runtime_payments_off
  "$UVARSI_HEALTH_PY" "$UVARSI_REPAIR_PY" recover
  state=$(_uvarsi_diagnose_tesco_bridge)
  if [ "$state" = OK ]; then
    "$UVARSI_BASH" "$UVARSI_DEPLOY_STATE_SCRIPT" run-supervisor
    uvarsi_require_production_readiness
    return
  fi
  _uvarsi_bridge_state_repairable "$state" || return 1
  "$UVARSI_HEALTH_PY" "$UVARSI_REPAIR_PY" begin
  if _uvarsi_require_tesco_bridge_transport && \
      "$UVARSI_BASH" "$UVARSI_DEPLOY_STATE_SCRIPT" run-supervisor && \
      uvarsi_require_production_readiness; then
    "$UVARSI_HEALTH_PY" "$UVARSI_REPAIR_PY" commit
    return 0
  fi
  "$UVARSI_HEALTH_PY" "$UVARSI_REPAIR_PY" rollback || return 2
  return 1
)
```

Presná implementácia musí zachovať návratový kód `75` pre obsadený zámok a `2` pre zlyhaný rollback. `run-supervisor` naďalej používa existujúci štvorhodinový timeout a staging, takže live DB ani landing JSON sa pri zlyhaní neprepíšu.

- [ ] **Step 5: Implementuj bezpečný interaktívny bootstrap tokenu**

`install-cloudflare-token` musí vyžadovať root a TTY, vypnúť echo cez `stty`, prečítať token z `/dev/tty`, zapísať kandidáta s `umask 077`, overiť ho cez `verify-token` a až potom ho atomicky presunúť na finálnu cestu. Trap musí vždy obnoviť echo a odstrániť kandidáta. Pri chybe nesmie meniť existujúci platný token.

- [ ] **Step 6: Spusť shell a deployment testy**

Run: `python -m pytest tests/test_tesco_bridge_deployment.py tests/test_deploy_safety.py -q`

Expected: PASS.

Run: `bash -n hetzner/uvarsi-deploy-state.sh`

Expected: exit 0 bez výstupu.

- [ ] **Step 7: Commitni serverovú orchestration**

```powershell
git add hetzner/uvarsi-deploy-state.sh tests/test_tesco_bridge_deployment.py tests/test_deploy_safety.py
git commit -m "feat: orchestrate Tesco bridge repair on server"
```

### Task 4: Nasadenie klienta a odstránenie lokálneho OAuth flow

**Files:**
- Modify: `nasad.ps1:140-180,296-311`
- Modify: `hetzner/samopull.sh:109,193-225,312-318`
- Modify: `hetzner/uvarsi-deploy-state.sh:1646-1882`
- Modify: `ops/repair_tesco_bridge.ps1`
- Deploy unchanged source: `cloudflare/tesco-bridge/src/worker.js`
- Modify: `tests/test_deploy_manifest.py`
- Modify: `tests/test_deploy_covers_all_modules.py`
- Modify: `tests/test_operator_tesco_bridge_repair.py`

**Interfaces:**
- Consumes: CLI `repair-tesco-bridge` z Task 3.
- Produces: server obsahuje oba Python moduly; lokálny PowerShell vykoná iba SSH volanie pevného serverového príkazu.

- [ ] **Step 1: Napíš RED testy úplného deployment manifestu**

```python
def test_deploy_manifest_contains_server_cloudflare_repair_modules():
    script = Path("nasad.ps1").read_text(encoding="utf-8")
    assert '"$B\\hetzner\\uvarsi_cloudflare_worker.py"' in script
    assert '"$B\\hetzner\\uvarsi_tesco_bridge_repair.py"' in script
    assert '"$B\\cloudflare\\tesco-bridge\\src\\worker.js"' in script


def test_samopull_installs_and_rolls_back_repair_modules():
    script = Path("hetzner/samopull.sh").read_text(encoding="utf-8")
    for name in ("uvarsi_cloudflare_worker.py", "uvarsi_tesco_bridge_repair.py"):
        assert f"hetzner/{name}" in script
        assert f'$DIR/{name}' in script
        assert f'$PRED/{name}' in script
    assert 'cloudflare/tesco-bridge/src/worker.js' in script
    assert '$DIR/tesco-bridge-worker.js' in script


def test_runtime_services_never_receive_cloudflare_token_file():
    for path in (
        Path("hetzner/uvarsi.service"),
        Path("hetzner/uvarsi-plan-worker.service"),
        Path("hetzner/dozorca.sh"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "cloudflare-worker-token" not in source
        assert "CLOUDFLARE_API_TOKEN" not in source
```

- [ ] **Step 2: Napíš RED test lokálneho spúšťača bez Cloudflare klienta**

```python
def test_operator_repair_is_ssh_only_and_has_no_local_cloudflare_auth():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ssh" in source
    assert "repair-tesco-bridge" in source
    for forbidden in (
        "wrangler", "whoami", "CLOUDFLARE_API_TOKEN", "CF_API_TOKEN",
        "node.exe", "versions secret bulk", "versions deploy",
    ):
        assert forbidden.casefold() not in source.casefold()
```

- [ ] **Step 3: Spusť cielené testy a potvrď RED**

Run: `python -m pytest tests/test_deploy_manifest.py tests/test_deploy_covers_all_modules.py tests/test_operator_tesco_bridge_repair.py -q`

Expected: FAIL na chýbajúcich súboroch v manifeste a starom Wrangler flow.

- [ ] **Step 4: Rozšír oba deploy mechanizmy a rollback**

Pridaj oba Python súbory do manuálneho prenosu, samopull povinného manifestu, zálohy, inštalácie, rollbacku a `_uvarsi_apply_manual_targets`. Z rovnakého release prenes aj nezmenený `cloudflare/tesco-bridge/src/worker.js` na `/opt/uvarsi/tesco-bridge-worker.js`; repair modul odmietne iný SHA-256. Python súbory majú režim `0755`, Worker zdroj `0644`; tokenový adresár ani token sa nikdy nekopírujú zo stage alebo rollback backupu.

- [ ] **Step 5: Nahraď obsah PowerShell repair skriptu SSH-only wrapperom**

```powershell
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Write-Host 'Uvar.si: server opravuje Tesco most a obnovuje bloček.' -ForegroundColor Cyan
Write-Host 'Platby zostávajú vypnuté; Cloudflare token je iba na Hetzneri.' -ForegroundColor DarkGray
& ssh jarvis 'sudo /opt/uvarsi/uvarsi-deploy-state.sh repair-tesco-bridge'
if ($LASTEXITCODE -ne 0) {
    throw "Serverová oprava zlyhala s kódom $LASTEXITCODE. Platby zostali vypnuté."
}
Write-Host 'HOTOVO: server potvrdil bridge, aktuálne dáta a bloček.' -ForegroundColor Green
```

Nepridávaj fallback na lokálny Wrangler. Existujúci koreňový `OPRAV_TESCO_A_BLOCEK.cmd` môže zostať nezmenený, pretože už volá tento PowerShell súbor.

- [ ] **Step 6: Spusť deploy a operator testy**

Run: `python -m pytest tests/test_deploy_manifest.py tests/test_deploy_covers_all_modules.py tests/test_operator_tesco_bridge_repair.py tests/test_deploy_safety.py -q`

Expected: PASS.

- [ ] **Step 7: Commitni cutover**

```powershell
git add nasad.ps1 hetzner/samopull.sh hetzner/uvarsi-deploy-state.sh ops/repair_tesco_bridge.ps1 tests/test_deploy_manifest.py tests/test_deploy_covers_all_modules.py tests/test_operator_tesco_bridge_repair.py
git commit -m "refactor: move Cloudflare repair off the operator laptop"
```

### Task 5: Prevádzková dokumentácia, bezpečnostná kontrola a živý rollout

**Files:**
- Modify: `docs/prevadzka.md:493-610`
- Modify: `tests/test_operator_tesco_bridge_repair.py`
- Modify: `tests/test_deploy_safety.py`

**Interfaces:**
- Consumes: `install-cloudflare-token`, `verify-cloudflare-token`, `repair-tesco-bridge`.
- Produces: presný jednorazový bootstrap, rotácia/revokácia tokenu a overovací runbook bez tajomstiev.

- [ ] **Step 1: Napíš dokumentačné contract testy**

```python
def test_runbook_uses_scoped_server_token_not_wrangler_oauth():
    runbook = Path("docs/prevadzka.md").read_text(encoding="utf-8")
    section = runbook.split("## Tesco bridge", 1)[1]
    assert "uvarsi-tesco-bridge" in section
    assert "Editor" in section
    assert "/etc/uvarsi/secrets/cloudflare-worker-token" in section
    assert "install-cloudflare-token" in section
    assert "verify-cloudflare-token" in section
    assert "repair-tesco-bridge" in section
    assert "wrangler login" not in section
    assert "npx wrangler secret put" not in section
```

- [ ] **Step 2: Prepíš runbook na presný tokenový postup**

Dokumentuj tieto kroky:

1. Cloudflare Dashboard → Account API tokens → Create token.
2. Scope: presný účet PUMAR, resource: iba `uvarsi-tesco-bridge`, role: `Editor`.
3. Bez DNS, Routes, Admin, KV, R2, D1, billing alebo token-management oprávnení.
4. Na Hetzneri spusti `sudo /opt/uvarsi/uvarsi-deploy-state.sh install-cloudflare-token`; token vlož iba do skrytého remote promptu.
5. Over `sudo /opt/uvarsi/uvarsi-deploy-state.sh verify-cloudflare-token`.
6. Spusti `sudo /opt/uvarsi/uvarsi-deploy-state.sh repair-tesco-bridge` so stále vypnutými platbami.
7. Pri rotácii najprv nainštaluj a over nový token, až potom v Cloudflare odvolaj starý.
8. Reportuj iba čas, Worker, release/version ID a stabilný výsledok; nikdy token, response body alebo env.

- [ ] **Step 3: Spusť všetky relevantné testy**

Run: `python -m pytest tests/test_cloudflare_worker_client.py tests/test_server_tesco_bridge_repair.py tests/test_operator_tesco_bridge_repair.py tests/test_tesco_bridge_deployment.py tests/test_deploy_manifest.py tests/test_deploy_covers_all_modules.py tests/test_deploy_safety.py -q`

Expected: PASS.

Run: `python -m pytest -q`

Expected: plný suite PASS bez nových warningov alebo preskočených bezpečnostných testov.

Run: `bash -n hetzner/uvarsi-deploy-state.sh hetzner/samopull.sh`

Expected: exit 0 bez výstupu.

Run: `powershell.exe -NoProfile -Command "[void][ScriptBlock]::Create((Get-Content -Raw -LiteralPath 'ops\\repair_tesco_bridge.ps1')); 'POWERSHELL_PARSE_OK'"`

Expected: `POWERSHELL_PARSE_OK`.

- [ ] **Step 4: Spusť secret scan**

Run: `rg -n "Bearer [A-Za-z0-9]|oauth_token|refresh_token|cloudflare-worker-token=" . --glob '!docs/superpowers/**' --glob '!node_modules/**'`

Expected: žiadna skutočná hodnota tokenu; iba bezpečné názvy premenných/cesty a testovacie literály.

- [ ] **Step 5: Commitni dokumentáciu**

```powershell
git add docs/prevadzka.md tests/test_operator_tesco_bridge_repair.py tests/test_deploy_safety.py
git commit -m "docs: document scoped Cloudflare repair token"
```

- [ ] **Step 6: Vyžiadaj nezávislý read-only review pred živým nasadením**

Reviewer musí skontrolovať token leakage, Cloudflare scope, transakčný rollback, súbeh, payment gates, zachovanie posledného bločka a absenciu OAuth fallbacku. Všetky Critical/Important nálezy oprav pred ďalším krokom.

- [ ] **Step 7: Nasaď kód so stále vypnutými platbami**

Použi existujúci guarded release proces. Pred nasadením zaznamenaj presný HEAD. Po nasadení over `/api/health`, `PLATBY_ZAPNUTE=0`, `UVARSI_PAYMENTS_ENABLED=0`, aktívnu appku a plan worker. Token ešte neinštaluj, kým nový serverový kód a rollback nie sú živé.

- [ ] **Step 8: Jednorazovo vytvor a nainštaluj scoped token**

Používateľ vytvorí token v Cloudflare Dashboard podľa kroku 2. Token vloží do skrytého serverového promptu `install-cloudflare-token`. Po úspešnom `verify-cloudflare-token` sa OAuth konfigurácia na notebooku už nepoužíva; nemaž ju v tom istom kroku, kým live repair neprejde.

- [ ] **Step 9: Vykonaj jeden kontrolovaný živý repair**

Run on Hetzner: `sudo /opt/uvarsi/uvarsi-deploy-state.sh repair-tesco-bridge`

Expected: exit 0; presný Worker/release/version sa zhoduje; bridge check, ohraničený supervisor a readiness prejdú; Kaufland, Tesco a Lidl majú čerstvé overené dáta; landing má aktuálny bloček; platby sú stále OFF. Výstup neobsahuje token ani bridge secret.

- [ ] **Step 10: Over nezávislosť od notebooku a uzavri OAuth cestu**

Zatvor prehliadač a lokálny Wrangler session. Na Hetzneri zopakuj read-only `verify-cloudflare-token` a normálny `run-supervisor`; oba musia prejsť bez notebooku. Až potom odstráň starú OAuth vetvu z prevádzkového runbooku a označ migráciu za dokončenú. Automatickú samoopravu dozorcom nezapínaj.
