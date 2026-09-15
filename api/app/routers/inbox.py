"""The inbound inbox - replies that came back from recipients.

Inbound messages reach the database through the Node gateway's BlueBubbles
webhook on the Mac (`POST /v1/webhooks/bluebubbles`), which dedupes on
`provider_guid`. This API only *reads* them; it has no webhook of its own,
because BlueBubbles is on loopback on the Mac and cannot reach this server.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from ..db import Database
from ..deps import get_db
from ..errors import bad_request, not_found
from ..logging_config import log_context
from ..normalize import normalize_address
from ..schemas import Message, MessageList

log = logging.getLogger("api.inbox")
router = APIRouter(prefix="/v1", tags=["inbox"])


@router.get(
    "/inbox",
    response_model=MessageList,
    summary="Received messages, newest first",
    description=(
        "Inbound messages only (`direction='inbound'`), newest first.\n\n"
        "Inbound rows are observed facts: they arrive already `sent`, already carrying a "
        "`provider_guid`, and never have a `temp_guid`. Duplicate webhooks (BlueBubbles Defect B) "
        "are collapsed by a unique index before they ever get here, so every row in this list is "
        "a distinct real message.\n\n"
        "`unread_only=true` returns only messages with no `read_at`; mark one read with "
        "`POST /v1/inbox/{id}/read`. The read flag is operator-facing metadata and has no effect "
        "on sending or on the consent gate."
    ),
)
async def get_inbox(
    db: Annotated[Database, Depends(get_db)],
    since: Annotated[datetime | None, Query(description="created_at >= this instant (ISO 8601).")] = None,
    until: Annotated[datetime | None, Query()] = None,
    contact: Annotated[str | None, Query(description="Sender address of the reply; normalized before lookup.")] = None,
    unread_only: Annotated[bool, Query(description="Only messages that have not been marked read.")] = False,
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

    rows = await db.list_messages(
        direction="inbound",
        contact_id=contact_id,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
    )
    messages = [Message(**r) for r in rows]
    next_cursor = messages[-1].created_at if len(messages) == limit and messages else None
    return MessageList(count=len(messages), next_cursor=next_cursor, messages=messages)


@router.post(
    "/inbox/{message_id}/read",
    response_model=Message,
    summary="Mark an inbound message read",
    description="Sets `read_at`. Idempotent in effect - a second call just re-stamps it.",
)
async def mark_read(
    db: Annotated[Database, Depends(get_db)],
    message_id: Annotated[str, Path(description="Inbound message UUID.")],
) -> Message:
    row = await db.mark_read(message_id)
    if row is None:
        raise not_found("Inbound message", id=message_id)
    log_context(log, logging.INFO, "inbox read", message_id=message_id)
    return Message(**row)
