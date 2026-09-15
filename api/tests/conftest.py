"""Shared fixtures.

Unit tests never touch the network: `get_db` is overridden with a stub and the
real `Database.connect` is patched out. The live integration test
(`test_integration_live.py`) opts back in explicitly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

# Load api/.env first (the live integration test needs the real values), then
# fill any gap with dummies so the unit tests run on a machine with no .env.
try:  # pragma: no cover - convenience only
    from dotenv import load_dotenv

    load_dotenv(API_DIR / ".env")
except ImportError:  # pragma: no cover
    pass

os.environ.setdefault("SUPABASE_URL", "https://unit-test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "unit-test-key")
os.environ.setdefault("API_TOKEN", "unit-test-token-0123456789abcdef")
os.environ.setdefault("DEFAULT_SENDER_SLUG", "sender01")

TEST_TOKEN = os.environ["API_TOKEN"]

SENDER = {
    "id": "11111111-1111-1111-1111-111111111111",
    "slug": "sender01",
    "apple_email": "sender@example.com",
    "status": "active",
    "paused_reason": None,
    "bluebubbles_url": "http://127.0.0.1",
    "bluebubbles_port": 12341,
    "macos_user": "imsg01",
    "created_at": "2026-09-01T00:00:00Z",
}


class StubDatabase:
    """In-memory stand-in for `Database`, with the same method surface.

    Only the behaviour the tests assert on is modelled; anything unmodelled
    raises, so a test cannot silently pass against a hole in the stub.
    """

    def __init__(self) -> None:
        self.contacts: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.suppressed: set[str] = set()
        self.consented: set[str] = set()
        self.verdicts: dict[str, dict[str, Any]] = {}
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:08d}"

    async def ping(self) -> bool:
        return True

    async def get_senders(self, only_active: bool = False):
        return [SENDER]

    async def get_sender_by_slug(self, slug: str):
        return SENDER if slug == "sender01" else None

    async def get_sender_by_id(self, sender_id: str):
        return SENDER if sender_id == SENDER["id"] else None

    async def get_contact_by_address(self, normalized: str):
        return self.contacts.get(normalized)

    async def resolve_contact(self, raw_address: str, default_sender_id: str, display_name=None):
        from app.normalize import normalize_address

        normalized = normalize_address(raw_address)
        existing = self.contacts.get(normalized)
        if existing:
            return existing, False, existing["sticky_sender_id"] != default_sender_id
        row = {
            "id": self._id("contact"),
            "normalized_address": normalized,
            "raw_address": raw_address,
            "display_name": display_name,
            "sticky_sender_id": default_sender_id,
            "created_at": "2026-09-15T00:00:00Z",
        }
        self.contacts[normalized] = row
        return row, True, False

    async def resolve_conversation(self, contact: dict[str, Any]):
        key = contact["id"]
        if key not in self.conversations:
            self.conversations[key] = {
                "id": self._id("conv"),
                "contact_id": contact["id"],
                "sender_id": contact["sticky_sender_id"],
                "provider_chat_guid": None,
                "created_at": "2026-09-15T00:00:00Z",
            }
        return self.conversations[key]

    async def can_send(self, contact_id: str):
        return self.verdicts.get(contact_id, {"allowed": True, "reason": "ok"})

    async def enqueue_outbound(self, conversation_id, sender_id, body, scheduled_for=None):
        row = {
            "id": self._id("msg"),
            "conversation_id": conversation_id,
            "sender_id": sender_id,
            "direction": "outbound",
            "body": body,
            "state": "queued",
            "provider_guid": None,
            "temp_guid": f"temp-{self._id('t')}",
            "error_code": None,
            "reconciled": False,
            "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
            "queued_at": "2026-09-15T00:00:00Z",
            "sending_at": None,
            "sent_at": None,
            "delivered_at": None,
            "failed_at": None,
            "read_at": None,
            "created_at": "2026-09-15T00:00:00Z",
        }
        self.messages[row["id"]] = row
        return row

    async def get_message(self, message_id: str):
        return self.messages.get(message_id)

    async def get_attempts(self, message_id: str):
        return []

    async def cancel_queued_message(self, message_id: str):
        row = self.messages.get(message_id)
        if not row or row["state"] != "queued":
            return None
        row["state"] = "failed"
        row["error_code"] = "cancelled_by_api"
        row["reconciled"] = True
        row["failed_at"] = "2026-09-15T00:00:01Z"
        return row

    async def list_messages(self, **kwargs):
        return list(self.messages.values())

    async def queue_depth(self):
        return {"queued": 0, "scheduled": 0, "sending": 0, "unreconciled_failed": 0}

    async def get_suppression(self, normalized: str):
        return None

    async def latest_consent(self, contact_id: str):
        return None


@pytest.fixture()
def stub_db() -> StubDatabase:
    return StubDatabase()


@pytest.fixture()
def client(stub_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app import db as db_module
    from app.deps import get_db
    from app.main import app

    async def _noop(self):
        return None

    monkeypatch.setattr(db_module.Database, "connect", _noop)
    monkeypatch.setattr(db_module.Database, "close", _noop)

    app.dependency_overrides[get_db] = lambda: stub_db
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TEST_TOKEN}"})
        yield c
    app.dependency_overrides.clear()
