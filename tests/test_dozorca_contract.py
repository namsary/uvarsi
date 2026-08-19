import os
import subprocess
from datetime import date
from pathlib import Path

from app.landing_data import write_landing_data_atomic


ROOT = Path(__file__).resolve().parents[1]
BASH = Path("C:/Program Files/Git/bin/bash.exe")


def bash_path(path):
    return "/c" + path.as_posix()[2:]


def payload(week):
    return {
        "schema_version": 1,
        "generated_at": "2026-08-18T05:02:20+02:00",
        "week": week,
        "week_label": "17.–23. 8. 2026",
        "sources": [],
        "receipt": {
            "meals": [{"day": "PO", "name": "Test", "items": []}],
            "nakup_spolu": "1,00",
            "bezne": "2,00",
            "usetris": "1,00",
        },
    }


def test_dozorca_refreshes_stale_json_using_only_json_destination(tmp_path):
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-10"))
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  grep -q '\"week\":\"2026-08-10\"' \"$3\" && exit 1\n"
        "  exit 0\n"
        "fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "printf '{\"week\":\"2026-08-17\"}' > \"$3\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text("#!/bin/sh\necho 30\n", encoding="utf-8")
    fake_sqlite.chmod(0o755)

    environment = os.environ | {
        "UVARSI_DIR": bash_path(tmp_path),
        "UVARSI_LANDING_DATA": bash_path(landing_data),
        "UVARSI_PY": bash_path(fake_python),
        "UVARSI_TODAY": "2026-08-18",
        "PATH": f"{bash_path(tmp_path)}:/usr/bin",
    }
    result = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert calls.read_text(encoding="utf-8") == f"-u refresh_blocek.py {bash_path(landing_data)}\n"
    assert "index.html" not in calls.read_text(encoding="utf-8")


def test_dozorca_does_not_refresh_current_json(tmp_path):
    landing_data = tmp_path / "landing_data.json"
    write_landing_data_atomic(landing_data, payload("2026-08-17"))
    calls = tmp_path / "calls.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
        f"printf '%s\\n' \"$*\" >> '{bash_path(calls)}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sqlite = tmp_path / "sqlite3"
    fake_sqlite.write_text("#!/bin/sh\necho 30\n", encoding="utf-8")
    fake_sqlite.chmod(0o755)

    environment = os.environ | {
        "UVARSI_DIR": bash_path(tmp_path),
        "UVARSI_LANDING_DATA": bash_path(landing_data),
        "UVARSI_PY": bash_path(fake_python),
        "UVARSI_TODAY": "2026-08-18",
        "PATH": f"{bash_path(tmp_path)}:/usr/bin",
    }
    result = subprocess.run(
        [str(BASH), bash_path(ROOT / "hetzner" / "dozorca.sh")],
        cwd=str(ROOT),
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert not calls.exists()
