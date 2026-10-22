"""History rules: state changes only through PATCH, notes keep who really acted, paging order."""

from __future__ import annotations

import uuid

import pytest
from conftest import create_decision

STATE_EVENTS = [
    "created",
    "refreshed",
    "approved",
    "rejected",
    "deferred",
    "completed",
    "updated",
]


def history(client, headers, decision_id, **params):
    response = client.get(
        f"/v1/decisions/{decision_id}/events", headers=headers, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


@pytest.mark.parametrize("event_type", STATE_EVENTS)
def test_state_events_cannot_be_appended_directly(client, token, owner, event_type):
    headers = token(*owner)
    decision = create_decision(client, headers)
    response = client.post(
        f"/v1/decisions/{decision['id']}/events",
        headers=headers,
        json={"event_type": event_type},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "use_patch_for_state"
    assert [e["event_type"] for e in history(client, headers, decision["id"])] == [
        "created"
    ]


def test_a_note_keeps_the_callers_label_and_records_the_real_actor(
    client, token, owner
):
    headers = token(*owner)
    decision = create_decision(client, headers)
    response = client.post(
        f"/v1/decisions/{decision['id']}/events",
        headers=headers,
        json={
            "event_type": "reviewed",
            "actor": "weekly-review",
            "reason": "Still the right call",
            "metadata": {"authenticated_actor": "someone-else", "week": 43},
        },
    )
    assert response.status_code == 201
    note = response.json()
    assert note["actor"] == "weekly-review"
    assert note["metadata"] == {"authenticated_actor": str(owner[0]), "week": 43}
    assert note["from_status"] == note["to_status"] == "proposed"


def test_a_reader_cannot_add_a_note(client, token, owner, platform):
    decision = create_decision(client, token(*owner))
    reader = uuid.uuid4()
    platform.grants[reader] = {"decisions.decision:read"}
    response = client.post(
        f"/v1/decisions/{decision['id']}/events",
        headers=token(reader, owner[1]),
        json={"event_type": "reviewed"},
    )
    assert response.status_code == 403


def test_a_patch_that_changes_nothing_records_no_event_and_keeps_the_version(
    client, token, owner
):
    headers = token(*owner)
    decision = create_decision(client, headers)
    same = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=headers,
        json={"title": decision["title"]},
    ).json()
    assert same["version"] == 1
    assert len(history(client, headers, decision["id"])) == 1


def test_history_pages_oldest_first_after_a_given_event(client, token, owner):
    headers = token(*owner)
    decision = create_decision(client, headers)
    for week in (1, 2, 3):
        client.post(
            f"/v1/decisions/{decision['id']}/events",
            headers=headers,
            json={"event_type": "reviewed", "metadata": {"week": week}},
        )
    first_page = history(client, headers, decision["id"], order="asc", limit=2)
    assert [e["event_type"] for e in first_page] == ["created", "reviewed"]
    rest = history(
        client, headers, decision["id"], order="asc", after_id=first_page[-1]["id"]
    )
    assert [e["metadata"]["week"] for e in rest] == [2, 3]


def test_history_across_decisions_filters_by_event_type(client, token, owner):
    headers = token(*owner)
    first = create_decision(client, headers)
    create_decision(client, headers)
    client.post(
        f"/v1/decisions/{first['id']}/events",
        headers=headers,
        json={"event_type": "signal_cleared"},
    )
    response = client.get(
        "/v1/decision-events", headers=headers, params={"event_type": "signal_cleared"}
    )
    items = response.json()["items"]
    assert [(e["decision_id"], e["event_type"]) for e in items] == [
        (first["id"], "signal_cleared")
    ]
