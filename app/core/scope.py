"""Crawl scope: where a crawl walks.

* ``crawl_include`` — regexes searched in the canonical URL; a page's links are followed only
  when the page matches (the start page always). Empty = the whole site.
* ``stay_in_section`` — adds the start URL's section: its own URL and everything below its path
  (``/lt/lamps-c1331`` walks ``/lt/lamps-c1331/…`` and ``?page=2`` of it, not ``/lt/tools-c9``).

Target pages linked from a walked page are still fetched (product URLs usually live outside the
category path); links found on a page that is not walked are not followed.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def section_pattern(site_url: str | None) -> str | None:
    """``^https?://(www.)?host[:port]/path(/|?|#|$)`` for the start URL's section; None for a site root."""
    if not site_url:
        return None
    p = urlsplit(site_url)
    path = (p.path or "/").rstrip("/")
    if not path:
        return None
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    port = f":{p.port}" if p.port else ""
    return rf"^https?://(www\.)?{re.escape(host)}{re.escape(port)}{re.escape(path)}(?:[/?#]|$)"


def crawl_patterns(config: dict) -> list[re.Pattern]:
    pats = []
    for raw in config.get("crawl_include") or []:
        try:
            pats.append(re.compile(raw))
        except re.error:
            continue
    if config.get("stay_in_section"):
        sec = section_pattern(config.get("site_url"))
        if sec:
            pats.append(re.compile(sec))
    return pats


def in_scope(patterns: list[re.Pattern], canonical: str) -> bool:
    return not patterns or any(p.search(canonical) for p in patterns)
