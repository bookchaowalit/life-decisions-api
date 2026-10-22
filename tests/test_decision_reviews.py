"""Review rules: reviews belong to an existing decision, need their own permission, and lock."""

from __future__ import annotations

import uuid

from conftest import create_decision


def put_review(client, headers, decision_id, due, **body):
    return client.put(
        f"/v1/decisions/{decision_id}/reviews/{due}", headers=headers, json=body
    )


def test_a_review_needs_an_existing_decision(client, token, owner):
    response = put_review(client, token(*owner), 999_999_999, "2026-11-01")
    assert response.status_code == 404


def test_writing_a_review_needs_the_review_permission(client, token, owner, platform):
    decision = create_decision(client, token(*owner))
    writer = uuid.uuid4()
    platform.grants[writer] = {"decisions.decision:read", "decisions.decision:write"}
    response = put_review(client, token(writer, owner[1]), decision["id"], "2026-11-01")
    assert response.status_code == 403


def test_a_review_status_cannot_be_cleared(client, token, owner):
    headers = token(*owner)
    decision = create_decision(client, headers)
    review = put_review(client, headers, decision["id"], "2026-11-01").json()["review"]
    response = client.patch(
        f"/v1/decision-reviews/{review['id']}", headers=headers, json={"status": None}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["fields"] == ["status"]


def test_a_stale_review_version_is_a_conflict_and_changes_nothing(client, token, owner):
    headers = token(*owner)
    decision = create_decision(client, headers)
    review = put_review(client, headers, decision["id"], "2026-11-01").json()["review"]
    response = client.patch(
        f"/v1/decision-reviews/{review['id']}",
        headers=headers,
        json={"status": "skipped", "expected_version": 2},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"error": "version_conflict", "version": 1}
    current = client.get(f"/v1/decision-reviews/{review['id']}", headers=headers)
    assert current.json()["status"] == "pending"


def test_a_changed_review_counts_as_updated_and_bumps_its_version(client, token, owner):
    headers = token(*owner)
    decision = create_decision(client, headers)
    put_review(client, headers, decision["id"], "2026-11-01", task_id=7)
    again = put_review(client, headers, decision["id"], "2026-11-01", task_id=8).json()
    assert again["outcome"] == "updated"
    assert again["review"]["task_id"] == 8
    assert again["review"]["version"] == 2


def test_reviews_list_by_due_date_up_to_a_day(client, token, owner):
    headers = token(*owner)
    decision = create_decision(client, headers)
    for due in ("2026-11-15", "2026-11-01", "2026-12-01"):
        put_review(client, headers, decision["id"], due)
    due = client.get(
        "/v1/decision-reviews",
        headers=headers,
        params={"decision_id": decision["id"], "due_on_or_before": "2026-11-30"},
    ).json()["items"]
    assert [r["due_date"] for r in due] == ["2026-11-01", "2026-11-15"]
