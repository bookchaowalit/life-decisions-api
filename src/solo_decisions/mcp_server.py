"""MCP server for the decisions cell: tools that call the cell's own HTTP API.

Every tool calls the API with the caller's own token, so MCP has exactly the authorization the API has: no tool
reads the database, and no tool accepts an actor, workspace, delegation or token argument. Unknown arguments are
rejected by strict pydantic models (``extra="forbid"``) before any HTTP call is made.

Configuration (same conventions as solo-cell-kit ``cellkit.mcp``)::

    EMPIRE_DECISIONS_API=https://...   env, else the owner-only ~/.config/empire-platform/client.env
    EMPIRE_TOKEN=...                bearer, else
    EMPIRE_TOKEN_FILE=/path/x.jwt   a token file (mode 600), else the automation token
    EMPIRE_MCP_AUTOMATION=owner-mcp EMPIRE_AUTOMATION_TOKEN_<NAME> or ~/.config/empire-platform/tokens/<name>.jwt
    EMPIRE_MCP_READONLY=1           register only the read tools

    decisions-mcp                        stdio server (see .mcp.json)
"""

from __future__ import annotations

import inspect
import os
import re
import urllib.parse
import uuid
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import Tool as MCPTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolFailed(ToolError):
    """Reaches the MCP client as a tool error carrying only a fixed message mapped from the HTTP status."""


class ConfigError(RuntimeError):
    pass


# --- configuration (vendored from solo-cell-kit cellkit/mcp.py; do not import across repos) -----------


def client_file(environ: Any) -> dict[str, str]:
    override = (environ.get("EMPIRE_CLIENT_CONFIG") or "").strip()
    path = (
        Path(override).expanduser()
        if override
        else Path.home() / ".config" / "empire-platform" / "client.env"
    )
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return {}
    if mode & 0o077:
        raise ConfigError(
            f"refusing client config {path}: group/world accessible; run chmod 600"
        )
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def api_url(key: str, environ: Any = None) -> str:
    """The API base URL from ``key`` (env, else client.env): https only, http only on loopback."""
    environ = os.environ if environ is None else environ
    value = (environ.get(key) or "").strip() or client_file(environ).get(key, "")
    if not value:
        raise ConfigError(f"the API is not configured: set {key} (env or client.env)")
    parts = urllib.parse.urlsplit(value)
    host = (parts.hostname or "").lower()
    if (
        parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
    ):
        raise ConfigError(f"{key} must not carry credentials, a query or a fragment")
    if not (parts.scheme == "https" and host) and not (
        parts.scheme == "http" and host in LOOPBACK
    ):
        raise ConfigError(
            f"{key} must be https (http only for 127.0.0.1, ::1 or localhost)"
        )
    return value


def clean_token(value: str) -> str:
    """The token without surrounding whitespace; any inner whitespace or control character is refused."""
    token = value.strip()
    if not token or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in token):
        raise ConfigError(
            "the token has an unexpected format"
        ) from None  # never echo it
    return token


AUTOMATION = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
FORBIDDEN_ARGUMENTS = frozenset(
    {
        "actor",
        "actor_id",
        "workspace",
        "workspace_id",
        "tenant",
        "tenant_id",
        "delegation",
        "token",
        "authorization",
        "owner_id",
    }
)
IDEMPOTENCY_KEY = r"^[A-Za-z0-9._:-]{8,128}$"


def read_owner_file(path: Path, what: str) -> str:
    if path.stat().st_mode & 0o077:
        raise ConfigError(
            f"{what} file {path} must be readable by its owner only (chmod 600)"
        )
    return path.read_text(encoding="utf-8").strip()


# --- the server -----------------------------------------------------------------------------------


class StrictMCPServer(MCPServer):
    """MCPServer that validates arguments against a strict model before any tool runs."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.argument_models: dict[str, type[Strict]] = {}

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        for tool in tools:
            model = self.argument_models.get(tool.name)
            if model is not None:
                tool.input_schema = {
                    **model.model_json_schema(),
                    "additionalProperties": False,
                }
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> Any:
        model = self.argument_models.get(name)
        if model is None:
            raise ToolFailed("unknown_tool")
        try:
            model.model_validate(arguments or {})
            valid = True
        except ValidationError:
            valid = False  # raised below, outside the handler: no cause or context keeps the rejected input
        if not valid:
            raise ToolFailed(
                "invalid_arguments"
            ) from None  # never names the argument or echoes its value
        return await super().call_tool(name, arguments, context)


REASONS = {
    400: "invalid",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "invalid",
    409: "conflict",
    410: "not_found",
    413: "invalid",
    422: "invalid",
    429: "rate_limited",
}


def failure(status: int) -> str:
    """A fixed message from the status alone: upstream text and detail fields are never included."""
    return f"{status}: {'upstream_error' if status >= 500 else REASONS.get(status, 'error')}"


class Toolbox:
    """Registers tools on a strict server. Every tool is one HTTP call with the caller's own bearer."""

    def __init__(
        self,
        name: str,
        instructions: str,
        client: httpx.Client,
        token: Callable[[], str],
        *,
        read_only: bool = False,
        empire_headers: bool = False,
        server: StrictMCPServer | None = None,
    ) -> None:
        self.server = server or StrictMCPServer(name, instructions=instructions)
        self.client, self.token, self.read_only, self.empire_headers = (
            client,
            token,
            read_only,
            empire_headers,
        )

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        idempotency_key: str | None = None,
    ) -> Any:
        try:
            bearer = clean_token(self.token())
        except ConfigError:
            raise ToolFailed(
                "401: unauthorized"
            ) from None  # no token, or one with an unexpected format
        headers = {"Authorization": f"Bearer {bearer}"}
        if self.empire_headers:
            headers |= {
                "X-Empire-Channel": "mcp",
                "X-Correlation-Id": f"mcp-{uuid.uuid4().hex}",
            }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self.client.request(
                method, path, headers=headers, params=params, json=body
            )
        except httpx.HTTPError:
            raise ToolFailed(
                "503: unavailable"
            ) from None  # never echo the transport error
        if response.status_code >= 400:
            raise ToolFailed(failure(response.status_code))
        return response.json() if response.content else {"ok": True}

    def tool(
        self, name: str, model: type[Strict], description: str, *, write: bool = False
    ) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        def register(handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
            banned = FORBIDDEN_ARGUMENTS & set(model.model_fields)
            if banned:
                raise ValueError(
                    f"{name}: identity arguments are not allowed: {sorted(banned)}"
                )
            if write and self.read_only:
                return handler

            def function(**kwargs: Any) -> Any:
                return handler(model.model_validate(kwargs))

            params = [
                inspect.Parameter(
                    field,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=inspect.Parameter.empty
                    if info.is_required()
                    else info.default,
                    annotation=info.annotation,
                )
                for field, info in model.model_fields.items()
            ]
            function.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]  # functions take a __signature__ at runtime; the server's inspect.signature() reads it
            function.__annotations__ = {p.name: p.annotation for p in params}
            function.__name__ = name.replace(".", "_")
            self.server.argument_models[name] = model
            self.server.tool(name=name, description=description)(function)
            return handler

        return register


def query(
    args: BaseModel, rename: dict[str, str] | None = None, drop: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The set arguments as query parameters (``rename`` maps argument names to the API's names)."""
    values = args.model_dump(mode="json", exclude_none=True, exclude=set(drop))
    return {(rename or {}).get(key, key): value for key, value in values.items()}


def new_key() -> str:
    return f"mcp-{uuid.uuid4().hex}"


class Id(Strict):
    id: int = Field(gt=0)


API_KEY = "EMPIRE_DECISIONS_API"
DecisionStatus = Literal[
    "draft",
    "proposed",
    "approved",
    "in_progress",
    "completed",
    "deferred",
    "rejected",
    "superseded",
]
ApprovalStatus = Literal["pending", "approved", "rejected", "not_required"]
ActionStatus = Literal[
    "none", "pending_approval", "queued", "executed", "blocked", "failed"
]
EventType = Literal[
    "created",
    "refreshed",
    "approved",
    "rejected",
    "deferred",
    "completed",
    "signal_cleared",
    "review_task_created",
    "reviewed",
    "updated",
]
ReviewStatus = Literal["pending", "completed", "skipped"]


class DecisionList(Strict):
    status: list[DecisionStatus] | None = None
    domain: str | None = Field(default=None, max_length=80)
    approval_status: ApprovalStatus | None = None
    approval_required: bool | None = None
    action_status: ActionStatus | None = None
    source_type: str | None = Field(default=None, max_length=80)
    source_ref: str | None = Field(default=None, max_length=300)
    review_due_on_or_before: date | None = None
    updated_since: datetime | None = None
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class DecisionStats(Strict):
    group_by: Literal["status", "domain", "approval_status", "action_status"] = "status"


class DecisionEvents(Strict):
    id: int = Field(gt=0)
    limit: int = Field(default=100, ge=1, le=500)
    order: Literal["desc", "asc"] = "desc"
    after_id: int | None = Field(default=None, ge=0)


class AllEvents(Strict):
    event_type: EventType | None = None
    since: datetime | None = None
    limit: int = Field(default=100, ge=1, le=500)


class ReviewList(Strict):
    status: ReviewStatus | None = None
    decision_id: int | None = Field(default=None, gt=0)
    due_on_or_before: date | None = None
    limit: int = Field(default=100, ge=1, le=500)


class DecisionCreate(Strict):
    """A new decision. Approval and action fields are not accepted: it starts proposed and pending approval."""

    title: str = Field(min_length=1, max_length=500)
    decision: str = Field(min_length=1, max_length=4000)
    domain: str | None = Field(default=None, min_length=1, max_length=80)
    rationale: str | None = Field(default=None, max_length=8000)
    evidence_refs: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    expected_outcome: str | None = Field(default=None, max_length=4000)
    next_action: str | None = Field(default=None, max_length=2000)
    review_date: date | None = None
    idempotency_key: str | None = Field(default=None, pattern=IDEMPOTENCY_KEY)


class DecisionNote(Strict):
    """A history note. State changes (approve, reject, ...) are not notes and are not exposed here."""

    id: int = Field(gt=0)
    event_type: Literal["signal_cleared", "review_task_created", "reviewed"]
    reason: str | None = Field(default=None, max_length=2000)


class ReviewUpdate(Strict):
    id: int = Field(gt=0)
    expected_version: int | None = Field(default=None, ge=1)
    status: ReviewStatus | None = None
    outcome: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=8000)


def build_server(
    client: httpx.Client,
    token: Callable[[], str],
    *,
    read_only: bool = False,
    server: StrictMCPServer | None = None,
) -> Toolbox:
    box = Toolbox(
        "decisions",
        "Solo Empire decisions cell. Every tool calls the cell API as the authenticated "
        "caller only.",
        client,
        token,
        read_only=read_only,
        empire_headers=True,
        server=server,
    )

    @box.tool(
        "decisions.decision.list",
        DecisionList,
        "List decisions (newest first), filtered by status, domain, approval or review date.",
    )
    def decision_list(a: DecisionList) -> Any:
        return box.call("GET", "/v1/decisions", params=query(a))

    @box.tool("decisions.decision.get", Id, "Get one decision by id.")
    def decision_get(a: Id) -> Any:
        return box.call("GET", f"/v1/decisions/{a.id}")

    @box.tool(
        "decisions.decision.stats",
        DecisionStats,
        "Count decisions grouped by status, domain, approval or action status.",
    )
    def decision_stats(a: DecisionStats) -> Any:
        return box.call("GET", "/v1/decisions/stats", params=query(a))

    @box.tool(
        "decisions.decision.events",
        DecisionEvents,
        "The history of one decision (newest first; asc with after_id pages through it).",
    )
    def decision_events(a: DecisionEvents) -> Any:
        return box.call(
            "GET", f"/v1/decisions/{a.id}/events", params=query(a, drop=("id",))
        )

    @box.tool(
        "decisions.event.list",
        AllEvents,
        "Recent decision events across all decisions.",
    )
    def event_list(a: AllEvents) -> Any:
        return box.call("GET", "/v1/decision-events", params=query(a))

    @box.tool(
        "decisions.review.list",
        ReviewList,
        "List decision reviews, by status, decision or due date.",
    )
    def review_list(a: ReviewList) -> Any:
        return box.call("GET", "/v1/decision-reviews", params=query(a))

    @box.tool("decisions.review.get", Id, "Get one decision review by id.")
    def review_get(a: Id) -> Any:
        return box.call("GET", f"/v1/decision-reviews/{a.id}")

    @box.tool(
        "decisions.decision.create",
        DecisionCreate,
        "Propose a decision. It starts proposed and pending approval; this tool cannot approve or act on it.",
        write=True,
    )
    def decision_create(a: DecisionCreate) -> Any:
        return box.call(
            "POST",
            "/v1/decisions",
            body=a.model_dump(
                mode="json", exclude_none=True, exclude={"idempotency_key"}
            ),
            idempotency_key=a.idempotency_key or new_key(),
        )

    @box.tool(
        "decisions.decision.note",
        DecisionNote,
        "Add a history note (signal_cleared, review_task_created or reviewed) to a decision.",
        write=True,
    )
    def decision_note(a: DecisionNote) -> Any:
        return box.call(
            "POST",
            f"/v1/decisions/{a.id}/events",
            body=a.model_dump(mode="json", exclude_none=True, exclude={"id"}),
        )

    @box.tool(
        "decisions.review.update",
        ReviewUpdate,
        "Change a review's status, outcome or notes; pass expected_version to refuse a stale write.",
        write=True,
    )
    def review_update(a: ReviewUpdate) -> Any:
        return box.call(
            "PATCH",
            f"/v1/decision-reviews/{a.id}",
            body=a.model_dump(mode="json", exclude_none=True, exclude={"id"}),
        )

    return box


def bearer(environ: Any = None) -> str:
    """The caller's own token: EMPIRE_TOKEN, EMPIRE_TOKEN_FILE, else the named automation token."""
    environ = os.environ if environ is None else environ
    if environ.get("EMPIRE_TOKEN"):
        return clean_token(environ["EMPIRE_TOKEN"])
    if environ.get("EMPIRE_TOKEN_FILE"):
        return clean_token(
            read_owner_file(Path(environ["EMPIRE_TOKEN_FILE"]).expanduser(), "token")
        )
    name = environ.get("EMPIRE_MCP_AUTOMATION") or "owner-mcp"
    if not AUTOMATION.fullmatch(name):
        raise ConfigError("EMPIRE_MCP_AUTOMATION must match ^[a-z0-9][a-z0-9-]{0,62}$")
    key = "EMPIRE_AUTOMATION_TOKEN_" + name.upper().replace("-", "_")
    if environ.get(key):
        return clean_token(environ[key])
    path = Path.home() / ".config" / "empire-platform" / "tokens" / f"{name}.jwt"
    if path.is_file():
        return clean_token(read_owner_file(path, "token"))
    raise ConfigError(f"no token: set EMPIRE_TOKEN, EMPIRE_TOKEN_FILE or {key}")


def main() -> None:
    try:
        url, _ = api_url(API_KEY), bearer()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    client = httpx.Client(base_url=url, timeout=30, follow_redirects=False)
    build_server(
        client, bearer, read_only=os.environ.get("EMPIRE_MCP_READONLY") == "1"
    ).server.run("stdio")


if __name__ == "__main__":
    main()
