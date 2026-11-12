"""Accept calls that came through the edge gateway (platform-api-gateway SPEC-002).

The gateway adds ``X-Gateway-Assertion``: an HMAC with this service's own key over the system id
(``SYSTEM``, this service's gateway route key), method, request target, issue time, request id,
and the SHA-256 of ``Authorization``, ``Content-Type`` and the body. ``report`` mode logs whether a
request carried a valid one; ``enforce`` answers 403 to one that did not. Probes and describe_cell
(``OPEN_PATHS``) are neither checked nor reported.

This is a copy of the gateway's reference (cells own their code), kept byte-compatible by the
pinned test vector in ``tests/test_gateway_assertion.py``; the same vector is pinned in
life-habits-api and platform-api-gateway, so change them together.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fastapi import FastAPI, Request, Response

SYSTEM = "decisions"  # this service's id in the gateway route table
HEADER = "x-gateway-assertion"
MAX_SKEW_SECONDS = 60
# Platform probes and describe_cell stay reachable without the gateway (the platform-* services probe
# /healthz and /readyz; some cells answer /ready).
OPEN_PATHS = (
    "/health",
    "/healthz",
    "/ready",
    "/readyz",
    "/openapi.json",
    "/.well-known/",
)
# A body the header says to wait for must arrive within this, or the request is "invalid" (a client that
# sends a well-formed header and then stalls cannot hold the connection).
BODY_TIMEOUT_SECONDS = 30
# The gateway refuses bodies above 1 MiB (MAX_BODY_BYTES) and forwards a Content-Length, so a bigger or
# length-less (chunked) body cannot carry a valid assertion: it is "invalid" without being read.
MAX_BODY_BYTES = 1_048_576
_HEADER_RE = re.compile(
    r"v1\.([0-9]{1,12})\.([A-Za-z0-9_-]{1,180})\.([A-Za-z0-9_-]{43})"
)

Mode = Literal["off", "report", "enforce"]
log = logging.getLogger("solo_decisions.gateway")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sha(value: bytes | str) -> str:
    data = (
        value if isinstance(value, bytes) else value.encode("utf-8", "surrogateescape")
    )
    return hashlib.sha256(data).hexdigest()


def expected(
    key: bytes,
    system: str,
    method: str,
    target: str,
    iat: int,
    request_id: str,
    authorization: str,
    content_type: str = "",
    body: bytes = b"",
) -> str:
    fields = (
        "gw1",
        system,
        method.upper(),
        target,
        str(iat),
        request_id,
        _sha(authorization),
        _sha(content_type),
        _sha(body),
    )
    mac = hmac.new(
        key, "\n".join(fields).encode("utf-8", "surrogateescape"), hashlib.sha256
    ).digest()
    return f"v1.{iat}.{_b64(request_id.encode())}.{_b64(mac)}"


def precheck(header: str | None, now: float) -> str | None:
    """'missing' or 'invalid' when the header alone settles it (no body read); None = needs the MAC."""
    if not header:
        return "missing"
    found = _HEADER_RE.fullmatch(header.strip())
    if not found or abs(now - int(found[1])) > MAX_SKEW_SECONDS:
        return "invalid"
    return None


def body_too_big(content_length: str | None, transfer_encoding: str | None) -> bool:
    """True when the body is not one the gateway could have sent (so it is never buffered)."""
    if transfer_encoding:
        return True
    if content_length is None:
        return False  # no body
    try:
        return not 0 <= int(content_length) <= MAX_BODY_BYTES
    except ValueError:
        return True


def check(
    key: bytes,
    system: str,
    header: str | None,
    method: str,
    target: str,
    authorization: str,
    now: float,
    content_type: str = "",
    body: bytes = b"",
) -> str:
    """'valid', 'missing' or 'invalid' (malformed, stale or a wrong MAC)."""
    early = precheck(header, now)
    if early:
        return early
    assert header is not None
    found = _HEADER_RE.fullmatch(header.strip())
    assert found is not None
    try:
        request_id = base64.urlsafe_b64decode(
            found[2] + "=" * (-len(found[2]) % 4)
        ).decode()
    except (ValueError, UnicodeDecodeError):
        return "invalid"
    want = expected(
        key,
        system,
        method,
        target,
        int(found[1]),
        request_id,
        authorization,
        content_type,
        body,
    )
    return (
        "valid"
        if hmac.compare_digest(want.encode(), header.strip().encode())
        else "invalid"
    )


def is_open(path: str) -> bool:
    # One trailing slash is ignored, as the routers do (/health/ is /health).
    path = path[:-1] if len(path) > 1 and path.endswith("/") else path
    return any(
        path == p or (p.endswith("/") and (path + "/").startswith(p))
        for p in OPEN_PATHS
    )


def report(result: str, method: str, path: str) -> None:
    # The route family only (/v1/notifications…): never the token, query or header value.
    family = "/".join(path.split("/")[:3])[:64]
    log.info("gateway_assertion result=%s method=%s path=%s", result, method, family)


@dataclass(frozen=True)
class GatewayCheck:
    system: str
    mode: Mode
    key: bytes

    @classmethod
    def from_env(
        cls, system: str, env: dict[str, str] | None = None
    ) -> GatewayCheck | None:
        env = dict(os.environ) if env is None else env
        mode = env.get("GATEWAY_ASSERTION_MODE", "off").strip() or "off"
        if mode == "off":
            return None
        if mode not in ("report", "enforce"):
            raise SystemExit("GATEWAY_ASSERTION_MODE must be off, report or enforce")
        try:
            key = bytes.fromhex(env.get("GATEWAY_ASSERTION_KEY", "").strip())
        except ValueError:
            key = b""
        if len(key) != 32:
            raise SystemExit(
                "GATEWAY_ASSERTION_KEY must be 64 hex characters when the check is on"
            )
        if not log.handlers:  # stdout keeps Railway from tagging the lines as errors
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(handler)
            log.setLevel(logging.INFO)
            log.propagate = False
        return cls(system, mode, key)  # type: ignore[arg-type]

    def refuses(self, path: str, result: str) -> bool:
        return self.mode == "enforce" and result != "valid" and not is_open(path)


def install(app: FastAPI, gateway: GatewayCheck | None) -> None:
    """Add the check as the outermost HTTP middleware (call before the app serves)."""
    if gateway is None:
        return
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def through_gateway(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if is_open(path):
            return await call_next(request)
        header = request.headers.get(HEADER)
        # A missing, malformed or stale header, or a body the gateway could not have sent, is settled
        # without reading the body, so a direct caller cannot make the service buffer it.
        result = precheck(header, time.time())
        if result is None and body_too_big(
            request.headers.get("content-length"),
            request.headers.get("transfer-encoding"),
        ):
            result = "invalid"
        if result is None:
            raw_path = (request.scope.get("raw_path") or path.encode()).decode(
                "latin-1"
            )
            query = request.scope.get("query_string", b"").decode("latin-1")
            try:
                body = await asyncio.wait_for(request.body(), BODY_TIMEOUT_SECONDS)
            except TimeoutError:
                # A stalled body: answered at once in either mode, and the connection is not held.
                report("invalid", request.method, path)
                return JSONResponse({"error": "request_timeout"}, status_code=408)
            result = check(
                gateway.key,
                gateway.system,
                header,
                request.method,
                raw_path + (f"?{query}" if query else ""),
                request.headers.get("authorization", ""),
                time.time(),
                request.headers.get("content-type", ""),
                body,  # cached by Starlette, so the route still reads it
            )
        report(result, request.method, path)
        if gateway.refuses(path, result):
            return JSONResponse({"error": "gateway_required"}, status_code=403)
        return await call_next(request)
