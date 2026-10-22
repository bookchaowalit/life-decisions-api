"""End to end with the real empire-platform (its own throwaway database, in-process HTTP).

Skipped unless empire-platform is installed in the environment (a sibling
checkout: ``uv pip install -e ../platform-empire[dev]``). Proves the contract:
real PDP decisions, executions bound to the operation, audit rows, and graph
ingest of the outbox through the publisher's delegated token.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("platform_api")

from empire_platform.devdb import Cluster, bootstrap
from empire_platform.devdb import migrate as platform_migrate
from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import DECISIONS_OWNER
from platform_api.routes import register
from tasks_api.auth import TokenVerifier as PlatformVerifier

from solo_decisions.app import create_app
from solo_decisions.auth import TokenVerifier
from solo_decisions.platform import HttpPlatform
from solo_decisions.publisher import publish_pending

ISSUER = "empire-e2e"


@pytest.fixture(scope="module")
def platform_cluster():
    root = Path(tempfile.mkdtemp(prefix="ep-e2e-"))
    cluster = Cluster(root / "data", root / "sock")
    cluster.init()
    cluster.start()
    try:
        bootstrap(cluster)
        platform_migrate(cluster)
        yield cluster
    finally:
        cluster.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def world(platform_cluster, tmp_path_factory):
    import importlib.util
    import sys

    # empire-platform's own workspace helpers (its tests/conftest.py, loaded under another name).
    path = (
        Path(__import__("empire_platform").__file__).resolve().parents[2]
        / "tests"
        / "conftest.py"
    )
    spec = importlib.util.spec_from_file_location("empire_platform_fixtures", path)
    fixtures = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = (
        fixtures  # dataclasses look their module up while the class is created
    )
    spec.loader.exec_module(fixtures)
    add_member, create_workspace, delegate = (
        fixtures.add_member,
        fixtures.create_workspace,
        fixtures.delegate,
    )

    private, public = key_pair(tmp_path_factory.mktemp("e2e-keys"))
    with platform_cluster.connect("platform_api") as conn:
        ws = create_workspace(
            conn, f"e2e-{uuid.uuid4().hex[:8]}", list(DECISIONS_OWNER)
        )
        reader = add_member(conn, ws, "reader", "agent")
        publisher = add_member(conn, ws, "decisions-publisher", "automation")
        read_only = delegate(conn, ws, ws.owner, reader, ["decisions.decision:read"])
        publish = delegate(conn, ws, ws.owner, publisher, ["graph.decisions:write"])
        conn.commit()
    platform_app = FastAPI()
    register(
        platform_app,
        connect_platform=lambda: platform_cluster.connect("tasks_api"),
        platform_verifier=PlatformVerifier(public, ISSUER, audience="platform-api"),
        graph_verifier=PlatformVerifier(public, ISSUER, audience="graph-api"),
    )
    http = HttpPlatform(
        "http://localhost", client=TestClient(platform_app, base_url="http://localhost")
    )
    audiences = ["decisions-api", "platform-api", "graph-api"]

    def bearer(sub, **kwargs):
        return mint(
            private, sub=sub, ws=ws.id, issuer=ISSUER, audience=audiences, **kwargs
        )

    return {
        "ws": ws,
        "public": public,
        "http": http,
        "bearer": bearer,
        "reader": reader,
        "read_only": read_only,
        "publisher": publisher,
        "publish": publish,
    }


@pytest.fixture(scope="module")
def cell(cluster, world):
    app = create_app(
        lambda: cluster.connect("decisions_api"),
        TokenVerifier(world["public"], ISSUER),
        world["http"],
    )
    return TestClient(app)


def test_owner_writes_and_the_platform_records_execution_and_audit(
    cell, world, platform_cluster
):
    owner = {"Authorization": f"Bearer {world['bearer'](world['ws'].owner)}"}
    created = cell.post(
        "/v1/decisions", headers=owner, json={"title": "E2E", "decision": "Ship it"}
    )
    assert created.status_code == 201, created.text
    decision_id = created.json()["id"]
    with platform_cluster.connect("postgres") as conn:
        rows = conn.execute(
            "SELECT e.status, a.service, a.action, a.resource_id FROM platform.audit_events a"
            " JOIN platform.executions e ON e.id = a.execution_id WHERE a.resource_id = %s AND a.service = 'decisions'",
            (str(decision_id),),
        ).fetchall()
    assert rows == [
        ("succeeded", "decisions", "decisions.decision.created", str(decision_id))
    ]
    assert (
        cell.patch(
            f"/v1/decisions/{decision_id}",
            headers=owner,
            json={"approval_status": "approved"},
        ).status_code
        == 200
    )


def test_read_only_delegation_reads_but_cannot_write(cell, world):
    reader = {
        "Authorization": "Bearer "
        + world["bearer"](
            world["reader"], delegation=world["read_only"], obo=world["ws"].owner
        )
    }
    assert cell.get("/v1/decisions", headers=reader).status_code == 200
    assert (
        cell.post(
            "/v1/decisions", headers=reader, json={"title": "x", "decision": "y"}
        ).status_code
        == 403
    )


def test_outbox_reaches_the_graph_through_the_publisher(
    cell, world, cluster, platform_cluster
):
    owner = {"Authorization": f"Bearer {world['bearer'](world['ws'].owner)}"}
    decision_id = cell.post(
        "/v1/decisions", headers=owner, json={"title": "Graph me", "decision": "Yes"}
    ).json()["id"]
    token = world["bearer"](
        world["publisher"], delegation=world["publish"], obo=world["ws"].owner
    )
    with cluster.connect("decisions_publisher") as conn:
        result = publish_pending(conn, world["http"], token, limit=1000)
    assert result["failed"] == 0
    assert result["published"] >= 1
    with platform_cluster.connect("postgres") as conn:
        node = conn.execute(
            "SELECT cell, type FROM graph.nodes WHERE urn = %s",
            (f"empire:decisions:decision:{decision_id}",),
        ).fetchone()
    assert node == ("decisions", "decision")
