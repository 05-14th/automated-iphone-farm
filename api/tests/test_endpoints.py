"""Endpoint behaviour against an in-memory stub of the database.

These cover the routing and gate-handling logic. The database's own invariants
(sticky routing, FIFO, the state machine) are exercised for real in
`test_integration_live.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

FUTURE = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()


def test_quick_send_returns_202_not_200(client):
    """202 is load-bearing: nothing has been sent at this point."""
    resp = client.post("/v1/messages", json={"to": "a@example.com", "body": "hi"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "queued"
    assert body["mode"] == "quick"
    assert body["scheduled_for"] is None
    assert body["temp_guid"].startswith("temp-")  # Defect A reconciliation key
    assert body["sender_slug"] == "sender01"


def test_scheduled_send_stores_the_instant(client):
    resp = client.post(
        "/v1/messages", json={"to": "b@example.com", "body": "hi", "mode": "scheduled", "send_at": FUTURE}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["mode"] == "scheduled"
    assert body["scheduled_for"] is not None
    assert body["state"] == "queued"  # scheduled is not a state; it is a due time


def test_gate_refusal_is_403_with_the_reason(client, stub_db):
    contact, _, _ = None, None, None
    client.post("/v1/messages", json={"to": "c@example.com", "body": "hi"})
    contact = stub_db.contacts["c@example.com"]
    stub_db.verdicts[contact["id"]] = {"allowed": False, "reason": "suppressed:stop"}

    resp = client.post("/v1/messages", json={"to": "c@example.com", "body": "hi"})
    assert resp.status_code == 403
    assert resp.json()["error"]["detail"]["reason"] == "suppressed:stop"


def test_in_flight_is_409_not_403(client, stub_db):
    """FIFO is an ordering problem (retry later), not a policy refusal."""
    client.post("/v1/messages", json={"to": "d@example.com", "body": "hi"})
    contact = stub_db.contacts["d@example.com"]
    stub_db.verdicts[contact["id"]] = {"allowed": False, "reason": "in_flight_message_exists"}

    resp = client.post("/v1/messages", json={"to": "d@example.com", "body": "again"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "in_flight_message_exists"


def test_sticky_mismatch_is_409_and_never_reroutes(client, stub_db):
    client.post("/v1/messages", json={"to": "e@example.com", "body": "hi"})
    stub_db.contacts["e@example.com"]["sticky_sender_id"] = "some-other-sender"

    resp = client.post("/v1/messages", json={"to": "e@example.com", "body": "hi", "sender_slug": "sender01"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "sticky_sender_mismatch"


def test_bulk_does_not_abort_on_one_refusal(client, stub_db):
    """The whole point of the bulk endpoint: one bad item must not kill the batch."""
    client.post("/v1/messages", json={"to": "blocked@example.com", "body": "seed"})
    blocked = stub_db.contacts["blocked@example.com"]["id"]
    stub_db.verdicts[blocked] = {"allowed": False, "reason": "no_consent_on_record"}

    resp = client.post(
        "/v1/messages/bulk",
        json={
            "messages": [
                {"to": "ok1@example.com", "body": "one"},
                {"to": "blocked@example.com", "body": "two"},
                {"to": "ok2@example.com", "body": "three"},
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted_count"] == 2
    assert body["refused_count"] == 1
    assert [r["accepted"] for r in body["results"]] == [True, False, True]
    assert body["results"][1]["error"]["detail"]["reason"] == "no_consent_on_record"
    # Indices must survive so the caller can line results up with its input.
    assert [r["index"] for r in body["results"]] == [0, 1, 2]
    # A refused item carries no message, an accepted one carries no error.
    assert body["results"][1]["message"] is None
    assert body["results"][0]["error"] is None


def test_bulk_scheduling_applies_to_every_item(client):
    resp = client.post(
        "/v1/messages/bulk",
        json={
            "mode": "scheduled",
            "send_at": FUTURE,
            "messages": [{"to": "s1@example.com", "body": "a"}, {"to": "s2@example.com", "body": "b"}],
        },
    )
    assert resp.status_code == 200
    for r in resp.json()["results"]:
        assert r["accepted"] is True
        assert r["message"]["mode"] == "scheduled"
        assert r["message"]["scheduled_for"] is not None


def test_cancel_a_queued_message(client):
    created = client.post("/v1/messages", json={"to": "f@example.com", "body": "hi"}).json()
    resp = client.delete(f"/v1/messages/{created['id']}")
    assert resp.status_code == 200
    assert resp.json() == {
        "id": created["id"],
        "state": "failed",
        "cancelled": True,
        "message": "Cancelled before pickup. Nothing was handed to BlueBubbles.",
    }


def test_cancel_is_409_once_it_is_no_longer_queued(client, stub_db):
    created = client.post("/v1/messages", json={"to": "g@example.com", "body": "hi"}).json()
    stub_db.messages[created["id"]]["state"] = "sending"

    resp = client.delete(f"/v1/messages/{created['id']}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "not_cancellable"


def test_cancel_unknown_message_is_404(client):
    resp = client.delete("/v1/messages/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_error_envelope_is_uniform(client):
    for resp in (
        client.get("/v1/messages/00000000-0000-0000-0000-000000000000"),
        client.post("/v1/messages", json={"to": "bad", "body": "x"}),
        client.get("/v1/messages", headers={"Authorization": "Bearer nope"}),
    ):
        body = resp.json()
        assert set(body) == {"error"}
        assert "code" in body["error"] and "message" in body["error"]


def test_health_needs_no_token_and_reports_the_queue(client):
    resp = client.get("/v1/health", headers={"Authorization": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"] is True
    assert set(body["queue"]) == {"queued", "scheduled", "sending", "unreconciled_failed"}


def test_openapi_documents_every_route(client):
    spec = client.get("/openapi.json").json()
    expected = {
        "/v1/messages",
        "/v1/messages/bulk",
        "/v1/messages/{message_id}",
        "/v1/inbox",
        "/v1/inbox/{message_id}/read",
        "/v1/conversations",
        "/v1/conversations/{conversation_id}/messages",
        "/v1/contacts/{address}",
        "/v1/consents",
        "/v1/suppressions",
        "/v1/suppressions/{address}",
        "/v1/health",
        "/v1/stats",
    }
    assert expected <= set(spec["paths"])
    # Every documented operation has a summary and a description.
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            assert op.get("summary"), f"{method.upper()} {path} has no summary"
            assert op.get("description"), f"{method.upper()} {path} has no description"
