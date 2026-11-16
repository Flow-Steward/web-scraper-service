"""ASGI application, lifespan and SIGTERM handling."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from app.api.http import ApiMiddleware, error_response
from app.api.routes import router
from app.core.errors import ApiError
from app.logging_setup import setup_logging
from app.service import Service, get_service, set_service
from app.settings import get_settings

log = logging.getLogger("wss")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    service = Service(settings)
    set_service(service)
    await service.start()
    log.info("service started (data %s)", settings.data_dir)
    try:
        yield
    finally:
        log.info("service stopping: jobs persist and resume after the next start")
        await service.shutdown()
        set_service(None)


def create_app() -> FastAPI:
    app = FastAPI(title="web-scraper-service", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(ApiMiddleware)
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 405:
            return error_response(request, ApiError("method_not_allowed", "wrong HTTP method for this route"))
        if exc.status_code == 404:
            return error_response(request, ApiError("not_found", "no such route"))
        return error_response(request, ApiError("internal_error", "unexpected HTTP error"))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return error_response(request, ApiError("validation_failed", "the request is not valid", fields=[]))

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return error_response(request, ApiError("internal_error", "unexpected service error"))

    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    config = uvicorn.Config("app.main:app", host=settings.host, port=settings.port, log_config=None, access_log=False, timeout_graceful_shutdown=90)
    server = uvicorn.Server(config)
    # uvicorn handles SIGTERM/SIGINT: it stops accepting requests and runs the lifespan exit,
    # where every job's crawler is stopped and persisted (the state stays, the job resumes).
    server.run()
