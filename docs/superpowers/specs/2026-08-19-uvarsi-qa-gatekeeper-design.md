# Uvar.si QA Gatekeeper Design

**Purpose:** Turn Uvar.si into a release-controlled product where current price data, login, pantry, recipes and later payments are proven end-to-end instead of patched independently.

## Decision

Use one project-local release authority, `agents/uvarsi-release-gatekeeper/SKILL.md`, alongside the existing release-integrity plan. It is a repeatable instruction set for implementation and review agents; it does not have credentials and cannot directly alter production.

## Boundaries

The gatekeeper coordinates code, tests, reviews and release evidence. It does not scrape private data, send customer mail, charge a card, inspect a secret, or log into Hetzner. Martin retains production execution through `ssh jarvis`.

## Release states

- **BLOCKED:** a customer journey lacks verified data or a test/review has failed.
- **LOCAL PASS:** source and automated tests pass; production has not yet been verified.
- **PRODUCTION VERIFIED:** the public domain and exact current-week health checks have passed after release.

Only the last state permits a claim that the feature works for a real customer.

## Required sequence

1. Complete the current-week landing data contract and replace the HTML-text dozorca.
2. Make flyer extraction cover every discovered page and record an offer’s source/validity.
3. Add end-to-end tests for magic link, profile/pantry, recipe/plan and shopping list.
4. Run three stable free-beta weekly cycles with real households.
5. Add payments, entitlements, refund/legal flows and test-mode checkout.
6. Enable a paid beta only after a production-verified release.

## Explicit non-goals

- No invented counters, savings or availability.
- No fallback to last week’s flyer as though it were current.
- No payments in this release sequence.
- No change to the adjacent Taktik service or Caddy topology.
