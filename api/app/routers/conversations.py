"""Threaded view: conversations and the messages inside one."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from ..db import Database
from ..deps import get_db
from ..errors import bad_request, not_found
from ..schemas import (
    ConversationList,
    ConversationSummary,
    Message,
    MessageList,
)

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


@router.get(
    "",
    response_model=ConversationList,
    summary="List conversations",
    description=(
        "A conversation is the `(contact, sender)` thread, and it is where per-conversation FIFO "
        "is scoped: messages inside one go out strictly in order, while unrelated conversations "
        "progress independently.\n\n"
        "`provider_chat_guid` being null does **not** mean no chat exists on the provider - "
        "`chat/new` can create the chat and still fail to return its id (Defect A)."
    ),
)
async def list_conversations(
    db: Annotated[Database, Depends(get_db)],
    sender_slug: Annotated[str | None, Query(pattern=r"^sender0[1-5]$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationList:
    sender_id = None
    if sender_slug:
        sender = await db.get_sender_by_slug(sender_slug)
        if sender is None:
            raise bad_request(f"Unknown sender_slug '{sender_slug}'.", sender_slug=sender_slug)
        sender_id = sender["id"]

    rows = await db.list_conversations(limit=limit, offset=offset, sender_id=sender_id)
    out = []
    for r in rows:
        contact = r.get("contacts") or {}
        out.append(
            ConversationSummary(
                id=r["id"],
                contact_id=r["contact_id"],
                sender_id=r["sender_id"],
                provider_chat_guid=r.get("provider_chat_guid"),
                address=contact.get("normalized_address"),
                display_name=contact.get("display_name"),
                created_at=r["created_at"],
            )
        )
    return ConversationList(count=len(out), conversations=out)


@router.get(
    "/{conversation_id}/messages",
    response_model=MessageList,
    summary="Messages in one conversation",
    description="Both directions, newest first. Reverse the list for a chat-style transcript.",
)
async def conversation_messages(
    db: Annotated[Database, Depends(get_db)],
    conversation_id: Annotated[str, Path(description="Conversation UUID.")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MessageList:
    conversation = await db.get_conversation(conversation_id)
    if conversation is None:
        raise not_found("Conversation", id=conversation_id)

    rows = await db.list_messages(conversation_id=conversation_id, limit=limit, offset=offset)
    messages = [Message(**r) for r in rows]
    next_cursor = messages[-1].created_at if len(messages) == limit and messages else None
    return MessageList(count=len(messages), next_cursor=next_cursor, messages=messages)
