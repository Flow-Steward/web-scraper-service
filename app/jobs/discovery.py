"""Sitemap discovery and the ``auto`` rule (README 4.4).

Every fetch goes through a *fetcher*: ``async (url) -> Fetched | None``. Jobs pass
``JobRunner.aux_fetch`` (the job's crawler: its proxy tiers, headers, session and challenge
route); the setup endpoints pass a one-off HTTP fetch with the profile's first
tier and the job headers. Sitemaps are parsed here (XML urlset and index, plain text, gzip
with a size cap); every nested sitemap URL is checked against the private-address rule
before it is fetched.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.core.netguard import url_is_private
from app.core.urls import absolute, canonical_url, site_key
from app.fetching.oneoff import Fetched, PrivateAddress, gunzip_capped, http_get

SITEMAP_CAP = 100_000
MAX_SITEMAP_FILES = 500
Fetcher = Callable[[str], Awaitable["Fetched | None"]]


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc.rsplit('@', 1)[-1]}"


def http_fetcher(proxy: str | None = None, headers: dict | None = None, timeout: float = 30.0) -> Fetcher:
    async def fetch(url: str) -> Fetched | None:
        try:
            return await http_get(url, proxy=proxy, headers=headers or None, timeout=timeout)
        except PrivateAddress:
            return None

    return fetch


async def robots_from(origin_url: str, fetch: Fetcher):
    from crawlee._utils.robots import RobotsTxtFile

    origin = _origin(origin_url)
    fetched = await fetch(origin + "/robots.txt")
    if fetched is None or not fetched.status or not 200 <= fetched.status < 300:
        return None
    text = fetched.text
    if "<html" in text[:2000].lower():  # an HTML page (challenge, soft 404) is not a robots.txt
        return None
    try:
        return await RobotsTxtFile.from_content(origin + "/robots.txt", text)
    except Exception:
        return None


def _looks_like_sitemap(body: bytes) -> bool:
    head = body[:4096].lstrip()
    return head[:2] == b"\x1f\x8b" or b"<urlset" in head or b"<sitemapindex" in head or head.startswith(b"<?xml")


async def find_sitemaps(site_url: str, sitemap_url: str | None, robots, fetch: Fetcher | None = None) -> list[str]:
    """``sitemap_url``, else robots.txt ``Sitemap:`` lines, else ``/sitemap.xml`` (when it answers)."""
    if sitemap_url:
        return [sitemap_url]
    found: list[str] = []
    if robots is not None:
        try:
            found = list(robots.get_sitemaps(enqueue_strategy="all"))
        except TypeError:
            found = list(robots.get_sitemaps())
        except Exception:
            found = []
    found = [u for u in found if re.match(r"^https?://\S+$", u)]
    if found:
        return found
    candidate = _origin(site_url) + "/sitemap.xml"
    fetched = await (fetch or http_fetcher())(candidate)
    if fetched is not None and fetched.status and 200 <= fetched.status < 300 and fetched.body and _looks_like_sitemap(fetched.body):
        return [candidate]
    return []


def _parse_lastmod(text: str | None) -> datetime | None:
    if not text:
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        try:
            value = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def parse_sitemap(body: bytes, base_url: str) -> tuple[list[tuple[str, datetime | None]], list[str]]:
    """Returns (page URLs with lastmod, nested sitemap URLs) of one sitemap file."""
    if body[:2] == b"\x1f\x8b":
        body = gunzip_capped(body)
    stripped = body.lstrip()
    pages: list[tuple[str, datetime | None]] = []
    nested: list[str] = []
    if stripped.startswith(b"<"):
        from lxml import etree

        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
        try:
            root = etree.fromstring(stripped, parser=parser)
        except etree.XMLSyntaxError:
            root = None
        if root is None:
            return pages, nested
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            tag = el.tag.rsplit("}", 1)[-1].lower()
            if tag not in ("url", "sitemap"):
                continue
            loc = lastmod = None
            for child in el:
                if not isinstance(child.tag, str):
                    continue
                ctag = child.tag.rsplit("}", 1)[-1].lower()
                if ctag == "loc" and child.text:
                    loc = child.text.strip()
                elif ctag == "lastmod" and child.text:
                    lastmod = child.text.strip()
            if not loc:
                continue
            full = absolute(base_url, loc)
            if not full:
                continue
            if tag == "sitemap":
                nested.append(full)
            else:
                pages.append((full, _parse_lastmod(lastmod)))
        return pages, nested
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if re.match(r"^https?://\S+$", line):
            pages.append((line, None))
    return pages, nested


async def iter_sitemaps(sitemaps: list[str], fetch: Fetcher, *, max_files: int = MAX_SITEMAP_FILES):
    """Yields (page_url, lastmod) of every sitemap (index included, breadth-first, each file once)."""
    queue = deque(sitemaps)
    seen: set[str] = set()
    files = 0
    while queue and files < max_files:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        if await url_is_private(url):
            continue
        fetched = await fetch(url)
        files += 1
        if fetched is None or not fetched.status or not 200 <= fetched.status < 300 or not fetched.body:
            continue
        pages, nested = parse_sitemap(fetched.body, fetched.final_url or url)
        for item in pages:
            yield item
        for n in nested:
            if n not in seen:
                queue.append(n)


async def collect_sitemap_urls(sitemaps: list[str], cap: int = SITEMAP_CAP, fetch: Fetcher | None = None) -> tuple[list[str], bool, Any]:
    """All page URLs of the sitemaps (index, text and gzip included), up to ``cap``."""
    urls: list[str] = []
    latest = None
    capped = False
    async for loc, lastmod in iter_sitemaps(sitemaps, fetch or http_fetcher()):
        urls.append(loc)
        if lastmod is not None and (latest is None or lastmod > latest):
            latest = lastmod
        if len(urls) >= cap:
            capped = True
            break
    return urls, capped, latest


def filter_urls(urls: list[str], include: list[str], exclude: list[str]) -> list[str]:
    inc = [re.compile(p) for p in include]
    exc = [re.compile(p) for p in exclude]
    out = []
    for url in urls:
        try:
            canonical = canonical_url(url)
        except ValueError:
            continue
        if inc and not any(p.search(canonical) for p in inc):
            continue
        if any(p.search(canonical) for p in exc):
            continue
        out.append(canonical)
    return out


def target_patterns(mappings: list[dict], include: list[str]) -> list[re.Pattern]:
    pats = []
    for m in mappings:
        for p in (m.get("match") or {}).get("url_patterns") or []:
            try:
                pats.append(re.compile(p))
            except re.error:
                continue
    for p in include:
        try:
            pats.append(re.compile(p))
        except re.error:
            continue
    return pats


def target_share(start_links: list[str], sitemap_set: set[str], patterns: list[re.Pattern], site: str) -> float | None:
    if not patterns:
        return None
    targets = set()
    for link in start_links:
        try:
            canonical = canonical_url(link)
            if site_key(link) != site:
                continue
        except ValueError:
            continue
        if any(p.search(canonical) for p in patterns):
            targets.add(canonical)
    if not targets:
        return None
    return round(len(targets & sitemap_set) / len(targets), 4)


def decide(sitemap_found: bool, matching_count: int, share: float | None) -> tuple[str, list[str]]:
    if not sitemap_found:
        return "crawl", ["no sitemap found"]
    if matching_count == 0:
        return "crawl", ["the sitemap has no URL left after url_include/url_exclude"]
    if share is None:
        return "sitemap", ["sitemap found; target share unknown"]
    if share >= 0.8:
        return "sitemap", [f"sitemap holds {int(share * 100)}% of the start page's target links"]
    return "sitemap_and_crawl", [f"sitemap holds only {int(share * 100)}% of the start page's target links"]


async def start_page_links(site_url: str, fetch: Fetcher | None = None) -> tuple[list[str], Any]:
    from app.extraction.page import Page

    fetched = await (fetch or http_fetcher())(site_url)
    if fetched is None or not fetched.status or fetched.status >= 400:
        return [], fetched
    return Page(
        fetched.text,
        fetched.final_url,
        fetched.status,
        extra_links=fetched.extra_links,
        extra_anchors=fetched.extra_anchors,
    ).links(), fetched


async def decide_discovery(runner, sitemaps: list[str], fetch: Fetcher | None = None) -> tuple[str, list[str]]:
    cfg = runner.config
    urls, _capped, _ = await collect_sitemap_urls(sitemaps, fetch=fetch) if sitemaps else ([], False, None)
    matching = filter_urls(urls, cfg.get("url_include") or [], cfg.get("url_exclude") or [])
    share = None
    if matching:
        links, _ = await start_page_links(cfg["site_url"], fetch)
        site = runner.doc["site"]
        share = target_share(links, set(matching), target_patterns([m for m in runner.mappings_for(site)], cfg.get("url_include") or []), site)
    return decide(bool(urls), len(matching), share)
