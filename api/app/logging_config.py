"""Structured (JSON-line) logging, with redaction.

Every record is one JSON object on one line. Anything that looks like a bearer
token, a Supabase key or an `Authorization` header is replaced with
`***redacted***` before it is written, on the principle that a log file gets
copied into a ticket eventually.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(\"?(?:api_token|apikey|api_key|authorization|service_role_key|"
               r"supabase_service_role_key|password|token)\"?\s*[:=]\s*\"?)[^\s\",}]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"sb_secret_[A-Za-z0-9_\-]+"),
]


def redact(value: Any) -> Any:
    """Replace anything secret-shaped in a string (or nested structure)."""
    if isinstance(value, str):
        out = value
        for pat in _SECRET_PATTERNS:
            if pat.groups:
                out = pat.sub(lambda m: m.group(1) + "***redacted***", out)
            else:
                out = pat.sub("***redacted***", out)
        return out
    if isinstance(value, dict):
        return {k: ("***redacted***" if _is_secret_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in ("token", "key", "password", "secret", "authorization"))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            # The type and message only. The traceback goes nowhere near a client;
            # it is still useful here, so keep it under an explicit key.
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for noisy in ("uvicorn.access", "httpx", "hpack", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_context(logger: logging.Logger, level: int, msg: str, **context: Any) -> None:
    logger.log(level, msg, extra={"context": context})
