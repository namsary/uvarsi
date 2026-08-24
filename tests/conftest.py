"""Shared test configuration for the Uvar.si release suite."""
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def ziadne_notifikacie_von(monkeypatch):
    """Testy nikdy nesmú zavolať ntfy.sh.

    Upozornenia majiteľovi sa v testoch overujú cez vlastné dvojníky; keby sa
    z testu odoslala skutočná notifikácia, majiteľovi by pri každom behu suity
    pípal telefón a test by čakal na sieť.
    """
    monkeypatch.syspath_prepend(str(ROOT / "app"))
    try:
        import naklady
    except Exception:  # naklady sa nedá importovať — potom niet čo umlčať
        return
    monkeypatch.setattr(naklady, "posli_ntfy", lambda sprava: None)
