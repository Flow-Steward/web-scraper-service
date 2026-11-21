"""Recognize explicit upstream authentication rejection without retaining secret text."""

from __future__ import annotations

import re

PROXY_AUTH_MESSAGE = (
    "The upstream proxy refused authentication. Check its credentials, the service IP "
    "allow-list, and provider account balance or traffic limits. Once provider access "
    "is approved, send Resume with the approved profile. Completed records and pending "
    "work are kept; existing job budgets still apply."
)


class ProxyAuthenticationRequired(Exception):
    """A resumable job pause, never an empty or failed target document."""

    def __init__(self) -> None:
        super().__init__(PROXY_AUTH_MESSAGE)


_AUTH = re.compile(
    r"NS_ERROR_PROXY_AUTHENTICATION_FAILED|ERR_PROXY_AUTH_REQUESTED|"
    r"ERR_PROXY_AUTH_UNSUPPORTED|proxy_auth_failed|"
    r"\b407\s+Proxy\s+Authentication\b|"
    # Camoufox's exit-IP lookup wraps requests.HTTPError in InvalidIP.
    r"\b407\s+Client\s+Error:\s*Proxy\s+Authentication\b|"
    r"\b(?:CONNECT|tunnel)[^\n]{0,100}\b407\b|"
    r"\b(?:HTTP/[\d.]+\s+|status(?:\s+code)?\s*[:=]?\s*|response\s+)407\b",
    re.IGNORECASE,
)


def is_proxy_auth_failure(error=None, *, status: int | None = None, proxy_used: bool = False) -> bool:
    """Require a configured proxy and explicit auth evidence; timeouts/403/5xx do not qualify.

    Crawlee, HTTP clients and browser pipelines wrap transport errors differently. Walk
    only their bounded exception chain, never response bodies or arbitrary object reprs.
    """
    if isinstance(error, ProxyAuthenticationRequired):
        return True
    if not proxy_used:
        return False
    if status == 407:
        return True
    pending = [error]
    seen: set[int] = set()
    while pending and len(seen) < 12:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ProxyAuthenticationRequired) or _AUTH.search(str(current)):
            return True
        for attr in ("wrapped_exception", "__cause__", "__context__"):
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False
