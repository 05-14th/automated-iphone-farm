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

    # 4. THE GATE. Suppression, consent, sender status, sticky match, in-flight.
    verdict = await db.can_send(contact["id"])
    if not verdict.get("allowed"):
        reason = verdict.get("reason", "refused")
        if reason in TRANSIENT_REASONS:
            raise conflict(
                "in_flight_message_exists",
                "Another message is already queued or sending on this conversation. "
                "Per-conversation FIFO is enforced by the database; try again once it clears.",
                reason=reason,
                contact_id=contact["id"],
                conversation_id=conversation["id"],
            )
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
        ),
    )


async def resolve_contact_or_404(db: Database, normalized: str) -> dict[str, Any]:
    contact = await db.get_contact_by_address(normalized)
    if contact is None:
        from .errors import not_found

        raise not_found("Contact", normalized_address=normalized)
    return contact


def unavailable() -> ApiError:  # pragma: no cover - re-export for convenience
    return upstream_error()
