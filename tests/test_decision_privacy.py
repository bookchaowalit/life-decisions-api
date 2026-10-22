"""Graph events follow the privacy class: what is shared, and how it is classified."""

from __future__ import annotations

import pytest
from conftest import create_decision, superuser


def outbox(cluster, decision_id):
    return superuser(
        cluster,
        "SELECT type, classification, payload FROM decisions.outbox"
        " WHERE subject = %s ORDER BY occurred_at, id",
        (f"empire:decisions:decision:{decision_id}",),
    )


@pytest.mark.parametrize(
    ("privacy_class", "classification"),
    [("private", "confidential"), ("internal", "internal")],
)
def test_graph_events_carry_the_title_unless_restricted(
    client, token, owner, cluster, privacy_class, classification
):
    decision = create_decision(
        client, token(*owner), privacy_class=privacy_class, domain="sales"
    )
    ((kind, sent_as, payload),) = outbox(cluster, decision["id"])
    assert kind == "decisions.decision.created"
    assert sent_as == classification
    assert payload == {
        "version": 1,
        "deleted": False,
        "status": "proposed",
        "domain": "sales",
        "title": "Pick a CRM",
    }


def test_sensitive_decisions_go_to_the_graph_restricted_and_without_a_title(
    client, token, owner, cluster
):
    decision = create_decision(client, token(*owner), privacy_class="sensitive")
    ((_, sent_as, payload),) = outbox(cluster, decision["id"])
    assert sent_as == "restricted"
    assert "title" not in payload


def test_a_deleted_decision_is_announced_without_its_content(
    client, token, owner, cluster
):
    headers = token(*owner)
    decision = create_decision(client, headers)
    client.delete(f"/v1/decisions/{decision['id']}", headers=headers)
    *_, (kind, sent_as, payload) = outbox(cluster, decision["id"])
    assert kind == "decisions.decision.deleted"
    assert sent_as == "confidential"
    assert payload == {"version": 2, "deleted": True}


@pytest.mark.parametrize("key", ["short", "k" * 201])
def test_idempotency_keys_must_be_8_to_200_characters(client, token, owner, key):
    response = client.post(
        "/v1/decisions",
        headers={**token(*owner), "Idempotency-Key": key},
        json={"title": "a", "decision": "b"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == ["Idempotency-Key"]
