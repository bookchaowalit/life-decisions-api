# Agent Instructions

- Human-facing replies are in Thai by default; keep code, commands and identifiers as-is.
- Never print, commit or copy secrets: database URLs, tokens and keys live in Railway
  variables and `~/.config/solo-decisions/` (owner-only).
- Tests use a throwaway local PostgreSQL and a fake platform; they must never reach
  the hosted database or the hosted platform.
- Run `.venv/bin/pytest -q` before committing. Migrations are append-only: never edit
  an applied file, add a new numbered one.
- Merging to `main` deploys production (Railway GitHub source + Wait for CI); verify `/health`
  and one authorized read afterwards. Do not `railway up` from a working tree.
- Keep the solo-empire registry entry (`decisions` in
  `repository-catalog/registries/online-systems.yaml`) in sync.
- `decisions-mcp` (mcp_server.py in the package) is this cell's MCP server: HTTP API only, caller's own token, no identity arguments; keep tools in step with the API routes and run `tests/test_mcp.py`.
