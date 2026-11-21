"""One-off fetches outside a job: analysis, samples, robots.txt, sitemaps, proxy tests.

HTTP goes through httpx with redirects followed by hand, so every hop passes the
private-address check (README 1). A browser fetch launches the job's browser for one page.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import zlib
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import httpx

from app.core.netguard import PrivateAddressError, guarded_transport, url_is_private
from app.core.urls import absolute
from app.fetching.proxy_auth import is_proxy_auth_failure

if TYPE_CHECKING:
    from app.fetching.navigation import (
        ActionPolicy,
        CancellationCheck,
        NavigationLimits,
        NavigationResult,
    )

log = logging.getLogger("wss.fetch")


def _now_iso() -> str:
    from app.core.timeutil import now_iso

    return now_iso()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.7",
}


MAX_DECOMPRESSED = 64 * 1024 * 1024


class PrivateAddress(Exception):
    pass


class HostStopped(Exception):
    """The host failed the protection route earlier in this request."""

    def __init__(self, failure: dict) -> None:
        super().__init__(failure.get("host"))
        self.failure = failure


def host_label(url: str) -> str:
    """The host a stop applies to: host[:port] as in the URL (``www.`` kept)."""
    from urllib.parse import urlsplit

    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        return f"{host}:{p.port}" if p.port else host
    except ValueError:
        return url


def gunzip_capped(data: bytes, limit: int = MAX_DECOMPRESSED) -> bytes:
    """Decompresses gzip data up to ``limit`` bytes (a gzip bomb stops there)."""
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(data, limit)
        return out
    except zlib.error:
        return data


@dataclass
class Fetched:
    url: str
    final_url: str
    status: int | None
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    redirects: list[str] = field(default_factory=list)
    # Discovery observations supplement the original HTML; extraction must keep
    # using the original body/final_url even after listing controls advance.
    extra_links: list[str] = field(default_factory=list)
    navigation: dict = field(default_factory=dict)
    extra_anchors: list[tuple[str, str | None, bool]] = field(default_factory=list)

    @property
    def text(self) -> str:
        data = self.body
        if data[:2] == b"\x1f\x8b":
            data = gunzip_capped(data)
        ctype = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return data.decode(charset, errors="replace")
        except LookupError:
            return data.decode("utf-8", errors="replace")


async def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
    max_redirects: int = 10,
    check_private: bool = True,
    max_bytes: int = 60 * 1024 * 1024,
    destination_policy=None,
) -> Fetched:
    merged = dict(DEFAULT_HEADERS)
    merged.update(headers or {})
    current = url
    hops: list[str] = []
    client_kwargs: dict = {"timeout": timeout, "follow_redirects": False, "verify": True}
    if proxy:
        client_kwargs["proxy"] = proxy
    elif check_private:
        client_kwargs["transport"] = guarded_transport(verify=True)
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            for _ in range(max_redirects + 1):
                if check_private and await url_is_private(current):
                    raise PrivateAddress(current)
                async with client.stream("GET", current, headers=merged) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                        nxt = absolute(current, resp.headers["location"])
                        if not nxt:
                            return Fetched(url, current, resp.status_code, b"", dict(resp.headers), "bad redirect", hops)
                        if check_private and await url_is_private(nxt):
                            raise PrivateAddress(nxt)
                        from app.fetching.destinations import redirect_destination_allowed

                        if not await redirect_destination_allowed(
                            nxt, site_url=url, destination_policy=destination_policy,
                        ):
                            return Fetched(url, current, resp.status_code, b"", dict(resp.headers), "redirect_policy_denied", hops)
                        hops.append(current)
                        current = nxt
                        continue
                    chunks = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            break
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    return Fetched(url, str(resp.url), resp.status_code, body, {k.lower(): v for k, v in resp.headers.items()}, None, hops)
            return Fetched(url, current, None, b"", {}, "too many redirects", hops)
    except PrivateAddress:
        raise
    except PrivateAddressError as exc:
        raise PrivateAddress(current) from exc
    except (httpx.HTTPError, OSError, ValueError) as exc:
        error = "proxy_auth_failed" if is_proxy_auth_failure(exc, proxy_used=bool(proxy)) else type(exc).__name__
        return Fetched(url, current, None, b"", {}, error, hops)



async def context_get(context, url: str, *, headers=None, destination_policy=None,
                      timeout_ms=60000, max_redirects=10) -> Fetched | None:
    """Raw context GET sharing cookies/proxy with per-hop destination checks."""
    from app.fetching.destinations import redirect_destination_allowed

    current, hops = url, []
    for _ in range(max_redirects + 1):
        if await url_is_private(current):
            return None
        response = await context.request.get(
            current, timeout=timeout_ms, max_redirects=0, headers=headers,
        )
        try:
            if response.status in (301, 302, 303, 307, 308) and response.headers.get("location"):
                target = absolute(current, response.headers["location"])
                if target is None:
                    return Fetched(url, current, response.status, error="bad redirect", redirects=hops)
                if await url_is_private(target):
                    return None
                if not await redirect_destination_allowed(
                    target, site_url=url, destination_policy=destination_policy,
                ):
                    return Fetched(url, current, response.status, error="redirect_policy_denied", redirects=hops)
                hops.append(current)
                current = target
                continue
            body = await response.body()
            return Fetched(url, response.url, response.status, body[:MAX_DECOMPRESSED],
                           {k.lower(): v for k, v in response.headers.items()}, redirects=hops)
        finally:
            await response.dispose()
    return Fetched(url, current, None, error="too many redirects", redirects=hops)


def _proxy_cfg(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    from urllib.parse import unquote, urlsplit

    p = urlsplit(proxy)
    cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        cfg["username"] = unquote(p.username)
        cfg["password"] = unquote(p.password or "")
    return cfg


class BrowserSession:
    """One browser, one context and one page for all browser fetches of one analysis or
    samples request: the clearance cookies a challenge route earned stay for the next URL.
    Every fetch runs the same protection route as a job (``run_route`` with the request's
    budget). Launch options as the job's plugins (and the old code): Camoufox with
    ``humanize``, ``disable_coop``, canvas noise, a Windows identity, ``window=(1280, 720)``,
    ``enable_cache``; the identity of the exit (identity.py); locale/timezone from the exit.
    Once a URL of a host fails the route, the host is recorded in ``failures`` and the session
    neither runs the route nor opens a page of that host again."""

    def __init__(self, browser: str, *, proxy: str | None = None, budget=None, timeout_s: int = 45,
                 proxy_tier: int | None = None, debug: bool = False, shots=None, variant=None,
                 delay: tuple[float, float] = (0.0, 0.0)) -> None:
        from app.fetching.browsers import Variant

        self.browser_name = browser
        self.variant = variant or Variant()  # Camoufox launch: browser_os, canvas_noise
        self.proxy = proxy
        self.proxy_tier = proxy_tier
        self.debug = debug
        self.shots = shots  # SampleStore (screenshots with debug)
        self.traces: list[dict] = []  # one entry per URL that met a challenge
        self.geo = None
        self.budget = budget
        self.timeout_s = timeout_s
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None
        self._refused: list[str] = []
        self.cleared_step: str | None = None  # the route step that earned this session's clearance
        self.identity = None  # identity.Identity of this session's exit
        # host -> the first failed protection route of this request: no further
        # route and no further page of that host in this request
        self.failures: dict[str, dict] = {}
        # host -> {"url", "step"} of the first passed route in this request: a host challenged
        # again after a pass gets the route once more, and the trace says it had passed
        self.passed_hosts: dict[str, dict] = {}
        # like a visitor: one tab, a pause of delay_min..delay_max seconds between navigations,
        # the previous page of the same host as referer
        self.delay = (float(delay[0] or 0), float(max(delay[1] or 0, delay[0] or 0)))
        self._last_url: str | None = None

    def failure_for(self, url: str) -> dict | None:
        return self.failures.get(host_label(url))

    @property
    def is_closed(self) -> bool:
        return bool(
            (self.page is not None and self.page.is_closed())
            or (self._browser is not None and not self._browser.is_connected())
        )

    async def start(self) -> BrowserSession:
        import os

        from playwright.async_api import async_playwright

        from app.fetching.geo import exit_geo

        self._pw = await async_playwright().start()
        proxy_cfg = _proxy_cfg(self.proxy)
        self.geo = await exit_geo(self.proxy)
        headless = False if os.environ.get("DISPLAY") else "virtual"
        if self.browser_name == "camoufox":
            from app.fetching.browsers import launch_camoufox
            from app.fetching.identity import camoufox_context, identity_for

            v = self.variant
            self._browser = await launch_camoufox(self._pw, headless=headless, variant=v)
            # the identity of this exit and launch variant: the same preset, fonts and voices as
            # every other context (analysis, samples, jobs, report) going out through it
            self.identity = await identity_for("camoufox", v.os_name(), self.proxy, self.geo, ff_version=self._browser.version.split(".", 1)[0],
                                               variant_key=v.key())
            geo_kw = self.geo.context_kwargs(camoufox=True) if self.geo else {}
            try:
                self.context = await camoufox_context(self._browser, self.identity, proxy=proxy_cfg, variant=v, **geo_kw)
            except ValueError:
                self.identity = None
                self.context = await camoufox_context(self._browser, None, proxy=proxy_cfg, variant=v, **geo_kw)
        else:
            from app.fetching.browsers import WINDOW
            from app.fetching.identity import chromium_context, identity_for

            self._browser = await self._pw.chromium.launch(
                headless=not os.environ.get("DISPLAY"), proxy=proxy_cfg, chromium_sandbox=False,
                args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            # the browserforge fingerprint of this exit (identity.py), as a job's context; its
            # screen holds the 1280x720 window
            from app.fetching.browsers import host_os_name

            self.identity = await identity_for("chromium", host_os_name(), self.proxy, self.geo)
            self.context = await chromium_context(self._browser, self.identity,
                                                  viewport={"width": WINDOW[0], "height": WINDOW[1]},
                                                  **(self.geo.context_kwargs(camoufox=False) if self.geo else {}))
        self.page = await self.context.new_page()
        await self.page.route("**/*", private_route_guard(self._refused))
        return self

    async def close(self) -> None:
        for closer in (getattr(self.context, "close", None), getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                pass

    async def get(self, url: str, *, wait_ms: int = 0, settle: bool = False) -> tuple[int | None, str, str, str | None]:
        """Returns (status, html, final_url, passed_at_step); ``passed_at_step`` is the route
        step that passed a challenge (``wait``, ``click``, ``solver``), else None."""
        from app.fetching.challenge import detect_page, page_html
        from app.fetching.protection import run_route

        if await url_is_private(url):
            raise PrivateAddress(url)
        stopped = self.failure_for(url)
        if stopped is not None:
            raise HostStopped(stopped)
        self._refused.clear()
        page = self.page
        referer = None
        if self._last_url is not None:
            if self.delay[1] > 0:
                await asyncio.sleep(random.uniform(*self.delay))
            if host_label(self._last_url) == host_label(url):
                referer = self._last_url
        self._last_url = url
        resp = await page.goto(url, timeout=self.timeout_s * 1000, wait_until="load", referer=referer)
        status = resp.status if resp else None
        passed = None
        from app.fetching.challenge import PageNotLoaded, settle as settle_page

        if not await settle_page(page, 20.0):  # a page still loading is not judged
            raise PageNotLoaded(f"the page did not finish loading within 20 s ({url})")
        verdict = await detect_page(page, status)
        if verdict.is_challenge:
            trace = {"url": url, "at": _now_iso(), "browser": self.browser_name, "proxy_tier": self.proxy_tier, "exit_ip": None,
                     "challenge": verdict.challenge, "http_status": status, "steps": []}
            earlier = self.passed_hosts.get(host_label(url))
            if earlier is not None:
                trace["passed_earlier"] = dict(earlier)  # the host had passed in this request; challenged again
            if self.budget is not None:
                passed = await run_route(page, self.budget, trace=trace["steps"])
            if passed:
                self.cleared_step = passed
                self.passed_hosts.setdefault(host_label(url), {"url": url, "step": passed})
                try:
                    await page.wait_for_load_state("load", timeout=15000)
                except Exception:
                    pass
                status = 200
            await self._finish_trace(trace, passed)
        if settle:  # a listing whose cards come by script after "load": up to 5 s of network quiet
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
        if wait_ms:
            await asyncio.sleep(wait_ms / 1000)
        html = await page_html(page) or ""
        final = page.url
        self._last_url = final or url
        if verdict.is_challenge and passed is None and self.budget is not None:
            self._record_failure(url, trace, html)
        # the final URL after HTTP, meta and JavaScript redirects (review M7a)
        if self._refused:
            raise PrivateAddress(self._refused[0])
        if final != url and await url_is_private(final):
            raise PrivateAddress(final)
        if passed is None and verdict.kind == "none" and self.cleared_step:
            passed = self.cleared_step  # passed thanks to the clearance this session earned earlier
        return status, html, final, passed

    async def discover_links(
        self,
        *,
        site_url: str | None = None,
        limits: NavigationLimits | None = None,
        before_action: ActionPolicy | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> NavigationResult:
        """Observe listing states in the already-loaded, guarded browser tab.

        Call after saving ``get()``'s original HTML/final URL. The returned links
        are supplementary discovery metadata; this leaves the tab at its last
        listing state. No extra browser or context is created.

        A supplied ``before_action`` owns robots, pause and rate/host spacing,
        and checks each action and deferred document destination after the
        private/failed-host checks. A ``navigation`` destination check must not
        charge another action or delay: the collector sends no document request.
        Without that callback, the session's inter-navigation delay applies to
        each action. Robots policy belongs to the caller, as for ``get()``.
        """
        from app.fetching.navigation import NavigationLimits, collect_listing_links

        if self.page is None:
            raise RuntimeError("browser session has no loaded page")
        initial_url = self.page.url
        if await url_is_private(initial_url):
            raise PrivateAddress(initial_url)
        stopped = self.failure_for(initial_url)
        if stopped is not None:
            raise HostStopped(stopped)
        limits = limits or NavigationLimits()
        limits = replace(limits, max_seconds=min(limits.max_seconds, float(self.timeout_s)))

        async def allowed(action):
            if self.failure_for(action.url) is not None or await url_is_private(action.url):
                return False
            if before_action is not None:
                decision = before_action(action)
                return bool(await decision if inspect.isawaitable(decision) else decision)
            if action.kind != "navigation" and self.delay[1] > 0:
                await asyncio.sleep(random.uniform(*self.delay))
            return True

        result = await collect_listing_links(
            self.page,
            site_url=site_url or initial_url,
            limits=limits,
            before_action=allowed,
            cancelled=cancelled,
        )
        # Preserve the request guard's normal failure contract if an allowed
        # script attempted a private redirect during listing navigation.
        if self._refused:
            raise PrivateAddress(self._refused[0])
        final_url = self.page.url
        if final_url != initial_url and await url_is_private(final_url):
            raise PrivateAddress(final_url)
        self._last_url = final_url or initial_url
        return result

    def _record_failure(self, url: str, trace: dict, html: str) -> None:
        host = host_label(url)
        if host in self.failures:
            return
        low = (html or "")[:300_000].lower()
        frames = []
        try:
            frames = [(f.url or "").lower() for f in self.page.frames]
        except Exception:
            pass
        cloudflare = (any(n.startswith(("cf_", "__cf")) for n in trace.get("cookies") or [])
                      or any("challenges.cloudflare.com" in u or "/cdn-cgi/challenge-platform" in u for u in frames)
                      or "_cf_chl_opt" in low or "/cdn-cgi/challenge-platform" in low)
        self.failures[host] = {"host": host, "url": url, "trace": trace, "vendor": "Cloudflare" if cloudflare else None}

    async def _finish_trace(self, trace: dict, passed: str | None) -> None:
        from app.core.redaction import redact
        from app.fetching.identity import env_of, preset_id_of
        from app.fetching.protection import page_environment, page_facts, trace_line

        trace["passed_at_step"] = passed
        trace["cookies"], trace["final_title"] = await page_facts(self.page)
        trace["environment"] = {"exit": self.geo.public() if self.geo else None, "browser_view": await page_environment(self.page),
                                "preset_id": preset_id_of(self.page), **env_of(self.page)}
        trace["exit_ip"] = self.geo.ip if self.geo else None
        if self.debug and self.shots is not None:
            try:
                png = await self.page.screenshot(full_page=False, timeout=15000)
                shot_id = self.shots.save_shot(png)
                if shot_id:
                    trace["screenshot_id"] = shot_id
            except Exception:
                pass
        self.traces.append(trace)
        log.info(redact(trace_line(trace), [self.proxy or ""]))

    async def raw(self, url: str, *, destination_policy=None, headers=None) -> Fetched | None:
        """The raw bytes of ``url`` (robots.txt, a sitemap) through the session's context: its
        cookies and proxy. A challenge is passed in the page first, then the bytes are fetched."""
        from app.fetching.challenge import detect_html

        if await url_is_private(url):
            return None
        if self.failure_for(url) is not None:
            return Fetched(url, url, None, b"", {}, "protection_not_passed")
        for attempt in range(2):
            try:
                fetched = await context_get(
                    self.context, url, headers=headers, destination_policy=destination_policy,
                )
                if fetched is None or fetched.error is not None:
                    return fetched
                body = fetched.body

            except Exception as exc:
                return Fetched(url, url, None, b"", {}, type(exc).__name__)
            head = body[:300_000].decode("utf-8", errors="replace") if body[:2] != b"\x1f\x8b" else ""
            if attempt == 0 and detect_html(head, fetched.status).is_challenge:
                try:
                    await self.get(url)
                except PrivateAddress:
                    return None
                except Exception:
                    return Fetched(url, url, fetched.status, b"", {}, "challenge")
                if self.failure_for(url) is not None:  # the route failed: no second fetch
                    return Fetched(url, url, fetched.status, b"", {}, "protection_not_passed")
                continue
            return fetched
        return None


async def browser_get(url: str, browser: str, *, proxy: str | None = None, wait_ms: int = 0, timeout_s: int = 45,
                      challenge_wait_s: float = 0.0, budget=None) -> tuple[int | None, str, str, str | None]:
    """One URL in a fresh session (kept for callers outside a request); prefer ``BrowserSession``."""
    if budget is None and challenge_wait_s > 0:
        from app.fetching.protection import RouteBudget

        budget = RouteBudget(counters={}, wait_seconds=challenge_wait_s)
    session = await BrowserSession(browser, proxy=proxy, budget=budget, timeout_s=timeout_s).start()
    try:
        return await session.get(url, wait_ms=wait_ms)
    finally:
        await session.close()


def private_route_guard(refused: list[str] | None = None, then=None):
    """A Playwright route handler that aborts every request to a refused host (navigations
    after JavaScript or meta redirects, frames, fetch/XHR, sub-resources), so a public page
    cannot make the browser read an internal address. ``then`` handles allowed requests."""

    async def _route(route):
        req_url = route.request.url
        if req_url.startswith(("http://", "https://")) and await url_is_private(req_url):
            if refused is not None:
                try:
                    main = route.request.is_navigation_request() and route.request.frame.parent_frame is None
                except Exception:
                    main = True
                if main:
                    refused.append(req_url)
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass
            return
        if then is not None:
            await then(route)
        else:
            try:
                await route.continue_()
            except Exception:
                pass

    return _route
