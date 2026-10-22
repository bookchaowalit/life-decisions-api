# Product

<!-- impeccable:product-schema 1 -->

## Platform

api

## Users

- The Solo Empire owner, through `life-dashboard-web` (decisions pages), the cell's own MCP server
  (`decisions-mcp`) and the hosted tool hub (`platform-mcp-remote-worker`).
- Automations with an `empire-platform` token, for example the ops-toolkit client
  `infra/scripts/utils/decision_store.py` (`EMPIRE_DECISIONS_API`), which can upsert by dedupe
  key [ต้องยืนยัน: which automations write decisions today], within the permissions `decisions.decision:read|write|approve|delete` and
  `decisions.review:write`.

## Product Purpose

One place to record a decision with its rationale, evidence, confidence and expected outcome, the
history of every change to it, and the dates on which it is reviewed against what actually
happened. Approval is explicit: a decision that needs approval is approved or rejected by someone
with the approve permission, and later edits to what was approved send it back for approval.

Success: important decisions exist once (dedupe keys for automated ones), every state change has
a history event, and reviews come due and get an outcome.

## Positioning

One capability, one service: its own API (Railway), its own database (Neon) with row-level
security per workspace, and authorization, executions, audit and the graph through
`empire-platform` over HTTP with the caller's own token. It fails closed: when the platform cannot
answer, reads and writes return 503.

## Operating Context

- Production: `https://decisions-api-production-1ef5.up.railway.app` (`/health`), Railway project
  `life-decisions-api`; `decisions-api` sleeps when idle (Serverless). Deployed by merging to
  `main` after CI (Railway Wait for CI).
- `decisions-publisher` (same image, start command `decisions-publish`) is a Railway cron every
  15 minutes that sends unpublished outbox rows to the platform graph with backoff.
- Data in Neon project `life-decisions-api`, database `decisions`; migrations are append-only.
- Monitored by `platform-uptime-worker`; errors go to Sentry without personal data when
  `SENTRY_DSN` is set.

## Capabilities and Constraints

- `/v1/decisions`: list (status, domain, approval, source, review due, updated since), stats
  (by status, domain, approval or action status), get by id, dedupe key or legacy id, create (with
  `Idempotency-Key`, 8 to 200 characters), upsert by dedupe key (`created`, `unchanged`,
  `refreshed`), patch (with `expected_version`, 409 on conflict), delete.
- History: `/v1/decisions/{id}/events` (append notes, read newest first or page oldest first) and
  `/v1/decision-events` across records. State events (`created`, `approved`, `updated` and the
  like) only come from writes, never from a direct append; every event records the authenticated
  actor next to the caller's label.
- Reviews: `PUT /v1/decisions/{id}/reviews/{due_date}` (one per decision and day),
  `PATCH /v1/decision-reviews/{id}`, list by status, decision and due date.
- Rules: approving, rejecting or dropping the approval requirement needs `:approve` (and `:write`
  as well when content changes in the same call); a decision that requires approval cannot be
  `not_required`; editing approved content (title, decision, rationale, evidence and the like)
  resets it to pending; an `execution_lock_key` is held by one decision at a time.
- Graph events follow the privacy class: `internal` and `private` (confidential) carry the title,
  `sensitive` (restricted) does not.
- Out of scope: carrying out a decision's action (tasks, messages, payments) and sending review
  reminders [ต้องยืนยัน: whether review reminders are wired to the notifications cell].

## Measures

- Decisions with a review that is past due and still pending (target: 0).
- Decisions pending approval for more than 7 days [ต้องยืนยัน: owner target].
- Outbox rows unpublished after an hour (target: 0).
- 5xx rate and p95 latency on `/v1/decisions` (Sentry and uptime worker).
