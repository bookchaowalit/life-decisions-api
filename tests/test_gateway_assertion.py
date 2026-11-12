"""platform-api-gateway SPEC-002: only calls that came through the edge gateway (report, then enforce)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from solo_decisions import gateway as gw
from solo_decisions.gateway import (
    MAX_BODY_BYTES,
    GatewayCheck,
    body_too_big,
    check,
    expected,
    install,
    is_open,
)

# Pinned vectors made by the gateway's reference (book_api_gateway.assertion.sign) from the public
# test master bytes(range(32)) for system "habits"; the same vectors are pinned in life-habits-api.
MASTER = bytes(range(32))
KEY = bytes.fromhex("fe0cf5b8aad53ea81449701c218d54e663bc33d864842e739bcef8742d94a56f")
VECTOR = "v1.1793700000.cmVxLjE6YQ.FIYOR4VOYVb7_PhDWnTiFqxKyHEqUxDcs1B8dB7b0sE"
POST_VECTOR = "v1.1793700000.cmVxLjE6YQ._L2Jgjl5CrbW9qTdTfgvtGubyou3nkRZHXmFKZrxsm8"
T0 = 1793700000


def test_the_key_is_derived_per_system_like_the_gateway():
    assert (
        hmac.new(MASTER, b"gateway-assertion/v1/habits", hashlib.sha256).digest() == KEY
    )


def test_matches_the_gateway_reference():
    get = ("GET", "/v1/habits?status=active", "Bearer t")
    assert expected(KEY, "habits", get[0], get[1], T0, "req.1:a", get[2]) == VECTOR
    assert check(KEY, "habits", VECTOR, *get, now=T0) == "valid"
    assert check(KEY, "habits", VECTOR, *get, now=T0 + 61) == "invalid"
    assert (
        check(KEY, "health", VECTOR, *get, now=T0) == "invalid"
    )  # another system's assertion
    assert check(KEY, "habits", VECTOR, "POST", *get[1:], now=T0) == "invalid"
    assert (
        check(KEY, "habits", VECTOR, "GET", "/v1/habits", "Bearer t", now=T0)
        == "invalid"
    )
    assert check(KEY, "habits", VECTOR, "GET", get[1], "Bearer u", now=T0) == "invalid"
    assert check(KEY, "habits", None, *get, now=T0) == "missing"
    assert check(KEY, "habits", "v1.x", *get, now=T0) == "invalid"
    body = b'{"name":"run"}'
    post = ("POST", "/v1/habits", "Bearer t")
    assert (
        expected(
            KEY,
            "habits",
            post[0],
            post[1],
            T0,
            "req.1:a",
            post[2],
            "application/json",
            body,
        )
        == POST_VECTOR
    )
    assert (
        check(KEY, "habits", POST_VECTOR, *post, T0, "application/json", body)
        == "valid"
    )
    assert (
        check(
            KEY,
            "habits",
            POST_VECTOR,
            *post,
            T0,
            "application/json",
            b'{"name":"swim"}',
        )
        == "invalid"
    )
    assert check(KEY, "habits", POST_VECTOR, *post, T0, "text/plain", body) == "invalid"


def test_open_paths():
    assert is_open("/health")
    assert is_open("/openapi.json")
    assert is_open("/.well-known/jwks.json")
    assert not is_open("/v1/health-metrics")
    assert not is_open("/healthz-not")
    assert not is_open("/.well-knownx")


def test_mode_and_key_come_from_the_environment():
    assert GatewayCheck.from_env("health", {}) is None
    assert GatewayCheck.from_env("health", {"GATEWAY_ASSERTION_MODE": "off"}) is None
    with pytest.raises(SystemExit):
        GatewayCheck.from_env(
            "health",
            {"GATEWAY_ASSERTION_MODE": "strict", "GATEWAY_ASSERTION_KEY": KEY.hex()},
        )
    with pytest.raises(SystemExit):
        GatewayCheck.from_env(
            "health",
            {"GATEWAY_ASSERTION_MODE": "report", "GATEWAY_ASSERTION_KEY": "ab"},
        )
    got = GatewayCheck.from_env(
        "health",
        {"GATEWAY_ASSERTION_MODE": "report", "GATEWAY_ASSERTION_KEY": KEY.hex()},
    )
    assert got == GatewayCheck("health", "report", KEY)


# --- the middleware --------------------------------------------------------------------------------

SYSTEM = "decisions"


def make_app(mode: str) -> TestClient:
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/items")
    def items():
        return {"items": []}

    @app.post("/v1/items")
    async def create(request: Request):
        return {"echo": (await request.body()).decode()}

    install(app, GatewayCheck(SYSTEM, mode, KEY))  # type: ignore[arg-type]
    return TestClient(app)


def signed(
    method: str,
    target: str,
    authorization: str = "Bearer t",
    content_type: str = "",
    body: bytes = b"",
) -> str:
    return expected(
        KEY,
        SYSTEM,
        method,
        target,
        int(time.time()),
        "req.9",
        authorization,
        content_type,
        body,
    )


def results(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.name == "solo_decisions.gateway"
    ]


@pytest.fixture(autouse=True)
def _propagate(monkeypatch):
    monkeypatch.setattr(gw.log, "propagate", True)  # let caplog see the lines


def test_report_mode_logs_and_lets_everything_through(caplog):
    caplog.set_level(logging.INFO, logger="solo_decisions.gateway")
    client = make_app("report")
    ok = client.get(
        "/v1/items?x=1",
        headers={
            "Authorization": "Bearer t",
            "X-Gateway-Assertion": signed("GET", "/v1/items?x=1"),
        },
    )
    direct = client.get("/v1/items", headers={"Authorization": "Bearer t"})
    forged = client.get(
        "/v1/items",
        headers={
            "Authorization": "Bearer u",
            "X-Gateway-Assertion": signed("GET", "/v1/items"),
        },
    )
    assert [ok.status_code, direct.status_code, forged.status_code] == [200, 200, 200]
    assert results(caplog) == [
        "gateway_assertion result=valid method=GET path=/v1/items",
        "gateway_assertion result=missing method=GET path=/v1/items",
        "gateway_assertion result=invalid method=GET path=/v1/items",
    ]


def test_enforce_mode_refuses_direct_calls_but_not_probes(caplog):
    caplog.set_level(logging.INFO, logger="solo_decisions.gateway")
    client = make_app("enforce")
    direct = client.get("/v1/items", headers={"Authorization": "Bearer t"})
    assert direct.status_code == 403
    assert direct.json() == {"error": "gateway_required"}
    assert client.get("/health").status_code == 200
    assert results(caplog) == [
        "gateway_assertion result=missing method=GET path=/v1/items"
    ]
    via = client.get(
        "/v1/items",
        headers={
            "Authorization": "Bearer t",
            "X-Gateway-Assertion": signed("GET", "/v1/items"),
        },
    )
    assert via.status_code == 200


def test_the_body_is_bound_and_still_reaches_the_route():
    client = make_app("enforce")
    body = b'{"value": 1.10}'
    header = signed("POST", "/v1/items", content_type="application/json", body=body)
    headers = {
        "Authorization": "Bearer t",
        "Content-Type": "application/json",
        "X-Gateway-Assertion": header,
    }
    ok = client.post("/v1/items", content=body, headers=headers)
    assert ok.status_code == 200
    assert ok.json() == {"echo": body.decode()}
    changed = client.post("/v1/items", content=b'{"value": 2}', headers=headers)
    assert changed.status_code == 403


def test_the_api_installs_the_check_for_decisions(monkeypatch):
    """main() wires the check with SYSTEM = "decisions", the gateway route key."""
    import uvicorn

    from solo_decisions import app as app_module
    from solo_decisions import db

    seen: dict[str, object] = {}
    monkeypatch.setattr(app_module, "create_app", lambda *a, **k: "app-sentinel")
    monkeypatch.setattr(app_module, "TokenVerifier", lambda *a, **k: None)
    monkeypatch.setattr(db, "default_cluster", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **k: seen.update(ran=app))
    monkeypatch.setattr(
        gw, "install", lambda app, check: seen.update(app=app, check=check)
    )
    for name in (
        "DECISIONS_DB_MODE",
        "DECISIONS_JWT_NEXT_PUBLIC_KEY_PEM",
        "HOST",
        "PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    env = {
        "DECISIONS_JWT_PUBLIC_KEY_PEM": "-----BEGIN PUBLIC KEY-----\nTEST\n-----END PUBLIC KEY-----",
        "DECISIONS_JWT_ISSUER": "empire-platform",
        "EMPIRE_PLATFORM_URL": "https://platform.example",
        "GATEWAY_ASSERTION_MODE": "report",
        "GATEWAY_ASSERTION_KEY": KEY.hex(),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app_module.main()
    assert seen["app"] == "app-sentinel"
    assert seen["ran"] == "app-sentinel"
    assert seen["check"] == GatewayCheck(gw.SYSTEM, "report", KEY)
    assert gw.SYSTEM == "decisions"


def test_a_body_the_gateway_could_not_send_is_never_read():
    assert not body_too_big(None, None)
    assert not body_too_big("0", None)
    assert not body_too_big(str(MAX_BODY_BYTES), None)
    assert body_too_big(str(MAX_BODY_BYTES + 1), None)
    assert body_too_big("-1", None)
    assert body_too_big("12x", None)
    assert body_too_big(None, "chunked")


def test_a_forged_header_with_a_big_body_is_refused_unread(monkeypatch):
    client = make_app("enforce")
    reads = []
    original = Request.body

    async def counting_body(self):
        reads.append(1)
        return await original(self)

    monkeypatch.setattr(Request, "body", counting_body)
    body = b"x" * (MAX_BODY_BYTES + 1)
    header = signed("POST", "/v1/items", content_type="application/json", body=body)
    headers = {
        "Authorization": "Bearer t",
        "Content-Type": "application/json",
        "X-Gateway-Assertion": header,
    }
    refused = client.post("/v1/items", content=body, headers=headers)
    assert refused.status_code == 403
    assert reads == []
