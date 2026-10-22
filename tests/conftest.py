"""A throwaway PostgreSQL per session, a fake platform, and token minting."""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from solo_decisions.app import create_app
from solo_decisions.auth import TokenVerifier
from solo_decisions.db import LocalCluster, migrate
from solo_decisions.platform import Action, Denied, PlatformUnavailable

ISSUER = "empire-platform-test"


@pytest.fixture(scope="session")
def cluster() -> Iterator[LocalCluster]:
    root = Path(tempfile.mkdtemp(prefix="sd-"))
    cluster = LocalCluster(root / "data", root / "sock")
    cluster.init()
    cluster.start()
    try:
        cluster.bootstrap()
        migrate(cluster)
        yield cluster
    finally:
        cluster.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def keys() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    return private, public


@dataclass
class FakePlatform:
    """Grants by actor; records every call like the real platform would."""

    grants: dict[uuid.UUID, set[str]] = field(default_factory=dict)
    down: bool = False
    executions: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    keys: dict[str, uuid.UUID] = field(default_factory=dict)
    published: list[dict[str, Any]] = field(default_factory=list)
    fail_publish: bool = False

    @staticmethod
    def _actor(token: str) -> uuid.UUID:
        return uuid.UUID(jwt.decode(token, options={"verify_signature": False})["sub"])

    def decide(self, token: str, channel: str, action: Action) -> bool:
        if self.down:
            raise PlatformUnavailable("down")
        return action.action in self.grants.get(self._actor(token), set())

    def begin(self, token, channel, action, idempotency_key, correlation_id, also=None):
        if self.down:
            raise PlatformUnavailable("down")
        if idempotency_key and idempotency_key in self.keys:
            execution = self.keys[idempotency_key]
            return execution, True, self.executions[execution]["status"]
        for needed in [action.action, *(also or [])]:
            if not self.decide(
                token, channel, Action(needed, action.resource_type, action.resource_id)
            ):
                raise Denied(403, "deny", "no_permission")
        execution = uuid.uuid4()
        self.executions[execution] = {
            "action": action.action,
            "status": "running",
            "actor": self._actor(token),
        }
        if idempotency_key:
            self.keys[idempotency_key] = execution
        return execution, False, "running"

    lose_finish: bool = False

    def finish(self, token, channel, execution, outcome):
        if self.lose_finish:
            raise PlatformUnavailable("lost")
        self.executions[execution].update(outcome)

    def publish(self, token, event):
        if self.fail_publish:
            raise PlatformUnavailable("down")
        self.published.append(event)
        return "applied"


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def client(cluster, keys, platform) -> TestClient:
    _, public = keys
    app = create_app(
        lambda: cluster.connect("decisions_api"),
        TokenVerifier(public, ISSUER),
        platform,
        publisher_token=lambda: None,
    )
    return TestClient(app)


@pytest.fixture
def token(keys):
    private, _ = keys

    def make(
        sub: uuid.UUID,
        ws: uuid.UUID,
        *,
        audience: Any = ("decisions-api", "platform-api"),
        ttl: int = 600,
        delegation: uuid.UUID | None = None,
    ) -> dict[str, str]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": list(audience) if not isinstance(audience, str) else audience,
            "sub": str(sub),
            "ws": str(ws),
            "iat": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        }
        if delegation:
            claims["del"] = str(delegation)
        return {
            "Authorization": f"Bearer {jwt.encode(claims, private, algorithm='RS256')}"
        }

    return make


OWNER_ACTIONS = {
    "decisions.decision:read",
    "decisions.decision:write",
    "decisions.decision:approve",
    "decisions.decision:delete",
    "decisions.review:write",
}


@pytest.fixture
def owner(platform: FakePlatform) -> tuple[uuid.UUID, uuid.UUID]:
    """An actor with every decisions permission, in a fresh workspace."""
    actor, ws = uuid.uuid4(), uuid.uuid4()
    platform.grants[actor] = set(OWNER_ACTIONS)
    return actor, ws


def create_decision(
    client: TestClient, headers: dict[str, str], **extra: Any
) -> dict[str, Any]:
    body = {"title": "Pick a CRM", "decision": "Use the hosted agency CRM", **extra}
    response = client.post("/v1/decisions", json=body, headers=headers)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def superuser(cluster: LocalCluster, query: str, params: Any = ()) -> list[Any]:
    with cluster.connect("postgres", "decisions") as conn:
        return conn.execute(query, params).fetchall()
