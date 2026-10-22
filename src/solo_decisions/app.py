"""Decisions API: decision records, their event history, and scheduled reviews.

The cell keeps its own database. Authorization, execution records, and audit
live in empire-platform and are reached over HTTP with the caller's token:

- reads ask ``/v1/authz/decide`` (``decisions.decision:read``);
- writes start an execution (``/v1/executions``), change the row, its event
  history, and the outbox in one local transaction, then finish the execution
  with row hashes for the audit trail.

Permissions: ``decisions.decision:read|write|approve|delete`` and
``decisions.review:write``. Changing ``approval_status`` needs ``approve``.
"""

import contextvars
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal, cast

import psycopg
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Response,
)
from fastapi import (
    Request as HttpRequest,
)
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from solo_decisions._sentry import capture
from solo_decisions.auth import Principal, TokenVerifier, Unauthenticated
from solo_decisions.platform import Action, Denied, Platform, PlatformUnavailable
from solo_decisions.publisher import publish_pending

log = logging.getLogger("solo_decisions")
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)
CORRELATION = re.compile(r"^[A-Za-z0-9-]{8,64}$")
Status = Literal[
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
ActionType = Literal[
    "none",
    "task",
    "calendar",
    "notification",
    "message",
    "financial",
    "external_change",
]
ActionStatus = Literal[
    "none", "pending_approval", "queued", "executed", "blocked", "failed"
]
Privacy = Literal["private", "sensitive", "internal"]
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
CLASSIFICATION = {
    "internal": "internal",
    "private": "confidential",
    "sensitive": "restricted",
}
FIELDS = (
    "title",
    "domain",
    "decision",
    "rationale",
    "evidence_refs",
    "confidence",
    "expected_outcome",
    "actual_outcome",
    "next_action",
    "status",
    "approval_required",
    "approval_status",
    "action_type",
    "action_status",
    "privacy_class",
    "source_type",
    "source_ref",
    "review_date",
    "decided_at",
    "dedupe_key",
    "execution_lock_key",
)
COLUMNS = "id, legacy_id, " + ", ".join(FIELDS) + ", version, created_at, updated_at"
REQUIRED_FIELDS = {
    "title",
    "domain",
    "decision",
    "evidence_refs",
    "status",
    "approval_required",
    "approval_status",
    "action_type",
    "action_status",
    "privacy_class",
}
# An approve-only caller may change these; anything else needs :write as well.
APPROVAL_FIELDS = {"approval_status", "status", "action_status", "decided_at"}
# What an approval vouches for: changing it sends an approved decision back to pending.
APPROVAL_COVERED = {
    "title",
    "domain",
    "decision",
    "rationale",
    "evidence_refs",
    "confidence",
    "expected_outcome",
    "next_action",
    "action_type",
    "privacy_class",
    "source_ref",
}
STATE_EVENTS = {
    "created",
    "refreshed",
    "approved",
    "rejected",
    "deferred",
    "completed",
    "updated",
}
REVIEW_COLUMNS = (
    "id, decision_id, task_id, due_date, status, reviewer, outcome, notes, reviewed_at, version,"
    " created_at, updated_at"
)
EVENT_COLUMNS = (
    "id, decision_id, event_type, actor, from_status, to_status, from_approval_status,"
    " to_approval_status, from_action_status, to_action_status, reason, metadata, created_at"
)


class EventIn(BaseModel):
    event_type: EventType
    actor: str = Field(default="system", min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionFields(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    domain: str | None = Field(default=None, min_length=1, max_length=80)
    decision: str | None = Field(default=None, min_length=1, max_length=4000)
    rationale: str | None = Field(default=None, max_length=8000)
    evidence_refs: list[Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    expected_outcome: str | None = Field(default=None, max_length=4000)
    actual_outcome: str | None = Field(default=None, max_length=4000)
    next_action: str | None = Field(default=None, max_length=2000)
    status: Status | None = None
    approval_required: bool | None = None
    approval_status: ApprovalStatus | None = None
    action_type: ActionType | None = None
    action_status: ActionStatus | None = None
    privacy_class: Privacy | None = None
    source_type: str | None = Field(default=None, max_length=80)
    source_ref: str | None = Field(default=None, max_length=300)
    review_date: date | None = None
    decided_at: datetime | None = None
    dedupe_key: str | None = Field(default=None, max_length=300)
    execution_lock_key: str | None = Field(default=None, max_length=300)


class DecisionCreate(DecisionFields):
    title: str = Field(min_length=1, max_length=500)
    decision: str = Field(min_length=1, max_length=4000)
    legacy_id: int | None = Field(default=None, gt=0)
    event: EventIn | None = None


class DecisionUpdate(DecisionFields):
    expected_version: int | None = Field(default=None, ge=1)
    event: EventIn | None = None


class Decision(BaseModel):
    id: int
    legacy_id: int | None
    title: str
    domain: str
    decision: str
    rationale: str | None
    evidence_refs: list[Any]
    confidence: float | None
    expected_outcome: str | None
    actual_outcome: str | None
    next_action: str | None
    status: str
    approval_required: bool
    approval_status: str
    action_type: str
    action_status: str
    privacy_class: str
    source_type: str | None
    source_ref: str | None
    review_date: date | None
    decided_at: datetime | None
    dedupe_key: str | None
    execution_lock_key: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class DecisionList(BaseModel):
    items: list[Decision]


class UpsertResult(BaseModel):
    outcome: Literal["created", "refreshed", "unchanged"]
    decision: Decision


class DecisionEvent(BaseModel):
    id: int
    decision_id: int
    event_type: str
    actor: str
    from_status: str | None
    to_status: str | None
    from_approval_status: str | None
    to_approval_status: str | None
    from_action_status: str | None
    to_action_status: str | None
    reason: str | None
    metadata: dict[str, Any]
    created_at: datetime


class EventList(BaseModel):
    items: list[DecisionEvent]


class ReviewPut(BaseModel):
    task_id: int | None = Field(default=None, gt=0)
    status: ReviewStatus | None = None
    reviewer: str | None = Field(default=None, max_length=120)
    outcome: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=8000)
    reviewed_at: datetime | None = None


class ReviewUpdate(ReviewPut):
    expected_version: int | None = Field(default=None, ge=1)


class Review(BaseModel):
    id: int
    decision_id: int
    task_id: int | None
    due_date: date
    status: str
    reviewer: str | None
    outcome: str | None
    notes: str | None
    reviewed_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class ReviewList(BaseModel):
    items: list[Review]


class ReviewResult(BaseModel):
    outcome: Literal["created", "updated", "unchanged"]
    review: Review


class Stats(BaseModel):
    groups: dict[str, int]


def row_hash(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, default=str).encode()
    ).hexdigest()


def subject(decision_id: int) -> str:
    return f"empire:decisions:decision:{decision_id}"


@dataclass
class Work:
    """One write's open local transaction; ``marker`` tags its rows until the execution exists."""

    conn: psycopg.Connection[Any]
    marker: uuid.UUID
    caller: Principal


@dataclass
class Plan:
    """What a write did: the permission and exact resource to authorize, and the audit record."""

    permission: str
    resource_id: str
    extra: list[str]
    audit: dict[str, Any]
    result: Any


def create_app(
    connect: Callable[[], psycopg.Connection[Any]],
    verifier: TokenVerifier,
    platform: Platform,
    publisher_token: Callable[[], str | None] = lambda: None,
) -> FastAPI:
    app = FastAPI(title="Decisions API", version="1.0.0", description=__doc__)

    @app.middleware("http")
    async def correlate(
        request: HttpRequest,
        call_next: Callable[[HttpRequest], Awaitable[Response]],
    ) -> Response:
        given = request.headers.get("x-correlation-id", "")
        token = correlation_id.set(
            given if CORRELATION.match(given) else uuid.uuid4().hex
        )
        try:
            response = await call_next(request)
        finally:
            value = correlation_id.get()
            correlation_id.reset(token)
        response.headers["X-Correlation-Id"] = value
        return response

    def principal(
        authorization: Annotated[str | None, Header()] = None,
        x_empire_channel: Annotated[str | None, Header()] = None,
    ) -> Principal:
        try:
            return verifier.verify(authorization, x_empire_channel)
        except Unauthenticated as exc:
            raise HTTPException(
                status_code=401, detail={"error": "unauthenticated", "reason": str(exc)}
            ) from exc

    Caller = Annotated[Principal, Depends(principal)]

    @contextmanager
    def bound(caller: Principal) -> Iterator[psycopg.Connection[Any]]:
        try:
            conn = connect()
        except psycopg.OperationalError as exc:
            raise HTTPException(
                status_code=503, detail={"error": "unavailable"}
            ) from exc
        conn.row_factory = dict_row
        try:
            conn.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (str(caller.workspace),),
            )
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def allow(
        caller: Principal,
        action: str,
        resource_id: str = "*",
        *,
        not_found: bool = False,
    ) -> None:
        try:
            allowed = platform.decide(
                caller.token, caller.channel, Action(action, "decision", resource_id)
            )
        except Denied as exc:
            raise HTTPException(
                status_code=401,
                detail={"error": "unauthenticated", "reason": exc.reason},
            ) from exc
        except PlatformUnavailable as exc:
            raise HTTPException(
                status_code=503, detail={"error": "authorization_unavailable"}
            ) from exc
        if not allowed:
            if not_found:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            raise HTTPException(status_code=403, detail={"error": "deny"})

    def write(
        caller: Principal,
        op: Callable[["Work"], "Plan"],
        *,
        precheck: tuple[str, str],
        idempotency_key: str | None = None,
    ) -> Any:
        """One authorized write across the platform and this cell's database.

        1. A recorded precheck (``precheck`` = permission, resource) so a caller without
           the right learns nothing from the change itself.
        2. ``op`` changes rows in an open local transaction and returns the Plan: the
           permission the change needs, the exact resource it touched, any extra
           permissions, and the audit record.
        3. The platform starts an execution bound to exactly that operation, enforcing
           every permission it needs with locks together. Denied: rolled back.
           A replay of the same operation (same idempotency key) continues a "running"
           execution (its answer was lost) and returns the existing result for a
           finished one.
        4. Local commit, then finish with the audit record (row hashes only).
        """
        allow(caller, *precheck)
        with bound(caller) as conn:
            work = Work(conn, uuid.uuid4(), caller)
            try:
                plan = op(work)
                try:
                    execution, replayed, status = platform.begin(
                        caller.token,
                        caller.channel,
                        Action(plan.permission, "decision", plan.resource_id),
                        idempotency_key,
                        correlation_id.get(),
                        also=plan.extra,
                    )
                except Denied as exc:
                    code = exc.status if exc.status in (401, 403, 409) else 403
                    raise HTTPException(
                        status_code=code,
                        detail={"error": exc.decision, "reason": exc.reason},
                    ) from exc
                except PlatformUnavailable as exc:
                    raise HTTPException(
                        status_code=503, detail={"error": "authorization_unavailable"}
                    ) from exc
                if replayed and status == "succeeded":
                    conn.rollback()
                    return plan.result
                if replayed and status != "running":
                    raise HTTPException(
                        status_code=409, detail={"error": "replayed", "status": status}
                    )
                # Rows written before the execution existed carry a local marker; bind them now.
                for table in ("events", "outbox"):
                    # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                    conn.execute(
                        f"UPDATE decisions.{table} SET execution_id = %s WHERE execution_id = %s",
                        (execution, work.marker),
                    )
                conn.commit()
            except psycopg.errors.UniqueViolation as exc:
                raise HTTPException(
                    status_code=409, detail={"error": "conflict"}
                ) from exc
            except psycopg.errors.ForeignKeyViolation as exc:
                raise HTTPException(
                    status_code=422, detail={"error": "reference_not_found"}
                ) from exc
            except (
                psycopg.errors.NotNullViolation,
                psycopg.errors.CheckViolation,
            ) as exc:
                raise HTTPException(
                    status_code=422, detail={"error": "invalid_value"}
                ) from exc
        _finish(
            caller,
            execution,
            {"status": "succeeded", "service": "decisions", **plan.audit},
        )
        log.info(
            json.dumps(
                {
                    "event": "decisions.write",
                    "action": plan.permission,
                    "execution": str(execution),
                    "actor": str(caller.actor),
                    "workspace": str(caller.workspace),
                }
            )
        )
        return plan.result

    def _finish(
        caller: Principal, execution: uuid.UUID, outcome: dict[str, Any]
    ) -> None:
        try:
            platform.finish(caller.token, caller.channel, execution, outcome)
        except (PlatformUnavailable, Denied) as exc:
            capture(exc)  # the write succeeded; only the audit finish failed
            # The local change is committed; the execution stays "running" until reconciled.
            log.warning(
                json.dumps(
                    {"event": "decisions.finish_failed", "execution": str(execution)}
                )
            )

    def load(
        conn: psycopg.Connection[Any], decision_id: int, *, lock: bool = False
    ) -> dict[str, Any]:
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
        row: dict[str, Any] | None = conn.execute(
            f"SELECT {COLUMNS} FROM decisions.records WHERE id = %s{' FOR UPDATE' if lock else ''}",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return row

    def record_event(
        work: Work,
        decision_id: int,
        event: EventIn,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        def pair(name: str) -> tuple[Any, Any]:
            return (before or {}).get(name), (after or {}).get(name)

        (fs, ts), (fa, ta), (fx, tx) = (
            pair("status"),
            pair("approval_status"),
            pair("action_status"),
        )
        # The label is the caller's; who actually acted is recorded from the token.
        metadata = {**event.metadata, "authenticated_actor": str(work.caller.actor)}
        work.conn.execute(
            "INSERT INTO decisions.events (workspace_id, decision_id, event_type, actor, from_status, to_status,"
            " from_approval_status, to_approval_status, from_action_status, to_action_status, reason, metadata,"
            " execution_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                work.caller.workspace,
                decision_id,
                event.event_type,
                event.actor,
                fs,
                ts,
                fa,
                ta,
                fx,
                tx,
                event.reason,
                json.dumps(metadata, default=str),
                work.marker,
            ),
        )

    def emit(
        work: Work,
        verb: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> dict[str, Any]:
        current = after or before or {}
        decision_id = current["id"]
        classification = CLASSIFICATION.get(
            current.get("privacy_class", "private"), "confidential"
        )
        payload: dict[str, Any] = {
            "version": current.get("version", 0) + (1 if after is None else 0),
            "deleted": after is None,
        }
        if after is not None:
            payload.update(status=after["status"], domain=after["domain"])
            if classification != "restricted":
                payload["title"] = after["title"]
        work.conn.execute(
            "INSERT INTO decisions.outbox (workspace_id, type, subject, execution_id, classification, payload,"
            " correlation_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                work.caller.workspace,
                f"decisions.decision.{verb}",
                subject(decision_id),
                work.marker,
                classification,
                json.dumps(payload, default=str),
                correlation_id.get(),
            ),
        )
        return audit(f"decisions.decision.{verb}", decision_id, before, after)

    def audit(
        action: str,
        decision_id: int,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "action": action,
            "resource_type": "decision",
            "resource_id": str(decision_id),
            "before_hash": row_hash(before),
            "after_hash": row_hash(after),
        }

    def publish_soon(background: BackgroundTasks, caller: Principal) -> None:
        token = publisher_token()
        if not token:
            return

        def run() -> None:
            try:
                with bound(caller) as conn:
                    publish_pending(conn, platform, token, limit=20)
            except Exception as exc:  # noqa: BLE001 - the cron sweep retries; never fail the request for this
                log.warning(
                    json.dumps(
                        {
                            "event": "decisions.inline_publish_failed",
                            "error": type(exc).__name__,
                        }
                    )
                )

        background.add_task(run)

    def values(body: DecisionFields, names: set[str]) -> dict[str, Any]:
        data = {name: getattr(body, name) for name in FIELDS if name in names}
        nulls = sorted(
            name for name in REQUIRED_FIELDS if name in data and data[name] is None
        )
        if nulls:
            raise HTTPException(
                status_code=422, detail={"error": "invalid_value", "fields": nulls}
            )
        if "evidence_refs" in data:
            data["evidence_refs"] = json.dumps(data["evidence_refs"])
        return data

    def changed(before: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for name, value in data.items():
            current = before.get(name)
            if name == "evidence_refs":
                current = json.dumps(current)
            if isinstance(value, datetime) and isinstance(current, datetime):
                equal = value == current
            else:
                equal = (
                    str(current) == str(value)
                    if current is not None and value is not None
                    else current == value
                )
            if not equal:
                out[name] = value
        return out

    def permissions_for(
        before: dict[str, Any] | None, data: dict[str, Any], event: EventIn | None
    ) -> tuple[str, list[str], dict[str, Any]]:
        """(primary permission, extra permissions, data with approval reset) for one change.

        Approving or rejecting (and dropping the approval requirement) needs :approve;
        anything else needs :write, and both when a change does both. Content that an
        approval covered sends an approved decision back to pending approval.
        """
        after = {**(before or {}), **data}
        if (
            after.get("approval_required", True)
            and after.get("approval_status") == "not_required"
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_value",
                    "reason": "approval_required_but_not_required",
                },
            )
        sensitive = data.get("approval_status") in ("approved", "rejected") or (
            before is not None
            and before["approval_required"]
            and data.get("approval_required") is False
        )
        if (
            event is not None
            and event.event_type in ("approved", "rejected")
            and data.get("approval_status") != (event.event_type)
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_value",
                    "reason": "approval_event_without_approval",
                },
            )
        content = set(data) - APPROVAL_FIELDS
        if (
            not sensitive
            and before is not None
            and before["approval_required"]
            and before["approval_status"] == "approved"
            and content & APPROVAL_COVERED
        ):
            data = {
                **data,
                "approval_status": "pending",
                "status": "proposed"
                if after["status"] == "approved"
                else after["status"],
                "action_status": "pending_approval"
                if after["action_type"] != "none"
                else "none",
            }
        if not sensitive:
            return "decisions.decision:write", [], data
        return (
            "decisions.decision:approve",
            (["decisions.decision:write"] if content else []),
            data,
        )

    def reserve(caller: Principal, key: str) -> int:
        """The record id this caller's idempotency key stands for (reserved once, committed at once)."""
        if not 8 <= len(key) <= 200:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_value", "fields": ["Idempotency-Key"]},
            )
        with bound(caller) as conn:
            conn.execute(
                "INSERT INTO decisions.create_keys (workspace_id, actor, key, decision_id)"
                " VALUES (%s, %s, %s, nextval(pg_get_serial_sequence('decisions.records', 'id')))"
                " ON CONFLICT DO NOTHING",
                (caller.workspace, caller.actor, key),
            )
            # Inserted above or already there: the key's row always exists here.
            row = cast(
                "dict[str, Any]",
                conn.execute(
                    "SELECT decision_id FROM decisions.create_keys WHERE actor = %s AND key = %s",
                    (caller.actor, key),
                ).fetchone(),
            )
            conn.commit()
        return int(row["decision_id"])

    def insert(
        work: Work,
        body: DecisionCreate,
        data: dict[str, Any],
        record_id: int | None = None,
    ) -> dict[str, Any]:
        data = {
            k: v
            for k, v in data.items()
            if v is not None
            or k
            not in {
                "domain",
                "status",
                "approval_required",
                "approval_status",
                "action_type",
                "action_status",
                "privacy_class",
                "evidence_refs",
            }
        }
        columns = ["workspace_id", *data]
        params = [work.caller.workspace, *data.values()]
        if body.legacy_id is not None:
            columns.append("legacy_id")
            params.append(body.legacy_id)
        overriding = ""
        if record_id is not None:  # an id reserved for an idempotency key
            columns.append("id")
            params.append(record_id)
            overriding = " OVERRIDING SYSTEM VALUE"
        # INSERT ... RETURNING always yields the new row.
        row = cast(
            "dict[str, Any]",
            work.conn.execute(
                f"INSERT INTO decisions.records ({', '.join(columns)}){overriding}"
                f" VALUES ({', '.join(['%s'] * len(columns))}) RETURNING {COLUMNS}",
                params,
            ).fetchone(),
        )
        event = (
            body.event.model_copy(update={"event_type": "created"})
            if body.event
            else EventIn(event_type="created", actor="api")
        )
        record_event(work, row["id"], event, None, row)
        return row

    def apply(
        work: Work, before: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        if not data:
            return before
        assignments = ", ".join(f"{name} = %s" for name in data)
        # The row is locked by the caller, so UPDATE ... RETURNING always yields it.
        return cast(
            "dict[str, Any]",
            work.conn.execute(
                f"UPDATE decisions.records SET {assignments}, version = version + 1, updated_at = now()"
                f" WHERE id = %s RETURNING {COLUMNS}",
                [*data.values(), before["id"]],
            ).fetchone(),
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/decisions", response_model=DecisionList)
    def list_decisions(
        caller: Caller,
        status: Annotated[list[Status] | None, Query()] = None,
        domain: str | None = None,
        approval_status: ApprovalStatus | None = None,
        approval_required: bool | None = None,
        action_status: ActionStatus | None = None,
        source_type: str | None = None,
        source_ref: str | None = None,
        review_due_on_or_before: date | None = None,
        updated_since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> DecisionList:
        allow(caller, "decisions.decision:read")
        where: list[str] = ["TRUE"]
        params: list[Any] = []
        for column, value in (
            ("domain", domain),
            ("approval_status", approval_status),
            ("approval_required", approval_required),
            ("action_status", action_status),
            ("source_type", source_type),
            ("source_ref", source_ref),
        ):
            if value is not None:
                where.append(f"{column} = %s")
                params.append(value)
        if status:
            where.append("status = ANY(%s)")
            params.append(list(status))
        if review_due_on_or_before is not None:
            where.append("review_date <= %s")
            params.append(review_due_on_or_before)
        if updated_since is not None:
            where.append("updated_at >= %s")
            params.append(updated_since)
        with bound(caller) as conn:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM decisions.records WHERE {' AND '.join(where)}"
                " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                [*params, limit, offset],
            ).fetchall()
        return DecisionList(items=[Decision(**row) for row in rows])

    @app.get("/v1/decisions/stats", response_model=Stats)
    def stats(
        caller: Caller,
        group_by: Literal[
            "status", "domain", "approval_status", "action_status"
        ] = "status",
    ) -> Stats:
        allow(caller, "decisions.decision:read")
        with bound(caller) as conn:
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- group_by is checked against a fixed allow-list before use; values are %s parameters
            rows = conn.execute(
                f"SELECT {group_by} AS key, count(*) AS n FROM decisions.records GROUP BY 1"
            ).fetchall()
        return Stats(groups={row["key"]: row["n"] for row in rows})

    @app.get("/v1/decisions/dedupe/{key}", response_model=Decision)
    def by_dedupe(key: str, caller: Caller) -> Decision:
        allow(caller, "decisions.decision:read", not_found=True)
        with bound(caller) as conn:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
            row = conn.execute(
                f"SELECT {COLUMNS} FROM decisions.records WHERE dedupe_key = %s", (key,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return Decision(**row)

    @app.get("/v1/decisions/legacy/{legacy_id}", response_model=Decision)
    def by_legacy(legacy_id: int, caller: Caller) -> Decision:
        allow(caller, "decisions.decision:read", not_found=True)
        with bound(caller) as conn:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
            row = conn.execute(
                f"SELECT {COLUMNS} FROM decisions.records WHERE legacy_id = %s",
                (legacy_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return Decision(**row)

    @app.get("/v1/decisions/{decision_id}", response_model=Decision)
    def get_decision(decision_id: int, caller: Caller) -> Decision:
        allow(caller, "decisions.decision:read", str(decision_id), not_found=True)
        with bound(caller) as conn:
            return Decision(**load(conn, decision_id))

    @app.post("/v1/decisions", status_code=201, response_model=Decision)
    def create(
        body: DecisionCreate,
        caller: Caller,
        background: BackgroundTasks,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> Decision:
        data = values(body, body.model_fields_set | {"title", "decision"})
        permission, extra, data = permissions_for(None, data, body.event)
        reserved = reserve(caller, idempotency_key) if idempotency_key else None

        def op(work: Work) -> Plan:
            if reserved is not None:
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
                existing = work.conn.execute(
                    f"SELECT {COLUMNS} FROM decisions.records WHERE id = %s",
                    (reserved,),
                ).fetchone()
                if (
                    existing is not None
                ):  # this create already happened; replay answers with it
                    return Plan(
                        permission,
                        str(reserved),
                        extra,
                        audit("decisions.decision.created", reserved, None, existing),
                        existing,
                    )
            row = insert(work, body, data, record_id=reserved)
            return Plan(
                permission, str(row["id"]), extra, emit(work, "created", None, row), row
            )

        row = write(
            caller, op, precheck=(permission, "new"), idempotency_key=idempotency_key
        )
        publish_soon(background, caller)
        return Decision(**row)

    @app.put("/v1/decisions/dedupe/{key}", response_model=UpsertResult)
    def upsert(
        key: str, body: DecisionCreate, caller: Caller, background: BackgroundTasks
    ) -> UpsertResult:
        body.dedupe_key = key
        names = body.model_fields_set | {"title", "decision", "dedupe_key"}

        def op(work: Work) -> Plan:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
            before = work.conn.execute(
                f"SELECT {COLUMNS} FROM decisions.records WHERE dedupe_key = %s FOR UPDATE",
                (key,),
            ).fetchone()
            if before is None:
                permission, extra, data = permissions_for(
                    None, values(body, names), body.event
                )
                row = insert(work, body, data)
                return Plan(
                    permission,
                    str(row["id"]),
                    extra,
                    emit(work, "created", None, row),
                    ("created", row),
                )
            data = changed(before, values(body, names - {"dedupe_key"}))
            permission, extra, data = permissions_for(before, data, body.event)
            if not data:
                return Plan(
                    permission,
                    str(before["id"]),
                    extra,
                    audit("decisions.decision.unchanged", before["id"], before, before),
                    ("unchanged", before),
                )
            row = apply(work, before, data)
            record_event(
                work,
                row["id"],
                body.event or EventIn(event_type="refreshed", actor="api"),
                before,
                row,
            )
            return Plan(
                permission,
                str(row["id"]),
                extra,
                emit(work, "updated", before, row),
                ("refreshed", row),
            )

        outcome, row = write(
            caller, op, precheck=("decisions.decision:write", f"key:{key}")
        )
        publish_soon(background, caller)
        return UpsertResult(outcome=outcome, decision=Decision(**row))

    @app.patch("/v1/decisions/{decision_id}", response_model=Decision)
    def update(
        decision_id: int,
        body: DecisionUpdate,
        caller: Caller,
        background: BackgroundTasks,
    ) -> Decision:
        names = body.model_fields_set - {"expected_version", "event"}
        requested = values(body, names)
        first = (
            "decisions.decision:approve"
            if requested.get("approval_status") in ("approved", "rejected")
            else "decisions.decision:write"
        )

        def op(work: Work) -> Plan:
            before = load(work.conn, decision_id, lock=True)
            if (
                body.expected_version is not None
                and before["version"] != body.expected_version
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "version_conflict", "version": before["version"]},
                )
            permission, extra, data = permissions_for(
                before, changed(before, requested), body.event
            )
            row = apply(work, before, data)
            if data or body.event is not None:
                record_event(
                    work,
                    decision_id,
                    body.event or EventIn(event_type="updated", actor="api"),
                    before,
                    row,
                )
            action = (
                emit(work, "updated", before, row)
                if data
                else audit("decisions.decision.unchanged", decision_id, before, before)
            )
            return Plan(permission, str(decision_id), extra, action, row)

        row = write(caller, op, precheck=(first, str(decision_id)))
        publish_soon(background, caller)
        return Decision(**row)

    @app.delete("/v1/decisions/{decision_id}", status_code=204)
    def delete(decision_id: int, caller: Caller, background: BackgroundTasks) -> None:
        def op(work: Work) -> Plan:
            before = load(work.conn, decision_id, lock=True)
            work.conn.execute(
                "DELETE FROM decisions.records WHERE id = %s", (decision_id,)
            )
            return Plan(
                "decisions.decision:delete",
                str(decision_id),
                [],
                emit(work, "deleted", before, None),
                None,
            )

        write(caller, op, precheck=("decisions.decision:delete", str(decision_id)))
        publish_soon(background, caller)

    @app.post(
        "/v1/decisions/{decision_id}/events",
        status_code=201,
        response_model=DecisionEvent,
    )
    def add_event(decision_id: int, body: EventIn, caller: Caller) -> DecisionEvent:
        if (
            body.event_type in STATE_EVENTS
        ):  # state changes go through PATCH, with their permissions
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_value", "reason": "use_patch_for_state"},
            )

        def op(work: Work) -> Plan:
            row = load(work.conn, decision_id)
            record_event(work, decision_id, body, row, row)
            event = work.conn.execute(
                f"SELECT {EVENT_COLUMNS} FROM decisions.events WHERE execution_id = %s"
                " ORDER BY id DESC LIMIT 1",
                (work.marker,),
            ).fetchone()
            return Plan(
                "decisions.decision:write",
                str(decision_id),
                [],
                audit("decisions.decision.noted", decision_id, row, row),
                event,
            )

        return DecisionEvent(
            **write(caller, op, precheck=("decisions.decision:write", str(decision_id)))
        )

    @app.get("/v1/decisions/{decision_id}/events", response_model=EventList)
    def decision_events(
        decision_id: int,
        caller: Caller,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        order: Literal["desc", "asc"] = "desc",
        after_id: Annotated[int | None, Query(ge=0)] = None,
    ) -> EventList:
        """Newest first by default; ``order=asc`` with ``after_id`` pages through the whole history."""
        allow(caller, "decisions.decision:read", str(decision_id), not_found=True)
        with bound(caller) as conn:
            load(conn, decision_id)
            if order == "asc":
                rows = conn.execute(
                    f"SELECT {EVENT_COLUMNS} FROM decisions.events WHERE decision_id = %s"
                    " AND id > %s ORDER BY id LIMIT %s",
                    (decision_id, after_id or 0, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {EVENT_COLUMNS} FROM decisions.events WHERE decision_id = %s"
                    " ORDER BY created_at DESC, id DESC LIMIT %s",
                    (decision_id, limit),
                ).fetchall()
        return EventList(items=[DecisionEvent(**row) for row in rows])

    @app.get("/v1/decision-events", response_model=EventList)
    def all_events(
        caller: Caller,
        event_type: EventType | None = None,
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> EventList:
        allow(caller, "decisions.decision:read")
        where: list[str] = ["TRUE"]
        params: list[Any] = []
        if event_type:
            where.append("event_type = %s")
            params.append(event_type)
        if since:
            where.append("created_at >= %s")
            params.append(since)
        with bound(caller) as conn:
            rows = conn.execute(
                f"SELECT {EVENT_COLUMNS} FROM decisions.events WHERE {' AND '.join(where)}"
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                [*params, limit],
            ).fetchall()
        return EventList(items=[DecisionEvent(**row) for row in rows])

    @app.get("/v1/decision-reviews", response_model=ReviewList)
    def list_reviews(
        caller: Caller,
        status: ReviewStatus | None = None,
        decision_id: int | None = None,
        due_on_or_before: date | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> ReviewList:
        allow(caller, "decisions.decision:read")
        where: list[str] = ["TRUE"]
        params: list[Any] = []
        for column, value in (("status", status), ("decision_id", decision_id)):
            if value is not None:
                where.append(f"{column} = %s")
                params.append(value)
        if due_on_or_before is not None:
            where.append("due_date <= %s")
            params.append(due_on_or_before)
        with bound(caller) as conn:
            rows = conn.execute(
                f"SELECT {REVIEW_COLUMNS} FROM decisions.reviews WHERE {' AND '.join(where)}"
                " ORDER BY due_date, id LIMIT %s",
                [*params, limit],
            ).fetchall()
        return ReviewList(items=[Review(**row) for row in rows])

    @app.get("/v1/decision-reviews/{review_id}", response_model=Review)
    def get_review(review_id: int, caller: Caller) -> Review:
        allow(caller, "decisions.decision:read", f"review:{review_id}", not_found=True)
        with bound(caller) as conn:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
            row = conn.execute(
                f"SELECT {REVIEW_COLUMNS} FROM decisions.reviews WHERE id = %s",
                (review_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found"})
        return Review(**row)

    def review_values(body: ReviewPut, names: set[str]) -> dict[str, Any]:
        data: dict[str, Any] = {
            name: getattr(body, name)
            for name in (
                "task_id",
                "status",
                "reviewer",
                "outcome",
                "notes",
                "reviewed_at",
            )
            if name in names
        }
        if "status" in data and data["status"] is None:
            raise HTTPException(
                status_code=422, detail={"error": "invalid_value", "fields": ["status"]}
            )
        return data

    @app.put(
        "/v1/decisions/{decision_id}/reviews/{due_date}", response_model=ReviewResult
    )
    def put_review(
        decision_id: int, due_date: date, body: ReviewPut, caller: Caller
    ) -> ReviewResult:
        data = review_values(body, body.model_fields_set)

        def op(work: Work) -> Plan:
            load(work.conn, decision_id)
            before = work.conn.execute(
                f"SELECT {REVIEW_COLUMNS} FROM decisions.reviews WHERE decision_id = %s"
                " AND due_date = %s FOR UPDATE",
                (decision_id, due_date),
            ).fetchone()
            if before is None:
                columns = ["workspace_id", "decision_id", "due_date", *data]
                row = work.conn.execute(
                    f"INSERT INTO decisions.reviews ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
                    f" RETURNING {REVIEW_COLUMNS}",
                    [caller.workspace, decision_id, due_date, *data.values()],
                ).fetchone()
                outcome = "created"
            else:
                diff = {k: v for k, v in data.items() if before.get(k) != v}
                if diff:
                    assignments = ", ".join(f"{name} = %s" for name in diff)
                    row = work.conn.execute(
                        f"UPDATE decisions.reviews SET {assignments}, version = version + 1, updated_at = now()"
                        f" WHERE id = %s RETURNING {REVIEW_COLUMNS}",
                        [*diff.values(), before["id"]],
                    ).fetchone()
                    outcome = "updated"
                else:
                    row, outcome = before, "unchanged"
            return Plan(
                "decisions.review:write",
                str(decision_id),
                [],
                audit(f"decisions.review.{outcome}", decision_id, before, row),
                (outcome, row),
            )

        outcome, row = write(
            caller, op, precheck=("decisions.review:write", str(decision_id))
        )
        return ReviewResult(outcome=outcome, review=Review(**row))

    @app.patch("/v1/decision-reviews/{review_id}", response_model=Review)
    def update_review(review_id: int, body: ReviewUpdate, caller: Caller) -> Review:
        data = review_values(body, body.model_fields_set)

        def op(work: Work) -> Plan:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table/column names are module constants or spec-validated identifiers; values are bound as %s parameters
            before = work.conn.execute(
                f"SELECT {REVIEW_COLUMNS} FROM decisions.reviews WHERE id = %s FOR UPDATE",
                (review_id,),
            ).fetchone()
            if before is None:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            if (
                body.expected_version is not None
                and before["version"] != body.expected_version
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "version_conflict", "version": before["version"]},
                )
            diff = {k: v for k, v in data.items() if before.get(k) != v}
            row = before
            if diff:
                assignments = ", ".join(f"{name} = %s" for name in diff)
                row = work.conn.execute(
                    f"UPDATE decisions.reviews SET {assignments}, version = version + 1, updated_at = now()"
                    f" WHERE id = %s RETURNING {REVIEW_COLUMNS}",
                    [*diff.values(), review_id],
                ).fetchone()
            # Audited against the decision the review belongs to.
            return Plan(
                "decisions.review:write",
                str(before["decision_id"]),
                [],
                audit("decisions.review.updated", before["decision_id"], before, row),
                row,
            )

        def precheck_target() -> tuple[str, str]:
            with bound(caller) as conn:
                found = conn.execute(
                    "SELECT decision_id FROM decisions.reviews WHERE id = %s",
                    (review_id,),
                ).fetchone()
            allow(caller, "decisions.review:write", f"review:{review_id}")
            if found is None:
                raise HTTPException(status_code=404, detail={"error": "not_found"})
            return "decisions.review:write", str(found["decision_id"])

        return Review(**write(caller, op, precheck=precheck_target()))

    return app


def main() -> None:
    import uvicorn

    from solo_decisions.db import default_cluster
    from solo_decisions.platform import HttpPlatform

    hosted = os.environ.get("DECISIONS_DB_MODE") == "hosted"
    pem = os.environ.get("DECISIONS_JWT_PUBLIC_KEY_PEM", "").strip()
    # A second key for the same issuer during a signing-key rollover; unset = off.
    next_pem = os.environ.get("DECISIONS_JWT_NEXT_PUBLIC_KEY_PEM", "").strip()
    issuer = os.environ.get("DECISIONS_JWT_ISSUER", "")
    platform_url = os.environ.get("EMPIRE_PLATFORM_URL", "")
    if not pem or not issuer or not platform_url:
        raise SystemExit(
            "set DECISIONS_JWT_PUBLIC_KEY_PEM, DECISIONS_JWT_ISSUER and EMPIRE_PLATFORM_URL"
        )
    if hosted and issuer == "empire-platform-dev":
        raise SystemExit("hosted mode refuses the dev issuer")
    cluster = default_cluster()
    app = create_app(
        lambda: cluster.connect("decisions_api"),
        TokenVerifier(
            pem, issuer, extra_public_key_pems=(next_pem,) if next_pem else ()
        ),
        HttpPlatform(platform_url),
        publisher_token=lambda: os.environ.get("DECISIONS_PUBLISHER_TOKEN") or None,
    )
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0" if hosted else "127.0.0.1"),
        port=int(os.environ.get("PORT", "3610")),
        proxy_headers=False,
    )
