"""Consent ledger and suppression list.

These two tables are the compliance surface. The rules they encode are not
negotiable in code:

* **Suppression is global.** There is no `sender_id` column: someone who said
  STOP to `sender01` has said STOP to the operator and must not be reachable
  from `sender03`.
* **Consent is an append-only ledger.** A revocation is a new row, never an
  update. `can_send()` reads the newest row per contact.
* **Suppression outranks consent.** `can_send()` checks it first, so a later
  opt-in cannot quietly reopen a channel somebody asked to leave.

This table records what happened; it does not decide what is lawful. Qualified
counsel must validate the consent, identification and unsubscribe requirements
for every jurisdiction contacted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status

from ..auth import actor_of
from ..config import get_settings
from ..db import Database
from ..deps import client_ip, get_db
from ..errors import bad_request, not_found
from ..logging_config import log_context
from ..normalize import normalize_address
from ..schemas import (
    ConsentRecord,
    ConsentRequest,
    ConsentResponse,
    GateVerdict,
    SuppressionList,
    SuppressionRecord,
    SuppressionRequest,
    SuppressionResponse,
    SuppressionDeleteResponse,
)

log = logging.getLogger("api.compliance")
router = APIRouter(prefix="/v1", tags=["compliance"])


@router.post(
    "/consents",
    status_code=status.HTTP_201_CREATED,
    response_model=ConsentResponse,
    summary="Record consent granted or revoked",
    description=(
        "Appends a row to the consent ledger. **Nothing is ever updated** - a revocation is a new "
        "row with `status='revoked'`, and the history stays intact.\n\n"
        "`can_send()` refuses every contact with `no_consent_on_record`, so this call is the "
        "prerequisite for sending to anyone.\n\n"
        "`evidence` is stored verbatim as the proof blob: form payload, IP, timestamp, recording "
        "reference - whatever your jurisdiction requires you to be able to produce later.\n\n"
        "If the address has no contact row yet, one is created and pinned to `sender_slug` (or "
        "the configured default). That pin is permanent."
    ),
)
async def post_consent(
    payload: ConsentRequest,
    db: Annotated[Database, Depends(get_db)],
) -> ConsentResponse:
    normalized = normalize_address(payload.address)
    if not normalized:
        raise bad_request("address does not normalize.", address=payload.address)

    slug = payload.sender_slug or get_settings().default_sender_slug
    sender = await db.get_sender_by_slug(slug)
    if sender is None:
        raise bad_request(f"Unknown sender_slug '{slug}'.", sender_slug=slug)

    contact, created, _mismatch = await db.resolve_contact(payload.address, sender["id"])

    row = await db.add_consent(
        contact_id=contact["id"],
        status=payload.status,
        source=payload.source,
        evidence=payload.evidence,
        at=payload.occurred_at,
    )
    verdict = await db.can_send(contact["id"])

    log_context(
        log,
        logging.INFO,
        "consent recorded",
        contact_id=contact["id"],
        status=payload.status,
        source=payload.source,
        contact_created=created,
    )
    return ConsentResponse(
        contact_id=contact["id"],
        normalized_address=normalized,
        consent=ConsentRecord(**row),
        can_send=GateVerdict(**verdict),
    )


@router.get(
    "/suppressions",
    response_model=SuppressionList,
    summary="List suppressions",
    description="Newest first. Global across every sender; there is no per-sender suppression.",
)
async def list_suppressions(
    db: Annotated[Database, Depends(get_db)],
    reason: Annotated[str | None, Query(pattern="^(stop|dnc|bounce|manual)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuppressionList:
    rows = await db.list_suppressions(limit=limit, offset=offset, reason=reason)
    return SuppressionList(count=len(rows), suppressions=[SuppressionRecord(**r) for r in rows])


@router.post(
    "/suppressions",
    status_code=status.HTTP_201_CREATED,
    response_model=SuppressionResponse,
    summary="Suppress an address",
    description=(
        "Idempotent: a second STOP from the same person is a no-op, not an error (`created: "
        "false`).\n\n"
        "The address is normalized before it is stored, which is what makes suppression actually "
        "work - a STOP recorded as `(778) 323-1234` suppresses a contact stored as "
        "`+17783231234`.\n\n"
        "`reason`: `stop` (recipient opt-out) | `dnc` (registry/legal) | `bounce` "
        "(undeliverable - the worker writes these itself when Apple returns `error=22`) | "
        "`manual` (operator)."
    ),
)
async def post_suppression(
    payload: SuppressionRequest,
    db: Annotated[Database, Depends(get_db)],
) -> SuppressionResponse:
    row, created = await db.add_suppression(payload.address, payload.reason)
    log_context(
        log,
        logging.INFO,
        "suppression recorded",
        normalized_address=row["normalized_address"],
        reason=row["reason"],
        created=created,
    )
    return SuppressionResponse(suppression=SuppressionRecord(**row), created=created)


@router.delete(
    "/suppressions/{address:path}",
    response_model=SuppressionDeleteResponse,
    summary="Un-suppress an address (audited)",
    description=(
        "Removes a suppression, making that person reachable again. This is the most "
        "consequential write the API offers, so it is **always audited**: a row is written to "
        "`suppression_deletions` recording who deleted it, when, from which IP, and the original "
        "reason - *before* the delete happens.\n\n"
        "Identify the caller with an `X-Actor` header (e.g. `X-Actor: ops@example.com`). The API "
        "authenticates a single shared token, so this is recorded as a **claim**, alongside the "
        "source IP - never as proof of identity.\n\n"
        "Deleting a `stop` suppression does not restore consent: the consent ledger is separate, "
        "and `can_send()` will still refuse with `consent_revoked` if the person opted out by "
        "replying STOP."
    ),
    responses={404: {"description": "The address is not suppressed."}},
)
async def delete_suppression(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
    address: Annotated[str, Path(description="Email or phone number, in any format.")],
    note: Annotated[str | None, Query(max_length=500, description="Why. Stored in the audit row.")] = None,
) -> SuppressionDeleteResponse:
    normalized = normalize_address(address)
    if not normalized:
        raise bad_request("address does not normalize.", address=address)

    actor = actor_of(request)
    ip = client_ip(request)
    deleted = await db.delete_suppression(normalized, actor=actor, source_ip=ip, note=note)
    if deleted is None:
        raise not_found("Suppression", normalized_address=normalized)

    deleted_at = datetime.now(timezone.utc)
    log_context(
        log,
        logging.WARNING,
        "suppression DELETED",
        normalized_address=normalized,
        reason=deleted["reason"],
        deleted_by=actor,
        source_ip=ip,
        note=note,
    )
    return SuppressionDeleteResponse(
        deleted=SuppressionRecord(**deleted),
        audit={
            "deleted_by": actor,
            "deleted_at": deleted_at,
            "source_ip": ip,
            "note": note,
            "recorded_in": "public.suppression_deletions",
        },
    )
