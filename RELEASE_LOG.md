
## 2026-08-21 00:12 — BLOCKED (vydanie 2026.08.18.1)
  OK  VERSION: 2026.08.18.1
  OK  testy: 260 presly, 0 zlyhalo
  !!  git revizia: ?
  !!  nezapisane zmeny: 1 suborov nie je commitnutych
  OK  /api/health: {"vydanie": "2026.08.18.1", "tyzden": "2026-08-17", "pocet": 431}
  OK  verzia na webe: 2026.08.18.1 (ocakavam 2026.08.18.1)
  OK  tyzden dat: 2026-08-17 (aktualny pondelok 2026-08-17)
  OK  pocet ponuk: 431 (prah 30)
  OK  landing: HTTP 200
  OK  appka: HTTP 200
  !!  landing JSON: HTTP 503
  OK  prihlasovacia stranka: HTTP 200

## 2026-08-26 — SEO GEO release gate update (vydanie 2026.08.25.2)

- Scope: release gate now blocks on robots, sitemap, public SEO pages, weekly freshness signal, private-route `noindex`, immutable font cache, `www` canonical redirect, and homepage canonical/JSON-LD/internal-link regressions.
- Test evidence: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_release_gate_seo.py -q` -> `5 passed in 0.27s`; `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest -q` -> `1002 passed, 47 skipped in 94.95s (0:01:34)`.
- Payment isolation: release-gate work only; `app/platby.py` and payment runtime behavior stay untouched.
- Rollback note: revert commit `chore: gate SEO GEO release`, restore the prior `VERSION` and rerun the local suite before any future deploy attempt.
- Production status: live deploy and production verification are still pending explicit authorization.
