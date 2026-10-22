"""Decisions API behaviour: authorization through the platform, history, outbox, isolation."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from conftest import OWNER_ACTIONS

from solo_decisions.platform import PlatformUnavailable
from solo_decisions.publisher import publish_pending


@pytest.fixture
def owner(platform):
    actor, ws = uuid.uuid4(), uuid.uuid4()
    platform.grants[actor] = set(OWNER_ACTIONS)
    return actor, ws


def create(client, headers, **extra):
    body = {"title": "Pick a CRM", "decision": "Use the hosted agency CRM", **extra}
    response = client.post("/v1/decisions", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def superuser(cluster, query, params=()):
    with cluster.connect("postgres", "decisions") as conn:
        return conn.execute(query, params).fetchall()


def test_create_records_history_outbox_and_audit(
    client, token, owner, platform, cluster
):
    headers = token(*owner)
    decision = create(
        client, headers, domain="sales", confidence=0.7, evidence_refs=["crm-deal:4"]
    )
    assert decision["status"] == "proposed"
    assert decision["approval_status"] == "pending"
    assert decision["evidence_refs"] == ["crm-deal:4"]
    assert decision["version"] == 1
    events = client.get(
        f"/v1/decisions/{decision['id']}/events", headers=headers
    ).json()["items"]
    assert [e["event_type"] for e in events] == ["created"]
    outbox = superuser(
        cluster,
        "SELECT type, subject, classification FROM decisions.outbox WHERE subject = %s",
        (f"empire:decisions:decision:{decision['id']}",),
    )
    assert outbox == [
        (
            "decisions.decision.created",
            f"empire:decisions:decision:{decision['id']}",
            "confidential",
        )
    ]
    finished = [
        e
        for e in platform.executions.values()
        if e.get("resource_id") == str(decision["id"])
    ]
    assert finished
    assert finished[0]["status"] == "succeeded"
    assert finished[0]["after_hash"]


def test_reader_cannot_write_and_nothing_is_written(client, token, platform, cluster):
    actor, ws = uuid.uuid4(), uuid.uuid4()
    platform.grants[actor] = {"decisions.decision:read"}
    response = client.post(
        "/v1/decisions", json={"title": "x", "decision": "y"}, headers=token(actor, ws)
    )
    assert response.status_code == 403
    assert superuser(
        cluster, "SELECT count(*) FROM decisions.records WHERE workspace_id = %s", (ws,)
    ) == [(0,)]
    assert client.get("/v1/decisions", headers=token(actor, ws)).status_code == 200


def test_changing_approval_needs_the_approve_permission(client, token, owner, platform):
    decision = create(client, token(*owner))
    writer = uuid.uuid4()
    platform.grants[writer] = {"decisions.decision:read", "decisions.decision:write"}
    denied = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=token(writer, owner[1]),
        json={"approval_status": "approved"},
    )
    assert denied.status_code == 403
    approved = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=token(*owner),
        json={
            "approval_status": "approved",
            "status": "approved",
            "event": {
                "event_type": "approved",
                "actor": "owner",
                "reason": "looks right",
            },
        },
    )
    assert approved.status_code == 200
    assert approved.json()["version"] == 2
    event = client.get(
        f"/v1/decisions/{decision['id']}/events", headers=token(*owner)
    ).json()["items"][0]
    assert (
        event["event_type"],
        event["from_status"],
        event["to_status"],
        event["to_approval_status"],
    ) == ("approved", "proposed", "approved", "approved")


def test_version_conflict_changes_nothing_and_starts_no_execution(
    client, token, owner, platform
):
    decision = create(client, token(*owner))
    started = len(platform.executions)
    response = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=token(*owner),
        json={"title": "New", "expected_version": 5},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["version"] == 1
    assert (
        client.get(f"/v1/decisions/{decision['id']}", headers=token(*owner)).json()[
            "title"
        ]
        == "Pick a CRM"
    )
    assert len(platform.executions) == started


def test_execution_lock_key_is_exclusive(client, token, owner):
    first = create(client, token(*owner))
    second = create(client, token(*owner))
    lock = {"execution_lock_key": f"paas:{uuid.uuid4().hex}"}
    assert (
        client.patch(
            f"/v1/decisions/{first['id']}", headers=token(*owner), json=lock
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/v1/decisions/{second['id']}", headers=token(*owner), json=lock
        ).status_code
        == 409
    )


def test_dedupe_upsert_creates_then_is_unchanged_then_refreshes(client, token, owner):
    headers, key = token(*owner), f"engine:{uuid.uuid4().hex}"
    body = {
        "title": "Renew domain",
        "decision": "Renew for one year",
        "source_type": "engine",
    }
    first = client.put(f"/v1/decisions/dedupe/{key}", headers=headers, json=body).json()
    again = client.put(f"/v1/decisions/dedupe/{key}", headers=headers, json=body).json()
    changed = client.put(
        f"/v1/decisions/dedupe/{key}",
        headers=headers,
        json={**body, "decision": "Renew for two"},
    ).json()
    assert (first["outcome"], again["outcome"], changed["outcome"]) == (
        "created",
        "unchanged",
        "refreshed",
    )
    assert first["decision"]["id"] == changed["decision"]["id"]
    assert changed["decision"]["version"] == 2
    assert (
        client.get(f"/v1/decisions/dedupe/{key}", headers=headers).json()["decision"]
        == "Renew for two"
    )
    other = f"engine:{uuid.uuid4().hex}"
    event = {
        "event_type": "refreshed",
        "actor": "decision-engine",
        "metadata": {"dedupe_key": other},
    }
    created = client.put(
        f"/v1/decisions/dedupe/{other}", headers=headers, json={**body, "event": event}
    ).json()
    history = client.get(
        f"/v1/decisions/{created['decision']['id']}/events", headers=headers
    ).json()["items"]
    assert [(e["event_type"], e["actor"], e["metadata"]) for e in history] == [
        (
            "created",
            "decision-engine",
            {"dedupe_key": other, "authenticated_actor": str(owner[0])},
        )
    ]


def test_idempotent_create_returns_the_same_record(client, token, owner, platform):
    headers = {**token(*owner), "Idempotency-Key": f"create-{uuid.uuid4().hex}"}
    first = client.post(
        "/v1/decisions", headers=headers, json={"title": "a", "decision": "b"}
    )
    again = client.post(
        "/v1/decisions", headers=headers, json={"title": "a", "decision": "b"}
    )
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]


def test_create_retry_after_a_lost_answer_completes_the_same_execution(
    client, token, owner, platform, cluster
):
    key = f"create-{uuid.uuid4().hex}"
    headers = {**token(*owner), "Idempotency-Key": key}
    real_begin = platform.begin

    def lost_answer(
        *args, **kwargs
    ):  # the platform started the execution, the answer never arrived
        real_begin(*args, **kwargs)
        raise PlatformUnavailable("lost")

    platform.begin = lost_answer
    assert (
        client.post(
            "/v1/decisions", headers=headers, json={"title": "a", "decision": "b"}
        ).status_code
        == 503
    )
    platform.begin = real_begin
    retried = client.post(
        "/v1/decisions", headers=headers, json={"title": "a", "decision": "b"}
    )
    assert retried.status_code == 201
    execution = platform.keys[key]
    assert platform.executions[execution]["status"] == "succeeded"
    assert platform.executions[execution]["resource_id"] == str(retried.json()["id"])
    rows = superuser(
        cluster,
        "SELECT count(*) FROM decisions.records WHERE id = %s",
        (retried.json()["id"],),
    )
    assert rows == [(1,)]


def test_workspaces_are_isolated(client, token, owner, platform):
    decision = create(client, token(*owner))
    stranger, other_ws = uuid.uuid4(), uuid.uuid4()
    platform.grants[stranger] = set(OWNER_ACTIONS)
    assert (
        client.get(
            f"/v1/decisions/{decision['id']}", headers=token(stranger, other_ws)
        ).status_code
        == 404
    )
    assert (
        client.get("/v1/decisions", headers=token(stranger, other_ws)).json()["items"]
        == []
    )
    patched = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=token(stranger, other_ws),
        json={"title": "x"},
    )
    assert patched.status_code == 404


def test_runtime_role_sees_nothing_without_a_workspace(cluster, client, token, owner):
    create(client, token(*owner))
    with cluster.connect("decisions_api") as conn:
        assert conn.execute("SELECT count(*) FROM decisions.records").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("ALTER TABLE decisions.records DISABLE ROW LEVEL SECURITY")


def test_list_filters_and_stats(client, token, owner):
    headers = token(*owner)
    create(client, headers, domain="finance", status="draft")
    create(client, headers, domain="finance")
    assert (
        len(
            client.get(
                "/v1/decisions", headers=headers, params={"domain": "finance"}
            ).json()["items"]
        )
        == 2
    )
    drafts = client.get(
        "/v1/decisions", headers=headers, params={"status": ["draft"]}
    ).json()["items"]
    assert [d["status"] for d in drafts] == ["draft"]
    assert client.get("/v1/decisions/stats", headers=headers).json()["groups"] == {
        "draft": 1,
        "proposed": 1,
    }


def test_delete_emits_a_deleted_event(client, token, owner, cluster):
    decision = create(client, token(*owner))
    assert (
        client.delete(
            f"/v1/decisions/{decision['id']}", headers=token(*owner)
        ).status_code
        == 204
    )
    assert (
        client.get(f"/v1/decisions/{decision['id']}", headers=token(*owner)).status_code
        == 404
    )
    types = superuser(
        cluster,
        "SELECT type FROM decisions.outbox WHERE subject = %s ORDER BY occurred_at",
        (f"empire:decisions:decision:{decision['id']}",),
    )
    assert [t[0] for t in types] == [
        "decisions.decision.created",
        "decisions.decision.deleted",
    ]


def test_reviews_upsert_and_update(client, token, owner):
    headers = token(*owner)
    decision = create(client, headers)
    path = f"/v1/decisions/{decision['id']}/reviews/2026-11-01"
    first = client.put(path, headers=headers, json={"task_id": 12}).json()
    same = client.put(path, headers=headers, json={"task_id": 12}).json()
    assert (first["outcome"], same["outcome"]) == ("created", "unchanged")
    review = client.patch(
        f"/v1/decision-reviews/{first['review']['id']}",
        headers=headers,
        json={"status": "completed", "outcome": "worked"},
    ).json()
    assert review["status"] == "completed"
    assert review["version"] == 2
    pending = client.get(
        "/v1/decision-reviews", headers=headers, params={"status": "pending"}
    ).json()["items"]
    assert all(r["id"] != review["id"] for r in pending)


def test_platform_down_fails_closed(client, token, owner, platform):
    platform.down = True
    assert client.get("/v1/decisions", headers=token(*owner)).status_code == 503
    assert (
        client.post(
            "/v1/decisions", headers=token(*owner), json={"title": "a", "decision": "b"}
        ).status_code
        == 503
    )


def test_tokens_for_other_audiences_or_too_long_are_refused(client, token, owner):
    assert (
        client.get(
            "/v1/decisions", headers=token(*owner, audience=["tasks-api"])
        ).status_code
        == 401
    )
    assert (
        client.get("/v1/decisions", headers=token(*owner, ttl=7200)).status_code == 401
    )
    assert client.get("/v1/decisions").status_code == 401


def test_publisher_marks_published_and_backs_off_on_failure(
    client, token, owner, platform, cluster
):
    decision = create(client, token(*owner))
    publisher = token(uuid.uuid4(), owner[1])["Authorization"].removeprefix("Bearer ")
    platform.fail_publish = True
    with cluster.connect("decisions_publisher") as conn:
        failed = publish_pending(conn, platform, publisher)
    assert failed["failed"] >= 1
    assert failed["published"] == 0
    with cluster.connect("postgres", "decisions") as conn:
        conn.execute(
            "UPDATE decisions.outbox SET next_attempt_at = now() WHERE published_at IS NULL"
        )
    platform.fail_publish = False
    with cluster.connect("decisions_publisher") as conn:
        done = publish_pending(conn, platform, publisher, limit=1000)
    assert done["published"] >= 1
    subjects = {e["subject"] for e in platform.published}
    assert f"empire:decisions:decision:{decision['id']}" in subjects
    pending = superuser(
        cluster,
        "SELECT count(*) FROM decisions.outbox WHERE published_at IS NULL AND workspace_id = %s",
        (owner[1],),
    )
    assert pending == [(0,)]


def test_approval_and_content_need_their_own_permissions(
    client, token, owner, platform
):
    decision = create(client, token(*owner))
    approver, writer = uuid.uuid4(), uuid.uuid4()
    platform.grants[approver] = {
        "decisions.decision:read",
        "decisions.decision:approve",
    }
    platform.grants[writer] = {"decisions.decision:read", "decisions.decision:write"}
    path = f"/v1/decisions/{decision['id']}"
    sneaky = client.patch(
        path,
        headers=token(approver, owner[1]),
        json={"approval_status": "approved", "decision": "Something else"},
    )
    assert sneaky.status_code == 403
    assert (
        client.get(path, headers=token(*owner)).json()["decision"]
        == "Use the hosted agency CRM"
    )
    assert (
        client.post(
            "/v1/decisions",
            headers=token(approver, owner[1]),
            json={"title": "x", "decision": "y", "approval_status": "approved"},
        ).status_code
        == 403
    )
    assert (
        client.patch(
            path,
            headers=token(approver, owner[1]),
            json={"approval_status": "approved", "status": "approved"},
        ).status_code
        == 200
    )
    assert (
        client.patch(
            path, headers=token(writer, owner[1]), json={"approval_status": "rejected"}
        ).status_code
        == 403
    )


def test_changing_approved_content_sends_it_back_for_approval(client, token, owner):
    headers = token(*owner)
    decision = create(client, headers, action_type="external_change")
    path = f"/v1/decisions/{decision['id']}"
    client.patch(
        path,
        headers=headers,
        json={
            "approval_status": "approved",
            "status": "approved",
            "action_status": "queued",
        },
    )
    edited = client.patch(
        path, headers=headers, json={"decision": "Use a different CRM"}
    ).json()
    assert (edited["approval_status"], edited["status"], edited["action_status"]) == (
        "pending",
        "proposed",
        "pending_approval",
    )
    claimed = client.patch(
        path,
        headers=headers,
        json={"approval_status": "approved", "status": "approved"},
    ).json()
    unchanged = client.patch(
        path,
        headers=headers,
        json={
            "action_status": "blocked",
            "execution_lock_key": f"k-{uuid.uuid4().hex}",
        },
    ).json()
    assert claimed["approval_status"] == unchanged["approval_status"] == "approved"


def test_invalid_values_are_client_errors(client, token, owner):
    headers = token(*owner)
    decision = create(client, headers)
    path = f"/v1/decisions/{decision['id']}"
    assert client.patch(path, headers=headers, json={"title": None}).status_code == 422
    assert (
        client.patch(
            path, headers=headers, json={"approval_status": "not_required"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/decisions",
            headers=headers,
            json={"title": "a", "decision": "b", "approval_status": "not_required"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{path}/events", headers=headers, json={"event_type": "approved"}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            path,
            headers=headers,
            json={"title": "x", "event": {"event_type": "approved"}},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            path,
            headers=headers,
            json={"approval_required": False, "event": {"event_type": "approved"}},
        ).status_code
        == 422
    )
    no_approval = client.post(
        "/v1/decisions",
        headers=headers,
        json={
            "title": "a",
            "decision": "b",
            "approval_required": False,
            "approval_status": "not_required",
        },
    )
    assert no_approval.status_code == 201
