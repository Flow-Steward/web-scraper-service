"""Shared document URL policy; checks preserve the original request identity."""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from app.core.linkrules import url_skip
from app.core.scope import in_scope
from app.core.urls import InvalidUrl, canonical_url, site_key

DestinationPolicy = Callable[[str], bool | Awaitable[bool]]
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class DestinationDenied(Exception):
    def __init__(self, url: str, reason: str = "redirect_policy_denied"):
        super().__init__(reason)
        self.url = url
        self.reason = reason


async def resolved(value):
    return await value if inspect.isawaitable(value) else value


async def document_destination_allowed(
    url: str, *, site_url: str, rules: set[str], scope=(),
    robots_allowed: DestinationPolicy | None = None, cancelled=None,
) -> bool:
    """Check a destination without canonicalizing away action/query identity.

    The caller decides scope exemptions for admitted targets/auxiliary resources.
    Exceptions and task cancellation propagate; callers must fail closed.
    """
    if cancelled is not None and await resolved(cancelled()):
        return False
    try:
        parts = urlsplit(url)
        canonical = canonical_url(url)
        if (parts.scheme not in ("http", "https") or parts.username is not None
                or parts.password is not None or site_key(url) != site_key(site_url)):
            return False
    except (InvalidUrl, ValueError, UnicodeError):
        return False
    # Canonical path inspection matches ordinary link admission (/%63art is
    # /cart), while the raw query retains session/action names canonicalization
    # would strip, including form_key and uenc. Keep the request URL unchanged.
    if (url_skip(url, rules) or url_skip(canonical, rules & {"sensitive_path"})
            or not in_scope(scope, canonical)):
        return False
    if robots_allowed is not None and not await resolved(robots_allowed(url)):
        return False
    return not (cancelled is not None and await resolved(cancelled()))


async def redirect_destination_allowed(url, *, site_url, destination_policy=None):
    if not await document_destination_allowed(
        url, site_url=site_url, rules={"sensitive_path", "action_params"},
    ):
        return False
    return destination_policy is None or bool(await resolved(destination_policy(url)))
