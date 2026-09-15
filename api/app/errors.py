"""One JSON error envelope for the whole service.

    {"error": {"code": "refused", "message": "...", "detail": {...}}}

Nothing else is ever returned on a failure path. In particular a stack trace,
a Supabase URL or the service_role key must never reach a client: the global
handler in `main.py` logs the exception server-side and returns a generic 500.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class ApiError(HTTPException):
    """An error that is safe to show a caller."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.extra = detail or {}

    def envelope(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.extra:
            body["error"]["detail"] = self.extra
        return body


def unauthorized(message: str = "A valid bearer token is required.") -> ApiError:
    return ApiError(401, "unauthorized", message)


def not_found(what: str, **detail: Any) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found.", detail or None)


def refused(reason: str, **detail: Any) -> ApiError:
    """A `can_send()` policy refusal. 403 - the caller may not message this contact."""
    return ApiError(
        403,
        "refused",
        f"Refused by the pre-send gate: {reason}",
        {"reason": reason, **detail},
    )


def conflict(code: str, message: str, **detail: Any) -> ApiError:
    return ApiError(409, code, message, detail or None)


def bad_request(message: str, **detail: Any) -> ApiError:
    return ApiError(400, "bad_request", message, detail or None)


def upstream_error(message: str = "The database is unavailable.") -> ApiError:
    return ApiError(503, "upstream_unavailable", message)
