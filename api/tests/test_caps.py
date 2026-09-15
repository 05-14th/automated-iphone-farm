"""Operating caps [guide step 36] as the API surfaces them.

Two things are being pinned down here:

1. A cap-blocked message is **accepted**, not refused. It is enqueued, stays
   `queued`, and the worker sends it when the window reopens. Returning 403 would
   tell the caller to re-post it later - which is exactly how a system built to
   never send twice ends up sending twice.
2. A suppression or consent refusal still 403s, unchanged. The distinction
   between "no" and "not yet" is the whole point.
"""

from __future__ import annotations

from tests.conftest import SENDER, cap_status, cap_window

CAP_REASONS = ["cap_daily", "cap_hourly", "cap_new_conversation", "quiet_hours"]
RETRY_AT = "2026-09-16T08:00:00+00:00"


def _seed_contact(client, stub_db, address):
    client.post("/v1/messages", json={"to": address, "body": "seed"})
    return stub_db.contacts[address]["id"]


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------
def test_cap_blocked_message_is_accepted_not_refused(client, stub_db):
    for reason in CAP_REASONS:
        address = f"{reason}@example.com"
        contact_id = _seed_contact(client, stub_db, address)
        stub_db.verdicts[contact_id] = {
            "allowed": False,
            "reason": reason,
            "retry_after": RETRY_AT,
        }

        resp = client.post("/v1/messages", json={"to": address, "body": "hi"})
        assert resp.status_code == 202, f"{reason} must NOT be refused"
        body = resp.json()
        assert body["state"] == "queued"
        assert body["cap_blocked"] is True
        assert body["cap_reason"] == reason
        assert body["retry_after"].startswith("2026-09-16T08:00:00")


def test_cap_blocked_message_is_really_enqueued(client, stub_db):
    """Accepting it without writing a row would silently drop the message."""
    contact_id = _seed_contact(client, stub_db, "enqueued@example.com")
    before = len(stub_db.messages)
    stub_db.verdicts[contact_id] = {"allowed": False, "reason": "cap_hourly", "retry_after": RETRY_AT}

    resp = client.post("/v1/messages", json={"to": "enqueued@example.com", "body": "hi"})
    assert resp.status_code == 202
    assert len(stub_db.messages) == before + 1
    assert stub_db.messages[resp.json()["id"]]["state"] == "queued"


def test_an_unblocked_send_reports_no_cap(client):
    body = client.post("/v1/messages", json={"to": "clear@example.com", "body": "hi"}).json()
    assert body["cap_blocked"] is False
    assert body["cap_reason"] is None
    assert body["retry_after"] is None


def test_cap_without_retry_after_still_accepted(client, stub_db):
    """retry_after is advisory; a cap of 0 has no meaningful one."""
    contact_id = _seed_contact(client, stub_db, "nocap-retry@example.com")
    stub_db.verdicts[contact_id] = {"allowed": False, "reason": "cap_daily"}

    body = client.post("/v1/messages", json={"to": "nocap-retry@example.com", "body": "hi"}).json()
    assert body["cap_blocked"] is True
    assert body["retry_after"] is None


def test_suppression_is_still_403_not_a_cap(client, stub_db):
    """The distinction that matters: "no" is not "not yet"."""
    contact_id = _seed_contact(client, stub_db, "stopped@example.com")
    stub_db.verdicts[contact_id] = {"allowed": False, "reason": "suppressed:stop"}

    resp = client.post("/v1/messages", json={"to": "stopped@example.com", "body": "hi"})
    assert resp.status_code == 403
    assert resp.json()["error"]["detail"]["reason"] == "suppressed:stop"


def test_in_flight_is_still_409(client, stub_db):
    contact_id = _seed_contact(client, stub_db, "fifo@example.com")
    stub_db.verdicts[contact_id] = {"allowed": False, "reason": "in_flight_message_exists"}

    resp = client.post("/v1/messages", json={"to": "fifo@example.com", "body": "hi"})
    assert resp.status_code == 409


def test_bulk_counts_a_cap_blocked_item_as_accepted(client, stub_db):
    contact_id = _seed_contact(client, stub_db, "bulkcap@example.com")
    stub_db.verdicts[contact_id] = {"allowed": False, "reason": "cap_daily", "retry_after": RETRY_AT}

    resp = client.post(
        "/v1/messages/bulk",
        json={"messages": [{"to": "bulkcap@example.com", "body": "one"}, {"to": "bulkok@example.com", "body": "two"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted_count"] == 2
    assert body["refused_count"] == 0
    assert body["results"][0]["message"]["cap_blocked"] is True
    assert body["results"][1]["message"]["cap_blocked"] is False


# ---------------------------------------------------------------------------
# GET /v1/senders
# ---------------------------------------------------------------------------
def test_senders_lists_caps_and_usage(client):
    resp = client.get("/v1/senders")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1

    sender = body["senders"][0]
    assert sender["slug"] == "sender01"
    assert sender["status"] == "active"
    assert sender["timezone"] == "America/Vancouver"

    caps = sender["caps"]
    assert caps["daily"]["cap"] == 200
    assert caps["daily"]["used"] == 3
    assert caps["daily"]["remaining"] == 197
    assert caps["hourly"]["window_seconds"] == 3600
    assert caps["new_conversation_daily"]["cap"] == 20
    assert caps["quiet_hours"]["timezone"] == "America/Vancouver"
    assert sender["blocked_by"] is None


def test_senders_names_the_binding_cap(client, stub_db):
    stub_db.cap_status = cap_status(
        hourly=cap_window(cap=30, used=30, window_seconds=3600, exceeded=True, retry_after=RETRY_AT)
    )
    sender = client.get("/v1/senders").json()["senders"][0]
    assert sender["blocked_by"] == "cap_hourly"
    assert sender["caps"]["hourly"]["remaining"] == 0
    assert sender["caps"]["hourly"]["retry_after"].startswith("2026-09-16T08:00:00")


def test_quiet_hours_outrank_a_blown_cap(client, stub_db):
    """Both can be true at once; the one that governs is reported."""
    stub_db.cap_status = cap_status(
        daily=cap_window(cap=200, used=200, exceeded=True, retry_after=RETRY_AT),
        quiet={
            "start": "21:00:00",
            "end": "08:00:00",
            "timezone": "America/Vancouver",
            "configured": True,
            "in_quiet_hours": True,
            "resumes_at": RETRY_AT,
            "next_quiet_end": RETRY_AT,
        },
    )
    sender = client.get("/v1/senders").json()["senders"][0]
    assert sender["blocked_by"] == "quiet_hours"
    assert sender["caps"]["quiet_hours"]["in_quiet_hours"] is True


def test_daily_is_reported_ahead_of_hourly(client, stub_db):
    """The daily window frees up last; reporting hourly would be optimistic."""
    stub_db.cap_status = cap_status(
        daily=cap_window(cap=200, used=200, exceeded=True, retry_after=RETRY_AT),
        hourly=cap_window(cap=30, used=30, window_seconds=3600, exceeded=True, retry_after=RETRY_AT),
    )
    assert client.get("/v1/senders").json()["senders"][0]["blocked_by"] == "cap_daily"


def test_no_limit_is_reported_as_null(client, stub_db):
    stub_db.cap_status = cap_status(daily=cap_window(cap=None, used=9000))
    caps = client.get("/v1/senders").json()["senders"][0]["caps"]
    assert caps["daily"]["cap"] is None
    assert caps["daily"]["remaining"] is None
    assert caps["daily"]["exceeded"] is False


def test_usage_can_be_switched_off(client):
    sender = client.get("/v1/senders?usage=false").json()["senders"][0]
    assert sender["caps"] is None
    assert sender["blocked_by"] is None


def test_single_sender_lookup(client):
    resp = client.get(f"/v1/senders/{SENDER['slug']}")
    assert resp.status_code == 200
    assert resp.json()["caps"]["daily"]["cap"] == 200


def test_unknown_sender_is_404(client):
    assert client.get("/v1/senders/sender05").status_code == 404


def test_senders_requires_a_token(client):
    resp = client.get("/v1/senders", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
