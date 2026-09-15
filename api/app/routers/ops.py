"""Operational endpoints: health and stats."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from ..db import Database
from ..deps import get_db
from ..schemas import HealthResponse, QueueDepth, SenderRef, StatsResponse
from ..version import __version__

router = APIRouter(tags=["ops"])


async def _health(db: Database) -> HealthResponse:
    reachable = await db.ping()
    if not reachable:
        return HealthResponse(
            status="degraded",
            database=False,
            senders=[],
            queue=None,
            version=__version__,
            checked_at=datetime.now(timezone.utc),
            note="Supabase is unreachable from this host. Nothing can be enqueued.",
        )

    senders = await db.get_senders()
    queue = await db.queue_depth()
    degraded = (
        not senders
        or not any(s["status"] == "active" for s in senders)
        or queue["unreconciled_failed"] > 0
    )
    note = None
    if queue["unreconciled_failed"] > 0:
        note = (
            f"{queue['unreconciled_failed']} failed message(s) have not been reconciled against "
            "provider state (Defect A). Their real outcome is UNKNOWN. No retry sweep is safe "
            "until this is zero."
        )
    elif not any(s["status"] == "active" for s in senders):
        note = "No active sender. Queued messages will not go out until one is restored."

    return HealthResponse(
        status="degraded" if degraded else "ok",
        database=True,
        senders=[SenderRef(**s) for s in senders],
        queue=QueueDepth(**queue),
        version=__version__,
        checked_at=datetime.now(timezone.utc),
        note=note,
    )


@router.get(
    "/v1/health",
    response_model=HealthResponse,
    summary="Health: database, senders, queue depth",
    description=(
        "**No authentication.** Safe for a load balancer or an uptime monitor.\n\n"
        "Reports whether Supabase is reachable from *this* host, every sender row with its "
        "current status, and the queue depth:\n\n"
        "- `queued` - outbound and due now\n"
        "- `scheduled` - outbound, queued, not yet due\n"
        "- `sending` - claimed by the worker, in flight\n"
        "- `unreconciled_failed` - **the Defect A triage queue**. Non-zero means some messages "
        "have an unknown real outcome; no retry sweep is safe until it is zero, and the response "
        "reports `status: degraded`.\n\n"
        "This endpoint says nothing about whether the **Mac** is healthy. It cannot: BlueBubbles "
        "is on loopback there and unreachable from this server. A sender row can read `active` "
        "while its BlueBubbles instance is dead - that is what the Node health checker on the Mac "
        "is for."
    ),
)
async def health_v1(db: Annotated[Database, Depends(get_db)], response: Response) -> HealthResponse:
    result = await _health(db)
    if result.status == "degraded":
        # Still 200: degraded is information, not a reason for a load balancer to
        # pull the host. 503 is reserved for "this process cannot serve".
        response.headers["X-Health"] = "degraded"
    return result


@router.get(
    "/health",
    response_model=HealthResponse,
    include_in_schema=False,
    summary="Alias of /v1/health",
)
async def health_root(db: Annotated[Database, Depends(get_db)], response: Response) -> HealthResponse:
    return await health_v1(db, response)


@router.get(
    "/v1/stats",
    response_model=StatsResponse,
    summary="Message counts by state over a window",
    description=(
        "Counts over `[since, until]` (defaults: the last 24 hours).\n\n"
        "`outbound` is broken down by state; `inbound` reports totals and unread. "
        "`totals.unreconciled_failed` is the Defect A triage count over the same window."
    ),
)
async def stats(
    db: Annotated[Database, Depends(get_db)],
    since: Annotated[datetime | None, Query(description="Start of the window (ISO 8601). Default: 24h ago.")] = None,
    until: Annotated[datetime | None, Query(description="End of the window. Default: now.")] = None,
) -> StatsResponse:
    now = datetime.now(timezone.utc)
    start = (since or now - timedelta(hours=24)).astimezone(timezone.utc)
    end = (until or now).astimezone(timezone.utc)
    window = {"created_at__gte": start.isoformat(), "created_at__lte": end.isoformat()}

    outbound = {
        state: await db.count("messages", direction="outbound", state=state, **window)
        for state in ("queued", "sending", "sent", "failed")
    }
    inbound_total = await db.count("messages", direction="inbound", **window)
    inbound_unread = await db.count("messages", direction="inbound", read_at__isnull=True, **window)
    unreconciled = await db.count("messages", state="failed", reconciled=False, **window)
    scheduled = await db.count("messages", direction="outbound", state="queued", **window)

    return StatsResponse(
        since=start,
        until=end,
        outbound=outbound,
        inbound={"total": inbound_total, "unread": inbound_unread},
        totals={
            "outbound": sum(outbound.values()),
            "inbound": inbound_total,
            "all": sum(outbound.values()) + inbound_total,
            "unreconciled_failed": unreconciled,
            "queued_including_scheduled": scheduled,
        },
    )
