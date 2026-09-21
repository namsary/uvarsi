"""Transactional, server-side repair for the Uvar.si Tesco Worker bridge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

try:
    from .uvarsi_cloudflare_worker import (
        CloudflareApiError,
        CloudflareWorkerClient,
        WORKER_HOST,
        WORKER_URL,
    )
except ImportError:  # pragma: no cover - used by the installed standalone script
    from uvarsi_cloudflare_worker import (  # type: ignore[no-redef]
        CloudflareApiError,
        CloudflareWorkerClient,
        WORKER_HOST,
        WORKER_URL,
    )


EXPECTED_WORKER_SHA256 = (
    "de485d6776e311877b8e7602ec6c4a0aa0dd962175b75fd64424b2cc37472f4d"
)
DEFAULT_TOKEN_PATH = Path("/etc/uvarsi/secrets/cloudflare-worker-token")
DEFAULT_ENV_PATH = Path("/opt/uvarsi/uvarsi.env")
DEFAULT_BACKUP_DIR = Path("/opt/uvarsi/backups")
DEFAULT_TRANSACTION_PATH = Path("/opt/uvarsi/.tesco-bridge-repair.json")
DEFAULT_WORKER_SOURCE = Path("/opt/uvarsi/tesco-bridge-worker.js")
MAX_ENV_BYTES = 1_000_000
MAX_TRANSACTION_BYTES = 32_768

_RELEASE = re.compile(r"[0-9a-f]{12,64}\Z")
_VERSION = re.compile(r"[A-Za-z0-9._-]{8,128}\Z")
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_FALSE_VALUES = {"", "0", "false", "off", "no", "nie"}
_MANAGED_ENV = {
    "UVARSI_ENV",
    "UVARSI_TESCO_BRIDGE_URL",
    "UVARSI_TESCO_BRIDGE_WORKER_HOST",
    "UVARSI_TESCO_BRIDGE_RELEASE",
    "UVARSI_TESCO_BRIDGE_VERSION_ID",
    "UVARSI_TESCO_BRIDGE_SECRET",
    "PLATBY_ZAPNUTE",
    "UVARSI_PAYMENTS_ENABLED",
}


@dataclass(frozen=True)
class RepairPaths:
    token: Path = DEFAULT_TOKEN_PATH
    env: Path = DEFAULT_ENV_PATH
    backup_dir: Path = DEFAULT_BACKUP_DIR
    transaction: Path = DEFAULT_TRANSACTION_PATH
    worker_source: Path = DEFAULT_WORKER_SOURCE


@dataclass(frozen=True)
class Transaction:
    phase: str
    backup_path: Path
    base_version_id: str
    candidate_version_id: str | None
    release: str
    worker_sha256: str


class RepairError(RuntimeError):
    """A stable, redacted repair failure."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate_token_metadata(metadata: os.stat_result, expected_uid: int = 0) -> None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RepairError("token_target")
    if metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RepairError("token_permissions")


def read_token(path: Path, expected_uid: int = 0) -> str:
    try:
        before = path.lstat()
        validate_token_metadata(before, expected_uid)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            validate_token_metadata(opened, expected_uid)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise RepairError("token_target")
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
    except RepairError:
        raise
    except OSError:
        raise RepairError("token_read") from None
    if len(raw) > 4096:
        raise RepairError("token_format")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if b"\n" in raw or b"\r" in raw:
        raise RepairError("token_format")
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        raise RepairError("token_format") from None
    if len(value) < 32 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        raise RepairError("token_format")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _exclusive_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError:
        raise RepairError("transaction_exists") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_regular_file(path: Path, maximum: int, reason: str) -> bytes:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RepairError(reason)
        if metadata.st_size > maximum:
            raise RepairError(reason)
        content = path.read_bytes()
    except RepairError:
        raise
    except OSError:
        raise RepairError(reason) from None
    if len(content) > maximum:
        raise RepairError(reason)
    return content


def _read_env(path: Path) -> str:
    raw = _read_regular_file(path, MAX_ENV_BYTES, "env_invalid")
    if b"\0" in raw:
        raise RepairError("env_invalid")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RepairError("env_invalid") from None
    _require_payments_off(content)
    return content


def _require_payments_off(content: str) -> None:
    values: dict[str, list[str]] = {
        "PLATBY_ZAPNUTE": [],
        "UVARSI_PAYMENTS_ENABLED": [],
    }
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) in values:
            values[match.group(1)].append(line.split("=", 1)[1].strip().lower())
    if any(len(found) != 1 or found[0] not in _FALSE_VALUES for found in values.values()):
        raise RepairError("payments_not_off")


def _updated_env(
    original: str, release: str, version_id: str, bridge_secret: str
) -> str:
    kept: list[str] = []
    for line in original.splitlines():
        match = _ASSIGNMENT.match(line)
        if not line.lstrip().startswith("#") and match and match.group(1) in _MANAGED_ENV:
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    kept.extend(
        [
            "",
            "UVARSI_ENV=production",
            f"UVARSI_TESCO_BRIDGE_URL={WORKER_URL}",
            f"UVARSI_TESCO_BRIDGE_WORKER_HOST={WORKER_HOST}",
            f"UVARSI_TESCO_BRIDGE_RELEASE={release}",
            f"UVARSI_TESCO_BRIDGE_VERSION_ID={version_id}",
            f"UVARSI_TESCO_BRIDGE_SECRET={bridge_secret}",
            "PLATBY_ZAPNUTE=0",
            "UVARSI_PAYMENTS_ENABLED=0",
        ]
    )
    return "\n".join(kept) + "\n"


def _transaction_payload(transaction: Transaction) -> str:
    payload = {
        "schema": 1,
        "phase": transaction.phase,
        "backup_path": str(transaction.backup_path),
        "base_version_id": transaction.base_version_id,
        "candidate_version_id": transaction.candidate_version_id,
        "release": transaction.release,
        "worker_sha256": transaction.worker_sha256,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def _write_transaction(path: Path, transaction: Transaction, exclusive: bool = False) -> None:
    content = _transaction_payload(transaction)
    if exclusive:
        _exclusive_write(path, content)
    else:
        atomic_write(path, content)


def _load_transaction(paths: RepairPaths) -> Transaction:
    raw = _read_regular_file(paths.transaction, MAX_TRANSACTION_BYTES, "transaction_invalid")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise RepairError("transaction_invalid") from None
    expected_keys = {
        "schema",
        "phase",
        "backup_path",
        "base_version_id",
        "candidate_version_id",
        "release",
        "worker_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys or payload["schema"] != 1:
        raise RepairError("transaction_invalid")
    phase = payload["phase"]
    base = payload["base_version_id"]
    candidate = payload["candidate_version_id"]
    release = payload["release"]
    worker_sha = payload["worker_sha256"]
    backup_raw = payload["backup_path"]
    if (
        phase not in {"prepared", "deployed", "env_synced"}
        or not isinstance(base, str)
        or _VERSION.fullmatch(base) is None
        or (candidate is not None and (not isinstance(candidate, str) or _VERSION.fullmatch(candidate) is None))
        or not isinstance(release, str)
        or _RELEASE.fullmatch(release) is None
        or worker_sha != EXPECTED_WORKER_SHA256
        or not isinstance(backup_raw, str)
    ):
        raise RepairError("transaction_invalid")
    backup = Path(backup_raw)
    try:
        backup.resolve().relative_to(paths.backup_dir.resolve())
    except (OSError, ValueError):
        raise RepairError("transaction_invalid") from None
    if not backup.name.startswith("uvarsi-env-before-bridge-repair-"):
        raise RepairError("transaction_invalid")
    if phase in {"deployed", "env_synced"} and candidate is None:
        raise RepairError("transaction_invalid")
    return Transaction(phase, backup, base, candidate, release, worker_sha)


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except FileNotFoundError:
        return
    except OSError:
        raise RepairError("local_io") from None


def _new_backup_path(paths: RepairPaths) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return paths.backup_dir / (
        f"uvarsi-env-before-bridge-repair-{stamp}-{secrets.token_hex(6)}"
    )


def begin_repair(
    paths: RepairPaths, client: CloudflareWorkerClient, release: str
) -> str:
    if paths.transaction.exists() or paths.transaction.is_symlink():
        raise RepairError("transaction_exists")
    if not isinstance(release, str) or _RELEASE.fullmatch(release) is None:
        raise RepairError("invalid_release")

    original_env = _read_env(paths.env)
    try:
        paths.backup_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        if paths.backup_dir.is_symlink() or not paths.backup_dir.is_dir():
            raise RepairError("backup_target")
        os.chmod(paths.backup_dir, 0o700)
    except RepairError:
        raise
    except OSError:
        raise RepairError("backup_target") from None

    backup = _new_backup_path(paths)
    try:
        atomic_write(backup, original_env, 0o600)
    except OSError:
        raise RepairError("backup_write") from None

    transaction_written = False
    try:
        try:
            base_version = client.get_active_version_id()
        except CloudflareApiError as error:
            raise RepairError(error.reason) from None

        worker_source = _read_regular_file(
            paths.worker_source, 10 * 1024 * 1024, "worker_source"
        )
        worker_sha = hashlib.sha256(worker_source).hexdigest()
        if worker_sha != EXPECTED_WORKER_SHA256:
            raise RepairError("worker_hash")

        transaction = Transaction(
            "prepared", backup.resolve(), base_version, None, release, worker_sha
        )
        _write_transaction(paths.transaction, transaction, exclusive=True)
        transaction_written = True

        bridge_secret = release + "." + secrets.token_urlsafe(32)
        tag = "repair-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            uploaded = client.upload_version(
                worker_source,
                release,
                bridge_secret,
                base_version,
                tag,
            )
        except CloudflareApiError as error:
            raise RepairError(error.reason) from None

        transaction = replace(transaction, candidate_version_id=uploaded.id)
        _write_transaction(paths.transaction, transaction)
        try:
            # New BRIDGE_SECRET and WORKER_RELEASE bindings require Cloudflare's
            # explicit force gate even though this is a forward deployment.
            client.deploy_version(
                uploaded.id,
                expected_active_version=base_version,
                force=True,
            )
        except CloudflareApiError as error:
            raise RepairError(error.reason) from None

        transaction = replace(transaction, phase="deployed")
        _write_transaction(paths.transaction, transaction)
        candidate_env = _updated_env(
            original_env, release, uploaded.id, bridge_secret
        )
        try:
            atomic_write(paths.env, candidate_env, 0o600)
        except OSError:
            raise RepairError("env_replace") from None

        transaction = replace(transaction, phase="env_synced")
        _write_transaction(paths.transaction, transaction)
        return uploaded.id
    except RepairError:
        if not transaction_written:
            try:
                _remove_file(backup)
            except RepairError:
                pass
        raise
    except OSError:
        if not transaction_written:
            try:
                _remove_file(backup)
            except RepairError:
                pass
        raise RepairError("local_io") from None


def _restore_env_and_cleanup(paths: RepairPaths, transaction: Transaction) -> None:
    backup = _read_regular_file(transaction.backup_path, MAX_ENV_BYTES, "backup_invalid")
    try:
        original = backup.decode("utf-8")
    except UnicodeDecodeError:
        raise RepairError("backup_invalid") from None
    try:
        atomic_write(paths.env, original, 0o600)
    except OSError:
        raise RepairError("env_replace") from None
    _remove_file(paths.transaction)
    _remove_file(transaction.backup_path)


def rollback_repair(paths: RepairPaths, client: CloudflareWorkerClient) -> None:
    transaction = _load_transaction(paths)
    candidate = transaction.candidate_version_id
    if candidate is not None:
        try:
            active = client.get_active_version_id()
            if active == candidate:
                client.deploy_version(
                    transaction.base_version_id,
                    expected_active_version=candidate,
                    force=True,
                )
            elif active != transaction.base_version_id:
                raise RepairError("concurrent_deploy")
        except CloudflareApiError as error:
            raise RepairError(error.reason) from None
    _restore_env_and_cleanup(paths, transaction)


def recover_repair(paths: RepairPaths, client: CloudflareWorkerClient) -> None:
    rollback_repair(paths, client)


def commit_repair(paths: RepairPaths) -> None:
    transaction = _load_transaction(paths)
    if transaction.phase != "env_synced":
        raise RepairError("transaction_not_ready")
    # Delete the transaction first.  A crash can leave an inert backup behind,
    # but never a recovery instruction whose required backup has disappeared.
    _remove_file(paths.transaction)
    _remove_file(transaction.backup_path)


def _default_paths() -> RepairPaths:
    return RepairPaths(
        token=Path(os.environ.get("UVARSI_CLOUDFLARE_TOKEN_FILE", DEFAULT_TOKEN_PATH)),
        env=Path(os.environ.get("UVARSI_ENV_FILE", DEFAULT_ENV_PATH)),
        backup_dir=Path(os.environ.get("UVARSI_REPAIR_BACKUP_DIR", DEFAULT_BACKUP_DIR)),
        transaction=Path(
            os.environ.get("UVARSI_REPAIR_TRANSACTION", DEFAULT_TRANSACTION_PATH)
        ),
        worker_source=Path(
            os.environ.get("UVARSI_REPAIR_WORKER_SOURCE", DEFAULT_WORKER_SOURCE)
        ),
    )


def _client_from_token(paths: RepairPaths) -> CloudflareWorkerClient:
    return CloudflareWorkerClient(read_token(paths.token))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify-token")
    begin = subparsers.add_parser("begin")
    begin.add_argument("release")
    subparsers.add_parser("commit")
    subparsers.add_parser("rollback")
    subparsers.add_parser("recover")
    arguments = parser.parse_args(argv)
    paths = _default_paths()
    try:
        if arguments.command == "commit":
            commit_repair(paths)
            print("repair_committed")
            return 0
        client = _client_from_token(paths)
        if arguments.command == "verify-token":
            client.get_active_version_id()
            print("cloudflare_token_ok")
        elif arguments.command == "begin":
            print(begin_repair(paths, client, arguments.release))
        elif arguments.command == "rollback":
            rollback_repair(paths, client)
            print("repair_rolled_back")
        elif arguments.command == "recover":
            recover_repair(paths, client)
            print("repair_recovered")
        return 0
    except (RepairError, CloudflareApiError) as error:
        reason = error.reason
        print(f"repair_error={reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
