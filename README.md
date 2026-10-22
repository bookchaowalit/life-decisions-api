# life-decisions-api
The decisions cell: decision records, their event history, and scheduled
reviews. It keeps its own Neon database and reuses
[empire-platform](https://github.com/bookchaowalit/platform-empire) for
identity, authorization, execution and audit records, and the graph, all over
HTTP with the caller's own token.

| | |
|---|---|
| API | Railway project `life-decisions-api`, service `decisions-api` (Serverless: sleeps when idle) |
| Outbox sweep | service `decisions-publisher`, cron `*/15 * * * *` |
| Data | Neon project `life-decisions-api`, database `decisions` |
| Registry | solo-empire `repository-catalog/registries/online-systems.yaml` (`decisions`) |

## How a request works

- **Read:** verify the token (`decisions-api` audience), ask the platform
  `POST /v1/authz/decide`, then read under row-level security bound to the
  token's workspace.
- **Write:** `POST /v1/executions` on the platform (decision with locks plus an
  execution record), then one local transaction that changes the row, appends
  the event history and the outbox, then `POST /v1/executions/{id}/finish`
  with row hashes for the audit trail.
- **Graph:** outbox rows go to the platform `POST /v1/graph/events` right
  after the write (best effort) and from the cron sweep (retries with backoff).

Fails closed: if the platform cannot answer, reads and writes return 503.

Permissions: `decisions.decision:read|write|approve|delete`,
`decisions.review:write`; changing `approval_status` needs `approve`. The
publisher identity needs `graph.decisions:write`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/decisions` | list (status, domain, approval, source, review due, updated since) |
| GET | `/v1/decisions/stats` | counts by status, domain, approval or action status |
| GET | `/v1/decisions/{id}` · `/dedupe/{key}` · `/legacy/{id}` | one record |
| POST | `/v1/decisions` | create (`Idempotency-Key` header supported) |
| PUT | `/v1/decisions/dedupe/{key}` | create or refresh by dedupe key |
| PATCH | `/v1/decisions/{id}` | change fields, optional `event` and `expected_version` |
| DELETE | `/v1/decisions/{id}` | delete |
| POST/GET | `/v1/decisions/{id}/events` | append or read history |
| GET | `/v1/decision-events` | history across records |
| GET | `/v1/decision-reviews` | reviews (status, decision, due) |
| PUT | `/v1/decisions/{id}/reviews/{due_date}` | create or update a review |
| PATCH | `/v1/decision-reviews/{id}` | update a review |

## Develop

```bash
uv sync --frozen --all-extras        # versions from uv.lock (the image installs the same lock)
.venv/bin/pytest -q
uvx ruff@0.17.1 format --check . && uvx ruff@0.17.1 check . && .venv/bin/mypy
```

## Operate

```bash
NEON_API_KEY=... .venv/bin/decisions-db provision    # project, roles, migrations; URLs owner-only
DECISIONS_DB_MODE=hosted .venv/bin/decisions-db migrate
npm install && railway config apply                  # service settings only; code deploys from GitHub (below)
```

Deploys (CD, since 2026-10-18): `decisions-api` and `decisions-publisher` are connected to this
repository's `main` with Railway's Wait for CI (`checkSuites`). A merge to `main` deploys only
after the CI and security checks pass; a failing check leaves production on the previous
deployment. Roll back by redeploying an earlier deployment in Railway. `railway up` is no
longer used.

Railway variables (never in the repository): `SENTRY_DSN` (error reporting; unset = off, see
`src/solo_decisions/_sentry.py`), `DECISIONS_DB_MODE=hosted`,
`DECISIONS_DB_URL_API`, `DECISIONS_DB_URL_PUBLISHER`,
`DECISIONS_JWT_PUBLIC_KEY_PEM`, `DECISIONS_JWT_ISSUER`, `EMPIRE_PLATFORM_URL`,
`DECISIONS_PUBLISHER_TOKEN`.

## MCP server (`decisions-mcp`)

`solo_decisions.mcp_server` is this cell's MCP server (stdio). Every tool calls the cell's HTTP API with the caller's own
token, so MCP has exactly the authorization the API has: it never touches the database, and no tool accepts
an actor, workspace, delegation or token argument (strict models reject unknown arguments before any HTTP
call). Errors reach the client as a fixed message from the HTTP status only (for example `403: forbidden`), never upstream text; tokens with whitespace or control characters are refused.

- Tools: `decisions.decision.{list,get,stats,events}`, `decisions.event.list`, `decisions.review.{list,get}`.
- Write tools: `decisions.decision.create` (optional `idempotency_key`; starts proposed and pending approval; cannot approve or act), `decisions.decision.note` (non-state history notes only) and `decisions.review.update`. Set `EMPIRE_MCP_READONLY=1` to register only the read tools.
- URL: `EMPIRE_DECISIONS_API` (env, else the owner-only `~/.config/empire-platform/client.env`); https only (http only on loopback), no credentials, query or fragment.
- Token, first match wins: `EMPIRE_TOKEN`, `EMPIRE_TOKEN_FILE` (mode 600), then the automation token named by
  `EMPIRE_MCP_AUTOMATION` (default `owner-cli`, lower-case letters, digits and hyphens) from `EMPIRE_AUTOMATION_TOKEN_<NAME>` or
  `~/.config/empire-platform/tokens/<name>.jwt`.
- Run: `uv pip install --python .venv/bin/python -e ".[mcp]"`, then `.venv/bin/decisions-mcp` (see `.mcp.json`).
- Tests: `.venv/bin/pytest tests/test_mcp.py` (fake HTTP transport; no database, no network).
