"""Bearer-token auth.

One shared token, compared with `secrets.compare_digest` so the comparison does
not leak its length or prefix through timing. `/health`, `/v1/health`, `/docs`,
`/redoc` and `/openapi.json` are the only unauthenticated routes.
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Request
from fastapi.security import HTTPBearer

from .config import get_settings
from .errors import unauthorized

# Declared so the OpenAPI schema shows the padlock and /docs offers an
# "Authorize" button. auto_error is off: we raise our own envelope.
bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <API_TOKEN>")


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def token_is_valid(given: str | None) -> bool:
    if not given:
        return False
    expected = get_settings().api_token
    # compare_digest on the raw strings is already constant-time for equal
    # lengths; hashing first makes it constant-time for unequal lengths too.
    return secrets.compare_digest(
        hashlib.sha256(given.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


async def require_token(request: Request) -> None:
    """FastAPI dependency. Raises 401 with the standard envelope."""
    if not token_is_valid(_extract_token(request)):
        raise unauthorized()


def actor_of(request: Request) -> str:
    """Who is acting, for audit rows.

    There is one shared token, so the API cannot prove identity on its own.
    A caller may declare itself with `X-Actor: alice@example.com`; the value is
    recorded as a *claim*, alongside the source IP, never as proof.
    """
    claimed = (request.headers.get("x-actor") or "").strip()
    return claimed[:200] or "api_token"
