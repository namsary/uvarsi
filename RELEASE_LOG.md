
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

## 2026-08-27 — Final integrated SEO release gap closure

- Scope: close the full-size homepage gate, evergreen content, publishable-evidence boundary, samopull root-asset rollback, and all alternate-host redirect gaps.
- Focused evidence: public pages/routes/auth `201 passed, 24 skipped in 29.94s`; samopull/deploy contracts `74 passed, 6 skipped in 1.46s`; release gate `14 passed in 0.53s`.
- Full-suite evidence: `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe -m pytest -q` -> `1024 passed, 47 skipped in 78.75s (0:01:18)`.
- Safety: no deploy, SSH, push, merge, payment change, Caddy change, cron change, environment change, or other-app change was performed in this wave.
- Rollback note: revert commit `fix: close integrated SEO release gaps` and rerun the complete local suite before any future deploy attempt.
- Production status: deployment and live production verification remain pending explicit authorization.

## 2026-08-27 — Autonomous plan-cache recovery (vydanie 2026.08.27.1)

- Scope: retry plan precomputation on every hourly supervisor run after complete Kaufland, Tesco and Lidl data; refresh stale public receipt before warming plans.
- Cost safety: weekly precompute budget remains 0.40 EUR; six run slots allow recovery after transient failures without increasing the euro ceiling.
- Concurrency safety: a process-wide `flock` prevents overlapping supervisors from paying for the same missing cache twice; deployment verifies the dependency.
- Runtime coverage: current data, low offer count, missing store, refresh-before-warm ordering, occupied lock, hot-cache idempotence and success-failure-recovery.
- Full-suite evidence: `1033 passed, 47 skipped in 73.90s (0:01:13)`.
- Payment isolation: payment enablement and payment runtime behavior remain untouched.
- Production status: integration, deployment and live verification are pending the guarded release steps.
