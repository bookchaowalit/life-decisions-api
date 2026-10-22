# SPEC-001 — L2 baseline: locked dependencies, strict types, domain rules under test

Standard: ADR-0004 — TDD + SDD (architecture-docs, `docs/architecture/decisions/ADR-0004-tdd-sdd-standard.md`).
Big Loop Plan Wave 2 (solo-empire `repository-catalog/roadmaps/BIG-LOOP-PLAN-2026-10-21.md`).

## Document control

| Field | Value |
|---|---|
| Spec ID / version | SPEC-001 / 1.0 |
| Status | in review (implemented; owner approval pending) |
| Repository / path | bookchaowalit/life-decisions-api / `specs/001-l2-baseline/spec.md` |
| Owner (maker) | Claude Code (Wave 2 cells session) |
| Reviewer (checker) | Fresh Claude reviewer session (no maker context) |
| Approver | Owner (bookchaowalit): pending, see the Wave 2 report owner actions |
| Requirement links | Big Loop DoD L2: `PRODUCT.md`, ≥ 3 test files, Sentry, type check; L1 lockfile |
| Main-only | yes: committed straight to `main`, so the red run is local (ADR-0004 rule 2, v1.1) |
| Created / approved | 2026-10-22 / pending owner |

## 1. Problem and intent

- **Problem:** The decisions cell had no lockfile (the image installed unpinned versions with pip), no type, lint or coverage gates in CI, and its approval, history, privacy and review rules were tested only inside one broad API test file.
- **Outcome:** the image installs exactly `uv.lock` (`uv sync --frozen`), CI runs Ruff, mypy
  (strict on `src/`) and a coverage gate on shared CI v1.1.1, and the cell's own rules have
  tests of their own beyond the generic contract suite.
- **Why now:** Wave 2 of the Big Loop Plan; milestone M1 is 2026-11-15.

## 2. Scope

| In scope | Out of scope (non-goals) |
|---|---|
| Lock file and frozen install, CI caller bump, Ruff/mypy config and fixes, rule tests, `PRODUCT.md` | New endpoints or fields; schema changes |

## 3. Behaviour — scenarios

### AC-01 — Approval requirement cannot be bypassed

- **Given** a writer without :approve and a decision that requires approval
- **When** the writer drops approval_required
- **Then** 403 and the decision still requires approval
- **Test:** `tests/test_decision_approval.py::test_dropping_the_approval_requirement_needs_the_approve_permission` · level: unit / API with local fakes

### AC-02 — A required approval cannot be not_required

- **Given** a new decision that requires approval
- **When** it is created with approval_status not_required
- **Then** 422 approval_required_but_not_required
- **Test:** `tests/test_decision_approval.py::test_a_decision_that_needs_approval_cannot_be_marked_not_required` · level: unit / API with local fakes

### AC-03 — Editing approved content resets the approval

- **Given** an approved decision with no action
- **When** its rationale changes
- **Then** approval pending, status proposed, action_status none
- **Test:** `tests/test_decision_approval.py::test_editing_an_approved_decision_without_an_action_resets_only_the_approval` · level: unit / API with local fakes

### AC-04 — Outcomes do not reset the approval

- **Given** an approved decision
- **When** actual_outcome and review_date change
- **Then** it stays approved
- **Test:** `tests/test_decision_approval.py::test_recording_the_actual_outcome_keeps_the_approval` · level: unit / API with local fakes

### AC-05 — State events only through PATCH

- **Given** a decision
- **When** a state event is appended directly
- **Then** 422 use_patch_for_state and no event is stored
- **Test:** `tests/test_decision_history.py::test_state_events_cannot_be_appended_directly` · level: unit / API with local fakes

### AC-06 — Notes record the authenticated actor

- **Given** a note whose metadata claims another actor
- **When** it is appended
- **Then** the caller's label is kept and authenticated_actor is the token's subject
- **Test:** `tests/test_decision_history.py::test_a_note_keeps_the_callers_label_and_records_the_real_actor` · level: unit / API with local fakes

### AC-07 — Sensitive decisions leave no title in the graph

- **Given** a sensitive decision
- **When** it is created
- **Then** the outbox event is restricted and has no title
- **Test:** `tests/test_decision_privacy.py::test_sensitive_decisions_go_to_the_graph_restricted_and_without_a_title` · level: unit / API with local fakes

### AC-08 — Idempotency key length

- **Given** an Idempotency-Key shorter than 8 or longer than 200 characters
- **When** a decision is created
- **Then** 422 naming Idempotency-Key
- **Test:** `tests/test_decision_privacy.py::test_idempotency_keys_must_be_8_to_200_characters` · level: unit / API with local fakes

### AC-09 — Reviews need the review permission

- **Given** a writer without decisions.review:write
- **When** they put a review
- **Then** 403
- **Test:** `tests/test_decision_reviews.py::test_writing_a_review_needs_the_review_permission` · level: unit / API with local fakes

### AC-10 — Stale review version

- **Given** a review at version 1
- **When** it is patched with expected_version 2
- **Then** 409 version_conflict and nothing changes
- **Test:** `tests/test_decision_reviews.py::test_a_stale_review_version_is_a_conflict_and_changes_nothing` · level: unit / API with local fakes

## 4. Interfaces and data

| Interface | Change | Contract |
|---|---|---|
| P | O | S |
| P | U | T |
| P | O | S |
| G | E | T |
| P | U | T |
| P | A | T |
| d | e | c |

- **Data model / migration:** N/A — no schema change.
- **Backward compatibility:** same routes and payloads; only tooling, locks, typing fixes and tests changed unless a scenario above says otherwise.

## 5. Non-functional requirements

| NFR | Target | How it is tested |
|---|---|---|
| Performance | N/A — no hot-path change beyond one comparison per declared range | — |
| Security / privacy | Authorization unchanged; Sentry sends no PII and scrubs tokens/URLs | existing auth tests, Sentry tests |
| Reliability / observability | Reproducible install from the lock file; opt-in Sentry error reporting | image rehearsal before push; Sentry tests |
| Accessibility / UX | N/A — API only | — |

## 6. Test plan (TDD)

- **Test levels:** unit and API tests with local fakes; no hosted service is called.
- **Test data:** generated in each test; no production data.
- **Coverage:** CI gate `coverage-threshold: "78"` (measured 80.30% on 2026-10-22, minus 2 points).
- **Not test-first (exceptions):** tests that pin behaviour the service already had
  (characterization tests, marked below) were written after the code by nature; tooling and
  config changes are covered by CI itself.

### Red / green log

| Scenario | Test | Red: commit / run + failure message | Green: commit / run | Refactor note |
|---|---|---|---|---|
| AC-01 | `tests/test_decision_approval.py::test_dropping_the_approval_requirement_needs_the_approve_permission` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-02 | `tests/test_decision_approval.py::test_a_decision_that_needs_approval_cannot_be_marked_not_required` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-03 | `tests/test_decision_approval.py::test_editing_an_approved_decision_without_an_action_resets_only_the_approval` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-04 | `tests/test_decision_approval.py::test_recording_the_actual_outcome_keeps_the_approval` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-05 | `tests/test_decision_history.py::test_state_events_cannot_be_appended_directly` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-06 | `tests/test_decision_history.py::test_a_note_keeps_the_callers_label_and_records_the_real_actor` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-07 | `tests/test_decision_privacy.py::test_sensitive_decisions_go_to_the_graph_restricted_and_without_a_title` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-08 | `tests/test_decision_privacy.py::test_idempotency_keys_must_be_8_to_200_characters` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-09 | `tests/test_decision_reviews.py::test_writing_a_review_needs_the_review_permission` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |
| AC-10 | `tests/test_decision_reviews.py::test_a_stale_review_version_is_a_conflict_and_changes_nothing` | characterization (behaviour already present; passed on first run) | CI run of the pushed tip | none |

## 7. Risks and open questions

| # | Risk or question | Impact | Owner | Resolution / date |
|---|---|---|---|---|
| 1 | Stored rows written before a range rule existed may already be inverted | a patch touching one of those dates is refused until both are fixed | owner | patches that do not touch the range still work |

## 8. Change log

| Version | Date | Change | Scenarios affected | Approved by |
|---|---|---|---|---|
| 1.0 | 2026-10-22 | Implemented with the Wave 2 baseline | all | pending owner |

## Approval

| Gate | Role | Name / date | Decision | Evidence |
|---|---|---|---|---|
| Implemented (DoD) | Reviewer (checker) | Fresh Claude reviewer / 2026-10-22 | see Wave 2 report | CI run of the pushed tip |
| Implemented (DoD) | Approver | Owner | pending | Wave 2 report |
