# Uvar.si QA Gatekeeper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Each task needs a fresh implementation agent, an independent review, and recorded evidence.

**Goal:** Make every Uvar.si release evidence-based and prepare the existing release plan for a safe beta.

**Architecture:** The project-local gatekeeper defines non-negotiable safety gates; the existing release-integrity plan remains the implementation sequence for dynamic data, health, deployment and truthful public promises. This plan installs governance only; it does not alter customer behaviour.

**Tech Stack:** Markdown, Git, pytest, PowerShell, Bash.

**Spec:** `docs/superpowers/specs/2026-08-19-uvarsi-qa-gatekeeper-design.md`

## Global Constraints

- No secret, SSH key, API token or password may enter source, test output or release reports.
- No direct production mutation by an agent.
- A failed production health check leaves the release status `BLOCKED`.

### Task 1: Install project-local gatekeeper

**Files:**
- Create: `agents/uvarsi-release-gatekeeper/SKILL.md`
- Create: `docs/superpowers/specs/2026-08-19-uvarsi-qa-gatekeeper-design.md`
- Create: `docs/superpowers/plans/2026-08-19-uvarsi-qa-gatekeeper.md`

- [ ] Verify the gatekeeper explicitly covers current flyers, landing prices, authentication, pantry/recipes, shopping list and payments.
- [ ] Verify it prohibits secrets, stale-price fallback, unverified production claims and cross-project changes.
- [ ] Commit only the three listed documents with message `docs: add Uvar QA release gatekeeper`.

### Task 2: Apply the gatekeeper to the existing integrity plan

**Files:**
- Modify: `docs/superpowers/ledgers/2026-08-18-uvarsi-release-integrity.md`

- [ ] Record the production evidence from 2026-08-19: Uvar service started after a startup race; the actual blocker is zero current-week offers.
- [ ] Record the ruling that legacy HTML-text freshness checks and last-week fallback wording are release blockers.
- [ ] Record that payments are out of scope until beta gate completion.

### Task 3: Execute the existing integrity plan under the gate

**Files:**
- Follow `docs/superpowers/plans/2026-08-18-uvarsi-release-integrity.md` tasks 3–7.

- [ ] Complete dynamic validated landing data and replace the HTML-based dozorca.
- [ ] Remove fixed flyer page/food-page caps, add source/validity evidence, then verify a late food page regression case.
- [ ] Add full browser/API end-to-end tests for login, pantry, recipes and grouped shopping list.
- [ ] Stop before payment implementation and produce a paid-beta readiness report.

## Plan self-review

- Task 1 creates the reusable agent policy.
- Task 2 preserves the production facts and rulings through compaction.
- Task 3 links the policy to the current implementation plan without duplicating its code-level steps.
