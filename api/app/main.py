"""FastAPI application: the PUBLIC interface of the iMessage bridge.

    caller ──HTTPS──▶ [ this API ] ──▶ Supabase ◀── [ Node worker on the Mac ] ──▶ BlueBubbles

This process runs on a public server. The Node worker runs on the Mac mini.
They share a database and nothing else. This API has no route to BlueBubbles
and must never be given one - BlueBubbles listens on 127.0.0.1:12341 on the Mac
and stays there.

Consequence: a 202 from this service means *accepted for sending*, never *sent*.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import require_token
from .config import get_settings
from .db import Database
from .errors import ApiError
from .logging_config import configure_logging, log_context
from .routers import compliance, contacts, conversations, inbox, messages, ops
from .version import __version__

log = logging.getLogger("api")

DESCRIPTION = """
REST interface to an email-identity iMessage bridge.

## Two processes, one database

```
        (public internet)                         (Mac mini, private)
 caller ──HTTPS──▶ Python API ──▶ Supabase ◀── Node worker ──▶ BlueBubbles ──▶ Messages.app
                                     ▲                              │
                                     └──── webhook ingest ◀─────────┘
```

**This API never sends anything.** It validates, gates and enqueues. A separate
Node worker on the Mac mini claims queued rows and is the only process that
touches BlueBubbles - which listens on `127.0.0.1:12341` there and is
deliberately not exposed.

So `POST /v1/messages` returns **202 Accepted**, never 200. Nothing has left the
Mac at that point.

## Pacing

The worker paces sends at **one message every 8-12 seconds, per sender**, on
purpose: the constraint is Apple's tolerance for a young sender identity, not
throughput. This API cannot and will not deliver faster than that, whatever rate
you post at.

## What a state means

* `queued` - accepted, waiting. With `scheduled_for` set, waiting until then.
* `sending` - claimed by the worker, handed to BlueBubbles, outcome not yet known.
* `sent` - confirmed. `delivered_at` may lag `sent_at` by minutes if the Mac was offline.
* `failed` **with `reconciled: false`** - the outcome is genuinely **unknown**, not
  failed. BlueBubbles returns HTTP 500 or times out on sends that actually
  delivered; such a row must never be retried blind.
"""

TAGS = [
    {"name": "messages", "description": "Enqueue, cancel and read messages."},
    {"name": "inbox", "description": "Received (inbound) messages."},
    {"name": "conversations", "description": "Threaded view of a (contact, sender) pair."},
    {"name": "contacts", "description": "Who a contact is, who they hear from, and whether we may write."},
    {"name": "compliance", "description": "Consent ledger and the global suppression list."},
    {"name": "ops", "description": "Health and stats. /v1/health needs no token."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    db = Database(settings)
    await db.connect()
    app.state.db = db
    log_context(
        log,
        logging.INFO,
        "api started",
        version=__version__,
        host=settings.api_host,
        port=settings.api_port,
        default_sender=settings.default_sender_slug,
        cors=bool(settings.cors_origin_list),
    )
    try:
        yield
    finally:
        # Graceful shutdown. Nothing is in flight here - this process never holds
        # a send - so closing the HTTP pool cleanly is all that is required.
        # uvicorn has already stopped accepting and drained open requests.
        await db.close()
        log_context(log, logging.INFO, "api stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="iMessage Bridge API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # CORS is OFF unless CORS_ORIGINS is set. A browser has no business holding
    # this token, so the default is no browser access at all.
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Actor"],
        )

    # ---- auth ------------------------------------------------------------
    open_paths = {"/health", "/v1/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in open_paths:
            return await call_next(request)
        try:
            await require_token(request)
        except ApiError as err:
            return JSONResponse(status_code=err.status_code, content=err.envelope())
        return await call_next(request)

    # ---- one error envelope, everywhere ----------------------------------
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.envelope())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": "http_error", "message": str(exc.detail)}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The request body or query string is invalid.",
                    "detail": {
                        "errors": [
                            {
                                "loc": [str(p) for p in e.get("loc", [])],
                                "msg": e.get("msg"),
                                "type": e.get("type"),
                            }
                            for e in exc.errors()
                        ]
                    },
                }
            },
        )

    @app.exception_handler(APIError)
    async def _postgrest_error(request: Request, exc: APIError):
        # The Supabase URL and service_role key are NOT in this message, but the
        # code is logged and only a short message goes out.
        log_context(
            log,
            logging.ERROR,
            "postgrest error",
            path=request.url.path,
            pg_code=getattr(exc, "code", None),
            pg_message=getattr(exc, "message", None),
        )
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "database_error",
                    "message": "The database rejected or could not serve this request.",
                    "detail": {"pg_code": getattr(exc, "code", None)},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Log the traceback; return nothing that could leak a key, a URL or a path.
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Internal server error."}},
        )

    # ---- routes ----------------------------------------------------------
    app.include_router(ops.router)
    app.include_router(messages.router)
    app.include_router(inbox.router)
    app.include_router(conversations.router)
    app.include_router(contacts.router)
    app.include_router(compliance.router)

    return app


app = create_app()
