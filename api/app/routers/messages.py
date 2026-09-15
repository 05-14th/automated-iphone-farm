"""Sending and reading messages."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from ..config import get_settings
from ..db import Database
from ..deps import get_db
from ..errors import ApiError, bad_request, conflict, not_found
from ..logging_config import log_context
from ..normalize import normalize_address
from ..schemas import (
    AcceptedMessage,
    BulkResultItem,
    BulkSendRequest,
    BulkSendResponse,
    CancelResponse,
    ErrorDetail,
    Message,
    MessageDetail,
    MessageList,
    SendRequest,
)
from ..sending import enqueue

log = logging.getLogger("api.messages")
router = APIRouter(prefix="/v1", tags=["messages"])

MAX_BULK = 100


@router.post(
    "/messages",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedMessage,
    summary="Enqueue one message",
    response_description="Accepted for sending. NOT sent - the worker on the Mac does that.",
    description=(
        "Normalizes the address, resolves (or creates) the contact and conversation honouring "
        "**sticky routing**, runs the database's `can_send()` consent/suppression gate, and "
        "enqueues.\n\n"
        "Returns **202**, never 200: nothing has been sent at this point and this API refuses to "
        "imply otherwise. A message becomes `sent` only once the Node worker on the Mac mini has "
        "handed it to BlueBubbles *and* reconciled the outcome.\n\n"
        "- `mode=quick` (default) - due immediately; the worker picks it up on its next tick.\n"
        "- `mode=scheduled` - requires `send_at` in the future; stored as UTC in `scheduled_for`.\n\n"
        "**Pacing:** the Mac deliberately paces sends at one message every 8-12 s *per sender*. "
        "Accepting a message says nothing about when it goes out."
    ),
    responses={
        403: {"description": "Refused by the gate: suppressed, no consent, sender paused.", "model": ErrorDetail},
        409: {"description": "sticky_sender_mismatch, or a message is already in flight on this conversation."},
        422: {"description": "Validation failed (e.g. mode=scheduled with no future send_at)."},
    },
)
async def post_message(
    payload: SendRequest,
    db: Annotated[Database, Depends(get_db)],
) -> AcceptedMessage:
    outcome = await enqueue(
        db,
        payload,
        mode=payload.mode,
        send_at=payload.send_at,
        default_sender_slug=get_settings().default_sender_slug,
    )
    if not outcome.accepted or outcome.message is None:
        raise outcome.error or bad_request("Could not enqueue.")
    return outcome.message


@router.post(
    "/messages/bulk",
    response_model=BulkSendResponse,
    summary="Enqueue up to 100 messages",
    description=(
        "Each item is resolved and gated **independently**. One refusal does not abort the batch: "
        "the response reports per-item `accepted` with the reason for every refusal.\n\n"
        "The HTTP status is always 200 - it describes the batch call, not the individual "
        "outcomes. Read `results[].accepted`.\n\n"
        "Note that per-conversation FIFO applies: two messages to the *same* contact in one batch "
        "will see the second refused with `in_flight_message_exists`, because the first is now "
        "queued ahead of it. That is the database enforcing ordering, working as designed."
    ),
)
async def post_bulk(
    payload: BulkSendRequest,
    db: Annotated[Database, Depends(get_db)],
) -> BulkSendResponse:
    if len(payload.messages) > MAX_BULK:
        raise bad_request(f"At most {MAX_BULK} messages per call.", submitted=len(payload.messages))

    default_slug = get_settings().default_sender_slug
    results: list[BulkResultItem] = []
    accepted = 0

    for index, item in enumerate(payload.messages):
        outcome = await enqueue(
            db, item, mode=payload.mode, send_at=payload.send_at, default_sender_slug=default_slug
        )
        if outcome.accepted and outcome.message is not None:
            accepted += 1
            results.append(BulkResultItem(index=index, to=item.to, accepted=True, message=outcome.message))
        else:
            err = outcome.error
            results.append(
                BulkResultItem(
                    index=index,
                    to=item.to,
                    accepted=False,
                    error=ErrorDetail(
                        code=getattr(err, "code", "error"),
                        message=getattr(err, "message", "refused"),
                        detail=getattr(err, "extra", None) or None,
                    ),
                )
            )

    log_context(
        log, logging.INFO, "bulk enqueue", submitted=len(payload.messages), accepted=accepted, mode=payload.mode
    )
    return BulkSendResponse(
        accepted_count=accepted,
        refused_count=len(results) - accepted,
        results=results,
    )


@router.delete(
    "/messages/{message_id}",
    response_model=CancelResponse,
    summary="Cancel a queued message",
    description=(
        "Cancels a message that is still `queued` and has not been picked up.\n\n"
        "Once the worker has claimed it (`sending`) there is nothing to cancel: BlueBubbles has "
        "the text and, per Defect A, the HTTP response would not tell us whether it went out "
        "anyway. Those return **409**, as does an already-`sent` message.\n\n"
        "A cancelled row becomes `state=failed`, `error_code=cancelled_by_api`, `reconciled=true` "
        "- honest, because nothing was ever handed to the provider, and it keeps the row out of "
        "the Defect A triage queue."
    ),
    responses={409: {"description": "Already sending, sent, or failed."}, 404: {"description": "No such message."}},
)
async def cancel_message(
    db: Annotated[Database, Depends(get_db)],
    message_id: Annotated[str, Path(description="Message UUID.")],
) -> CancelResponse:
    existing = await db.get_message(message_id)
    if existing is None:
        raise not_found("Message", id=message_id)
    if existing["direction"] != "outbound":
        raise conflict("not_outbound", "Inbound messages cannot be cancelled.", id=message_id)
    if existing["state"] != "queued":
        raise conflict(
            "not_cancellable",
            f"Message is '{existing['state']}', not 'queued'. Only an unclaimed message can be cancelled.",
            id=message_id,
            state=existing["state"],
        )

    updated = await db.cancel_queued_message(message_id)
    if updated is None:
        # Lost the race: the worker claimed it between the read and the update.
        current = await db.get_message(message_id)
        raise conflict(
            "not_cancellable",
            "The worker claimed this message before the cancel landed.",
            id=message_id,
            state=(current or {}).get("state"),
        )

    log_context(log, logging.INFO, "cancelled", message_id=message_id)
    return CancelResponse(id=message_id, state=updated["state"], cancelled=True)


@router.get(
    "/messages/{message_id}",
    response_model=MessageDetail,
    summary="Full state of one message",
    description=(
        "Every timestamp, the provider GUID, the `reconciled` flag, and the full send-attempt "
        "history.\n\n"
        "Read the attempts as a story: `timeout -> reconciled_sent` is a correctly handled false "
        "failure; `timeout -> accepted` is a **double-send**.\n\n"
        "`state='failed'` with `reconciled=false` means the outcome is **unknown**, not that the "
        "message failed."
    ),
)
async def get_message(
    db: Annotated[Database, Depends(get_db)],
    message_id: Annotated[str, Path(description="Message UUID.")],
) -> MessageDetail:
    row = await db.get_message(message_id)
    if row is None:
        raise not_found("Message", id=message_id)
    attempts = await db.get_attempts(message_id)
    return MessageDetail(**row, attempts=attempts)


@router.get(
    "/messages",
    response_model=MessageList,
    summary="List messages",
    description=(
        "Newest first. Filter by state, direction, contact address, sender slug and a time "
        "window.\n\n"
        "Paginate either way: `cursor` (keyset - pass `next_cursor` from the previous page, "
        "stable under concurrent inserts) or `offset` (simpler, drifts if rows are inserted "
        "while you page). Default limit 50, maximum 200."
    ),
)
async def list_messages(
    db: Annotated[Database, Depends(get_db)],
    state: Annotated[str | None, Query(pattern="^(queued|sending|sent|failed)$")] = None,
    direction: Annotated[str | None, Query(pattern="^(outbound|inbound)$")] = None,
    contact: Annotated[str | None, Query(description="Recipient address; normalized before lookup.")] = None,
    sender_slug: Annotated[str | None, Query(pattern=r"^sender0[1-5]$")] = None,
    conversation_id: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query(description="created_at >= this instant (ISO 8601).")] = None,
    until: Annotated[datetime | None, Query(description="created_at <= this instant (ISO 8601).")] = None,
    cursor: Annotated[datetime | None, Query(description="Keyset cursor: created_at < this instant.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MessageList:
    contact_id = None
    if contact:
        normalized = normalize_address(contact)
        if not normalized:
            raise bad_request("contact does not normalize to an address.", contact=contact)
        row = await db.get_contact_by_address(normalized)
        if row is None:
            return MessageList(count=0, next_cursor=None, messages=[])
        contact_id = row["id"]

    sender_id = None
    if sender_slug:
        sender = await db.get_sender_by_slug(sender_slug)
        if sender is None:
            raise bad_request(f"Unknown sender_slug '{sender_slug}'.", sender_slug=sender_slug)
        sender_id = sender["id"]

    rows = await db.list_messages(
        state=state,
        direction=direction,
        contact_id=contact_id,
        sender_id=sender_id,
        conversation_id=conversation_id,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit,
        offset=offset,
    )
    messages = [Message(**r) for r in rows]
    next_cursor = messages[-1].created_at if len(messages) == limit and messages else None
    return MessageList(count=len(messages), next_cursor=next_cursor, messages=messages)
