# Task 2 Report — Additive auth-v3 schema

## Status

Implemented, verified, self-reviewed, and committed. The migration is additive and idempotent, and the regression fixture preserves populated user, entitlement, plan, magic-token, and session rows across two migration calls.

## RED evidence

The first attempted command with bare `python` did not reach pytest because no Python launcher was on `PATH`; it is not counted as RED evidence. Using the Codex workspace Python runtime produced the intended RED:

```text
Command:
C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q tests/test_auth.py -k "auth_v3_migration" --basetemp=.pytest-tmp-auth-v3-red2

Output:
FF                                                                       [100%]
FAILED tests/test_auth.py::test_auth_v3_migration_is_idempotent_and_preserves_existing_rows
  missing last_seen_at, device_name, and revoked_at
FAILED tests/test_auth.py::test_auth_v3_migration_adds_account_tables_and_session_metadata
  missing all four auth-v3 tables
2 failed, 70 deselected in 1.45s
```

Both tests failed for the intended missing-schema reasons. The preservation assertion ran before the missing-column assertion and passed, proving the fixture itself was readable after both auth-v2 migration calls.

## GREEN evidence

Focused migration tests:

```text
Command:
C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q tests/test_auth.py -k "auth_v3_migration" --basetemp=.pytest-tmp-auth-v3-green

Output:
..                                                                       [100%]
2 passed, 70 deselected in 0.90s
```

Required one-time full-suite run:

```text
Command:
C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q --basetemp=.pytest-tmp-auth-v3-full

Output:
1304 passed, 44 skipped in 195.15s (0:03:15)
```

The full suite was run exactly once.

## Changed files

Committed files:

- `app/auth_data.py` — added the four exact auth-v3 tables and guarded nullable session metadata columns.
- `tests/test_auth.py` — added populated auth-v2 preservation/idempotency coverage and exact auth-v3 field assertions.

Reporting artifact, intentionally ignored and excluded from the code commit:

- `.superpowers/sdd/2026-08-29-auth-heslo-passkey-implementation/task-2-report.md`

## Commit

```text
c12d25a2c1c79047f67fbd3a4dde285f11871a9a
feat(auth): add additive account schema
```

Commit scope from `git show`:

```text
M  app/auth_data.py
M  tests/test_auth.py
```

Task 1 commit `b789a1e` remains an ancestor of this commit.

## Self-review

- Scope: the commit contains exactly `app/auth_data.py` and `tests/test_auth.py`.
- Additivity: existing tables are never dropped, renamed, replaced, or rewritten.
- Idempotency: every new table uses `CREATE TABLE IF NOT EXISTS`; each `sessions_v2` column is added only when absent from `PRAGMA table_info(sessions_v2)`.
- Preservation: the migration test compares the original columns of populated `pouzivatelia`, `naroky`, `plany`, `magic_tokens_v2`, and `sessions_v2` rows before and after two migrations.
- Schema fidelity: table names, field names, types, nullability, primary keys, defaults, and both purpose checks matched the Task 2 brief by source inspection. The initial tests did not behaviorally exercise the two CHECK constraints; fix round 1 below adds that missing coverage.
- Compatibility: existing session inserts name their original four columns, so the new nullable columns do not change current login behavior.
- Hygiene: staged scope was exactly two files; `git diff --cached --check` and `git show --check` exited successfully.
- Initial review result: the self-review missed behavior-level coverage for both purpose CHECK constraints; fix round 1 below addresses the finding.

## Concerns

- Git emitted a non-fatal permission warning while attempting maintenance on unrelated worktree metadata for `uvarsi-protect-costs`; commit `c12d25a` succeeded and was verified independently.
- Concurrent tracked/untracked work and pytest temp directories remain in the shared worktree. They were not reset, cleaned, staged, or committed.

## Fix round 1/5 — purpose CHECK behavior

### Finding verified

The original migration tests asserted the `purpose` columns through `PRAGMA table_info`, which cannot expose or execute a table CHECK expression. They therefore stayed green if either purpose CHECK was removed.

The added tests use real SQLite inserts and cover every permitted value:

- `auth_action_tokens`: `confirm`, `reset`, and `setup` insert successfully; `login` raises `sqlite3.IntegrityError`.
- `auth_webauthn_challenges`: `register` and `login` insert successfully; `reset` raises `sqlite3.IntegrityError`.

The invalid fixtures deliberately use a value permitted by the other table, so accidentally swapping or broadening the purpose sets is detected.

### RED evidence

After adding the tests, both CHECK clauses were temporarily removed from the uncommitted `AUTH_SCHEMA`. No other production line changed.

```text
Command:
C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q tests/test_auth.py -k "auth_v3 and purpose" --basetemp=.pytest-tmp-task2-fix1-constraints-red

Output:
...F..F                                                                  [100%]
FAILED tests/test_auth.py::test_auth_v3_action_token_purpose_rejects_an_invalid_value
  Failed: DID NOT RAISE IntegrityError
FAILED tests/test_auth.py::test_auth_v3_webauthn_challenge_purpose_rejects_an_invalid_value
  Failed: DID NOT RAISE IntegrityError
2 failed, 5 passed, 72 deselected in 1.18s
```

This is the intended mutation failure: every allowed purpose still inserted, while both forbidden purposes were incorrectly accepted without their CHECK constraints.

### GREEN evidence

The two original CHECK clauses were restored byte-for-byte. `git diff --exit-code -- app/auth_data.py` then exited 0, proving the production implementation was unchanged.

```text
Command:
C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q tests/test_auth.py -k "auth_v3 and purpose" --basetemp=.pytest-tmp-task2-fix1-constraints-green

Output:
.......                                                                  [100%]
7 passed, 72 deselected in 0.94s
```

Per the fix-round instruction, only focused tests were run; the full suite was not rerun.

Final focused verification including the two original auth-v3 migration tests:

```text
Command:
C:\Users\Ucet\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q tests/test_auth.py -k "auth_v3" --basetemp=.pytest-tmp-task2-fix1-final-focused

Output:
.........                                                                [100%]
9 passed, 70 deselected in 0.95s
```

### Fix scope and self-review

- Production code is unchanged from `c12d25a`.
- Tests assert successful persistence for all five permitted literals, not merely absence of an exception.
- Both invalid inserts explicitly require `sqlite3.IntegrityError` with a CHECK-constraint failure.
- No mocks, source-text assertions, other task files, or unrelated shared-worktree changes are involved.
- This report is intentionally included alongside `tests/test_auth.py` in the fix-round commit.
