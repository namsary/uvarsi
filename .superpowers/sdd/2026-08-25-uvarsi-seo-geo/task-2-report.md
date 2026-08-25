# Task 2 report — SEO/GEO

## Changed files

- `app/server.py`
- `app/static/app.html`
- `tests/test_server.py`
- `tests/test_auth.py`
- `tests/test_app_html_contract.py`

## Tests and results

- Intended command: `.venv\Scripts\python.exe -m pytest tests/test_server.py tests/test_auth.py tests/test_app_html_contract.py -q`
- Result: not runnable in this environment. The checked-in virtualenv points to `C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe`, which is missing, so neither `python`, `py`, nor the venv launcher can execute pytest here.
- Static verification completed instead: diff review against the task brief, route/header string checks, and contract/test alignment review for all touched files.

## Commit SHA

- `06754cb`

## Self-review

- Public SEO routes are thin wrappers over Task 1 `public_pages` renderers and the existing `LANDING_DATA` path; they do not query the database or call AI.
- The weekly route fails closed: missing/stale/malformed payloads render the recovery page with `503`, `Retry-After: 900`, and `Cache-Control: no-store`.
- `/sitemap.xml` derives weekly `lastmod` only from the same weekly page object when it remains indexable.
- `/app` and `/prihlasenie` are protected centrally with `X-Robots-Tag: noindex, nofollow, noarchive` and `Cache-Control: private, no-store`.
- Login HTML and `app/static/app.html` now include the fallback robots meta tag.

## Concerns

- Runtime verification is still outstanding until a working Python interpreter is restored in this worktree.
- Because tests could not run, any mismatch in FastAPI response header casing/default content-type formatting remains unconfirmed.

## Fix Round 1 evidence

- Findings addressed:
  - `_weekly_public_page()` let malformed on-disk JSON escape as `json.JSONDecodeError`.
  - `tests/test_server.py` lacked a real malformed-file regression.
- New regressions:
  - `test_malformed_landing_file_recovers_weekly_page_instead_of_500`
  - `test_malformed_landing_file_keeps_sitemap_truthful_and_parseable`
- Red run command:
  - `& 'C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe' -m pytest tests/test_server.py tests/test_auth.py tests/test_app_html_contract.py -q`
- Red result:
  - `2 failed, 179 passed, 24 skipped in 26.03s`
  - Both failures traced to `json.decoder.JSONDecodeError` escaping `load_landing_data()` inside `_weekly_public_page()`.
- Narrow fix:
  - `_weekly_public_page()` now catches `FileNotFoundError`, `json.JSONDecodeError`, and `UnicodeDecodeError`, then falls back to the existing safe recovery rendering.
- Green run command:
  - `& 'C:\Users\Ucet\AppData\Local\Programs\Python\Python312\python.exe' -m pytest tests/test_server.py tests/test_auth.py tests/test_app_html_contract.py -q`
- Green result:
  - `181 passed, 24 skipped in 22.19s`
