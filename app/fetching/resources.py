"""Optional browser-resource economy, separate from crawl destination scope."""
from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit


# Vendor host families, never supplier hosts. Unknown CDNs/APIs and challenge
# dependencies remain available. A family used as the target is first-party.
_WIDGET_FAMILIES = (
    "tawk.to", "omnisnippet1.com", "omnisendlink.com", "soundestlink.com",
    "cdn-cookieyes.com", "log.cookieyes.com", "facebook.net", "clarity.ms",
    "analytics.tiktok.com",
)
_HEAVY_TYPES = frozenset({"image", "media", "font"})


def _host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return (parsed.hostname or "").lower().rstrip(".") if parsed.scheme in {"http", "https"} else ""
    except ValueError:
        return ""


def _in_family(host: str, family: str) -> bool:
    return host == family or host.endswith("." + family)


def should_block_resource(url: str, page_url: str, resource_type: str, *, main_document: bool = False) -> bool:
    if main_document:
        return False  # Existing native document policy authorizes navigations.
    if resource_type in _HEAVY_TYPES:
        return True
    host, page_host = _host(url), _host(page_url)
    return any(_in_family(host, family) and not _in_family(page_host, family) for family in _WIDGET_FAMILIES)


def resource_route_guard(page_url: Callable[[], str], *, enabled: bool, then):
    """Use behind private_route_guard, retaining native I/O for allowed requests."""
    async def route_resource(route):
        if enabled:
            request = route.request
            try:
                main = request.is_navigation_request() and request.frame.parent_frame is None
            except Exception:
                main = True  # Leave an unavailable document/frame to native authorization.
            if should_block_resource(request.url, page_url(), request.resource_type, main_document=main):
                await route.abort("blockedbyclient")
                return
        await then(route)

    return route_resource
