"""Contact lookup: who they are, who they hear from, and whether we may write."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from ..db import Database
from ..deps import get_db
from ..errors import ApiError, bad_request
from ..normalize import normalize_address
from ..schemas import ContactDetail, GateVerdict, SenderRef

router = APIRouter(prefix="/v1/contacts", tags=["contacts"])


@router.get(
    "/{address:path}",
    response_model=ContactDetail,
    summary="Contact, consent, suppression and sticky sender",
    description=(
        "Accepts an **un-normalized** address - `(778) 323-1234`, `MEIDY765@Gmail.com` and "
        "`+17783231234` all resolve to the same contact.\n\n"
        "Returns the contact, its immutable sticky sender (with that sender's current status), "
        "the newest row of the append-only consent ledger, any suppression, and a live "
        "`can_send()` verdict.\n\n"
        "If the address has **no contact row but is suppressed**, the response is 404 with the "
        "suppression still attached: people can opt out before we ever create a row for them, "
        "and hiding that would be the most dangerous possible 404."
    ),
    responses={404: {"description": "No contact. Any suppression on the address is still reported."}},
)
async def get_contact(
    db: Annotated[Database, Depends(get_db)],
    address: Annotated[str, Path(description="Email or phone number, in any format.")],
) -> ContactDetail:
    normalized = normalize_address(address)
    if not normalized:
        raise bad_request("address does not normalize.", address=address)

    contact = await db.get_contact_by_address(normalized)
    suppression = await db.get_suppression(normalized)

    if contact is None:
        raise ApiError(
            404,
            "not_found",
            "No contact row for this address.",
            {
                "normalized_address": normalized,
                "suppression": (
                    {"reason": suppression["reason"], "created_at": suppression["created_at"]}
                    if suppression
                    else None
                ),
                "note": (
                    "Suppressed with no contact row - an opt-out recorded before we ever "
                    "messaged this address. It will be refused at enqueue time."
                    if suppression
                    else None
                ),
            },
        )

    sender = await db.get_sender_by_id(contact["sticky_sender_id"])
    consent = await db.latest_consent(contact["id"])
    verdict = await db.can_send(contact["id"])

    return ContactDetail(
        id=contact["id"],
        normalized_address=contact["normalized_address"],
        raw_address=contact.get("raw_address"),
        display_name=contact.get("display_name"),
        sticky_sender=SenderRef(**sender) if sender else None,
        consent=consent,
        suppression=suppression,
        can_send=GateVerdict(**verdict),
        created_at=contact["created_at"],
    )
