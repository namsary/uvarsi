---
name: uvarsi-release-gatekeeper
description: Enforce an evidence-based release gate for Uvar.si changes affecting flyer data, prices, recipes, pantry, authentication, landing content, or payments. Use before declaring any Uvar.si change ready or supplying a production deployment command.
---

# Uvar.si Release Gatekeeper

Use this as the release authority for the Uvar.si worktree. Its job is to prevent a partial patch from being presented as a working paid product.

## Non-negotiable safety rules

- Never read, print, persist, request, or copy API keys, mail tokens, passwords, or SSH private keys.
- Do not directly deploy to Hetzner. Production commands are supplied only after every local gate passes; Martin executes them through his `jarvis` SSH alias.
- Never modify `taktik-mapa`, Caddy configuration, non-Uvar cron entries, or backups for other applications.
- Never publish previous-week prices as current, invent a saving, invent a signup count, or silently substitute unavailable flyer data.
- Payments remain disabled until the paid-beta gate in this file is explicitly passed with Martin.

## Required change workflow

1. State the customer journey being changed and the failure that would be harmful.
2. Write an automated regression test before production code. Confirm it fails for the intended reason.
3. Implement the smallest change that passes the test. Run the focused suite and then the full local suite.
4. Use a fresh independent reviewer for every task. Critical or Important findings block the next task until resolved and re-reviewed.
5. Generate a release manifest: exact source revision, touched files, test command/output summary, no-secret configuration prerequisites, rollback target, and required public checks.
6. Mark status only as `BLOCKED`, `LOCAL PASS`, or `PRODUCTION VERIFIED`. `LOCAL PASS` is never described as deployed or working publicly.

## Customer-journey gates

All applicable gates must pass before a release:

| Journey | Proof required |
|---|---|
| Flyer → database | All discovered pages are scanned; no fixed page limit or evenly sampled food-page cap can omit later food pages. Each offer has source, week and validity window before it can influence a plan. |
| Current data | The database contains enough offers for the exact current Monday; stale weeks return a truthful unavailable state. No LLM is called after this failure is known. |
| Landing receipt | Landing content is stored separately from static HTML, validates week and money arithmetic, and is atomically replaced. Public UI hides savings claims when data is unavailable. |
| Login | Magic links use exactly `https://uvar.si`, expire clearly, are single-use by design, and mail-provider failure is shown honestly. |
| Plan, recipes, pantry | Authenticated user can save pantry preferences, generate a plan only from validated current offers, see recipe ingredients/steps, and receive a grouped shopping list. |
| Paid plan | Entitlement state, checkout success/cancel/refund, invoice/privacy/terms, and customer support path work in a test environment before any live charge. |

## Production evidence

A production release needs a single safe Martin-run command that verifies only Uvar files and reports:

- `uvarsi.service` is active;
- `/api/health` returns the exact release ID, current week, current database offer count and current landing-data week;
- the public app returns 200 from `https://uvar.si`;
- the current-week plan flow is successful with a test account;
- the release’s rollback command restores only the preceding Uvar revision.

If an external dependency blocks this evidence, do not call the release complete. Record the component, failure time, retry policy and user-facing fallback.

## Required status report format

```text
Uvar.si release status: BLOCKED | LOCAL PASS | PRODUCTION VERIFIED
Customer impact:
Evidence:
Open blockers:
Next autonomous action:
One Martin action (only if essential):
```
