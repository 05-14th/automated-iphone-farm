"""Pydantic v2 models for every request and response body.

Nothing goes on the wire as a bare dict. The models are also the OpenAPI
documentation: each carries examples that appear verbatim in /docs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalize import looks_like_address, normalize_address

SendMode = Literal["quick", "scheduled"]
MessageState = Literal["queued", "sending", "sent", "failed"]
Direction = Literal["outbound", "inbound"]
SuppressionReason = Literal["stop", "dnc", "bounce", "manual"]
ConsentStatus = Literal["granted", "revoked"]

Address = Annotated[str, Field(min_length=3, max_length=320, examples=["meidy765@gmail.com"])]
Body = Annotated[str, Field(min_length=1, max_length=4000, examples=["Hi - following up on your enquiry."])]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
class ErrorDetail(BaseModel):
    code: str = Field(..., examples=["refused"])
    message: str = Field(..., examples=["Refused by the pre-send gate: suppressed:stop"])
    detail: dict[str, Any] | None = Field(default=None, examples=[{"reason": "suppressed:stop"}])


class ErrorResponse(BaseModel):
    """The one error envelope. Every non-2xx response has this shape."""

    error: ErrorDetail


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------
class _SendFields(BaseModel):
    @field_validator("to", check_fields=False)
    @classmethod
    def _validate_address(cls, v: str) -> str:
        if not looks_like_address(v):
            raise ValueError(
                "must be an email address or a phone number that normalizes to E.164"
            )
        return v.strip()


class MessageItem(_SendFields):
    """One recipient + body. Used on its own and inside a bulk request."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"to": "meidy765@gmail.com", "body": "Hi - following up.", "sender_slug": "sender01"}]
        }
    )

    to: Address = Field(..., description="Recipient address. Normalized server-side before every suppression and routing check.")
    body: Body = Field(..., description="Message text.")
    sender_slug: str | None = Field(
        default=None,
        pattern=r"^sender0[1-5]$",
        description=(
            "Which sender a BRAND NEW contact is pinned to. An existing contact keeps its "
            "sticky sender forever; naming a different one returns 409, never a reroute."
        ),
        examples=["sender01"],
    )
    display_name: str | None = Field(default=None, max_length=200)

    @property
    def normalized(self) -> str | None:
        return normalize_address(self.to)


class SendRequest(MessageItem):
    """POST /v1/messages"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"to": "meidy765@gmail.com", "body": "Quick send, picked up by the worker on its next tick."},
                {
                    "to": "+17783231234",
                    "body": "Scheduled send.",
                    "mode": "scheduled",
                    "send_at": "2027-01-01T17:00:00Z",
                },
            ]
        }
    )

    mode: SendMode = Field(default="quick", description="quick = enqueue for immediate pickup. scheduled = hold until send_at.")
    send_at: datetime | None = Field(
        default=None,
        description="Required when mode=scheduled. ISO 8601 with an offset; stored as UTC. Must be in the future.",
    )

    @model_validator(mode="after")
    def _check_schedule(self) -> "SendRequest":
        if self.mode == "scheduled":
            if self.send_at is None:
                raise ValueError("send_at is required when mode='scheduled'")
            if self.send_at.tzinfo is None:
                raise ValueError("send_at must carry a timezone offset (e.g. ...Z or +00:00)")
            if self.send_at <= datetime.now(timezone.utc):
                raise ValueError("send_at must be in the future")
        elif self.send_at is not None:
            raise ValueError("send_at is only valid when mode='scheduled'")
        return self


class BulkSendRequest(BaseModel):
    """POST /v1/messages/bulk - one mode and one send_at for the whole batch."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "mode": "quick",
                    "messages": [
                        {"to": "meidy765@gmail.com", "body": "First."},
                        {"to": "+17783231234", "body": "Second."},
                    ],
                }
            ]
        }
    )

    messages: list[MessageItem] = Field(..., min_length=1, max_length=100)
    mode: SendMode = "quick"
    send_at: datetime | None = None

    @model_validator(mode="after")
    def _check_schedule(self) -> "BulkSendRequest":
        if self.mode == "scheduled":
            if self.send_at is None:
                raise ValueError("send_at is required when mode='scheduled'")
            if self.send_at.tzinfo is None:
                raise ValueError("send_at must carry a timezone offset")
            if self.send_at <= datetime.now(timezone.utc):
                raise ValueError("send_at must be in the future")
        elif self.send_at is not None:
            raise ValueError("send_at is only valid when mode='scheduled'")
        return self


class AcceptedMessage(BaseModel):
    """202 body. Accepted for sending - NOT sent. Nothing has left the Mac yet."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "0b6e2b58-9a0e-4e21-9a4f-3c4b0f1a11aa",
                    "state": "queued",
                    "mode": "quick",
                    "scheduled_for": None,
                    "conversation_id": "5d2a...",
                    "contact_id": "9f81...",
                    "sender_slug": "sender01",
                    "normalized_address": "meidy765@gmail.com",
                    "temp_guid": "temp-4f0f...",
                    "queued_at": "2026-09-15T18:20:31.115Z",
                }
            ]
        }
    )

    id: str
    state: MessageState
    mode: SendMode
    scheduled_for: datetime | None = None
    conversation_id: str
    contact_id: str
    sender_slug: str
    normalized_address: str
    temp_guid: str | None = None
    queued_at: datetime


class BulkResultItem(BaseModel):
    """One row of a bulk result. `accepted` decides which of the other two is set."""

    index: int = Field(..., description="Position in the submitted array.")
    to: str
    accepted: bool
    message: AcceptedMessage | None = None
    error: ErrorDetail | None = None


class BulkSendResponse(BaseModel):
    accepted_count: int
    refused_count: int
    results: list[BulkResultItem]


class CancelResponse(BaseModel):
    id: str
    state: MessageState
    cancelled: bool
    message: str = Field(
        default="Cancelled before pickup. Nothing was handed to BlueBubbles.",
    )


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
class SendAttempt(BaseModel):
    """One HTTP call to BlueBubbles, whatever the outcome (Defect A audit trail).

    Read the list as a story: `timeout -> reconciled_sent` is a correctly handled
    false failure; `timeout -> accepted` is a double-send.
    """

    attempt_no: int
    outcome: Literal["accepted", "timeout", "error", "reconciled_sent"]
    http_status: int | None = None
    created_at: datetime


class Message(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "0b6e2b58-9a0e-4e21-9a4f-3c4b0f1a11aa",
                    "conversation_id": "5d2a...",
                    "sender_id": "8cb4...",
                    "direction": "outbound",
                    "state": "sent",
                    "body": "Hi - following up.",
                    "provider_guid": "E81C8130-...",
                    "temp_guid": "temp-4f0f...",
                    "error_code": None,
                    "reconciled": True,
                    "scheduled_for": None,
                    "queued_at": "2026-09-15T18:20:31Z",
                    "sending_at": "2026-09-15T18:20:39Z",
                    "sent_at": "2026-09-15T18:22:39Z",
                    "delivered_at": "2026-09-15T18:22:40Z",
                    "failed_at": None,
                    "read_at": None,
                }
            ]
        }
    )

    id: str
    conversation_id: str
    sender_id: str
    direction: Direction
    state: MessageState
    body: str | None = None
    provider_guid: str | None = None
    temp_guid: str | None = None
    error_code: str | None = None
    reconciled: bool = Field(
        ...,
        description=(
            "Defect A. state='failed' with reconciled=false means the outcome is UNKNOWN, "
            "not that the message failed. Such a row must never be retried blind."
        ),
    )
    scheduled_for: datetime | None = Field(
        default=None, description="Null for a quick send. Set for a scheduled one; the worker holds it until due."
    )
    queued_at: datetime
    sending_at: datetime | None = None
    sent_at: datetime | None = Field(default=None, description="Provider ACCEPTED it. Acceptance is not delivery.")
    delivered_at: datetime | None = Field(default=None, description="Confirmed error=0 echo. Can lag sent_at by minutes.")
    failed_at: datetime | None = None
    read_at: datetime | None = None
    created_at: datetime | None = None


class MessageDetail(Message):
    attempts: list[SendAttempt] = []


class MessageList(BaseModel):
    count: int
    next_cursor: datetime | None = Field(
        default=None,
        description="Pass as ?cursor= to fetch the next (older) page. Null when the page is not full.",
    )
    messages: list[Message]


class ConversationSummary(BaseModel):
    id: str
    contact_id: str
    sender_id: str
    provider_chat_guid: str | None = Field(
        default=None,
        description="Null never means 'no chat exists' - chat/new can create the chat and still fail to return its id (Defect A).",
    )
    address: str | None = None
    display_name: str | None = None
    created_at: datetime


class ConversationList(BaseModel):
    count: int
    conversations: list[ConversationSummary]


# ---------------------------------------------------------------------------
# contacts / compliance
# ---------------------------------------------------------------------------
class SenderRef(BaseModel):
    id: str
    slug: str
    status: Literal["active", "paused", "failed"]
    paused_reason: str | None = None
    apple_email: str | None = None


class ConsentRecord(BaseModel):
    id: str
    status: ConsentStatus
    source: str
    evidence: dict[str, Any] = {}
    granted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class SuppressionRecord(BaseModel):
    id: str
    normalized_address: str
    raw_address: str | None = None
    reason: SuppressionReason
    created_at: datetime


class GateVerdict(BaseModel):
    allowed: bool
    reason: str = Field(..., examples=["ok", "suppressed:stop", "no_consent_on_record", "sender_paused:sender01"])


class ContactDetail(BaseModel):
    id: str | None = None
    normalized_address: str
    raw_address: str | None = None
    display_name: str | None = None
    sticky_sender: SenderRef | None = None
    consent: ConsentRecord | None = None
    suppression: SuppressionRecord | None = None
    can_send: GateVerdict | None = None
    created_at: datetime | None = None


class ConsentRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "address": "meidy765@gmail.com",
                    "status": "granted",
                    "source": "web_form",
                    "evidence": {"form_id": "lead-2026-09", "ip": "203.0.113.7"},
                }
            ]
        }
    )

    address: Address
    status: ConsentStatus
    source: str = Field(..., min_length=1, max_length=100, examples=["web_form", "verbal", "import", "operator"])
    evidence: dict[str, Any] = Field(default_factory=dict, description="Proof blob kept verbatim for the audit trail.")
    sender_slug: str | None = Field(
        default=None,
        pattern=r"^sender0[1-5]$",
        description="Only used if the contact does not exist yet and has to be created (and therefore pinned).",
    )
    occurred_at: datetime | None = Field(default=None, description="When consent was actually given. Defaults to now.")

    @field_validator("address")
    @classmethod
    def _addr(cls, v: str) -> str:
        if not looks_like_address(v):
            raise ValueError("must be an email address or a phone number that normalizes to E.164")
        return v.strip()


class ConsentResponse(BaseModel):
    contact_id: str
    normalized_address: str
    consent: ConsentRecord
    can_send: GateVerdict


class SuppressionRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"address": "meidy765@gmail.com", "reason": "stop"}]}
    )

    address: Address
    reason: SuppressionReason = "manual"

    @field_validator("address")
    @classmethod
    def _addr(cls, v: str) -> str:
        if normalize_address(v) is None:
            raise ValueError("address does not normalize")
        return v.strip()


class SuppressionResponse(BaseModel):
    suppression: SuppressionRecord
    created: bool = Field(..., description="False when the address was already suppressed. The call is idempotent.")


class SuppressionList(BaseModel):
    count: int
    suppressions: list[SuppressionRecord]


class SuppressionDeleteResponse(BaseModel):
    """A deletion makes someone reachable again. It is always audited."""

    deleted: SuppressionRecord
    audit: dict[str, Any] = Field(
        ...,
        examples=[{"deleted_by": "ops@example.com", "deleted_at": "2026-09-15T19:02:00Z", "source_ip": "203.0.113.7"}],
    )


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------
class QueueDepth(BaseModel):
    queued: int = Field(..., description="Outbound, due now (unscheduled, or scheduled_for already passed).")
    scheduled: int = Field(..., description="Outbound, queued but not yet due.")
    sending: int
    unreconciled_failed: int = Field(
        ...,
        description="Defect A triage queue. Must be zero before any retry sweep is safe.",
    )


class HealthResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "database": True,
                    "senders": [{"id": "8cb4...", "slug": "sender01", "status": "active", "paused_reason": None}],
                    "queue": {"queued": 0, "scheduled": 1, "sending": 0, "unreconciled_failed": 0},
                    "version": "1.0.0",
                    "checked_at": "2026-09-15T19:10:00Z",
                }
            ]
        }
    )

    status: Literal["ok", "degraded"]
    database: bool
    senders: list[SenderRef] = []
    queue: QueueDepth | None = None
    version: str
    checked_at: datetime
    note: str | None = None


class StatsResponse(BaseModel):
    since: datetime
    until: datetime
    outbound: dict[str, int]
    inbound: dict[str, int]
    totals: dict[str, int]
