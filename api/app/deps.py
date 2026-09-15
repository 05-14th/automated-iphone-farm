"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from .db import Database


def get_db(request: Request) -> Database:
    db: Database | None = getattr(request.app.state, "db", None)
    if db is None:  # pragma: no cover - only reachable before startup completes
        from .errors import upstream_error

        raise upstream_error("Service is still starting.")
    return db


def client_ip(request: Request) -> str | None:
    """Best-effort source IP, honouring one proxy hop.

    Recorded in audit rows for context only. X-Forwarded-For is client-settable,
    so it is never treated as identity - see api/DEPLOYMENT.md.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None
