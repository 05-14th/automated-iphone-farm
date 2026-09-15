"""Sender identities, their operating caps, and how much of each is spent.

This is the endpoint that answers "am I about to get an Apple Account shut
down?". Every number here comes from `public.sender_cap_status()` - the same
function `can_send()` calls to decide a cap refusal - so what an operator reads
here and what the gate enforces can never drift apart.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from ..db import Database
from ..deps import get_db
from ..errors import not_found
from ..schemas import SenderCaps, SenderDetail, SenderList

router = APIRouter(prefix="/v1", tags=["senders"])

# The order a refusal would actually happen in, which is the order can_send()
# evaluates: the blanket time gate, then the window that frees up last, then the
# burst window, then the narrowest cap of all.
_BLOCK_ORDER = (
    ("quiet_hours", lambda caps: bool(caps.get("quiet_hours", {}).get("in_quiet_hours"))),
    ("cap_daily", lambda caps: bool(caps.get("daily", {}).get("exceeded"))),
    ("cap_hourly", lambda caps: bool(caps.get("hourly", {}).get("exceeded"))),
    ("cap_new_conversation", lambda caps: bool(caps.get("new_conversation_daily", {}).get("exceeded"))),
)


def _blocked_by(caps: dict) -> str | None:
    for reason, test in _BLOCK_ORDER:
        if test(caps):
            return reason
    return None


async def _detail(db: Database, row: dict, with_usage: bool) -> SenderDetail:
    caps_model = None
    blocked = None
    if with_usage:
        status = await db.sender_cap_status(row["id"])
        if status.get("found"):
            caps_model = SenderCaps(
                daily=status["daily"],
                hourly=status["hourly"],
                new_conversation_daily=status["new_conversation_daily"],
                quiet_hours=status["quiet_hours"],
            )
            blocked = _blocked_by(status)

    return SenderDetail(
        id=row["id"],
        slug=row["slug"],
        status=row["status"],
        paused_reason=row.get("paused_reason"),
        apple_email=row.get("apple_email"),
        bluebubbles_url=row.get("bluebubbles_url"),
        bluebubbles_port=row.get("bluebubbles_port"),
        macos_user=row.get("macos_user"),
        timezone=row.get("timezone"),
        caps=caps_model,
        blocked_by=blocked,
        created_at=row.get("created_at"),
    )


@router.get(
    "/senders",
    response_model=SenderList,
    summary="Senders, their operating caps, and current usage",
    description=(
        "Every sender identity with its configured caps [guide step 36] and how much of each "
        "window is already spent:\n\n"
        "- `daily` - rolling 24 h total\n"
        "- `hourly` - rolling 60 min burst guard\n"
        "- `new_conversation_daily` - rolling 24 h FIRST CONTACTS only, capped separately and "
        "more tightly, because opening a conversation with someone who never asked to hear from "
        "us is the riskiest thing this system does\n"
        "- `quiet_hours` - evaluated in the **sender's** timezone, not UTC\n\n"
        "Each window reports `used`, `remaining`, `window_resets_at` (when the oldest counted "
        "message ages out and one slot frees up) and, while full, `retry_after`.\n\n"
        "`blocked_by` names the cap that would refuse this sender's next send right now. A "
        "sender being blocked is **not** an error: queued messages stay queued and go out when "
        "the window reopens.\n\n"
        "What counts against a cap: an outbound message that is `sending` or `sent`, plus a "
        "`failed` one that reconciliation proved actually delivered (Defect A - BlueBubbles "
        "reports failure on sends that landed, and Apple counted those). A `queued` message "
        "counts for nothing; it has not left the Mac.\n\n"
        "A `null` cap means no limit. Caps are per-sender configuration: change one with an "
        "UPDATE on `public.senders` and the next gate call honours it - no deploy, no restart."
    ),
)
async def list_senders(
    db: Annotated[Database, Depends(get_db)],
    usage: Annotated[bool, Query(description="Include live cap usage. Off = configuration only.")] = True,
    only_active: Annotated[bool, Query(description="Only senders with status='active'.")] = False,
) -> SenderList:
    rows = await db.get_senders(only_active=only_active)
    senders = [await _detail(db, row, usage) for row in rows]
    return SenderList(count=len(senders), senders=senders)


@router.get(
    "/senders/{slug}",
    response_model=SenderDetail,
    summary="One sender, with its caps and usage",
    description=(
        "The same view as `GET /v1/senders`, for a single slug. `blocked_by` names the cap that "
        "would refuse this sender's next send right now, or null when nothing is binding. Pass "
        "`usage=false` for configuration only, without the four window counts."
    ),
    responses={404: {"description": "No sender with that slug."}},
)
async def get_sender(
    db: Annotated[Database, Depends(get_db)],
    slug: Annotated[str, Path(pattern=r"^sender0[1-5]$", description="sender01 .. sender05")],
    usage: Annotated[bool, Query(description="Include live cap usage.")] = True,
) -> SenderDetail:
    row = await db.get_sender_by_slug(slug)
    if row is None:
        raise not_found("Sender", slug=slug)
    return await _detail(db, row, usage)
