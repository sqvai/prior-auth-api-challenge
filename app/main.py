# Copyright (c) 2026 John Harris. All rights reserved. Proprietary.

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import router
from app.db import SessionLocal
from app.errors import ApiError

log = logging.getLogger("prior_auth")


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    app = FastAPI(
        title="Prior Authorization API",
        version="1.0.0",
        description=(
            "Utilization-management prior authorization for provider offices, members, "
            "reviewers, and operations. Synthetic data only. Partner documentation is this schema."
        ),
    )
    app.include_router(router)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        incoming = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["x-request-id"] = incoming
        return response

    @app.get("/health")
    def health():
        session = SessionLocal()
        try:
            session.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception:
            log.info("health check failed")
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        finally:
            session.close()

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "message": exc.message})

    @app.exception_handler(RequestValidationError)
    async def invalid_body(_request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for err in exc.errors():
            loc = [str(part) for part in err["loc"] if part != "body"]
            problems.append({"code": "INVALID_FIELD", "field": ".".join(loc) or None, "message": err["msg"]})
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "message": "The body has fields we can't accept. Fix each problem and resubmit.",
                "problems": problems,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled %s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "The request failed. Quote the X-Request-Id when you call support.",
            },
        )

    return app


app = create_app()
