"""The enqueue path, shared by the single and bulk send endpoints.

This module is the whole "sending" story on the API side, and it is short,
because **the API does not send**. It resolves a contact, resolves the
conversation, asks the database's `can_send()` gate for permission, and inserts
a `queued` row. The Node worker on the Mac mini claims that row and is the only
thing that ever talks to BlueBubbles.

Every refusal below comes from the database, not from logic reimplemented here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from postgrest.exceptions import APIError

from .db import Database
from .errors import ApiError, bad_request, conflict, refused, upstream_error
from .logging_config import log_context
from .schemas import AcceptedMessage, MessageItem, SendMode

log = logging.getLogger("api.sending")

# `can_send()` reasons that are an ordering/timing problem rather than a policy
# decision. These get 409 (try again later); everything else gets 403.
TRANSIENT_REASONS = {"in_flight_message_exists"}

# Operating caps and quiet hours [guide step 36]. These are NOT refusals: the
# message is accepted, enqueued, and left `queued`; the worker sends it when the
# window reopens. A 403 here would be a lie - the caller would think the message
# was rejected and would re-post it later, which is how you get duplicates from a
# system whose whole point is not to send twice. The response is 202 with the cap
# reason and a retry_after attached, so the caller knows why nothing has moved.
CAP_REASONS = {"cap_daily", "cap_hourly", "cap_new_conversation", "quiet_hours"}


@dataclass(slots=True)
class EnqueueOutcome:
    accepted: bool
    message: AcceptedMessage | None = None
    error: ApiError | None = None


async def enqueue(
    db: Database,
    item: MessageItem,
    *,
    mode: SendMode,
    send_at: datetime | None,
    default_sender_slug: str,
) -> EnqueueOutcome:
    """Resolve, gate, and enqueue one message.

    Returns an outcome rather than raising, so a bulk call can refuse one item
    without aborting the batch. The single-message endpoint re-raises.
    """
    try:
        return await _enqueue(db, item, mode=mode, send_at=send_at, default_sender_slug=default_sender_slug)
    except ApiError as err:
        return EnqueueOutcome(accepted=False, error=err)
    except APIError as err:
        # A PostgREST/Postgres error. The message is safe to show (it is written
        # by our own triggers and says which invariant was violated); the URL and
        # key are not in it.
        log_context(log, logging.ERROR, "database refused enqueue", code=getattr(err, "code", None))
        return EnqueueOutcome(
            accepted=False,
            error=ApiError(
                422,
                "database_rejected",
                getattr(err, "message", "The database rejected this write."),
                {"pg_code": getattr(err, "code", None)},
            ),
        )


async def _enqueue(
    db: Database,
    item: MessageItem,
    *,
    mode: SendMode,
    send_at: datetime | None,
    default_sender_slug: str,
) -> EnqueueOutcome:
    normalized = item.normalized
    if not normalized:
        raise bad_request("Address does not normalize.", to=item.to)

    if mode == "scheduled":
        if send_at is None:
            raise ApiError(422, "send_at_required", "mode='scheduled' requires send_at.")
        if send_at <= datetime.now(timezone.utc):
            raise ApiError(422, "send_at_in_past", "send_at must be in the future.")

    # 1. Which sender would a BRAND NEW contact be pinned to?
    slug = item.sender_slug or default_sender_slug
    sender = await db.get_sender_by_slug(slug)
    if sender is None:
        raise bad_request(f"Unknown sender_slug '{slug}'.", sender_slug=slug)

    # 2. Contact, honouring sticky routing.
    contact, _created, sticky_mismatch = await db.resolve_contact(
        item.to, sender["id"], item.display_name
    )

    if sticky_mismatch:
        # The caller named a sender this contact is not pinned to. Refuse loudly.
        # Do NOT reroute, and do NOT silently substitute the sticky sender - the
        # caller asked for something impossible and needs to know that.
        sticky = await db.get_sender_by_id(contact["sticky_sender_id"])
        raise conflict(
            "sticky_sender_mismatch",
            "This contact is permanently pinned to another sender. Rerouting is never "
            "performed: the recipient would see the conversation continue from a different "
            "identity, which is the worst trust failure available in this channel.",
            requested_sender=slug,
            sticky_sender=(sticky or {}).get("slug", contact["sticky_sender_id"]),
            contact_id=contact["id"],
        )

    # 3. Conversation (the (contact, sender) thread; FIFO is scoped here).
    conversation = await db.resolve_conversation(contact)

    # 4. THE GATE. Suppression, consent, sender status, sticky match, caps, in-flight.
    verdict = await db.can_send(contact["id"])
    cap_reason: str | None = None
    retry_after: datetime | None = None

    if not verdict.get("allowed"):
        reason = verdict.get("reason", "refused")

        if reason in CAP_REASONS:
            # Cap-blocked, not refused. Fall through and enqueue: the whole point
            # of a cap is that the message goes out later, unchanged.
            cap_reason = reason
            retry_after = _parse_ts(verdict.get("retry_after"))
            log_context(
                log,
                logging.INFO,
                "accepted but cap-blocked",
                contact_id=contact["id"],
                reason=reason,
                retry_after=verdict.get("retry_after"),
            )
        elif reason in TRANSIENT_REASONS:
            raise conflict(
                "in_flight_message_exists",
                "Another message is already queued or sending on this conversation. "
                "Per-conversation FIFO is enforced by the database; try again once it clears.",
                reason=reason,
                contact_id=contact["id"],
                conversation_id=conversation["id"],
            )
        else:
            raise refused(reason, contact_id=contact["id"], conversation_id=conversation["id"])

    # 5. Enqueue. Nothing has been sent; the worker does that.
    row = await db.enqueue_outbound(
        conversation_id=conversation["id"],
        sender_id=contact["sticky_sender_id"],
        body=item.body,
        scheduled_for=send_at if mode == "scheduled" else None,
    )

    sticky_sender = (
        sender if sender["id"] == contact["sticky_sender_id"] else await db.get_sender_by_id(contact["sticky_sender_id"])
    )

    log_context(
        log,
        logging.INFO,
        "enqueued",
        message_id=row["id"],
        contact_id=contact["id"],
        sender=(sticky_sender or {}).get("slug"),
        mode=mode,
        scheduled_for=row.get("scheduled_for"),
        cap_reason=cap_reason,
    )

    return EnqueueOutcome(
        accepted=True,
        message=AcceptedMessage(
            id=row["id"],
            state=row["state"],
            mode=mode,
            scheduled_for=row.get("scheduled_for"),
            conversation_id=conversation["id"],
            contact_id=contact["id"],
            sender_slug=(sticky_sender or {}).get("slug", slug),
            normalized_address=normalized,
            temp_guid=row.get("temp_guid"),
            queued_at=row["queued_at"],
            cap_blocked=cap_reason is not None,
            cap_reason=cap_reason,
            retry_after=retry_after,
        ),
    )


def _parse_ts(value: Any) -> datetime | None:
    """The gate's `retry_after`, which is a Postgres timestamptz rendered as JSON."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - the database always sends ISO 8601
        log_context(log, logging.WARNING, "unparsable retry_after from the gate", value=str(value))
        return None


async def resolve_contact_or_404(db: Database, normalized: str) -> dict[str, Any]:
    contact = await db.get_contact_by_address(normalized)
    if contact is None:
        from .errors import not_found

        raise not_found("Contact", normalized_address=normalized)
    return contact


def unavailable() -> ApiError:  # pragma: no cover - re-export for convenience
    return upstream_error()
