"""Approval rules: who may approve, and what an approval stops covering when content changes."""

from __future__ import annotations

import uuid

from conftest import create_decision


def approve(client, headers, decision_id, **extra):
    response = client.patch(
        f"/v1/decisions/{decision_id}",
        headers=headers,
        json={"approval_status": "approved", "status": "approved", **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_decision_that_needs_approval_cannot_be_marked_not_required(
    client, token, owner
):
    response = client.post(
        "/v1/decisions",
        headers=token(*owner),
        json={"title": "a", "decision": "b", "approval_status": "not_required"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "approval_required_but_not_required"


def test_dropping_the_approval_requirement_needs_the_approve_permission(
    client, token, owner, platform
):
    decision = create_decision(client, token(*owner))
    writer = uuid.uuid4()
    platform.grants[writer] = {"decisions.decision:read", "decisions.decision:write"}
    response = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=token(writer, owner[1]),
        json={"approval_required": False, "approval_status": "not_required"},
    )
    assert response.status_code == 403
    current = client.get(f"/v1/decisions/{decision['id']}", headers=token(*owner))
    assert current.json()["approval_required"] is True


def test_an_approval_event_must_match_the_approval_it_records(client, token, owner):
    decision = create_decision(client, token(*owner))
    response = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=token(*owner),
        json={"approval_status": "approved", "event": {"event_type": "rejected"}},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "approval_event_without_approval"


def test_editing_an_approved_decision_without_an_action_resets_only_the_approval(
    client, token, owner
):
    headers = token(*owner)
    decision = create_decision(client, headers)
    approve(client, headers, decision["id"])
    edited = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=headers,
        json={"rationale": "A cheaper plan appeared"},
    ).json()
    assert edited["approval_status"] == "pending"
    assert edited["status"] == "proposed"
    assert edited["action_status"] == "none"


def test_a_decision_already_in_progress_keeps_its_status_when_sent_back(
    client, token, owner
):
    headers = token(*owner)
    decision = create_decision(client, headers, action_type="task")
    approve(client, headers, decision["id"])
    client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=headers,
        json={"status": "in_progress"},
    )
    edited = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=headers,
        json={"next_action": "Book a demo"},
    ).json()
    assert edited["approval_status"] == "pending"
    assert edited["status"] == "in_progress"
    assert edited["action_status"] == "pending_approval"


def test_recording_the_actual_outcome_keeps_the_approval(client, token, owner):
    headers = token(*owner)
    decision = create_decision(client, headers)
    approve(client, headers, decision["id"])
    edited = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=headers,
        json={
            "actual_outcome": "Signed up, onboarding done",
            "review_date": "2026-12-01",
        },
    ).json()
    assert edited["approval_status"] == "approved"
    assert edited["status"] == "approved"


def test_a_decision_without_an_approval_requirement_stays_not_required_when_edited(
    client, token, owner
):
    headers = token(*owner)
    decision = create_decision(
        client, headers, approval_required=False, approval_status="not_required"
    )
    edited = client.patch(
        f"/v1/decisions/{decision['id']}",
        headers=headers,
        json={"decision": "Use the self-hosted CRM"},
    ).json()
    assert edited["approval_status"] == "not_required"
    assert edited["version"] == 2
