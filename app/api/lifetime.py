"""Ephemeral setup work ends with its caller or the service setup deadline."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from starlette.requests import Request

from app.core.errors import ApiError


async def run_setup(request: Request, operation: Coroutine[Any, Any, Any], *, timeout: float):
    """Call only after reading the body: this task owns the remaining ASGI receive.

    Await cancelled work so its browser/HTTP finally blocks finish before returning.
    No background setup task survives a disconnect, route cancellation or deadline.
    """
    async def disconnected():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((work, watcher), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return await work
        if watcher in done:
            await watcher  # propagate unexpected receive failures
            reason, message = "caller_disconnected", "The setup caller disconnected."
        else:
            reason, message = "setup_deadline_exceeded", "The setup request exceeded the service time budget."
        raise ApiError("service_unavailable", message, details={"reason": reason})
    finally:
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)
