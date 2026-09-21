import json
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from hetzner.uvarsi_cloudflare_worker import CloudflareApiError, VersionInfo
from hetzner.uvarsi_tesco_bridge_repair import (
    RepairError,
    RepairPaths,
    begin_repair,
    commit_repair,
    read_token,
    recover_repair,
    rollback_repair,
    validate_token_metadata,
)


BASE_VERSION = "11111111-1111-4111-8111-111111111111"
NEW_VERSION = "22222222-2222-4222-8222-222222222222"
OTHER_VERSION = "33333333-3333-4333-8333-333333333333"
RELEASE = "f320e6b58243b9f06ef5f368907ffd12750533df"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_SOURCE = PROJECT_ROOT / "cloudflare" / "tesco-bridge" / "src" / "worker.js"


class FakeCloudflareClient:
    def __init__(self):
        self.active_version = BASE_VERSION
        self.fail_at = None
        self.active_reads = 0
        self.mutation_started = False
        self.uploads = []
        self.deployed_versions = []

    def get_active_version_id(self):
        self.active_reads += 1
        if self.fail_at == "active_version" and self.active_reads == 1:
            raise CloudflareApiError("http_503")
        return self.active_version

    def upload_version(self, source, release, bridge_secret, base_version_id, tag):
        self.mutation_started = True
        if self.fail_at == "upload_version":
            raise CloudflareApiError("transport_error")
        self.uploads.append(
            {
                "source": source,
                "release": release,
                "bridge_secret": bridge_secret,
                "base_version_id": base_version_id,
                "tag": tag,
            }
        )
        return VersionInfo(NEW_VERSION, 2)

    def deploy_version(self, version_id, expected_active_version):
        if self.fail_at == "predeploy_recheck":
            raise CloudflareApiError("concurrent_deploy")
        if self.active_version != expected_active_version:
            raise CloudflareApiError("concurrent_deploy")
        if self.fail_at == "deploy":
            raise CloudflareApiError("http_503")
        self.active_version = version_id
        self.deployed_versions.append(version_id)


class RepairFixture:
    def __init__(self, tmp_path):
        self.original_env = (
            "UNRELATED=value\n"
            "UVARSI_ENV=production\n"
            "UVARSI_TESCO_BRIDGE_URL=https://uvarsi-tesco-bridge.example.workers.dev\n"
            "UVARSI_TESCO_BRIDGE_WORKER_HOST=uvarsi-tesco-bridge.example.workers.dev\n"
            f"UVARSI_TESCO_BRIDGE_RELEASE={RELEASE}\n"
            f"UVARSI_TESCO_BRIDGE_VERSION_ID={BASE_VERSION}\n"
            f"UVARSI_TESCO_BRIDGE_SECRET={RELEASE}.old_secret_abcdefghijklmnopqrstuvwxyz\n"
            "PLATBY_ZAPNUTE=0\n"
            "UVARSI_PAYMENTS_ENABLED=0\n"
        )
        env = tmp_path / "uvarsi.env"
        env.write_text(self.original_env, encoding="utf-8")
        worker = tmp_path / "tesco-bridge-worker.js"
        shutil.copyfile(WORKER_SOURCE, worker)
        self.paths = RepairPaths(
            token=tmp_path / "cloudflare-worker-token",
            env=env,
            backup_dir=tmp_path / "backups",
            transaction=tmp_path / "repair.json",
            worker_source=worker,
        )
        self.client = FakeCloudflareClient()

    def env_text(self):
        return self.paths.env.read_text(encoding="utf-8")

    def transaction_data(self):
        return json.loads(self.paths.transaction.read_text(encoding="utf-8"))


@pytest.fixture
def repair_fixture(tmp_path):
    return RepairFixture(tmp_path)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666])
def test_token_file_requires_root_only_mode(mode):
    metadata = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=0)
    with pytest.raises(RepairError, match="token_permissions"):
        validate_token_metadata(metadata, expected_uid=0)


def test_token_file_rejects_symlink():
    metadata = SimpleNamespace(st_mode=stat.S_IFLNK | 0o600, st_uid=0)
    with pytest.raises(RepairError, match="token_target"):
        validate_token_metadata(metadata, expected_uid=0)


def test_read_token_rejects_multiline_value(tmp_path, monkeypatch):
    from hetzner import uvarsi_tesco_bridge_repair as repair

    token = tmp_path / "token"
    token.write_text("a" * 32 + "\nsecond-line\n", encoding="ascii")
    # Windows exposes regular files as 0666 even after chmod(0600).  Metadata
    # enforcement has dedicated platform-neutral tests above; isolate the
    # content-format contract here.
    monkeypatch.setattr(repair, "validate_token_metadata", lambda *_args, **_kwargs: None)

    with pytest.raises(RepairError, match="token_format"):
        read_token(token, expected_uid=token.stat().st_uid)


def test_begin_writes_only_redacted_transaction_and_exact_worker(repair_fixture):
    new_version = begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)

    assert new_version == NEW_VERSION
    assert repair_fixture.client.active_version == NEW_VERSION
    assert repair_fixture.client.uploads[0]["source"] == WORKER_SOURCE.read_bytes()
    assert repair_fixture.client.uploads[0]["base_version_id"] == BASE_VERSION
    transaction_text = repair_fixture.paths.transaction.read_text(encoding="utf-8")
    assert repair_fixture.client.uploads[0]["bridge_secret"] not in transaction_text
    assert "unit-token" not in transaction_text
    assert repair_fixture.transaction_data()["phase"] == "env_synced"
    env = repair_fixture.env_text()
    assert f"UVARSI_TESCO_BRIDGE_RELEASE={RELEASE}" in env
    assert f"UVARSI_TESCO_BRIDGE_VERSION_ID={NEW_VERSION}" in env
    assert repair_fixture.client.uploads[0]["bridge_secret"] in env
    assert env.count("PLATBY_ZAPNUTE=0") == 1
    assert env.count("UVARSI_PAYMENTS_ENABLED=0") == 1


def test_recover_restores_version_and_env_after_interrupted_begin(repair_fixture):
    new_version = begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)
    assert new_version == NEW_VERSION
    assert repair_fixture.paths.transaction.exists()

    recover_repair(repair_fixture.paths, repair_fixture.client)

    assert repair_fixture.client.deployed_versions[-1] == BASE_VERSION
    assert repair_fixture.env_text() == repair_fixture.original_env
    assert not repair_fixture.paths.transaction.exists()
    assert not list(repair_fixture.paths.backup_dir.glob("*"))


def test_commit_keeps_candidate_and_removes_recovery_material(repair_fixture):
    begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)
    backup = Path(repair_fixture.transaction_data()["backup_path"])

    commit_repair(repair_fixture.paths)

    assert repair_fixture.client.active_version == NEW_VERSION
    assert f"UVARSI_TESCO_BRIDGE_VERSION_ID={NEW_VERSION}" in repair_fixture.env_text()
    assert not repair_fixture.paths.transaction.exists()
    assert not backup.exists()


def test_rollback_refuses_concurrent_deploy_without_touching_env(repair_fixture):
    begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)
    candidate_env = repair_fixture.env_text()
    repair_fixture.client.active_version = OTHER_VERSION

    with pytest.raises(RepairError, match="concurrent_deploy"):
        rollback_repair(repair_fixture.paths, repair_fixture.client)

    assert repair_fixture.env_text() == candidate_env
    assert repair_fixture.paths.transaction.exists()


def test_recover_prepared_upload_that_was_not_deployed_only_cleans_up(repair_fixture):
    repair_fixture.client.fail_at = "predeploy_recheck"
    with pytest.raises(RepairError, match="concurrent_deploy"):
        begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)
    assert repair_fixture.client.active_version == BASE_VERSION

    repair_fixture.client.fail_at = None
    recover_repair(repair_fixture.paths, repair_fixture.client)

    assert repair_fixture.client.deployed_versions == []
    assert repair_fixture.env_text() == repair_fixture.original_env
    assert not repair_fixture.paths.transaction.exists()


@pytest.mark.parametrize(
    "failure",
    ["active_version", "upload_version", "predeploy_recheck", "deploy"],
)
def test_begin_failure_never_leaves_untracked_partial_state(
    repair_fixture, failure, capsys
):
    repair_fixture.client.fail_at = failure

    with pytest.raises(RepairError):
        begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)

    captured = capsys.readouterr()
    assert "unit-token" not in captured.out + captured.err
    assert repair_fixture.env_text() == repair_fixture.original_env
    if repair_fixture.client.mutation_started:
        assert repair_fixture.paths.transaction.exists()
    else:
        assert not repair_fixture.paths.transaction.exists()
        assert not list(repair_fixture.paths.backup_dir.glob("*"))


def test_env_replace_failure_leaves_deployed_transaction_for_recovery(
    repair_fixture, monkeypatch
):
    from hetzner import uvarsi_tesco_bridge_repair as repair

    original_atomic_write = repair.atomic_write

    def fail_env_write(path, content, mode=0o600):
        if path == repair_fixture.paths.env:
            raise OSError("simulated env failure")
        return original_atomic_write(path, content, mode)

    monkeypatch.setattr(repair, "atomic_write", fail_env_write)

    with pytest.raises(RepairError, match="env_replace"):
        begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)

    assert repair_fixture.client.active_version == NEW_VERSION
    assert repair_fixture.transaction_data()["phase"] == "deployed"
    assert repair_fixture.env_text() == repair_fixture.original_env

    monkeypatch.setattr(repair, "atomic_write", original_atomic_write)
    recover_repair(repair_fixture.paths, repair_fixture.client)
    assert repair_fixture.client.active_version == BASE_VERSION
    assert not repair_fixture.paths.transaction.exists()


def test_invalid_worker_source_is_rejected_before_cloudflare_mutation(repair_fixture):
    repair_fixture.paths.worker_source.write_text("changed", encoding="utf-8")

    with pytest.raises(RepairError, match="worker_hash"):
        begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)

    assert not repair_fixture.client.mutation_started
    assert repair_fixture.env_text() == repair_fixture.original_env
    assert not repair_fixture.paths.transaction.exists()
    assert not list(repair_fixture.paths.backup_dir.glob("*"))


def test_existing_transaction_blocks_second_begin(repair_fixture):
    repair_fixture.paths.transaction.write_text("{}", encoding="utf-8")

    with pytest.raises(RepairError, match="transaction_exists"):
        begin_repair(repair_fixture.paths, repair_fixture.client, RELEASE)

    assert not repair_fixture.client.mutation_started
