# Uvar.si Release Integrity — execution ledger

Plan: `docs/superpowers/plans/2026-08-18-uvarsi-release-integrity.md`

## Preflight

- Ruling: Create a source-only initial Git snapshot before Task 1 — the plan's staged file list would not version the existing deployable source, so it cannot prove source/production alignment. Cost if wrong: the snapshot contains existing unsafe behavior, but later commits identify the fixes precisely.
- Ruling: Exclude `kluc.ps1` and `odkaz.ps1` from version control until a separate security review — they handle credentials or privileged server access. Cost if wrong: they are not preserved in the release history.
- Ruling: Use the explicitly installed Python 3.12 executable rather than `py` — the launcher is absent from the active shell path. Cost if wrong: future contributors must use the documented executable or add it to PATH.
- Ruling: Fix the Task 4 test clock injection before implementing it — `UVARSI_TODAY` alone cannot make the shell's independent `date` calls deterministic. Cost if wrong: the watcher tests could give false confidence.
- Ruling: Add FastAPI and Anthropic test dependencies only when server-route tests begin, not to the configuration-only Task 1 environment. Cost if wrong: Task 2 setup will require an additional dependency step.

## Completed tasks

- Task 1: fix round 1/5 — strict production URL wiring and non-canonical URL coverage added; scoped re-review clean (commit `65e0376`).
- Task 1: complete (commits `9912c76..65e0376`, review clean).
