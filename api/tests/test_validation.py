"""Request validation, especially the scheduling rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas import BulkSendRequest, SendRequest

FUTURE = datetime.now(timezone.utc) + timedelta(days=365)
PAST = datetime.now(timezone.utc) - timedelta(days=1)


def test_quick_is_the_default():
    req = SendRequest(to="a@example.com", body="hi")
    assert req.mode == "quick"
    assert req.send_at is None


def test_scheduled_requires_send_at():
    with pytest.raises(ValidationError, match="send_at is required"):
        SendRequest(to="a@example.com", body="hi", mode="scheduled")


def test_scheduled_rejects_a_past_send_at():
    with pytest.raises(ValidationError, match="must be in the future"):
        SendRequest(to="a@example.com", body="hi", mode="scheduled", send_at=PAST)


def test_scheduled_rejects_a_naive_send_at():
    naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone offset"):
        SendRequest(to="a@example.com", body="hi", mode="scheduled", send_at=naive)


def test_quick_rejects_a_send_at():
    """Silently ignoring send_at on a quick send would be the worst outcome."""
    with pytest.raises(ValidationError, match="only valid when mode='scheduled'"):
        SendRequest(to="a@example.com", body="hi", send_at=FUTURE)


def test_scheduled_accepts_a_future_send_at():
    req = SendRequest(to="a@example.com", body="hi", mode="scheduled", send_at=FUTURE)
    assert req.send_at == FUTURE


def test_body_must_not_be_empty():
    with pytest.raises(ValidationError):
        SendRequest(to="a@example.com", body="")


@pytest.mark.parametrize("bad", ["not-an-address", "@example.com", "12"])
def test_address_must_be_plausible(bad):
    with pytest.raises(ValidationError, match="normalizes to E.164|at least"):
        SendRequest(to=bad, body="hi")


@pytest.mark.parametrize("slug", ["sender06", "sender1", "SENDER01", "admin"])
def test_sender_slug_is_constrained(slug):
    with pytest.raises(ValidationError):
        SendRequest(to="a@example.com", body="hi", sender_slug=slug)


def test_bulk_caps_at_100():
    items = [{"to": "a@example.com", "body": "x"}] * 101
    with pytest.raises(ValidationError, match="at most 100"):
        BulkSendRequest(messages=items)


def test_bulk_needs_at_least_one():
    with pytest.raises(ValidationError):
        BulkSendRequest(messages=[])


def test_bulk_scheduling_rules_match_the_single_send():
    with pytest.raises(ValidationError, match="send_at is required"):
        BulkSendRequest(messages=[{"to": "a@example.com", "body": "x"}], mode="scheduled")
    with pytest.raises(ValidationError, match="must be in the future"):
        BulkSendRequest(messages=[{"to": "a@example.com", "body": "x"}], mode="scheduled", send_at=PAST)
    ok = BulkSendRequest(messages=[{"to": "a@example.com", "body": "x"}], mode="scheduled", send_at=FUTURE)
    assert ok.send_at == FUTURE
