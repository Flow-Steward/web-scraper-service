"""Setup endpoints: analysis, samples, mapping test, proxy and solver tests."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import string
import time
from typing import Any

import httpx

from app.core.config import selected_tiers
from app.core.errors import ApiError, field_error, validation_failed
from app.core.ids import new_id
from app.core.netguard import url_is_private
from app.core.redaction import redact
from app.core.timeutil import iso, now, now_iso, plus
from app.core.urls import canonical_url, site_key
from app.core.url_patterns import compile_patterns, matches_any, url_batch, url_pattern_batch
from app.core.words import count as plural
from app.extraction.distill import sample_doc, suggest_template_id
from app.extraction.mapping import apply_mapping, looks_like_target, passes_target_selector, pick_template, required_set
from app.extraction.page import Page
from app.extraction.pricing import annotate as annotate_price, plain as plain_prices, price_view
from app.extraction.sampling import Explain, ProbePlan, link_groups, spread_by_shape, url_groups
from app.fetching.challenge import detect_html
from app.fetching.navigation import needs_browser_navigation
from app.fetching.protection import describe_failure
from app.fetching.oneoff import BrowserSession, Fetched, HostStopped, PrivateAddress, http_get
from app.fetching.proxies import tier_url
log = logging.getLogger("wss.setup")

from app.jobs.discovery import (  # noqa: E402
    collect_sitemap_urls,
    decide,
    filter_urls,
    find_sitemaps,
    http_fetcher,
    iter_sitemaps,
    robots_from,
    target_patterns,
    target_share,
)


# Analysis is a preview, not the job's full sitemap inventory. These limits include
# sitemap discovery and nested fetches; counts from a limited preview are lower bounds.
SITEMAP_METADATA_URL_CAP = 1_000
SITEMAP_METADATA_FILE_CAP = 20
SITEMAP_METADATA_SECONDS = 10.0


class _SitemapMetadataLimit(Exception):
    pass


async def _sitemap_metadata(site_url, config, robots, fetch):
    from contextlib import aclosing

    sitemaps, urls, latest = [], [], None
    limited = False
    cache = {}

    async def bounded_fetch(url):
        if url not in cache:
            if len(cache) >= SITEMAP_METADATA_FILE_CAP:
                raise _SitemapMetadataLimit
            cache[url] = await fetch(url)
        return cache[url]

    try:
        async with asyncio.timeout(SITEMAP_METADATA_SECONDS):
            sitemaps = await find_sitemaps(site_url, config.get("sitemap_url"), robots, bounded_fetch)
            async with aclosing(iter_sitemaps(sitemaps, bounded_fetch, max_files=SITEMAP_METADATA_FILE_CAP)) as entries:
                async for loc, lastmod in entries:
                    urls.append(loc)
                    if lastmod is not None and (latest is None or lastmod > latest):
                        latest = lastmod
                    if len(urls) >= SITEMAP_METADATA_URL_CAP:
                        limited = True
                        break
            # Reaching the file cap cannot establish that the index was exhausted.
            limited = limited or len(cache) >= SITEMAP_METADATA_FILE_CAP
    except (TimeoutError, _SitemapMetadataLimit):
        limited = True
    return sitemaps, urls, limited, latest


def _first_tier_proxy(config: dict, profile: dict) -> tuple[str | None, int | None]:
    tiers = selected_tiers(config, profile)
    if not tiers:
        return None, None
    session = "".join(random.choices(string.ascii_lowercase, k=8))
    try:
        return tier_url(tiers[0], session), tiers[0]["tier"]
    except ValueError:
        return None, None


async def _robots(origin_url: str, fetch=None):
    """Robots.txt through the profile's first tier and the job headers."""
    return await robots_from(origin_url, fetch or http_fetcher())


class SetupBrowserUnavailable(ApiError):
    """A dead setup browser cannot be diagnosed as a missing product selector."""

    def __init__(self, exception_type: str) -> None:
        super().__init__(
            "service_unavailable",
            f"The setup browser session closed or could not start ({exception_type}). "
            "Retry the setup request; if it repeats, check the scraper service logs.",
            details={"reason": "browser_unavailable", "exception_type": exception_type},
        )


class SetupFetch:
    """The fetches of one analysis or samples request: HTTP through the profile's first tier
    with the job headers, and — for a challenge or a page that needs JavaScript — ONE browser
    session for the whole request running the same protection route as a job (wait -> click
    -> solver, with the config's flags and caps). robots.txt and sitemaps that are challenged
    over HTTP are read through that session after clearance."""

    def __init__(self, svc, config: dict, profile: dict, *, strict_stop: bool = False) -> None:
        from app.fetching.browsers import resolve_browser
        from app.fetching.protection import RouteBudget

        self.svc = svc
        self.config = config
        self._destination_site_url = config.get("site_url") or next(
            (entry["url"] for entry in config.get("urls") or [] if entry.get("url")), None
        )
        # the job's page timeout for every fetch of the call (HTTP and browser navigation)
        self.page_timeout = int(config.get("page_timeout_seconds") or 45)
        self.proxy, self.tier = _first_tier_proxy(config, profile)
        self.headers = config.get("headers") or None
        self.browser = resolve_browser(config.get("browser", "auto")) or resolve_browser("auto")
        settings = svc.settings
        solver = None
        if config.get("use_solver") and settings.solvers_allowed and (profile or {}).get("solver"):
            from app.fetching.solvers import from_profile

            try:
                solver = from_profile(profile["solver"])
            except Exception:
                solver = None
        self.counters = {"clicks_used": 0, "solves_used": 0, "frame_not_found": 0}
        click_skip = None if config.get("challenge_click") and settings.challenge_click_allowed else (
            "not_allowed_by_service" if config.get("challenge_click") else "not_requested")
        solver_skip = None if solver is not None else (
            "not_requested" if not config.get("use_solver") else "not_allowed_by_service" if not settings.solvers_allowed else "not_configured")
        self.budget = RouteBudget(
            click_skip=click_skip,
            solver_skip=solver_skip,
            counters=self.counters,
            click_allowed=bool(config.get("challenge_click")) and settings.challenge_click_allowed,
            clicks_limit=int(config.get("max_clicks_per_job") or 0),
            solver=solver,
            solves_limit=int(((profile or {}).get("solver") or {}).get("max_solves_per_job", 0) or 0),
            wait_seconds=settings.challenge_wait_seconds,
            click_wait_seconds=settings.challenge_click_wait_seconds,
            humanized=self.browser == "camoufox",
        )
        self._session: BrowserSession | None = None
        self._session_failed = False
        # once a host failed the protection route, samples (strict) fetch nothing more of
        # it; analysis still reads robots.txt/sitemaps over plain HTTP but never runs the route
        # again for that host
        self.strict_stop = strict_stop
        self.skipped: dict[str, int] = {}  # host -> page URLs not fetched because of the stop
        self._http: dict[str, Fetched] = {}  # canonical URL -> the request's one HTTP load of it
        self._browser_hosts: set[str] = set()  # hosts that answered HTTP with a challenge: browser only from then on
        self.last_how = "none"  # how fetch_page got its last page: http | browser | reused | none
        self._browser: dict[str, tuple] = {}  # canonical URL -> the request's one browser load of it
        self.navigation: dict[str, Any] = {}
        self._navigation_robots: dict[str, Any] = {}
        self._navigation_actions = 0
        from app.jobs.throttle import HostThrottle

        floor = 60 / float(config.get("max_requests_per_minute") or 60000)
        self._navigation_throttle = HostThrottle(
            max(floor, config.get("delay_min_seconds", 0)),
            max(floor, config.get("delay_max_seconds", 0)),
        )

    async def _aux_destination_allowed(self, url: str, *, site_url: str | None = None) -> bool:
        """Auxiliary files keep their scope/file exemptions, with safe redirects."""
        from app.core.linkrules import enabled_rules
        from app.fetching.destinations import document_destination_allowed

        return await document_destination_allowed(
            url,
            site_url=site_url or self._destination_site_url or url,
            rules=enabled_rules(self.config) & {"sensitive_path", "action_params"},
            cancelled=lambda: bool(asyncio.current_task().cancelling()),
        )

    async def _destination_allowed(self, url: str, *, site_url: str | None = None, admitted_url: str | None = None) -> bool:
        """The same document destination rules for listing actions and redirects."""
        from app.core.linkrules import enabled_rules
        from app.core.scope import crawl_patterns
        from app.core.url_patterns import url_pattern_budget
        from app.core.urls import host_of
        from app.fetching.destinations import document_destination_allowed
        from app.jobs.discovery import _origin

        async def robots_allowed(destination):
            if not self.config.get("respect_robots", True):
                return True
            origin = _origin(destination)
            if origin not in self._navigation_robots:
                self._navigation_robots[origin] = await _robots(destination, self.http)
            robots = self._navigation_robots[origin]
            if robots is None:
                return True
            self._navigation_throttle.set_crawl_delay(host_of(destination), robots.get_crawl_delay())
            return robots.is_allowed(destination)

        with url_pattern_budget():
            if matches_any(compile_patterns(self.config.get("url_exclude") or [], "url_exclude"), _url_key(url)):
                return False
            store = getattr(self.svc, "mappings", None)
            mappings = [entry["mapping"] for entry in store.active_mappings(site_key(site_url or self._destination_site_url or url))] if store else []
            targets = target_patterns(mappings, self.config.get("url_include") or [])
            scope = crawl_patterns(self.config)
            if scope and self.config.get("discovery_mode") != "url_list" and (admitted_url is None or _url_key(url) != _url_key(admitted_url)):
                scope = [*scope, *targets]
            else:
                scope = []
            rules = enabled_rules(self.config)
            if matches_any(targets, _url_key(url)):
                rules = rules - {"facet_params"}  # target variants may use color/size as identity
            return await document_destination_allowed(
                url,
                site_url=site_url or self._destination_site_url or url,
                rules=rules,
                scope=scope,
                robots_allowed=robots_allowed,
                cancelled=lambda: bool(asyncio.current_task().cancelling()),
            )

    async def discover_links(self, url: str, page: Page | None = None):
        """Observe listing states while retaining the originally fetched document."""
        from app.fetching.navigation import NavigationLimits
        from app.core.urls import host_of

        key = _url_key(url)
        result = self.navigation.get(key)
        session = self._session
        if (
            result is None
            and session is not None
            and self.browser_cached(url) is not None
        ):
            got = self.browser_cached(url)
            if _url_key(session.page.url) != _url_key(got[2] or url):
                return None
            throttle = self._navigation_throttle

            async def allowed(action):
                document = action.kind == "navigation"
                if not document and self._navigation_actions >= min(
                    100, int(self.config.get("max_pages") or 12)
                ):
                    return False
                from app.core.linkrules import enabled_rules

                if (
                    getattr(action, "nofollow", False)
                    and "nofollow" in enabled_rules(self.config)
                ) or not await self._destination_allowed(action.url, site_url=url, admitted_url=url):
                    return False
                if not document:
                    await throttle.wait(host_of(action.url))
                    self._navigation_actions += 1
                return True

            try:
                result = await session.discover_links(
                    site_url=self.config.get("site_url") or url,
                    limits=NavigationLimits(
                        max_steps=min(100, int(self.config.get("max_pages") or 12)),
                        max_seconds=self.page_timeout,
                    ),
                    before_action=allowed,
                )
            except (PrivateAddress, HostStopped, SetupBrowserUnavailable):
                raise
            except Exception as exc:
                if session.is_closed:
                    await self._abort_browser(exc)
                raise
            if session.is_closed:
                await self._abort_browser(RuntimeError("browser closed during listing observation"))
            self.navigation[key] = result
        if page is not None and result is not None:
            page.extra_links = list(result.links)
            page.extra_anchors = list(result.anchors)
            page.deferred_links = list(getattr(result, "deferred_links", []))
        return result

    def failure_for(self, url: str) -> dict | None:
        return self._session.failure_for(url) if self._session is not None else None

    async def fetch_json(self, url: str):
        """A shop's product JSON for a variant source: through the call's cleared
        browser context when the host needed the browser (never a plain HTTP client for a
        Cloudflare host), else one plain HTTP request through the profile's first tier."""
        if await url_is_private(url):
            return None
        self._destination_site_url = self._destination_site_url or url
        try:
            if self.needs_browser(url) and self._session is not None:
                fetched = await self._session.raw(url, destination_policy=lambda destination: self._aux_destination_allowed(destination, site_url=url), headers={"Accept": "application/json"})
                return json.loads(fetched.text) if fetched is not None and fetched.status and fetched.status < 400 and not fetched.error else None
            if self.needs_browser(url):
                return None
            fetched = await http_get(url, proxy=self.proxy, headers={**(self.headers or {}), "Accept": "application/json"},
                                     timeout=float(self.page_timeout), destination_policy=lambda destination: self._aux_destination_allowed(destination, site_url=url))
            return json.loads(fetched.text) if fetched.status and fetched.status < 400 else None
        except (ValueError, PrivateAddress):
            return None
        except SetupBrowserUnavailable:
            raise
        except Exception as exc:
            if self._session is not None and self._session.is_closed:
                await self._abort_browser(exc)
            return None

    def needs_browser_navigation(self, page: Page) -> bool:
        """Render potential listing navigation while leaving usable targets on HTTP."""
        if not needs_browser_navigation(page, is_target=False):
            return False
        store = getattr(self.svc, "mappings", None)
        mappings = (
            [entry["mapping"] for entry in store.active_mappings(site_key(page.url))]
            if store is not None
            else []
        )
        canonical = canonical_url(page.url)
        return not (
            _url_looks_target(canonical, self.config, mappings)
            or pick_template(mappings, page, canonical) is not None
            or looks_like_target(page, self.config, False)
        )

    def needs_browser(self, url: str) -> bool:
        """A host that answered plain HTTP with a challenge, or passed one in the browser, is
        loaded only in the call's cleared tab from then on (its pages were probed over
        HTTP again, answered with challenges)."""
        from app.fetching.oneoff import host_label

        if not self.browser or self.config.get("fetch_mode") == "http":
            return False
        host = host_label(url)
        return host in self._browser_hosts or (self._session is not None and host in self._session.passed_hosts)

    def mark_challenged(self, url: str) -> None:
        from app.fetching.oneoff import host_label

        self._browser_hosts.add(host_label(url))

    @property
    def failures(self) -> list[dict]:
        return list(self._session.failures.values()) if self._session is not None else []

    def skip_if_stopped(self, url: str) -> bool:
        """True (and counted) when ``url``'s host failed the route earlier in this request."""
        failure = self.failure_for(url)
        if failure is None:
            return False
        self.skipped[failure["host"]] = self.skipped.get(failure["host"], 0) + 1
        return True

    async def session(self) -> BrowserSession | None:
        if self._session is None and self.browser and not self._session_failed:
            try:
                from app.fetching.browsers import Variant

                self._session = BrowserSession(self.browser, proxy=self.proxy, budget=self.budget, proxy_tier=self.tier,
                                                     debug=bool(self.config.get("debug")), shots=self.svc.samples,
                                                     variant=Variant.from_config(self.config),
                                                     delay=(0, 0),
                                                     timeout_s=self.page_timeout)
                self._session = await self._session.start()
            except Exception as exc:
                await self._abort_browser(exc)
        return self._session

    async def _abort_browser(self, exc: Exception) -> None:
        session, self._session = self._session, None
        self._session_failed = True
        if session is not None:
            await session.close()
        # No automatic relaunch: keep this request's deadline and protection budget.
        # Exception messages may contain proxy credentials; expose only the type.
        raise SetupBrowserUnavailable(type(exc).__name__) from None

    @property
    def traces(self) -> list[dict]:
        return list(self._session.traces) if self._session is not None else []

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def http(self, url: str) -> Fetched:
        """One HTTP load per URL and request (never load a URL twice in one call)."""
        self._destination_site_url = self._destination_site_url or url
        key = _url_key(url)
        hit = self._http.get(key)
        if hit is None:
            from app.core.urls import host_of
            await self._navigation_throttle.wait(host_of(url))
            hit = await http_get(
                url, proxy=self.proxy, headers=self.headers, timeout=float(self.page_timeout),
                destination_policy=(
                    (lambda destination: self._destination_allowed(destination, site_url=url))
                    if _is_document(url)
                    else (lambda destination: self._aux_destination_allowed(destination, site_url=url))
                ),
            )
            self._http[key] = hit
        return hit

    def browser_cached(self, url: str):
        return self._browser.get(_url_key(url))

    async def browser_get(self, url: str, *, wait_ms: int = 0, settle: bool = False):
        """One browser load per URL and request, in the request's single tab: the start page
        loaded (and cleared) for discovery is the same page a sample takes, never loaded again."""
        self._destination_site_url = self._destination_site_url or url
        key = _url_key(url)
        hit = self._browser.get(key)
        if hit is not None:
            return hit
        session = await self.session()
        if session is None:
            raise RuntimeError("no browser")
        from app.core.urls import host_of
        await self._navigation_throttle.wait(host_of(url))
        try:
            got = await session.get(url, wait_ms=wait_ms, settle=settle)
        except (PrivateAddress, HostStopped):
            raise
        except Exception as exc:
            if session.is_closed:
                await self._abort_browser(exc)
            raise
        if session.is_closed:
            await self._abort_browser(RuntimeError("browser closed during document read"))
        self._browser[key] = got
        if settle and got[0] and got[0] < 400 and detect_html(got[1], got[0]).kind == "none":
            await self.discover_links(url)
        return got

    def _browser_fetched(self, url, got):
        from dataclasses import asdict
        fetched = _as_fetched(url, got)
        navigation = self.navigation.get(_url_key(url))
        if navigation is not None:
            fetched.extra_links = list(navigation.links)
            fetched.extra_anchors = list(navigation.anchors)
            fetched.navigation = {k: v for k, v in asdict(navigation).items() if k not in ("links", "anchors")}
        return fetched

    async def fetch(self, url: str) -> Fetched | None:
        """Discovery fetcher (robots.txt, sitemaps, start page): HTTP, then the browser session
        when HTTP answers with a challenge — a page is loaded in the tab (and kept for samples),
        robots.txt and sitemaps are read through the session after clearance."""
        if self.strict_stop and self.failure_for(url) is not None:
            return None
        hit = self.browser_cached(url)
        if hit is not None:
            return self._browser_fetched(url, hit)
        page_in_browser = bool(self.browser) and _is_document(url) and (self.config.get("fetch_mode") == "browser" or self.needs_browser(url))
        if page_in_browser:  # Fetch mode browser, or a host that needed the browser: the page's links as the browser sees them
            try:
                return self._browser_fetched(url, await self.browser_get(url, wait_ms=self.config.get("wait_after_load_ms", 0), settle=True))
            except PrivateAddress:
                return None
            except SetupBrowserUnavailable:
                raise
            except Exception:
                pass
        try:
            fetched = await self.http(url)
        except PrivateAddress:
            return None
        head = fetched.body[:300_000].decode("utf-8", errors="replace") if fetched.body[:2] != b"\x1f\x8b" else ""
        challenged = bool(fetched.status) and detect_html(head, fetched.status).is_challenge
        if challenged:
            self.mark_challenged(url)
        if challenged and self.failure_for(url) is not None:
            return fetched  # the same failure as before: no second route for this host
        auto_page = (
            Page(fetched.text, fetched.final_url or url, fetched.status)
            if (
                not challenged
                and self.browser
                and self.config.get("fetch_mode") == "auto"
                and _is_document(url)
                and fetched.status
                and fetched.status < 400
            )
            else None
        )
        if auto_page is not None and (
            auto_page.visible_text_chars < 300
            or self.needs_browser_navigation(auto_page)
        ):
            try:  # a page that needs JavaScript: its links as the browser sees them
                return self._browser_fetched(
                    url,
                    await self.browser_get(
                        url,
                        wait_ms=self.config.get("wait_after_load_ms", 0),
                        settle=True,
                    ),
                )
            except PrivateAddress:
                return None
            except SetupBrowserUnavailable:
                raise
            except Exception:
                return fetched
        if challenged and self.browser and self.config.get("fetch_mode") != "http":
            try:
                if _is_document(url):
                    return self._browser_fetched(
                        url,
                        await self.browser_get(
                            url,
                            wait_ms=self.config.get("wait_after_load_ms", 0),
                            settle=True,
                        ),
                    )
                session = await self.session()
                result = await session.raw(url, destination_policy=lambda destination: self._aux_destination_allowed(destination, site_url=url)) if session else fetched
                if session is not None and session.is_closed:
                    await self._abort_browser(RuntimeError("browser closed during auxiliary fetch"))
                return result
            except PrivateAddress:
                return None
            except SetupBrowserUnavailable:
                raise
            except Exception:
                return fetched
        return fetched


def _url_key(url: str) -> str:
    try:
        return canonical_url(url)
    except ValueError:
        return url


def _is_document(url: str) -> bool:
    """A page (loaded in the tab), not robots.txt or a sitemap file (read as bytes)."""
    from urllib.parse import urlsplit

    path = urlsplit(url).path.lower()
    return not path.endswith((".xml", ".xml.gz", ".gz", ".txt"))


def _as_fetched(url: str, got: tuple) -> Fetched:
    status, html, final, _passed = got
    return Fetched(url, final or url, status or 200, (html or "").encode("utf-8"),
                   {"content-type": "text/html; charset=utf-8", "x-wss-loaded": "browser"}, None)


# ====================================================================== analysis
async def analyze(svc, body: dict) -> dict:
    config, profile = body["config"], body["profile"]
    site_url = config.get("site_url")
    if not site_url:
        raise validation_failed([field_error("config.site_url", "required", "analysis needs site_url")])
    if await url_is_private(site_url):
        raise validation_failed([field_error("config.site_url", "private_address_refused", "the host resolves to a private address")])
    sf = SetupFetch(svc, config, profile)
    started = time.monotonic()
    try:
        result = await _analyze(svc, config, site_url, sf)
    finally:
        await sf.close()
    sm, pr, rec = result["sitemap"], result["protection"], result["recommended"]
    log.info(redact(f"analysis site={result['site']} http={result['http_status']} robots={'yes' if result['robots']['present'] else 'no'} "
                    f"sitemap={sm['url_count']}{'(capped)' if sm['url_count_capped'] else ''} matching={sm['matching_url_count']} "
                    f"protection={pr['kind']}/{pr['passed_at_step']} js={result['rendering']['js_required']} "
                    f"recommended={rec['discovery_mode']}/{rec['fetch_mode']}/{rec['browser']} challenges={len(result.get('protection_trace') or [])} "
                    f"warnings={','.join(w['code'] for w in result['warnings']) or '-'} seconds={round(time.monotonic() - started)}"))
    return result


async def _analyze(svc, config: dict, site_url: str, sf: SetupFetch) -> dict:
    warnings: list[dict] = []
    fetch = sf.fetch
    robots = await _robots(site_url, fetch)
    robots_part = {
        "present": robots is not None,
        "site_url_allowed": robots.is_allowed(site_url) if robots is not None else True,
        "crawl_delay_seconds": robots.get_crawl_delay() if robots is not None else None,
        "sitemaps": [],
    }
    if robots is not None:
        try:
            robots_part["sitemaps"] = [u for u in robots.get_sitemaps(enqueue_strategy="all") if re.match(r"^https?://\S+$", u)][:50]
        except Exception:
            robots_part["sitemaps"] = []
    fetched = await sf.http(site_url)
    html = fetched.text if fetched.status else ""
    page = Page(html, fetched.final_url or site_url, fetched.status)
    verdict = detect_html(html, fetched.status)
    kind = "none"
    challenge = None
    if fetched.status == 429:
        kind = "rate_limited"
    elif verdict.is_challenge:
        kind = "challenge"
        challenge = verdict.challenge
    elif verdict.is_block:
        kind = "hard_block"
    passed_at = None
    browser = sf.browser
    browser_chars = None
    final_url = fetched.final_url or site_url
    status = fetched.status
    if browser and (verdict.kind != "none" or page.visible_text_chars < 500) and sf.failure_for(site_url) is None:
        try:
            b_status, b_html, b_final, b_passed = await sf.browser_get(site_url)
            b_page = Page(b_html, b_final, b_status)
            browser_chars = b_page.visible_text_chars
            b_verdict = detect_html(b_html, None)
            if verdict.kind != "none":
                if b_verdict.kind == "none":
                    passed_at = b_passed or browser
                    page = b_page
                    final_url = b_final
                    status = b_status
            elif b_verdict.kind == "none":
                page = b_page if browser_chars > page.visible_text_chars else page
        except PrivateAddress:
            raise validation_failed([field_error("config.site_url", "private_address_refused", "a redirect leads to a private address")])
        except HostStopped:
            pass
        except SetupBrowserUnavailable:
            raise
        except Exception as exc:
            warnings.append(field_error("browser", "invalid_value", f"the browser fetch failed ({type(exc).__name__})"))
    http_chars = Page(html, final_url, status).visible_text_chars if html else 0
    js_required = browser_chars is not None and browser_chars > max(500, http_chars * 2)
    requested_mode = config.get("discovery_mode", "auto")
    skipped = requested_mode in ("crawl", "url_list")
    sitemaps, urls, capped, latest = ([], [], False, None) if skipped else await _sitemap_metadata(site_url, config, robots, fetch)
    matching = filter_urls(urls, config.get("url_include") or [], config.get("url_exclude") or [])
    site = site_key(site_url)
    share = None
    if matching and not capped:
        links = page.links()
        share = target_share(links, set(matching), target_patterns(svc.mappings.active_mappings(site) and [m["mapping"] for m in svc.mappings.active_mappings(site)], config.get("url_include") or []), site)
    if skipped:
        mode, reasons = requested_mode, [f"explicit {requested_mode} discovery; sitemap inspection skipped"]
    elif capped:
        mode, reasons = ("sitemap_and_crawl" if matching else "crawl"), ["sitemap preview limited; full coverage is unknown"]
    else:
        mode, reasons = decide(bool(urls), len(matching), share)
    types = page.jsonld_types()
    wanted = config.get("structured_data_types") or ["Product", "Offer", "AggregateOffer"]
    needs_proxy = kind in ("hard_block", "rate_limited")
    needs_challenge = kind == "challenge" and passed_at is None
    fetch_mode = "browser" if (js_required or kind == "challenge") else "http"
    if js_required:
        reasons.append("the page needs JavaScript")
    if kind != "none":
        reasons.append(f"protection: {kind}")
    try:
        final_canonical = canonical_url(final_url)
    except ValueError:
        final_canonical = site_url
    for failure in sf.failures:
        warnings.append(field_error("site_url", "protection_not_passed", describe_failure(
            failure, "The challenge route was not run again for this host in this analysis (robots.txt and sitemaps were read over plain HTTP only).")))
    return {
        "site": site,
        "site_url": site_url,
        "final_url": final_canonical,
        "http_status": status,
        "robots": robots_part,
        "sitemap": {
            "found": None if skipped or (capped and not urls) else bool(urls),
            "url": sitemaps[0] if sitemaps and re.match(r"^https?://\S+$", sitemaps[0]) else None,
            "url_count": None if skipped or (capped and not urls) else len(urls),
            "url_count_capped": capped,
            "matching_url_count": None if skipped or (capped and not urls) else len(matching),
            "inspection": "skipped" if skipped else "limited" if capped else "complete",
            "target_share": share,
            "latest_lastmod": iso(latest) if latest else None,
        },
        "rendering": {"js_required": js_required, "http_text_chars": http_chars, "browser_text_chars": browser_chars},
        "structured_data": {"jsonld_types": types, "target_types_found": [t for t in wanted if t in types]},
        "protection": {"kind": kind, "challenge": challenge, "passed_at_step": passed_at},
        "recommended": {
            "discovery_mode": mode,
            "fetch_mode": fetch_mode,
            "browser": browser or "chromium",
            "needs_proxy": needs_proxy,
            "needs_challenge_handling": needs_challenge,
            "reasons": reasons,
        },
        "warnings": warnings,
        "link_rules": link_rules_view(config),
        **({"protection_trace": sf.traces} if sf.traces else {}),
    }


def link_rules_view(config: dict) -> dict:
    """The link rules a run of this config applies (README 4.11; shown in the analysis)."""
    from app.core import linkrules as lr

    off = set(config.get("link_rules_off") or [])
    return {
        "rules": [{"name": n, "on": n not in off, "description": lr.RULE_WORDS[n]} for n in lr.RULES],
        "sensitive_segments": sorted(lr.SENSITIVE_SEGMENTS),
        "prefix_words": list(lr.PREFIX_WORDS),
        "action_params": sorted(lr.ACTION_PARAMS),
        "file_extensions": sorted(lr.EXTENSIONS),
        "schemes": list(lr.SCHEMES),
        "messenger_social_hosts": list(lr.SOCIAL_HOSTS),
        "facet_params": sorted(lr.FACET_PARAMS) + [p + "*" for p in lr.FACET_PREFIXES],
        "trap_pattern_budget": int(config.get("trap_pattern_budget", 100) or 0),
    }


# ====================================================================== samples
async def fetch_page(svc, url: str, config: dict, sf: SetupFetch) -> tuple[Page | None, dict, str | None]:
    """HTTP first; the request's browser session (with the protection route) for challenges
    and pages that look empty."""
    tier = sf.tier
    sf.last_how = "none"
    if sf.skip_if_stopped(url):
        return None, {}, "protection_not_passed"
    browser = sf.browser
    hit = sf.browser_cached(url)
    if hit is None and sf.needs_browser(url):
        # the host needed the browser: straight to the cleared tab, no HTTP probe
        try:
            hit = await sf.browser_get(url, wait_ms=config.get("wait_after_load_ms", 0))
        except PrivateAddress:
            raise
        except HostStopped:
            return None, {}, "protection_not_passed"
        except SetupBrowserUnavailable:
            raise
        except Exception as exc:
            return None, {"fetch_mode": "browser", "browser": browser, "proxy_tier": tier, "passed_at_step": None}, f"browser fetch failed ({type(exc).__name__})"
        sf.last_how = "browser"
    elif hit is not None:
        sf.last_how = "reused"
    if hit is not None:  # already loaded in this request's tab (e.g. the start page for discovery)
        status, html, final, passed = hit
        page = Page(html, final, status)
        verdict = detect_html(html, None)
        info = {"fetch_mode": "browser", "browser": browser, "proxy_tier": tier,
                "passed_at_step": passed if verdict.kind == "none" else None}
    else:
        fetched = await sf.http(url)
        sf.last_how = "http"
        if not fetched.status:
            return None, {}, fetched.error or "fetch failed"
        page = Page(fetched.text, fetched.final_url, fetched.status)
        verdict = detect_html(page.html, fetched.status)
        if verdict.is_challenge:
            sf.mark_challenged(url)
        info = {"fetch_mode": "http", "browser": None, "proxy_tier": tier, "passed_at_step": None}
        want_browser = config.get("fetch_mode") == "browser" or (
            config.get("fetch_mode") == "auto"
            and (
                verdict.kind != "none"
                or page.visible_text_chars < 300
                or sf.needs_browser_navigation(page)
            )
        )
        if browser and want_browser:
            try:
                status, html, final, passed = await sf.browser_get(url, wait_ms=config.get("wait_after_load_ms", 0))
                sf.last_how = "browser"
                page = Page(html, final, status)
                info = {"fetch_mode": "browser", "browser": browser, "proxy_tier": tier,
                        "passed_at_step": (passed or browser) if verdict.kind != "none" and detect_html(html).kind == "none" else None}
                verdict = detect_html(html, None)
            except PrivateAddress:
                raise
            except HostStopped:
                return None, info, "protection_not_passed"
            except SetupBrowserUnavailable:
                raise
            except Exception as exc:
                if verdict.kind != "none":
                    return None, info, f"browser fetch failed ({type(exc).__name__})"
    if verdict.kind != "none":
        return None, info, f"protection: {verdict.kind}"
    if page.status and page.status >= 400:
        return None, info, f"HTTP {page.status}"
    return page, info, None


async def samples(svc, body: dict) -> dict:
    config, profile = body["config"], body["profile"]
    limits = svc.settings.limits
    count = config.get("sample_count", 5)
    errors = []
    if count > limits["max_sample_count"]:
        errors.append(field_error("config.sample_count", "limit_exceeded", "sample_count above the service limit", limits["max_sample_count"]))
    if body.get("urls") and len(body["urls"]) > limits["max_sample_count"]:
        errors.append(field_error("urls", "limit_exceeded", "too many URLs", limits["max_sample_count"]))
    if errors:
        raise validation_failed(errors)
    sf = SetupFetch(svc, config, profile, strict_stop=True)
    try:
        return await _samples(svc, body, config, count, sf)
    finally:
        await sf.close()


@url_batch
def _sample_link_groups(page, site, config, mappings):
    """Navigation scope applies to intermediate links, not extraction admission."""
    from app.core.scope import crawl_patterns

    pats = target_patterns(mappings, config.get("url_include") or [])
    groups, _ = link_groups(
        page, site, compile_patterns(config.get("url_exclude") or [], "url_exclude"), pats
    )
    scope = crawl_patterns(config)
    if scope:
        groups = [
            dict(
                g,
                urls=[
                    u
                    for u in g["urls"]
                    if matches_any([*scope, *pats], u)
                ],
            )
            for g in groups
        ]
    return groups


async def _samples(svc, body: dict, config: dict, count: int, sf: SetupFetch) -> dict:
    warnings: list[dict] = []
    template_id = body.get("template_id")
    site = site_key(body["urls"][0]) if body.get("urls") else site_key(config["site_url"]) if config.get("site_url") else None
    if site is None and config.get("urls"):
        site = site_key(config["urls"][0]["url"])
    if site is None:
        raise validation_failed([field_error("config.site_url", "required", "samples need site_url or urls")])
    mappings = [m["mapping"] | {"__version": m["version"]} for m in svc.mappings.active_mappings(site)]
    only = [m for m in mappings if m["template_id"] == template_id] if template_id else None
    from app.extraction.target_guess import product_signals

    ex = Explain()
    candidates: list[tuple[str, str | None]] = []
    explicit = bool(body.get("urls"))
    if explicit:
        candidates = [(u, None) for u in body["urls"]]
        ex.discovery.update(source="urls", candidates=len(candidates))
    elif config.get("discovery_mode") == "url_list" and config.get("urls"):
        candidates = [(e["url"], e.get("key")) for e in config["urls"][: count * 4]]
        ex.discovery.update(source="url_list", candidates=len(candidates))
    listed = not explicit and config.get("discovery_mode") == "url_list" and bool(config.get("urls"))
    plan: ProbePlan | None = None
    if not explicit and not listed:
        plan = await _discover_candidates(config, mappings, count, sf.fetch, ex)

    selector = config.get("target_page_selector")

    def page_facts(page) -> dict:
        from app.core.urls import site_key as _sk

        try:
            here = _sk(page.url)
            links = len({u for u in page.links() if _safe_site(u) == here})
        except ValueError:
            links = 0
        return {"status": page.status, "links": links, "selector_found": page.has_selector(selector) if selector else None}

    def signals_of(page) -> list[str]:
        from app.extraction.target_guess import SIGNAL_NAMES

        try:
            return sorted(product_signals(page), key=list(SIGNAL_NAMES).index)
        except Exception:
            return []

    out: list[dict] = []
    taken = {m["template_id"] for m in mappings}
    seen_pages = 0
    for idx, (url, key) in (
        _plan_iter(plan, ex) if plan is not None else enumerate(candidates)
    ):
        if len(out) >= count and not explicit:
            break
        shown = redact(url)
        if sf.skip_if_stopped(url):
            ex.page(shown, "none", "skipped", "the host failed the protection route earlier in this call")
            continue  # the host failed the protection route; nothing more of it is fetched
        if await url_is_private(url):
            warnings.append(field_error(f"urls[{idx}]" if explicit else "site_url", "private_address_refused", "the host resolves to a private address"))
            ex.page(shown, "none", "skipped", "private address")
            continue
        try:
            page, info, err = await fetch_page(svc, url, config, sf)
            if (
                page is not None
                and plan is not None
                and config.get("fetch_mode") == "auto"
                and sf.last_how == "http"
                and sf.browser
                and sf.needs_browser_navigation(page)
                and plan.depths.get(canonical_url(page.url), 1) < 4
            ):
                # Fresh SSR links do not establish that a scripted listing is complete.
                # Its guarded browser observation may reveal additional listing states.
                try:
                    await sf.browser_get(
                        url, wait_ms=config.get("wait_after_load_ms", 0), settle=True
                    )
                    page, info, err = await fetch_page(svc, url, config, sf)
                    sf.last_how = "browser"
                except PrivateAddress:
                    raise
                except SetupBrowserUnavailable:
                    raise
                except Exception as exc:
                    page, err = None, f"browser discovery failed ({type(exc).__name__})"
        except PrivateAddress:
            warnings.append(
                field_error(
                    f"urls[{idx}]" if explicit else "site_url",
                    "private_address_refused",
                    "a redirect leads to a private address",
                )
            )
            ex.page(shown, sf.last_how, "skipped", "redirect to a private address")
            continue
        seen_pages += 1
        if page is None:
            if explicit:
                warnings.append(field_error(f"urls[{idx}]", "invalid_value", redact(f"{url}: {err}")[:500]))
            reason = str(err or "")
            outcome = "challenged" if reason.startswith("protection") else "failed"
            if reason == "protection_not_passed":
                outcome, reason = "skipped", "the host failed the protection route earlier in this call"
            elif reason.startswith("protection") and sf.last_how == "http" and not sf.browser:
                reason += " (over HTTP; no browser in this service)"
            elif reason.startswith("protection") and sf.last_how == "http":
                reason += " (over HTTP; Fetch mode http keeps it out of the browser)"
            m = re.search(r"HTTP (\d{3})", reason)
            ex.page(shown, sf.last_how, outcome, reason, status=int(m.group(1)) if m else None)
            continue
        how, signals = sf.last_how, signals_of(page)
        facts = page_facts(page)
        try:
            canonical = canonical_url(page.url)
        except ValueError:
            ex.page(shown, how, "failed", "no usable final URL", signals, **facts)
            continue
        if plan is not None:
            await sf.discover_links(url, page)
            groups = _sample_link_groups(page, site, config, mappings)
            plan.extend(
                groups,
                canonical,
                preferred=target_patterns(mappings, config.get("url_include") or []),
            )
            ex.discovery["shapes"] = len(plan.groups)
        if (
            not explicit
            and not listed
            and config.get("url_include")
            and not matches_any(compile_patterns(config["url_include"], "url_include"), canonical)
        ):
            ex.page(
                shown,
                how,
                "not_target",
                "outside url_include (links still explored)",
                signals,
                **facts,
            )
            continue
        if not explicit and not listed and not passes_target_selector(page, config):
            ex.page(shown, how, "not_target", "target_page_selector absent", signals, **facts)
            continue  # without target_page_selector a page is not a target page
        tmpl = pick_template(only if only is not None else mappings, page, canonical)
        if only is not None and tmpl is None:
            ex.page(shown, how, "not_target", f"template {template_id} does not match", signals, **facts)
            continue
        if (
            not explicit
            and not listed
            and tmpl is None
            and not looks_like_target(page, config, False)
            and not _url_looks_target(canonical, config, mappings)
        ):
            ex.page(
                shown,
                how,
                "not_target",
                "no matching template, no structured data of the wanted types, no selector or URL pattern hint",
                signals,
                **facts,
            )
            continue
        ex.page(shown, how, "sample", None, signals, **facts)
        extracted = plain_prices(apply_mapping(tmpl, page, config.get("extraction_mode", "auto")).data) if tmpl else None
        ts = now()
        sample = sample_doc(
            sample_id=new_id("smp"), page=page, requested_url=url, source_key=key,
            fetched_at=iso(ts), expires_at=iso(plus(svc.settings.samples_hours * 3600, ts)), fetch=info,
            matched_template_id=tmpl["template_id"] if tmpl else None,
            suggested_template_id=tmpl["template_id"] if tmpl else suggest_template_id(canonical, taken),
            structured_types=config.get("structured_data_types") or [], max_bytes=svc.settings.limits["sample_html_max_bytes"],
            extracted=extracted,
        )
        await _attach_variant_sources(svc, sample, page, sf, ex)
        svc.samples.save(sample, page.html, page.url)
        out.append(sample)
    if not out and not explicit and _no_target_hints(config, mappings) and config.get("site_url") and sf.failure_for(config["site_url"]) is None:
        guessed = await _guessed_samples(svc, config, sf, count, taken, warnings, ex)
        out += guessed
    requested = count if not explicit else len(body["urls"])
    if len(out) < count and not explicit:
        warnings.append(field_error("sample_count", "invalid_value",
                                    f"found {len(out)} of {plural(count, 'target page')}: {ex.summary(config, count)}"[:2000]))
    result = {"site": site, "requested_count": requested, "samples": out, "warnings": warnings,
              "explanation": ex.public(config, requested)}
    log.info(redact(ex.log_line(site)))
    stopped = None
    for failure in sf.failures:
        skipped = sf.skipped.get(failure["host"], 0)
        field = "site_url"
        if explicit and failure["url"] in body["urls"]:
            field = f"urls[{body['urls'].index(failure['url'])}]"
        elif not config.get("site_url"):
            field = "urls"
        tail = (f"No further pages of this host were fetched ({plural(skipped, 'page')} skipped)." if skipped
                else "No further pages of this host were fetched.")
        warnings.append(field_error(field, "protection_not_passed", describe_failure(failure, tail)))
        if stopped is None:
            stopped = {"reason": "protection_not_passed", "host": failure["host"], "url": redact(failure["url"])[:2048], "skipped_urls": skipped}
    if stopped is not None:
        result["stopped"] = stopped
    if sf.traces:
        result["protection_trace"] = sf.traces
    return result


async def _attach_variant_sources(svc, sample: dict, page, sf: SetupFetch, ex: Explain | None) -> None:
    """What the AI may choose from — the variant sources found on the sample (in-page
    JSON, JSON-LD/microdata offers or ``hasVariant``, the shop's product JSON for Shopify and
    WooCommerce pages, fetched once and kept with the sample for the mapping test) and the
    page's microdata."""
    from app.extraction import variants as V

    found = list(sample.get("variant_sources") or []) if "variant_sources" in sample else V.detect_sources(page)
    captured: dict = {}

    async def remember(url: str):
        doc = await sf.fetch_json(url)
        if doc is not None:
            captured[url] = doc
        return doc

    for platform in V.page_platforms(page):
        src = {"kind": "shop_json", "platform": platform}
        try:
            got = await V.from_shop_json(page, src, remember)
        except SetupBrowserUnavailable:
            raise
        except Exception:
            got = []
        if got:
            found.append(V._described(src, got))
    if captured:
        svc.samples.save_extra(sample["sample_id"], {"shop_json": captured})
    sample["variant_sources"] = found
    if page.microdata_nodes:
        sample["microdata"] = [n for n in page.microdata_nodes if isinstance(n, dict)][:20]
    if ex is not None:
        ex.variants.append([d["source"] | {"count": d["count"]} for d in found])


def _safe_site(url: str) -> str | None:
    try:
        return site_key(url)
    except ValueError:
        return None


def _plan_iter(plan: ProbePlan, ex: Explain):
    """The plan's candidates, telling it after each one whether that page became a sample."""
    i, prev = 0, None
    while True:
        if prev is not None and ex.pages and ex.pages[-1]["url"] == redact(prev)[:2048]:
            plan.feedback(prev, ex.pages[-1]["outcome"] == "sample")
        url = plan.next()
        if url is None:
            return
        prev = url
        yield i, (url, None)
        i += 1


def _no_target_hints(config: dict, mappings: list[dict]) -> bool:
    return not (config.get("url_include") or config.get("target_page_selector") or mappings or config.get("discovery_mode") == "url_list")


async def _guessed_samples(svc, config: dict, sf: SetupFetch, count: int, taken: set, warnings: list, ex: Explain | None = None) -> list[dict]:
    """No structured data and no hint: guess the target pages by URL shape and page structure."""
    from app.extraction.target_guess import guess_targets

    fetched: dict[str, tuple] = {}

    async def fetch(url: str):
        if url in fetched:
            return fetched[url][0]
        if await url_is_private(url):
            fetched[url] = (None, {}, "private")
            return None
        try:
            page, info, err = await fetch_page(svc, url, config, sf)
        except PrivateAddress:
            page, info, err = None, {}, "private"
        if page is not None and (page.status or 200) >= 400:
            err, page = f"HTTP {page.status}", None
        if ex is not None:
            from app.extraction.target_guess import product_signals

            ex.page(redact(url), sf.last_how, "probed" if page is not None else ("challenged" if str(err).startswith("protection") else "failed"),
                    None if page is not None else str(err or "")[:200], sorted(product_signals(page)) if page is not None else None)
        fetched[url] = (page, info, err)
        return page

    exclude = compile_patterns(config.get("url_exclude") or [], "url_exclude")
    guess = await guess_targets(config["site_url"], fetch, exclude)
    if guess is None:
        if ex is not None:
            ex.guess = {"shape": None, "message": "no group of at least 3 leaf pages with one URL shape was found"}
        return []
    shape, members, signals = guess
    if ex is not None:
        ex.guess = {"shape": shape, "members": len(members), "signals": signals}
    out: list[dict] = []
    for url in members:
        if len(out) >= count:
            break
        page = await fetch(url)
        if page is None:
            continue
        info = fetched[url][1]
        try:
            canonical = canonical_url(page.url)
        except ValueError:
            continue
        ts = now()
        sample = sample_doc(
            sample_id=new_id("smp"), page=page, requested_url=url, source_key=None,
            fetched_at=iso(ts), expires_at=iso(plus(svc.settings.samples_hours * 3600, ts)), fetch=info,
            matched_template_id=None, suggested_template_id=suggest_template_id(canonical, taken),
            structured_types=config.get("structured_data_types") or [], max_bytes=svc.settings.limits["sample_html_max_bytes"],
            extracted=None,
        )
        await _attach_variant_sources(svc, sample, page, sf, ex)
        svc.samples.save(sample, page.html, page.url)
        out.append(sample)
        if ex is not None:
            ex.mark_sample(redact(url))
    if out:
        from app.extraction.target_guess import describe_signals

        why = (f"chosen for its product signals: {describe_signals(signals)}" if signals else
               "no page group showed product signals (price, add-to-cart, product tags, gallery), so the largest group of leaf pages was taken")
        warnings.append(field_error(
            "sample_count", "target_pages_guessed",
            (f"no structured data and no hint: target pages guessed from the URL shape {shape} ({plural(len(members), 'page')} of that shape), "
             f"{why}; set url_include or target_page_selector to choose them explicitly")[:2000],
        ))
    return out


def _url_looks_target(canonical: str, config: dict, mappings: list[dict]) -> bool:
    pats = target_patterns(mappings, config.get("url_include") or [])
    return matches_any(pats, canonical)


async def _discover_candidates(config: dict, mappings: list[dict], count: int, fetch=None, ex: Explain | None = None) -> ProbePlan:
    """Sample candidates as a probing plan. The start page's links are
    grouped by URL shape and ONE link of each shape is tried first (content links and small
    groups before a site-wide menu's categories, query variants never), a shape is dropped
    after 2 tried pages without a sample, and a shape that yields a sample is sampled on.
    Sitemap URLs: the same over their shapes (largest first). With ``discovery_mode`` auto and
    a start URL below the site root (a category), the start page's links come first and the
    sitemap after them; ``crawl`` uses only the links."""
    from urllib.parse import urlsplit

    ex = ex or Explain()
    site_url = config["site_url"]
    mode = config.get("discovery_mode") or "auto"
    include, exclude = config.get("url_include") or [], config.get("url_exclude") or []
    inc = compile_patterns(include, "url_include")
    pats = target_patterns(mappings, include)
    deep = urlsplit(site_url).path.strip("/") != ""
    ex.discovery["start_is_deep"] = deep
    limit, links_limit = count * 4, max(count * 6, 20)

    sitemap_groups: list[dict] = []
    preferred: list[str] = []
    if mode != "crawl":
        robots = await _robots(site_url, fetch)
        sitemaps = await find_sitemaps(site_url, config.get("sitemap_url"), robots, fetch)
        if sitemaps:
            urls, capped, _ = await collect_sitemap_urls(sitemaps, cap=5000, fetch=fetch)
            ex.discovery.update(sitemap_urls=len(urls), sitemap_capped=capped)
            with url_pattern_batch():
                matching = filter_urls(urls, include, exclude)
                ex.discovery["sitemap_matching"] = len(matching)
                preferred = [u for u in matching if matches_any(pats, u)] if pats else []
                sitemap_groups = url_groups(matching, include=inc)
    groups: list[dict] = []
    if mode == "crawl" or not (sitemap_groups or preferred) or (deep and mode == "auto" and not pats):
        groups = await _start_link_groups(site_url, fetch, exclude, ex, config)
        # Include patterns select extracted pages, not intermediate navigation.
        from app.core.scope import crawl_patterns

        with url_pattern_batch():
            scope = crawl_patterns(config)
            if scope and pats:  # inside the walk scope, or a target by URL
                groups = [dict(g, urls=[u for u in g["urls"] if matches_any([*scope, *pats], u)]) for g in groups]
                groups = [g for g in groups if g["urls"]]
            if pats:  # a known target pattern (url_include, an active mapping): matching shapes first
                groups.sort(key=lambda g: 0 if any(matches_any(pats, u) for u in g["urls"]) else 1)
            ex.discovery["links"] = sum(len(g["urls"]) for g in groups)
            # A loaded start page can itself be the requested record. Intermediate
            # links must not displace it merely because they now remain traversable.
            if _url_looks_target(canonical_url(site_url), config, mappings) and filter_urls(
                [canonical_url(site_url)], [], config.get("url_exclude") or []
            ):
                from app.extraction.sampling import url_shape

                shape = url_shape(site_url)
                own = next((g for g in groups if g["shape"] == shape), None)
                if own is None:
                    own = {"shape": shape, "urls": [], "links": 0, "in_menus": 0.0}
                else:
                    groups.remove(own)
                if site_url not in own["urls"]:
                    own["urls"].insert(0, site_url)
                    own["links"] += 1
                groups.insert(0, own)
    if groups and (sitemap_groups or preferred):
        ex.discovery["source"] = "start_page+sitemap"
        plan = ProbePlan(groups, limit=links_limit + limit, tail=preferred[:limit] or spread_by_shape(
            [u for g in sitemap_groups for u in g["urls"]], limit))
    elif groups:
        ex.discovery["source"] = "crawl" if mode == "crawl" else "start_page"
        plan = ProbePlan(groups, limit=links_limit)
    elif preferred:
        ex.discovery["source"] = "sitemap"
        plan = ProbePlan([], limit=limit, tail=preferred)
    elif sitemap_groups:
        ex.discovery["source"] = "sitemap"
        plan = ProbePlan(sitemap_groups, limit=limit)
    else:
        ex.discovery["source"] = "start_page"
        plan = ProbePlan([], limit=links_limit, tail=[site_url])
    plan.depths[_url_key(site_url)] = 0
    if ex.discovery.get("start_page"):
        plan._known.add(_url_key(site_url))  # backlinks cannot reprobe discovery's root
    ex.discovery["shapes"] = len(plan.groups)
    return plan


async def _start_link_groups(site_url: str, fetch, exclude: list[str], ex: Explain, config: dict) -> list[dict]:
    fetched = await (fetch or http_fetcher())(site_url)
    loaded = (fetched.headers.get("x-wss-loaded") or "http") if fetched is not None else "none"
    if fetched is None or not fetched.status or fetched.status >= 400:
        status = fetched.status if fetched is not None else None
        ex.page(redact(site_url), loaded, "failed", f"start page: HTTP {status}" if status else "start page not loaded", status=status)
        ex.discovery["start_page"] = {"url": redact(site_url), "status": status, "loaded": loaded}
        return []
    page = Page(
        fetched.text, fetched.final_url or site_url, fetched.status,
        extra_links=fetched.extra_links, extra_anchors=fetched.extra_anchors,
        deferred_links=(fetched.navigation or {}).get("deferred_links", []),
    )
    groups, stats = link_groups(page, site_key(site_url), compile_patterns(exclude, "url_exclude"), compile_patterns(config.get("url_include") or [], "url_include"))
    selector = config.get("target_page_selector")
    ex.page(redact(site_url), loaded, "start", None, status=fetched.status, links=stats["in_site"],
            selector_found=page.has_selector(selector) if selector else None)
    ex.discovery["start_page"] = {
        "url": redact(site_url), "status": fetched.status, "loaded": loaded, **stats,
        "shapes": [{"shape": g["shape"], "links": g["links"], "in_menus": g["in_menus"]}
                   for g in sorted(groups, key=lambda g: -g["links"])[:12]],
    }
    # Fresh suppliers have no mapping or URL pattern yet. Preserve the loaded
    # target as a candidate without counting a manufactured self-link in stats.
    if (
        passes_target_selector(page, config)
        and looks_like_target(page, config, False)
        and filter_urls(
            [canonical_url(page.url)],
            config.get("url_include") or [],
            config.get("url_exclude") or [],
        )
    ):
        from app.extraction.sampling import url_shape

        shape = url_shape(site_url)
        own = next((group for group in groups if group["shape"] == shape), None)
        if own is None:
            own = {"shape": shape, "urls": [], "links": 0, "in_menus": 0.0}
        else:
            groups.remove(own)
        if site_url not in own["urls"]:
            own["urls"].insert(0, site_url)
            own["links"] += 1
        groups.insert(0, own)
    return groups


# ====================================================================== mapping test
def validate_mapping_rules(mapping: dict, svc, prefix: str) -> list[dict]:
    errors = []
    limits = svc.settings.limits
    names: set[str] = set()
    fields = mapping.get("fields") or []
    if len(fields) > limits["max_fields_per_template"]:
        errors.append(field_error(f"{prefix}.fields", "limit_exceeded", "too many fields", limits["max_fields_per_template"]))
    for i, f in enumerate(fields):
        if f["name"] in names:
            errors.append(field_error(f"{prefix}.fields[{i}].name", "duplicate_field_name", f"field {f['name']} appears twice"))
        names.add(f["name"])
        if len(f.get("sources") or []) > limits["max_sources_per_field"]:
            errors.append(field_error(f"{prefix}.fields[{i}].sources", "limit_exceeded", "too many sources", limits["max_sources_per_field"]))
        for j, s in enumerate(f.get("sources") or []):
            if len(s.get("transforms") or []) > limits["max_transforms_per_source"]:
                errors.append(field_error(f"{prefix}.fields[{i}].sources[{j}].transforms", "limit_exceeded", "too many transforms", limits["max_transforms_per_source"]))
            for k, t in enumerate(s.get("transforms") or []):
                if t.get("pattern"):
                    try:
                        re.compile(t["pattern"])
                    except re.error:
                        errors.append(field_error(f"{prefix}.fields[{i}].sources[{j}].transforms[{k}].pattern", "pattern_invalid", "the regular expression does not compile"))
            if s.get("selector"):
                if not _css_ok(s["selector"]):
                    errors.append(field_error(f"{prefix}.fields[{i}].sources[{j}].selector", "invalid_value", "the CSS selector does not parse"))
    block = mapping.get("variants") or {}
    for i, src in enumerate(block.get("sources") or []):
        if src.get("selector") and not _css_ok(src["selector"]):
            errors.append(field_error(f"{prefix}.variants.sources[{i}].selector", "invalid_value", "the CSS selector does not parse"))
    seen_vf: set[str] = set()
    for i, spec in enumerate(block.get("fields") or []):
        if spec["name"] in seen_vf:
            errors.append(field_error(f"{prefix}.variants.fields[{i}].name", "duplicate_field_name", f"variant field {spec['name']} appears twice"))
        seen_vf.add(spec["name"])
        selector = spec.get("selector_template")
        if selector and not _css_ok(
            selector.replace("{id}", "sample").replace("{sku}", "sample")
        ):
            errors.append(
                field_error(
                    f"{prefix}.variants.fields[{i}].selector_template",
                    "invalid_value",
                    "the CSS selector template does not parse",
                )
            )
    for i, condition in enumerate(mapping.get("record_filter") or []):
        if condition["field"] not in names | seen_vf:
            errors.append(
                field_error(
                    f"{prefix}.record_filter[{i}].field",
                    "invalid_value",
                    "record filter must name a declared extracted field",
                )
            )
    if len(names | (seen_vf - names)) > limits["max_fields_per_template"]:
        errors.append(field_error(f"{prefix}.variants.fields", "limit_exceeded", "too many fields with the variant fields", limits["max_fields_per_template"]))
    match = mapping.get("match") or {}
    for i, p in enumerate(match.get("url_patterns") or []):
        try:
            re.compile(p)
        except re.error:
            errors.append(field_error(f"{prefix}.match.url_patterns[{i}]", "pattern_invalid", "the regular expression does not compile"))
    if match.get("selector") and not _css_ok(match["selector"]):
        errors.append(field_error(f"{prefix}.match.selector", "invalid_value", "the CSS selector does not parse"))
    return errors


def _css_ok(selector: str) -> bool:
    from cssselect import GenericTranslator, SelectorError

    try:
        GenericTranslator().css_to_xpath(selector)
        return True
    except SelectorError:
        return False
    except Exception:
        return False


async def mapping_test(svc, body: dict) -> dict:
    mappings = body["mappings"]
    errors = []
    for i, m in enumerate(mappings):
        errors += validate_mapping_rules(m, svc, f"mappings[{i}]")
    if errors:
        raise validation_failed(errors)
    missing_ids = []
    loaded = []
    for sid in body["sample_ids"]:
        found = svc.samples.load(sid)
        if found is None:
            missing_ids.append(sid)
        else:
            loaded.append((sid, found))
    if missing_ids:
        raise ApiError("sample_not_found", "unknown or expired sample ids", details={"sample_ids": missing_ids})
    threshold = float(body["required_fill_threshold"])
    extra_required = body.get("required_fields") or []
    mode = body.get("extraction_mode", "auto")
    ordered = sorted(mappings, key=lambda m: m["template_id"])
    per_template: dict[str, list[tuple[str, Any]]] = {m["template_id"]: [] for m in ordered}
    evaluated_rows: dict[str, list[tuple[str, dict, Any, dict]]] = {
        m["template_id"]: [] for m in ordered
    }
    sample_results = []
    unmatched = []
    per_variant: dict[str, list] = {}
    checks: dict[str, dict] = {}  # per sample, the page's price signal and variant data
    for sid, (doc, html, final_url) in loaded:
        page = Page(html, final_url, doc.get("http_status", 200))
        try:
            canonical = canonical_url(final_url)
        except ValueError:
            canonical = final_url
        tmpl = pick_template(ordered, page, canonical)
        if tmpl is None:
            unmatched.append(sid)
            sample_results.append({"sample_id": sid, "url": doc["url"], "template_id": None, "data": None, "missing_required": []})
            continue
        ext = apply_mapping(tmpl, page, mode)
        required = required_set(tmpl, extra_required)
        per_template[tmpl["template_id"]].append((sid, ext))
        view = price_view(page)  # what the page shows of its price, whatever the mapping reads
        own_record = True  # the page's own record decides whether a price was read (no variant rows)
        result = {"sample_id": sid, "url": doc["url"], "template_id": tmpl["template_id"], "data": ext.data, "missing_required": ext.missing(required),
                  "price_on_page": view.signal}
        sources_found = doc["variant_sources"] if "variant_sources" in doc else _in_page_sources(page)
        checks[sid] = {"signal": view.signal, "has_price": False, "variant_data": any((v or {}).get("count", 0) >= 2 for v in sources_found or [])}
        if tmpl.get("variants"):
            # the variant source applied to the stored sample (shop JSON as fetched with the sample)
            from app.extraction.variants import resolve, variant_rows

            captured = svc.samples.load_extra(sid).get("shop_json") or {}

            async def stored_json(url: str, _c=captured):
                return _c.get(url)

            variants, vinfo = await resolve(page, tmpl["variants"], stored_json)
            rows = variant_rows(
                tmpl,
                ext.data,
                variants,
                (vinfo or {}).get("kind"),
                page.base_url,
                page=page,
            )
            from app.extraction.record_filter import (
                record_filter_missing,
                record_matches,
            )

            raw_count = len(rows)
            filter_missing = {
                name
                for data, _meta, _filled in rows
                for name in record_filter_missing(tmpl, data)
            }
            rows = [row for row in rows if record_matches(tmpl, row[0])]
            result["missing_required"] = sorted(
                filter_missing
                | {
                    name
                    for data, _meta, _filled in rows
                    for name in required
                    if data.get(name) is None or data.get(name) == []
                }
            )
            checks[sid]["excluded"] = bool(raw_count and not rows)
            row_keys = [annotate_price(tmpl, data, view) for data, _meta, _f in rows]
            if variants:
                checks[sid]["has_price"] = any(k.get("price_status") in ("shown", "range") for k in row_keys)
            result["variants"] = {
                "count": len(variants),
                "included_count": len(rows),
                "excluded_count": raw_count - len(rows),
                "source": (vinfo or {}).get("kind"),
                "source_index": (vinfo or {}).get("index"),
                "items": [
                    {"variant": meta, "data": data, **keys}
                    for (data, meta, _f), keys in zip(rows[:20], row_keys)
                ]
                if variants
                else [],
            }
            per_variant.setdefault(tmpl["template_id"], []).append((sid, variants, rows, (vinfo or {}).get("kind")))
            evaluated_rows[tmpl["template_id"]].extend(
                (sid, data, ext, filled) for data, _meta, filled in rows
            )
            own_record = not variants
        keys = annotate_price(tmpl, ext.data, view)
        for name, value in ext.data.items():
            if value is None and ext.filled.get(name):
                ext.filled[name] = False  # a default currency emptied on a record without a price
        if not tmpl.get("variants"):
            from app.extraction.record_filter import (
                record_filter_missing,
                record_matches,
            )

            checks[sid]["excluded"] = not record_matches(tmpl, ext.data)
            result["missing_required"] = sorted(
                set(record_filter_missing(tmpl, ext.data))
                | set([] if checks[sid]["excluded"] else ext.missing(required))
            )
            if not checks[sid]["excluded"]:
                evaluated_rows[tmpl["template_id"]].append((sid, ext.data, ext, {}))
        result.update(keys)
        if (
            own_record
            and not checks[sid].get("excluded")
            and keys.get("price_status") in ("shown", "range")
        ):
            checks[sid]["has_price"] = True
            # a price read from visible text against what the page states for programs
            from app.extraction.pricing import machine_price, price_field, prices_differ

            pname = price_field(tmpl)
            stated = machine_price(page)
            mapped = ext.data.get(pname) if pname else None
            sale = view.sale
            if (
                sale is not None
                and isinstance(mapped, (int, float))
                and not prices_differ(float(mapped), sale["regular"], "sale")
            ):
                checks[sid]["old_price"] = (
                    float(mapped),
                    sale["current"],
                )  # the struck-through amount was read as the price
            elif (
                stated is not None
                and isinstance(mapped, (int, float))
                and prices_differ(float(mapped), stated[0], stated[1])
            ):
                checks[sid]["mismatch"] = (float(mapped), stated[0], stated[1])
        sample_results.append(result)
    templates = []
    warnings: list[dict] = []
    any_matched = False
    all_passed = True
    for m in ordered:
        tid = m["template_id"]
        hits = per_template[tid]
        evaluated = evaluated_rows[tid]
        required = required_set(m, extra_required)
        names = [f["name"] for f in m["fields"]]
        reported_fields = list(m["fields"])
        variant_specs = {
            spec["name"]: spec for spec in (m.get("variants") or {}).get("fields") or []
        }
        for name in sorted(required - set(names)):
            if name in variant_specs:
                reported_fields.append(
                    {
                        "name": name,
                        "type": variant_specs[name].get("type", "text"),
                        "sources": [],
                    }
                )
                names.append(name)
        fields_out = []
        t_passed = bool(hits) and not any(
            r["missing_required"] for r in sample_results if r["template_id"] == tid
        )
        for f in reported_fields:
            filled = [
                data
                for _sid, data, _ext, _vf in evaluated
                if data.get(f["name"]) is not None and data.get(f["name"]) != []
            ]
            rate = round(len(filled) / len(evaluated), 4) if evaluated else 0.0
            is_req = f["name"] in required
            passed = (not evaluated or rate >= threshold) if is_req else True
            if is_req and not passed:
                t_passed = False
            hits_by_source = [0] * len(f["sources"])
            for _sid, _data, ext, variant_filled in evaluated:
                if variant_filled.get(f["name"]):
                    continue  # a variant source, not the page source, supplied this value
                idx = ext.source_hits.get(f["name"])
                if idx is not None:
                    hits_by_source[idx] += 1
            examples = [data[f["name"]] for data in filled][:3]
            errs = [{"sample_id": sid, "message": msg} for sid, ext in hits for msg in ext.errors.get(f["name"], [])]
            notes = [{"sample_id": sid, **n} for sid, ext in hits for n in ext.notes.get(f["name"], [])][:10]
            fields_out.append(
                {
                    "name": f["name"],
                    "type": f["type"],
                    "required": is_req,
                    "sample_count": len(evaluated),
                    "filled_count": len(filled),
                    "fill_rate": rate,
                    "passed": passed and bool(hits),
                    "example_values": examples,
                    "source_hits": hits_by_source,
                    "errors": errs,
                    **({"reading_rules": notes} if notes else {}),
                }
            )
        for name in sorted(required - set(names)):
            # a required name missing from the mapping is always empty
            fields_out.append({"name": name, "type": "text", "required": True, "sample_count": len(hits), "filled_count": 0,
                               "fill_rate": 0.0, "passed": False, "example_values": [], "source_hits": [], "errors": []})
            t_passed = False
        if hits:
            any_matched = True
            all_passed = all_passed and t_passed
        entry = {"template_id": tid, "matched_samples": len(hits), "passed": t_passed, "fields": fields_out}
        if m.get("variants") and hits:
            entry.update(_variant_report(m, per_variant.get(tid) or [], len(hits)))
        failures = _price_and_variant_failures(m, mappings.index(m), [sid for sid, _ in hits], checks)
        if hits:
            entry["failures"] = failures
        if failures:
            t_passed = False
            entry["passed"] = False
            all_passed = False  # failures exist only for a template that matched a sample
            warnings.extend(failures)
        # fields of the mapping that no matched sample filled (a test passes on its required
        # fields only; an optional field empty everywhere is worth a loud warning)
        empty = [f["name"] for f in fields_out if evaluated and f["filled_count"] == 0]
        if hits:
            entry["empty_fields"] = empty
        if empty:
            required_names = sorted(required & set(names))
            warnings.append(field_error(
                f"mappings[{mappings.index(m)}].fields", "fields_never_filled",
                (f"template {tid}: {', '.join(empty)} filled on 0 of {plural(len(hits), 'matched sample')}"
                 + f" (the test {'passed' if t_passed else 'did not pass'} on the required fields: {', '.join(required_names) or 'none'})"
                 + "; check the mapping's sources, or whether the samples are really target pages")[:2000]))
        templates.append(entry)
    overall = any_matched and all_passed and (not body.get("require_all_samples_matched", True) or not unmatched)
    return {"passed": overall, "required_fill_threshold": threshold, "templates": templates, "samples": sample_results,
            "unmatched_sample_ids": unmatched, "warnings": warnings}


def _in_page_sources(page) -> list[dict]:
    from app.extraction.variants import detect_sources

    try:
        return detect_sources(page)
    except Exception:
        return []


def _price_and_variant_failures(mapping: dict, index: int, sample_ids: list[str], checks: dict[str, dict]) -> list[dict]:
    """Mapping-test failures (README 6.3): ``price_not_extracted`` — the template has a price
    field, yet no record of a sample whose page shows a price (``price_on_page`` shown or range)
    got one; ``variants_not_used`` — samples carry variant data (a variant source with two or
    more variants) and the mapping declares no ``variants``."""
    from app.extraction.pricing import price_field

    sample_ids = [sid for sid in sample_ids if not checks[sid].get("excluded")]
    tid = mapping["template_id"]
    out: list[dict] = []
    name = price_field(mapping)
    if name is not None:
        shown = [sid for sid in sample_ids if checks.get(sid, {}).get("signal") in ("shown", "range")]
        empty = [sid for sid in shown if not checks[sid]["has_price"]]
        if empty:
            pos = [f["name"] for f in mapping["fields"]].index(name)
            out.append(field_error(
                f"mappings[{index}].fields[{pos}]", "price_not_extracted",
                (f"template {tid}: the page shows a price on {plural(len(shown), 'matched sample')}, but `{name}` is empty on "
                 f"{len(empty)} of them ({', '.join(empty[:5])}); read the product's own price block (a range gives its lowest value), "
                 "or the variant prices when the product has variants")[:2000]))
    if name is not None:
        off = [(sid, checks[sid]["mismatch"]) for sid in sample_ids if checks.get(sid, {}).get("mismatch")]
        if off:
            pos = [f["name"] for f in mapping["fields"]].index(name)
            shown = "; ".join(f"{sid}: mapped {m:g}, the page states {st:g} ({src})" for sid, (m, st, src) in off[:5])
            out.append(field_error(
                f"mappings[{index}].fields[{pos}]", "price_mismatch",
                (f"template {tid}: `{name}` differs from the price the page states for programs on {plural(len(off), 'matched sample')} "
                 f"({shown}); read the machine-readable price first (itemprop=price content, JSON-LD offers.price, "
                 "meta product:price:amount) and the visible text only as a fallback")[:2000]))
    if name is not None:
        old = [(sid, checks[sid]["old_price"]) for sid in sample_ids if checks.get(sid, {}).get("old_price")]
        if old:
            pos = [f["name"] for f in mapping["fields"]].index(name)
            shown = "; ".join(f"{sid}: mapped {m:g}, current {c:g}" for sid, (m, c) in old[:5])
            out.append(field_error(
                f"mappings[{index}].fields[{pos}]", "price_is_old_price",
                (f"template {tid}: on {plural(len(old), 'sale page')} `{name}` holds the struck-through old price ({shown}); read the "
                 "current price (ins, special-price, price-new, JSON-LD offers.price) — the service gives the old one as regular_price")[:2000]))
    if not mapping.get("variants"):
        with_data = [sid for sid in sample_ids if checks.get(sid, {}).get("variant_data")]
        if with_data:
            out.append(field_error(
                f"mappings[{index}].variants", "variants_not_used",
                (f"template {tid}: {plural(len(with_data), 'matched sample')} {'carries' if len(with_data) == 1 else 'carry'} variant data ({', '.join(with_data[:5])}; "
                 "see the samples' variant_sources), but the mapping declares no variants; add a `variants` block so each "
                 "variant becomes its own record with its own price")[:2000]))
    return out


def _variant_report(mapping: dict, results: list, matched: int) -> dict:
    """Per-variant fill rates and variant counts: over every variant of the matched
    samples; a sample without variants counts as one row of page values."""
    from app.extraction.variants import variant_field_specs

    specs = variant_field_specs(mapping)
    rows = [(sid, data, filled) for sid, variants, vrows, _k in results for data, _meta, filled in vrows if variants]
    with_variants = sum(1 for _sid, variants, _r, _k in results if variants)
    fields = []
    for spec in specs:
        filled = [data.get(spec["name"]) for _sid, data, f in rows if f.get(spec["name"])]
        fields.append({"name": spec["name"], "variant_count": len(rows), "filled_count": len(filled),
                       "fill_rate": round(len(filled) / len(rows), 4) if rows else 0.0, "example_values": filled[:3]})
    sources: dict[str, int] = {}
    for _sid, variants, _vrows, kind in results:
        key = kind if variants and kind else "none"
        sources[key] = sources.get(key, 0) + 1
    return {"variant_count": len(rows), "samples_with_variants": with_variants, "variant_sources_used": sources, "variant_fields": fields}


# ====================================================================== proxy and solver tests
async def proxy_test(svc, body: dict) -> dict:
    profile = body["profile"]
    tier_no = body["tier"]
    tier = next((t for t in profile.get("proxy_tiers") or [] if t["tier"] == tier_no), None)
    if tier is None:
        raise validation_failed([field_error("tier", "proxy_tier_not_configured", f"tier {tier_no} is not in the profile")])
    base = {"tier": tier_no, "exit_ip": None, "country": None, "city": None, "target_http_status": None}
    try:
        url = tier_url(tier, "test" + "".join(random.choices(string.ascii_lowercase, k=6)))
    except ValueError as exc:
        return {"ok": False, "checked_at": now_iso(), "code": "proxy_connect_failed", "message": redact(str(exc))[:500], **base}
    started = time.monotonic()
    if await url_is_private(svc.settings.ip_echo_url):
        return {"ok": False, "checked_at": now_iso(), "code": "proxy_connect_failed", "message": "the IP echo URL resolves to a private address (refused)", **base}
    try:
        async with httpx.AsyncClient(proxy=url, timeout=20.0) as client:
            resp = await client.get(svc.settings.ip_echo_url)
            if resp.status_code == 407:
                return {"ok": False, "checked_at": now_iso(), "code": "proxy_auth_failed", "message": "the proxy refused the credentials (HTTP 407)", **base}
            try:
                data = resp.json()
            except ValueError:
                data = {"ip": resp.text.strip()}
            ip = data.get("ip") or data.get("origin") or data.get("query")
            base["exit_ip"] = str(ip)[:45] if ip else None
            country = data.get("country") or data.get("country_code") or data.get("countryCode")
            city = data.get("city")
            if not country and ip and svc.settings.geo_lookup_url and not await url_is_private(svc.settings.geo_lookup_url.format(ip=ip)):
                try:
                    async with httpx.AsyncClient(timeout=10.0) as plain:
                        geo = (await plain.get(svc.settings.geo_lookup_url.format(ip=ip))).json()
                    country = geo.get("country") or geo.get("country_code")
                    city = city or geo.get("city")
                except (httpx.HTTPError, ValueError, OSError):
                    pass
            if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country):
                base["country"] = country.upper()
            base["city"] = city if isinstance(city, str) else None
            if body.get("target_url"):
                if await url_is_private(body["target_url"]):
                    raise validation_failed([field_error("target_url", "private_address_refused", "the host resolves to a private address")])
                target = await client.get(body["target_url"])
                base["target_http_status"] = target.status_code
    except httpx.ProxyError as exc:
        code = "proxy_auth_failed" if "407" in str(exc) else "proxy_connect_failed"
        return {"ok": False, "checked_at": now_iso(), "latency_ms": int((time.monotonic() - started) * 1000), "code": code, "message": "the proxy refused the connection" if code == "proxy_connect_failed" else "the proxy refused the credentials (HTTP 407)", **base}
    except httpx.TimeoutException:
        return {"ok": False, "checked_at": now_iso(), "latency_ms": int((time.monotonic() - started) * 1000), "code": "proxy_timeout", "message": "no answer through the proxy within 20 s", **base}
    except (httpx.HTTPError, OSError) as exc:
        return {"ok": False, "checked_at": now_iso(), "latency_ms": int((time.monotonic() - started) * 1000), "code": "proxy_connect_failed", "message": f"the proxy endpoint could not be reached ({type(exc).__name__})", **base}
    latency = int((time.monotonic() - started) * 1000)
    if tier.get("country") and base["country"] and tier["country"].upper() != base["country"]:
        return {"ok": False, "checked_at": now_iso(), "latency_ms": latency, "code": "proxy_geo_mismatch", "message": f"exit country {base['country']} differs from {tier['country']}", **base}
    return {"ok": True, "checked_at": now_iso(), "latency_ms": latency, **base}


async def solver_test(svc, body: dict) -> dict:
    from app.fetching.solvers import Solver, SolverError

    solver_cfg = (body["profile"] or {}).get("solver")
    if not solver_cfg:
        raise validation_failed([field_error("profile.solver", "solver_not_configured", "the profile has no solver")])
    started = time.monotonic()
    base = {"provider": solver_cfg["provider"], "balance": None, "balance_currency": None}
    try:
        solver = Solver(solver_cfg["provider"], solver_cfg["api_key"], solver_cfg.get("endpoint"))
        bal = await solver.balance()
    except SolverError as err:
        return {"ok": False, "checked_at": now_iso(), "latency_ms": int((time.monotonic() - started) * 1000), "code": err.code, "message": redact(err.message, [solver_cfg.get("api_key") or ""])[:1000], **base}
    base["balance"], base["balance_currency"] = bal.amount, bal.currency
    return {"ok": True, "checked_at": now_iso(), "latency_ms": int((time.monotonic() - started) * 1000), **base}
