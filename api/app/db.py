"""The only thing this service talks to: Supabase (PostgREST), as `service_role`.

It never talks to BlueBubbles. It cannot: BlueBubbles listens on
127.0.0.1:12341 on the Mac mini and is deliberately not exposed. The Node worker
on that Mac is the only process that touches it. This API and that worker meet
in this database and nowhere else.

Every write here honours the invariants the schema already enforces
(docs/BRIDGE-SCHEMA.md): sticky routing, per-conversation FIFO, the send state
machine, and the two BlueBubbles defects. Nothing in this file may weaken them;
where the database refuses a write, the refusal is surfaced, not worked around.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from postgrest.exceptions import APIError
from supabase import AsyncClient, acreate_client
from supabase.lib.client_options import AsyncClientOptions

from .config import Settings
from .errors import upstream_error
from .logging_config import log_context
from .normalize import normalize_address

log = logging.getLogger("api.db")

UNIQUE_VIOLATION = "23505"

MESSAGE_STATES = ("queued", "sending", "sent", "failed")
DIRECTIONS = ("outbound", "inbound")


class Database:
    """Thin, explicit data-access layer. One instance per process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncClient | None = None

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        options = AsyncClientOptions(
            auto_refresh_token=False,
            persist_session=False,
            postgrest_client_timeout=self._settings.db_timeout_seconds,
        )
        self._client = await acreate_client(
            self._settings.supabase_url,
            self._settings.supabase_service_role_key,
            options=options,
        )

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.postgrest.aclose()
            except Exception:  # pragma: no cover - best-effort shutdown
                log.warning("postgrest client did not close cleanly")

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise upstream_error("Database connection is not initialised.")
        return self._client

    def table(self, name: str):
        return self.client.table(name)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def is_unique_violation(err: APIError) -> bool:
        return getattr(err, "code", None) == UNIQUE_VIOLATION

    async def ping(self) -> bool:
        try:
            await self.table("senders").select("id").limit(1).execute()
            return True
        except Exception as exc:  # noqa: BLE001 - health must never raise
            log_context(log, logging.WARNING, "database ping failed", error=str(exc))
            return False

    # -- senders -----------------------------------------------------------
    async def get_senders(self, only_active: bool = False) -> list[dict[str, Any]]:
        q = self.table("senders").select("*").order("slug")
        if only_active:
            q = q.eq("status", "active")
        return (await q.execute()).data or []

    async def get_sender_by_slug(self, slug: str) -> dict[str, Any] | None:
        res = await self.table("senders").select("*").eq("slug", slug).maybe_single().execute()
        return res.data if res else None

    async def get_sender_by_id(self, sender_id: str) -> dict[str, Any] | None:
        res = await self.table("senders").select("*").eq("id", sender_id).maybe_single().execute()
        return res.data if res else None

    # -- contacts ----------------------------------------------------------
    async def get_contact_by_address(self, normalized: str) -> dict[str, Any] | None:
        res = (
            await self.table("contacts")
            .select("*")
            .eq("normalized_address", normalized)
            .maybe_single()
            .execute()
        )
        return res.data if res else None

    async def resolve_contact(
        self, raw_address: str, default_sender_id: str, display_name: str | None = None
    ) -> tuple[dict[str, Any], bool, bool]:
        """Return (contact, created, sticky_mismatch).

        STICKY ROUTING: an existing contact keeps the sender it was pinned to on
        first contact, forever. `default_sender_id` only decides where a brand
        new contact is pinned. Naming a different sender for an existing contact
        is reported as a mismatch - never honoured, never rerouted.
        """
        normalized = normalize_address(raw_address)
        if not normalized:
            raise upstream_error("Address does not normalize.")

        existing = await self.get_contact_by_address(normalized)
        if existing:
            return existing, False, existing["sticky_sender_id"] != default_sender_id

        try:
            res = (
                await self.table("contacts")
                .insert(
                    {
                        "raw_address": raw_address.strip(),
                        "normalized_address": normalized,
                        "display_name": display_name,
                        "sticky_sender_id": default_sender_id,
                    }
                )
                .execute()
            )
            return res.data[0], True, False
        except APIError as err:
            if not self.is_unique_violation(err):
                raise
            # Concurrent create for the same address. The other writer won and
            # its sticky sender is now the truth.
            raced = await self.get_contact_by_address(normalized)
            if raced is None:  # pragma: no cover - would mean the row vanished
                raise
            return raced, False, raced["sticky_sender_id"] != default_sender_id

    # -- conversations -----------------------------------------------------
    async def resolve_conversation(self, contact: dict[str, Any]) -> dict[str, Any]:
        found = (
            await self.table("conversations")
            .select("*")
            .eq("contact_id", contact["id"])
            .eq("sender_id", contact["sticky_sender_id"])
            .maybe_single()
            .execute()
        )
        if found and found.data:
            return found.data

        try:
            res = (
                await self.table("conversations")
                .insert(
                    {"contact_id": contact["id"], "sender_id": contact["sticky_sender_id"]}
                )
                .execute()
            )
            return res.data[0]
        except APIError as err:
            if not self.is_unique_violation(err):
                raise
            raced = (
                await self.table("conversations")
                .select("*")
                .eq("contact_id", contact["id"])
                .eq("sender_id", contact["sticky_sender_id"])
                .maybe_single()
                .execute()
            )
            return raced.data

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        res = (
            await self.table("conversations")
            .select("*")
            .eq("id", conversation_id)
            .maybe_single()
            .execute()
        )
        return res.data if res else None

    async def list_conversations(
        self, limit: int, offset: int, sender_id: str | None = None
    ) -> list[dict[str, Any]]:
        q = (
            self.table("conversations")
            .select("*, contacts!inner(normalized_address, raw_address, display_name)")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
        )
        if sender_id:
            q = q.eq("sender_id", sender_id)
        return (await q.execute()).data or []

    # -- the gate ----------------------------------------------------------
    async def can_send(self, contact_id: str) -> dict[str, Any]:
        """`public.can_send(contact_id)` - the single pre-send gate.

        Checked in a fixed, load-bearing order: suppression, consent, sender
        status, sticky-sender match, in-flight message. Never widened, never
        second-guessed here.
        """
        res = await self.client.rpc("can_send", {"p_contact_id": contact_id}).execute()
        data = res.data
        if isinstance(data, dict):
            return data
        return {"allowed": False, "reason": "gate_unavailable"}

    # -- messages ----------------------------------------------------------
    @staticmethod
    def new_temp_guid() -> str:
        """Defect A reconciliation key. Written BEFORE any send is attempted."""
        return f"temp-{uuid.uuid4()}"

    async def enqueue_outbound(
        self,
        conversation_id: str,
        sender_id: str,
        body: str,
        scheduled_for: datetime | None = None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "conversation_id": conversation_id,
            "sender_id": sender_id,
            "direction": "outbound",
            "body": body,
            "state": "queued",
            "temp_guid": self.new_temp_guid(),
        }
        if scheduled_for is not None:
            row["scheduled_for"] = scheduled_for.astimezone(timezone.utc).isoformat()
        res = await self.table("messages").insert(row).execute()
        return res.data[0]

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        res = await self.table("messages").select("*").eq("id", message_id).maybe_single().execute()
        return res.data if res else None

    async def get_attempts(self, message_id: str) -> list[dict[str, Any]]:
        res = (
            await self.table("send_attempts")
            .select("*")
            .eq("message_id", message_id)
            .order("attempt_no")
            .execute()
        )
        return res.data or []

    async def list_messages(
        self,
        *,
        state: str | None = None,
        direction: str | None = None,
        conversation_id: str | None = None,
        contact_id: str | None = None,
        sender_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> list[dict[str, Any]]:
        q = self.table("messages").select("*").order("created_at", desc=True).order("id", desc=True)
        if state:
            q = q.eq("state", state)
        if direction:
            q = q.eq("direction", direction)
        if conversation_id:
            q = q.eq("conversation_id", conversation_id)
        if sender_id:
            q = q.eq("sender_id", sender_id)
        if contact_id:
            conv_ids = await self._conversation_ids_for_contact(contact_id)
            if not conv_ids:
                return []
            q = q.in_("conversation_id", conv_ids)
        if since:
            q = q.gte("created_at", since.astimezone(timezone.utc).isoformat())
        if until:
            q = q.lte("created_at", until.astimezone(timezone.utc).isoformat())
        if cursor:
            # Keyset pagination: strictly older than the last row seen.
            q = q.lt("created_at", cursor.astimezone(timezone.utc).isoformat())
        if unread_only:
            q = q.is_("read_at", "null")
        if offset:
            q = q.range(offset, offset + limit - 1)
        else:
            q = q.limit(limit)
        return (await q.execute()).data or []

    async def _conversation_ids_for_contact(self, contact_id: str) -> list[str]:
        res = (
            await self.table("conversations").select("id").eq("contact_id", contact_id).execute()
        )
        return [r["id"] for r in (res.data or [])]

    async def conversation_ids_for_contact(self, contact_id: str) -> list[str]:
        return await self._conversation_ids_for_contact(contact_id)

    async def cancel_queued_message(self, message_id: str) -> dict[str, Any] | None:
        """queued -> failed, with reconciled = true.

        Legal under `trg_messages_state_machine` (queued -> failed is a
        pre-flight rejection). `reconciled = true` is honest here and load
        bearing: nothing was ever handed to BlueBubbles, so there is no
        ambiguous provider outcome to resolve, and the row must not sit in the
        Defect A triage queue (`messages_unreconciled_failed_idx`) pretending
        there is.

        The `.eq("state", "queued")` is the concurrency guard: if the worker
        claimed the row a millisecond ago the UPDATE matches nothing and the
        caller gets a 409 instead of a torn cancel.
        """
        res = (
            await self.table("messages")
            .update(
                {
                    "state": "failed",
                    "error_code": "cancelled_by_api",
                    "reconciled": True,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", message_id)
            .eq("state", "queued")
            .execute()
        )
        return res.data[0] if res.data else None

    async def mark_read(self, message_id: str) -> dict[str, Any] | None:
        res = (
            await self.table("messages")
            .update({"read_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", message_id)
            .eq("direction", "inbound")
            .execute()
        )
        return res.data[0] if res.data else None

    # -- compliance --------------------------------------------------------
    async def get_suppression(self, normalized: str) -> dict[str, Any] | None:
        res = (
            await self.table("suppressions")
            .select("*")
            .eq("normalized_address", normalized)
            .maybe_single()
            .execute()
        )
        return res.data if res else None

    async def list_suppressions(
        self, limit: int, offset: int, reason: str | None = None
    ) -> list[dict[str, Any]]:
        q = (
            self.table("suppressions")
            .select("*")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
        )
        if reason:
            q = q.eq("reason", reason)
        return (await q.execute()).data or []

    async def add_suppression(self, raw_address: str, reason: str) -> tuple[dict[str, Any], bool]:
        """Idempotent. Returns (row, created)."""
        normalized = normalize_address(raw_address)
        if not normalized:
            raise upstream_error("Address does not normalize.")
        try:
            res = (
                await self.table("suppressions")
                .insert(
                    {
                        "raw_address": raw_address.strip(),
                        "normalized_address": normalized,
                        "reason": reason,
                    }
                )
                .execute()
            )
            return res.data[0], True
        except APIError as err:
            if not self.is_unique_violation(err):
                raise
            existing = await self.get_suppression(normalized)
            return existing, False

    async def delete_suppression(
        self, normalized: str, *, actor: str, source_ip: str | None, note: str | None
    ) -> dict[str, Any] | None:
        """Delete a suppression and write the audit row FIRST.

        Order matters. Removing someone from the suppression list makes them
        reachable again; that is the single most consequential write this API
        offers. The audit row is inserted before the delete so a crash between
        the two leaves an over-recorded audit trail rather than a silent
        un-suppression.
        """
        existing = await self.get_suppression(normalized)
        if not existing:
            return None

        await self.table("suppression_deletions").insert(
            {
                "normalized_address": existing["normalized_address"],
                "raw_address": existing.get("raw_address"),
                "reason": existing["reason"],
                "suppressed_at": existing["created_at"],
                "deleted_by": actor,
                "source_ip": source_ip,
                "note": note,
            }
        ).execute()

        await self.table("suppressions").delete().eq("normalized_address", normalized).execute()
        return existing

    async def latest_consent(self, contact_id: str) -> dict[str, Any] | None:
        res = (
            await self.table("consents")
            .select("*")
            .eq("contact_id", contact_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None

    async def add_consent(
        self,
        contact_id: str,
        status: str,
        source: str,
        evidence: dict[str, Any],
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Append to the consent ledger. Rows are NEVER updated; a revocation is
        a new row. `can_send()` reads the newest row per contact."""
        stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        row = {
            "contact_id": contact_id,
            "status": status,
            "source": source,
            "evidence": evidence,
            "granted_at": stamp if status == "granted" else None,
            "revoked_at": stamp if status == "revoked" else None,
        }
        res = await self.table("consents").insert(row).execute()
        return res.data[0]

    # -- ops ---------------------------------------------------------------
    async def count(self, table: str, **filters: Any) -> int:
        q = self.table(table).select("id", count="exact").limit(1)
        for key, value in filters.items():
            if value is None:
                continue
            if key.endswith("__gte"):
                q = q.gte(key[:-5], value)
            elif key.endswith("__lte"):
                q = q.lte(key[:-5], value)
            elif key.endswith("__isnull"):
                q = q.is_(key[:-8], "null" if value else "not.null")
            else:
                q = q.eq(key, value)
        res = await q.execute()
        return res.count or 0

    async def queue_depth(self) -> dict[str, int]:
        now = datetime.now(timezone.utc).isoformat()
        queued_total = await self.count("messages", state="queued", direction="outbound")
        scheduled = await self.count(
            "messages", state="queued", direction="outbound", scheduled_for__gte=now
        )
        sending = await self.count("messages", state="sending", direction="outbound")
        unreconciled = await self.count("messages", state="failed", reconciled=False)
        return {
            # Due now = queued and either unscheduled or past its send_at.
            "queued": max(queued_total - scheduled, 0),
            "scheduled": scheduled,
            "sending": sending,
            "unreconciled_failed": unreconciled,
        }
