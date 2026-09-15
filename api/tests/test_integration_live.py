"""LIVE integration test against the real Supabase project.

It creates real rows, asserts against them, and DELETES EVERYTHING IT CREATED in
a `finally` block, whatever happens.

Safety rules this test obeys:

* Addresses are synthetic (`api-live-test-<uuid>@example.com`). `example.com` is
  reserved by RFC 2606 and is not registered with Apple, so even if the Node
  worker on this Mac claims a queued row, **no message can reach a person**.
* It never starts the worker and never calls BlueBubbles.
* The scheduled message it creates is a year out and is cancelled immediately.

Run it with:  .venv/bin/python -m pytest tests -m live -q
Skip it with: .venv/bin/python -m pytest tests -m "not live" -q
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db import Database

pytestmark = pytest.mark.live

LIVE = bool(os.environ.get("SUPABASE_URL", "").startswith("https://")) and "unit-test" not in os.environ.get(
    "SUPABASE_URL", ""
)


@pytest.fixture()
def live_client():
    """TestClient with the REAL database - no dependency override."""
    if not LIVE:
        pytest.skip("No live SUPABASE_URL configured (api/.env).")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {get_settings().api_token}"})
        yield c


async def _hard_delete(db: Database, normalized: str) -> None:
    """Remove every row this test could have created, in FK-safe order."""
    contact = await db.get_contact_by_address(normalized)
    if contact:
        convs = (
            await db.table("conversations").select("id").eq("contact_id", contact["id"]).execute()
        ).data or []
        for conv in convs:
            msgs = (
                await db.table("messages").select("id").eq("conversation_id", conv["id"]).execute()
            ).data or []
            for m in msgs:
                await db.table("send_attempts").delete().eq("message_id", m["id"]).execute()
            await db.table("messages").delete().eq("conversation_id", conv["id"]).execute()
        await db.table("conversations").delete().eq("contact_id", contact["id"]).execute()
        await db.table("consents").delete().eq("contact_id", contact["id"]).execute()
        await db.table("contacts").delete().eq("id", contact["id"]).execute()
    await db.table("suppressions").delete().eq("normalized_address", normalized).execute()
    await db.table("suppression_deletions").delete().eq("normalized_address", normalized).execute()


async def _wait_until_settled(db: Database, message_id: str, seconds: int = 180) -> str:
    """If the running Node worker claimed the row, let it finish before deleting.

    Deleting a row out from under an in-flight send would leave the worker with
    nothing to reconcile against - exactly the ambiguity the whole design exists
    to avoid. So we wait it out instead.
    """
    for _ in range(seconds):
        row = await db.get_message(message_id)
        if row is None or row["state"] != "sending":
            return (row or {}).get("state", "deleted")
        await asyncio.sleep(1)
    return "sending"


async def test_full_lifecycle_against_live_supabase(live_client):
    settings = get_settings()
    db = Database(settings)
    await db.connect()

    address = f"api-live-test-{uuid.uuid4()}@example.com"
    normalized = address.lower()
    created_message_ids: list[str] = []

    try:
        # --- 1. The gate refuses an unknown contact: no consent on record ----
        resp = live_client.post("/v1/messages", json={"to": address, "body": "should be refused"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["detail"]["reason"] == "no_consent_on_record"

        # --- 2. Consent ledger ----------------------------------------------
        resp = live_client.post(
            "/v1/consents",
            json={
                "address": address,
                "status": "granted",
                "source": "integration_test",
                "evidence": {"test": "api/tests/test_integration_live.py"},
            },
        )
        assert resp.status_code == 201, resp.text
        consent = resp.json()
        contact_id = consent["contact_id"]
        assert consent["can_send"] == {"allowed": True, "reason": "ok"}
        assert consent["consent"]["granted_at"] is not None
        assert consent["consent"]["revoked_at"] is None

        # --- 3. Contact detail: sticky sender, consent, live verdict ---------
        resp = live_client.get(f"/v1/contacts/{address.upper()}")  # un-normalized on purpose
        assert resp.status_code == 200, resp.text
        contact = resp.json()
        assert contact["normalized_address"] == normalized
        assert contact["sticky_sender"]["slug"].startswith("sender0")
        assert contact["suppression"] is None
        assert contact["can_send"]["allowed"] is True

        # --- 4. A scheduled send lands with scheduled_for set ----------------
        send_at = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        resp = live_client.post(
            "/v1/messages",
            json={"to": address, "body": "live integration test", "mode": "scheduled", "send_at": send_at},
        )
        assert resp.status_code == 202, resp.text
        message = resp.json()
        created_message_ids.append(message["id"])
        assert message["state"] == "queued"
        assert message["mode"] == "scheduled"
        assert message["scheduled_for"] is not None
        assert message["temp_guid"].startswith("temp-")

        # The database really stored it.
        row = await db.get_message(message["id"])
        assert row["scheduled_for"] is not None
        assert row["direction"] == "outbound"

        # --- 5. FIFO: a second message on the same conversation is refused ---
        resp = live_client.post("/v1/messages", json={"to": address, "body": "second"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "in_flight_message_exists"

        # --- 6. Cancel it ----------------------------------------------------
        current = await db.get_message(message["id"])
        if current["state"] == "queued":
            resp = live_client.delete(f"/v1/messages/{message['id']}")
            assert resp.status_code == 200, resp.text
            assert resp.json()["cancelled"] is True
            after = await db.get_message(message["id"])
            assert after["state"] == "failed"
            assert after["error_code"] == "cancelled_by_api"
            # reconciled=true is required: nothing was handed to the provider, so
            # this row must NOT sit in the Defect A triage queue.
            assert after["reconciled"] is True
        else:
            # The Node worker on this Mac claimed the row before the cancel. That
            # is the documented gap: it does not yet honour scheduled_for.
            resp = live_client.delete(f"/v1/messages/{message['id']}")
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "not_cancellable"
            pytest.skip(
                "The running Node worker claimed the scheduled message before it could be "
                "cancelled - it does not yet filter on scheduled_for. See api/README.md, "
                "'Required Node worker change'."
            )

        # --- 7. Inbound: the inbox reads what the webhook wrote ---------------
        conversation_id = message["conversation_id"]
        guid = f"api-live-test-{uuid.uuid4()}"
        inbound = (
            await db.table("messages")
            .insert(
                {
                    "conversation_id": conversation_id,
                    "sender_id": contact["sticky_sender"]["id"],
                    "direction": "inbound",
                    "body": "synthetic inbound reply",
                    "state": "sent",
                    "provider_guid": guid,
                }
            )
            .execute()
        ).data[0]
        created_message_ids.append(inbound["id"])

        resp = live_client.get(f"/v1/inbox?contact={address}")
        assert resp.status_code == 200, resp.text
        inbox = resp.json()
        assert inbox["count"] == 1
        assert inbox["messages"][0]["id"] == inbound["id"]
        assert inbox["messages"][0]["direction"] == "inbound"
        assert inbox["messages"][0]["read_at"] is None

        # Defect B: a duplicate webhook with the SAME guid must be rejected.
        with pytest.raises(Exception) as exc:
            await db.table("messages").insert(
                {
                    "conversation_id": conversation_id,
                    "sender_id": contact["sticky_sender"]["id"],
                    "direction": "inbound",
                    "body": "synthetic inbound reply",
                    "state": "sent",
                    "provider_guid": guid,
                }
            ).execute()
        assert getattr(exc.value, "code", None) == "23505"

        # --- 8. Read flag -----------------------------------------------------
        assert live_client.get(f"/v1/inbox?contact={address}&unread_only=true").json()["count"] == 1
        resp = live_client.post(f"/v1/inbox/{inbound['id']}/read")
        assert resp.status_code == 200, resp.text
        assert resp.json()["read_at"] is not None
        assert live_client.get(f"/v1/inbox?contact={address}&unread_only=true").json()["count"] == 0

        # --- 9. Threaded view -------------------------------------------------
        resp = live_client.get(f"/v1/conversations/{conversation_id}/messages")
        assert resp.status_code == 200
        assert {m["direction"] for m in resp.json()["messages"]} == {"outbound", "inbound"}

        # --- 10. Suppression outranks consent ---------------------------------
        resp = live_client.post("/v1/suppressions", json={"address": address.upper(), "reason": "stop"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["created"] is True
        # Normalization is what makes suppression actually work.
        assert resp.json()["suppression"]["normalized_address"] == normalized
        # Idempotent.
        assert live_client.post("/v1/suppressions", json={"address": address, "reason": "stop"}).json()[
            "created"
        ] is False

        verdict = await db.can_send(contact_id)
        assert verdict == {"allowed": False, "reason": "suppressed:stop"}

        resp = live_client.post("/v1/messages", json={"to": address, "body": "must not be accepted"})
        assert resp.status_code == 403
        assert resp.json()["error"]["detail"]["reason"] == "suppressed:stop"

        # --- 11. Deleting a suppression is audited ----------------------------
        resp = live_client.delete(
            f"/v1/suppressions/{address}?note=integration+test",
            headers={"X-Actor": "pytest@integration.test"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["audit"]["deleted_by"] == "pytest@integration.test"

        audit = (
            await db.table("suppression_deletions")
            .select("*")
            .eq("normalized_address", normalized)
            .execute()
        ).data
        assert len(audit) == 1
        assert audit[0]["deleted_by"] == "pytest@integration.test"
        assert audit[0]["reason"] == "stop"
        assert audit[0]["note"] == "integration test"

        assert await db.get_suppression(normalized) is None

    finally:
        for mid in created_message_ids:
            await _wait_until_settled(db, mid)
        await _hard_delete(db, normalized)
        # Prove the cleanup worked.
        assert await db.get_contact_by_address(normalized) is None
        assert await db.get_suppression(normalized) is None
        leftovers = (
            await db.table("suppression_deletions")
            .select("id")
            .eq("normalized_address", normalized)
            .execute()
        ).data
        assert leftovers == []
        await db.close()
