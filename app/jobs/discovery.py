"""Sitemap discovery and the ``auto`` rule (README 4.4).

Every fetch goes through a *fetcher*: ``async (url) -> Fetched | None``. Jobs pass
``JobRunner.aux_fetch`` (the job's crawler: its proxy tiers, headers, session and challenge
route); the setup endpoints pass a one-off HTTP fetch with the profile's first
tier and the job headers. Sitemaps are parsed here (XML urlset and index, plain text, gzip
with a size cap); every nested sitemap URL is checked against the private-address rule
before it is fetched.
"""

from __future__ import annotations

import copy
import re
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.errors import ApiError
from app.core.netguard import host_is_private, url_is_private
from app.core.urls import absolute, canonical_url, host_of, site_key
from app.core.url_patterns import UrlPattern, compile_patterns, matches_any, url_batch, url_pattern_budget
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


def parse_sitemap(body: bytes, base_url: str, *, strict: bool = False) -> tuple[list[tuple[str, datetime | None]], list[str]]:
    """Returns (page URLs with lastmod, nested sitemap URLs) of one sitemap file."""
    if body[:2] == b"\x1f\x8b":
        body = gunzip_capped(body)
    stripped = body.lstrip()
    pages: list[tuple[str, datetime | None]] = []
    nested: list[str] = []
    if stripped.startswith(b"<"):
        from lxml import etree

        parser = etree.XMLParser(recover=not strict, resolve_entities=False, no_network=True, huge_tree=False)
        try:
            root = etree.fromstring(stripped, parser=parser)
        except etree.XMLSyntaxError:
            root = None
        if root is None:
            if strict:
                raise ValueError("invalid sitemap XML")
            return pages, nested
        if strict and (not isinstance(root.tag, str) or root.tag.rsplit("}", 1)[-1].lower() not in ("urlset", "sitemapindex")):
            raise ValueError("not a sitemap document")
        if strict:
            expected = "url" if root.tag.rsplit("}", 1)[-1].lower() == "urlset" else "sitemap"
            if any(isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1].lower() != expected for child in root):
                raise ValueError("invalid sitemap entries")
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
                if strict:
                    raise ValueError("sitemap entry has no location")
                continue
            full = absolute(base_url, loc)
            if not full:
                if strict:
                    raise ValueError("invalid sitemap location")
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
        elif strict and line:
            raise ValueError("invalid sitemap text location")
    if strict and not pages:
        raise ValueError("empty or invalid sitemap text")
    return pages, nested


def sitemap_report(state: dict) -> dict:
    """Public, bounded discovery coverage; never expose exception text or URL credentials/query."""
    sources = state.get("sources") or {}
    def item(url, entry):
        try:
            p = urlsplit(url)
            safe = urlunsplit((p.scheme, p.netloc.rsplit("@", 1)[-1], p.path, "", ""))
        except ValueError:
            safe = "invalid sitemap source"
        return {"url": safe, "reason": entry.get("reason") or "pending"}
    failed = [item(u, e) for u, e in sources.items() if e.get("status") == "failed"]
    pending = [item(u, e) for u, e in sources.items() if e.get("status") == "pending"]
    if state.get("pending_root"):
        pending.append(item(state["pending_root"], {"reason": "source_file_limit"}))
    refused = [item(u, e) for u, e in sources.items() if e.get("status") == "refused"]
    return {"complete": bool(sources) and not failed and not pending and not state.get("capped", False),
            "capped": bool(state.get("capped")), "cap_reason": state.get("cap_reason"),
            "entries_seen": int(state.get("entries_seen", 0)), "source_files": len(sources),
            "failed_sources": failed, "pending_sources": pending, "refused_sources": refused}


def sitemap_retry_state(state: dict) -> dict:
    """A linked run gets a fresh budget and only unfinished source cursors.

    Index offsets preserve undiscovered children beyond the source-file cap. Completed
    sources are removed to release space; their product entries are never replayed.
    """
    sources = {u: copy.deepcopy(e) for u, e in (state.get("sources") or {}).items()
               if e.get("status") in ("pending", "failed")}
    for entry in sources.values():
        entry.update(status="pending")
        entry.pop("reason", None)
    return {"sources": sources, "entries_seen": 0, "capped": False, "cap_reason": None,
            "root_offset": int(state.get("root_offset", 0)), "pending_root": state.get("pending_root")}


async def iter_sitemaps(sitemaps: list[str], fetch: Fetcher, *, max_files: int = MAX_SITEMAP_FILES,
                        state: dict | None = None, max_entries: int | None = None):
    """Breadth-first source traversal with optional per-source, caller-owned cursors.

    ``state`` MUST be a private copy: a cursor is advanced before yielding its URL,
    and the caller may commit it only after enqueue succeeds. Restart can replay a
    batch through queue deduplication, but never skip an uncommitted batch. Failed
    sources retain their own offsets, independent of successful sibling sources.
    """
    from app.fetching.proxy_auth import ProxyAuthenticationRequired

    state = {} if state is None else state
    sources = state.setdefault("sources", {})
    state.setdefault("entries_seen", 0)
    state.setdefault("capped", False)
    state.setdefault("cap_reason", None)
    # Root and child cursors are independent: even robots.txt with > max_files
    # roots must be partial and retry from the next root, never silently complete.
    if state["capped"] and state["cap_reason"] == "entry_limit":
        return
    for offset in range(int(state.get("root_offset", 0)), len(sitemaps)):
        url = sitemaps[offset]
        if url not in sources:
            if len(sources) >= max_files:
                state.update(capped=True, cap_reason="source_file_limit", pending_root=url)
                break
            sources[url] = {"status": "pending", "page_offset": 0, "nested_offset": 0}
        state["root_offset"] = offset + 1
        state.pop("pending_root", None)
    queue = deque(u for u, e in sources.items() if e["status"] in ("pending", "failed"))
    attempted = set()
    while queue:
        url = queue.popleft()
        if url in attempted:
            continue
        attempted.add(url)
        entry = sources[url]
        entry.update(status="pending")
        entry.pop("reason", None)
        if await url_is_private(url):
            # The guard also refuses unresolvable hosts. Keep that refusal,
            # but report DNS failure as incomplete rather than a neutral policy.
            try:
                private = await host_is_private(host_of(url))
            except ValueError:
                private = None
            if private is None:
                entry.update(status="failed", reason="fetch_failed")
            else:
                entry.update(status="refused", reason="private_address_refused")
            continue
        try:
            fetched = await fetch(url)
        except (ProxyAuthenticationRequired, ApiError):
            raise
        except Exception:
            entry.update(status="failed", reason="fetch_failed")
            continue
        if fetched is None or not fetched.status or not 200 <= fetched.status < 300:
            entry.update(status="failed", reason="fetch_failed")
            continue
        try:
            pages, nested = parse_sitemap(fetched.body, fetched.final_url or url, strict=True)
        except Exception:
            entry.update(status="failed", reason="invalid_sitemap")
            continue
        for offset in range(int(entry["page_offset"]), len(pages)):
            if max_entries is not None and state["entries_seen"] >= max_entries:
                entry["reason"] = "entry_limit"
                state.update(capped=True, cap_reason="entry_limit")
                return
            entry["page_offset"] = offset + 1
            state["entries_seen"] += 1
            yield pages[offset]
        for offset in range(int(entry["nested_offset"]), len(nested)):
            child = nested[offset]
            if child not in sources:
                if len(sources) >= max_files:
                    entry["reason"] = "source_file_limit"
                    state.update(capped=True, cap_reason="source_file_limit")
                    break
                sources[child] = {"status": "pending", "page_offset": 0, "nested_offset": 0}
                queue.append(child)
            entry["nested_offset"] = offset + 1
        else:
            entry["status"] = "complete"


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


@url_batch
def filter_urls(urls: list[str], include: list[str], exclude: list[str]) -> list[str]:
    inc = compile_patterns(include, "url_include")
    exc = compile_patterns(exclude, "url_exclude")
    out = []
    for url in urls:
        try:
            canonical = canonical_url(url)
        except ValueError:
            continue
        with url_pattern_budget():
            if inc and not matches_any(inc, canonical):
                continue
            if matches_any(exc, canonical):
                continue
        out.append(canonical)
    return out


def target_patterns(mappings: list[dict], include: list[str]) -> list[UrlPattern]:
    pats = []
    for m in mappings:
        field = f"mappings.{m.get('template_id', 'unknown')}.match.url_patterns"
        pats.extend(compile_patterns((m.get("match") or {}).get("url_patterns") or [], field))
    pats.extend(compile_patterns(include, "url_include"))
    return pats


@url_batch
def target_share(start_links: list[str], sitemap_set: set[str], patterns: list[UrlPattern], site: str) -> float | None:
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
        if matches_any(patterns, canonical):
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
