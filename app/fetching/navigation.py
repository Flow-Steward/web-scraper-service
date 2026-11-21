"""Bounded navigation of generic browser listings after their initial load.

Only explicit next-page/load-more controls are clicked. The caller retains the
browser context, request guards and robots/rate policy; ``before_action`` enforces
that policy before each action and deferred document destination. Links from all
observed states are retained, including query-distinct links on an unchanged URL.
No host, catalogue vocabulary or client-side framework is assumed.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from app.core.urls import InvalidUrl, site_key

if TYPE_CHECKING:
    from app.extraction.page import Page


@dataclass(frozen=True)
class NavigationLimits:
    max_steps: int = 12
    max_seconds: float = 20.0
    settle_ms: int = 1000
    action_timeout_ms: int = 3000
    max_no_progress: int = 2
    allow_scroll: bool = False
    # Runtime catalogues renew the transition deadline on real link progress.
    # Setup callers can retain one absolute wall-clock budget.
    progress_timeout: bool = True
    max_total_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_steps < 0 or self.max_seconds <= 0:
            raise ValueError("Navigation needs a nonnegative step and positive time budget")
        if self.settle_ms <= 0 or self.action_timeout_ms <= 0 or self.max_no_progress <= 0:
            raise ValueError("Navigation wait and no-progress limits must be positive")
        if self.max_total_seconds is not None and self.max_total_seconds <= 0:
            raise ValueError("Total navigation budget must be positive")


@dataclass(frozen=True)
class NavigationAction:
    kind: str
    label: str
    url: str
    nofollow: bool = False


@dataclass
class NavigationResult:
    links: list[str] = field(default_factory=list)
    steps: int = 0
    observations: int = 0
    stop_reason: str = "controls_exhausted"
    bounded: bool = False
    coverage_notes: list[str] = field(default_factory=list)
    anchors: list[tuple[str, str | None, bool]] = field(default_factory=list)
    deferred_links: list[str] = field(default_factory=list)


ActionPolicy = Callable[[NavigationAction], bool | Awaitable[bool]]
CancellationCheck = Callable[[], bool | Awaitable[bool]]

# Inspect DOM semantics in one round trip; the index is used immediately against
# the same selector. We never modify the page to mark or manufacture controls.
_CONTROL_SELECTOR = 'a[href], button, [role="button"], [role="link"]'
_SNAPSHOT_JS = r"""() => {
    const selector = 'a[href], button, [role="button"], [role="link"]';
    const text = value => (value || '').replace(/\s+/g, ' ').trim();
    const unsafe = /\b(cart|basket|checkout|buy|purchase|order|wishlist|login|log in|sign in|sign up|register|subscribe|submit|delete|remove|logout|log out)\b/i;
    const next = /^(next(?:\s+(?:page|results?))?|go to (?:the )?next page)\s*[\u203a»→>]*$/i;
    const more = /^(?:load|show|see|view)\s+more(?:\s+(?:results?|items?|products?|entries|records?))?\s*[\u203a»→>]*$/i;
    const bareMore = /^(?:load|show|see|view)\s+more\s*[\u203a»→>]*$/i;
    const anchors = Array.from(document.querySelectorAll('a[href]')).map(a => {
        const raw = a.getAttribute('href');
        let resolved = null;
        if (raw.trim() && !raw.trim().startsWith('#')) {
            try {
                const url = new URL(raw.trim(), document.baseURI);
                if (url.protocol === 'http:' || url.protocol === 'https:') resolved = url.href.split('#', 1)[0];
            } catch {}
        }
        const nofollow = (a.getAttribute('rel') || '').toLowerCase().split(/\s+/).includes('nofollow');
        return [raw, resolved, nofollow];
    });
    const links = anchors.flatMap(([, resolved]) => resolved ? [resolved] : []);
    const controls = Array.from(document.querySelectorAll(selector)).flatMap((el, index) => {
        if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden' ||
            el.closest('[hidden], [inert], [aria-hidden="true"]') ||
            el.disabled || el.getAttribute('aria-disabled') === 'true' ||
            el.closest('form') || el.form || el.hasAttribute('download') ||
            (el.getAttribute('target') && el.getAttribute('target') !== '_self')) return [];
        const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
            .map(id => document.getElementById(id)?.textContent || '').join(' ');
        const label = text(el.getAttribute('aria-label') || labelled || el.getAttribute('title') || el.textContent);
        if (unsafe.test(label)) return [];
        let pagination = false;
        let refinement = false;
        for (let node = el; node; node = node.parentElement) {
            const description = text([node.getAttribute('aria-roledescription'), node.getAttribute('aria-label'),
                node.getAttribute('role'), node.id, node.className].filter(v => typeof v === 'string').join(' '));
            if (/carousel|slideshow|slider|gallery|player|calendar|datepicker/i.test(description)) return [];
            if (/paginat|paging|pager|page[-_ ]?navigation/i.test(description)) pagination = true;
            if (/facet|filter|refinement|sidebar/i.test(description) ||
                node.tagName === 'ASIDE' || node.tagName === 'FIELDSET' ||
                node.getAttribute('role') === 'complementary') refinement = true;
        }
        // Expanding filter choices does not advance the listing. Explicit
        // next-page controls retain their pagination meaning in mixed layouts.
        if (refinement && bareMore.test(label)) return [];
        const rel = (el.getAttribute('rel') || '').toLowerCase().split(/\s+/);
        const relNext = rel.includes('next');
        const nextLabel = next.test(label);
        // A bare "Next" can advance a form or media flow. Require listing
        // context unless the accessible name explicitly says page/results.
        const explicitNext = nextLabel && (/page|results?/i.test(label) || pagination);
        const symbolNext = pagination && /^[\u203a»→>]$/.test(label);
        if (!relNext && !explicitNext && !symbolNext && !more.test(label)) return [];
        let href = null;
        if (el.hasAttribute('href')) {
            const raw = el.getAttribute('href').trim();
            if (raw && !raw.startsWith('#')) {
                try { href = new URL(raw, document.baseURI).href; } catch { return []; }
            }
        }
        return [{index, label: label || 'Next page', href, nofollow: rel.includes('nofollow'), rank: relNext ? 0 : more.test(label) ? 2 : 1}];
    }).sort((a, b) => a.rank - b.rank);
    const scrolling = document.scrollingElement;
    return {url: location.href, links, anchors, controls,
        scrollable: !!scrolling && scrolling.scrollHeight > scrolling.clientHeight + 1};
}"""


# Retain an actual DOM element across the policy callback, then verify that it
# still has the semantics and destination that the caller authorized. A locator
# by ordinal can silently retarget after hydration inserts or removes controls.
_REVALIDATE_JS = (
    "(el, expected) => { const snapshot = ("
    + _SNAPSHOT_JS
    + """
    )();
    const index = Array.from(document.querySelectorAll(expected.selector)).indexOf(el);
    return el.isConnected && snapshot.url === expected.page_url &&
        snapshot.controls.some(control => control.index === index &&
            control.label === expected.label && control.href === expected.href &&
            control.nofollow === expected.nofollow);
    }"""
)


_UNSAFE_CONTROL_NAME = re.compile(
    r"\b(cart|basket|checkout|buy|purchase|order|wishlist|login|log in|sign in|sign up|register|subscribe|submit|delete|remove|logout|log out)\b",
    re.I,
)
_NEXT_CONTROL_NAME = re.compile(
    r"^(next(?:\s+(?:page|results?))?|go to (?:the )?next page)\s*[\u203a»→>]*$",
    re.I,
)
_MORE_CONTROL_NAME = re.compile(
    r"^(?:load|show|see|view)\s+more(?:\s+(?:results?|items?|products?|entries|records?))?\s*[\u203a»→>]*$",
    re.I,
)
_BARE_MORE_CONTROL_NAME = re.compile(r"^(?:load|show|see|view)\s+more\s*[\u203a»→>]*$", re.I)
_REFINEMENT_CONTEXT = re.compile(r"facet|filter|refinement|sidebar", re.I)
_PAGINATION_CONTEXT = re.compile(r"paginat|paging|pager|page[-_ ]?navigation", re.I)
_NON_LISTING_CONTEXT = re.compile(
    r"carousel|slideshow|slider|gallery|player|calendar|datepicker", re.I
)


def has_navigation_scripts(page: Page) -> bool:
    """Executable JavaScript may reveal links when static navigation stalls."""
    return page.has_selector(
        'script[src], script:not([type]), script[type="module"], script[type="text/javascript"]'
    )


def needs_browser_navigation(page: Page, *, is_target: bool) -> bool:
    """Static graph progress cannot establish completeness of a scripted listing."""
    return not is_target and (
        has_listing_controls(page) or has_navigation_scripts(page)
    )



def has_listing_controls(page: Page) -> bool:
    """Hint that an HTTP document needs browser listing discovery.

    Substantial server-rendered text does not prove that all listing links are
    present. This recognizes explicit next-page/load-more semantics, independent
    of host or framework. It never clicks: visibility, live enabled state and
    the caller's navigation policy are still checked by the browser collector.
    """
    from app.core.linkrules import url_skip
    from app.core.urls import absolute

    try:
        site = site_key(page.url)
    except (InvalidUrl, ValueError, UnicodeError):
        return False
    labels = {}
    for labelled in page.selector.css("[id]"):
        labels.setdefault(labelled.attrib["id"], labelled.xpath("string(.)").get() or "")
    for control in page.selector.css(_CONTROL_SELECTOR):
        attrs = control.attrib
        if (
            "disabled" in attrs
            or attrs.get("aria-disabled", "").lower() == "true"
            or "form" in attrs
            or "download" in attrs
            or attrs.get("target", "_self") not in ("", "_self")
        ):
            continue
        labelled = " ".join(
            labels.get(identifier, "") for identifier in attrs.get("aria-labelledby", "").split()
        )
        label = " ".join(
            (
                attrs.get("aria-label")
                or labelled
                or attrs.get("title")
                or control.xpath("string(.)").get()
                or ""
            ).split()
        )
        if _UNSAFE_CONTROL_NAME.search(label):
            continue
        pagination = False
        refinement = False
        safe = True
        for ancestor in control.xpath("ancestor-or-self::*"):
            ancestor_attrs = ancestor.attrib
            if (
                ancestor.root.tag == "form"
                or "hidden" in ancestor_attrs
                or "inert" in ancestor_attrs
                or ancestor_attrs.get("aria-hidden", "").lower() == "true"
                or (ancestor.root.tag == "fieldset" and "disabled" in ancestor_attrs)
            ):
                safe = False
                break
            description = " ".join(
                ancestor_attrs.get(key, "")
                for key in ("aria-roledescription", "aria-label", "role", "id", "class")
            )
            if _NON_LISTING_CONTEXT.search(description):
                safe = False
                break
            pagination = pagination or bool(_PAGINATION_CONTEXT.search(description))
            refinement = refinement or (
                bool(_REFINEMENT_CONTEXT.search(description))
                or ancestor.root.tag in ("aside", "fieldset")
                or ancestor_attrs.get("role") == "complementary"
            )
        if not safe or (refinement and _BARE_MORE_CONTROL_NAME.fullmatch(label)):
            continue
        next_name = bool(_NEXT_CONTROL_NAME.fullmatch(label))
        explicit_next = next_name and (pagination or bool(re.search(r"page|results?", label, re.I)))
        symbol_next = pagination and bool(re.fullmatch(r"[\u203a»→>]", label))
        if not (
            "next" in attrs.get("rel", "").lower().split()
            or explicit_next
            or symbol_next
            or _MORE_CONTROL_NAME.fullmatch(label)
        ):
            continue
        href = attrs.get("href", "").strip()
        if href and not href.startswith("#"):
            resolved = absolute(page.base_url, href)
            if (
                resolved is None
                or _same_site_url(resolved, site) is None
                or url_skip(resolved, {"sensitive_path", "action_params"})
            ):
                continue
        return True
    return False


async def _resolved(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _same_site_url(url: str, site: str) -> str | None:
    try:
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None or site_key(url) != site:
            return None
        return url.split("#", 1)[0]
    except (InvalidUrl, ValueError, UnicodeError):
        return None


def _stopped(result: NavigationResult, reason: str) -> NavigationResult:
    result.stop_reason = reason
    result.bounded = reason != "controls_exhausted"
    notes = {
        "controls_exhausted": "No further recognized enabled listing controls; custom or unlabelled navigation may remain.",
        "step_limit": "Navigation reached its action budget; additional listing pages may remain.",
        "time_limit": "Navigation reached its time budget; additional listing pages may remain.",
        "total_time_limit": "Navigation reached the remaining job duration; additional listing pages may remain.",
        "no_progress": "Repeated listing actions revealed no new same-site links; coverage may be partial.",
        "loop_detected": "Listing navigation returned to an already observed state; coverage may be partial.",
        "navigation_deferred": "Document navigation was deferred to normal fetching before issuing a browser request; listing coverage is partial.",
        "policy_denied": "The caller's navigation policy declined an action; coverage is partial.",
        "cancelled": "Listing navigation was cancelled; collected links are partial.",
        "action_error": "A browser action or observation failed; collected links are partial.",
        "site_changed": "Listing navigation left the requested site; collected links are partial.",
    }
    result.coverage_notes.append(notes[reason])
    return result


async def collect_listing_links(
    page: Any,
    *,
    site_url: str | None = None,
    limits: NavigationLimits | None = None,
    before_action: ActionPolicy | None = None,
    cancelled: CancellationCheck | None = None,
    pace_action: Callable[[NavigationAction], Awaitable[None]] | None = None,
    on_progress: Callable[[], None] | None = None,
) -> NavigationResult:
    """Observe initial links and follow safe listing controls within fixed limits.

    New links renew the transition deadline when ``progress_timeout`` is enabled.
    Explicit host pacing is outside that deadline but requires a total duration
    bound; cancellation is polled during pacing. Policy/DOM hangs stay bounded.
    ``on_progress`` is a synchronous heartbeat, called only for new links.

    ``page`` is an already-loaded async Playwright page and is left at its final
    listing state. This never opens a product link. ``site_url`` defaults to the
    initial page URL, using the service's existing site-key semantics. Query
    parameters are preserved; callers may apply their normal URL filters later.

    A policy receives a ``NavigationAction`` with the target URL for anchors and
    the current URL for JS buttons/scrolls. Returning false stops immediately.
    Caller task cancellation propagates; the optional cancellation predicate
    returns a partial result and is checked during settling as well as actions.

    Main-frame document requests are stopped before issuance, including requests
    initiated by script. Playwright routes do not intercept every HTTP redirect
    hop, so following them here could bypass policy. Safe destinations are
    returned in ``deferred_links`` for normal fetching instead. The policy sees
    these as kind ``navigation``; they do not consume another action budget.
    XHR and subresources retain the caller's existing request guards. The
    browser context must block service workers, which bypass Playwright routes.
    """
    limits = limits or NavigationLimits()
    if pace_action is not None and limits.max_total_seconds is None:
        raise ValueError("Explicit pacing requires a total navigation duration")
    loop = asyncio.get_running_loop()
    transition_timer = asyncio.timeout(limits.max_seconds)
    total_timer = asyncio.timeout(limits.max_total_seconds)
    result = NavigationResult()
    site = site_key(site_url or page.url)
    seen_links: set[str] = set()
    seen_anchors: set[tuple[str, str | None, bool]] = set()
    seen_states: set[tuple[str, tuple[str, ...]]] = set()
    no_progress = 0
    request_stop: str | None = None
    request_tasks: set[asyncio.Task] = set()
    route_installed = False

    async def is_cancelled() -> bool:
        return bool(await _resolved(cancelled())) if cancelled else False

    async def pace(action: NavigationAction) -> bool:
        assert pace_action is not None
        deadline = transition_timer.when()
        assert deadline is not None
        remaining = max(0.0, deadline - loop.time())
        transition_timer.reschedule(None)
        task = asyncio.create_task(_resolved(pace_action(action)))
        try:
            while not task.done():
                if await is_cancelled():
                    return False
                await asyncio.wait({task}, timeout=0.1)
            await task
            return not await is_cancelled()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            transition_timer.reschedule(loop.time() + remaining)

    async def guard_document(route):
        nonlocal request_stop
        request = route.request
        if not request.is_navigation_request() or request.frame != page.main_frame:
            # Keep private-address/resource guards and rendering requests intact.
            await route.fallback()
            return
        task = asyncio.current_task()
        request_tasks.add(task)
        try:
            if await is_cancelled():
                request_stop = "cancelled"
                return
            from app.fetching.destinations import document_destination_allowed

            target = _same_site_url(request.url, site)
            if (
                request.method != "GET"
                or target is None
                or not await document_destination_allowed(
                    target, site_url=site_url or page.url,
                    rules={"sensitive_path", "action_params"}, cancelled=cancelled,
                )
            ):
                request_stop = "policy_denied"
                return
            action = NavigationAction("navigation", "Deferred listing navigation", target)
            if before_action is not None and not await _resolved(before_action(action)):
                request_stop = "policy_denied"
                return
            if await is_cancelled():
                request_stop = "cancelled"
                return
            if target not in seen_links:
                seen_links.add(target)
                result.links.append(target)
            if target not in result.deferred_links:
                result.deferred_links.append(target)
            anchor = (target, target, False)
            if anchor not in seen_anchors:
                seen_anchors.add(anchor)
                result.anchors.append(anchor)
            request_stop = "navigation_deferred"
        except Exception:
            request_stop = "action_error"
        finally:
            try:
                # A terminal 204 prevents network issuance and leaves the
                # existing document intact. Aborting a navigation can replace
                # it with a browser error document and poison session reuse.
                await route.fulfill(status=204, body="")
            except Exception:
                pass
            request_tasks.discard(task)

    async def observe() -> tuple[dict, int, tuple[str, tuple[str, ...]]]:
        snapshot = await page.evaluate(_SNAPSHOT_JS)
        for raw, resolved, nofollow in snapshot["anchors"]:
            anchor = (raw, resolved, bool(nofollow))
            if anchor not in seen_anchors:
                seen_anchors.add(anchor)
                result.anchors.append(anchor)
        new = 0
        current_links = []
        for raw in snapshot["links"]:
            link = _same_site_url(raw, site)
            if link is None:
                continue
            current_links.append(link)
            if link not in seen_links:
                seen_links.add(link)
                result.links.append(link)
                new += 1
        signature = (snapshot["url"].split("#", 1)[0], tuple(sorted(set(current_links))))
        if new:
            if limits.progress_timeout:
                transition_timer.reschedule(loop.time() + limits.max_seconds)
            if on_progress is not None:
                on_progress()
        return snapshot, new, signature

    try:
        # Policy, DOM inspection and each transition remain bounded. Only the
        # deliberate pacing hook pauses the transition clock, never the total.
        async with total_timer, transition_timer:
            await page.route("**/*", guard_document)
            route_installed = True
            snapshot, _, signature = await observe()
            seen_states.add(signature)
            result.observations = 1
            while True:
                if request_stop is not None:
                    return _stopped(result, request_stop)
                if await is_cancelled():
                    return _stopped(result, "cancelled")
                if not _same_site_url(snapshot["url"], site):
                    return _stopped(result, "site_changed")
                control = next(
                    (
                        c
                        for c in snapshot["controls"]
                        if c["href"] is None or _same_site_url(c["href"], site)
                    ),
                    None,
                )
                if control:
                    action = NavigationAction(
                        "click",
                        control["label"],
                        control["href"] or snapshot["url"],
                        nofollow=control["nofollow"],
                    )
                elif limits.allow_scroll and snapshot["scrollable"]:
                    action = NavigationAction("scroll", "Scroll listing", snapshot["url"])
                else:
                    return _stopped(result, "controls_exhausted")
                if result.steps >= limits.max_steps:
                    return _stopped(result, "step_limit")
                element = None
                try:
                    if control:
                        element = (
                            await page.locator(_CONTROL_SELECTOR)
                            .nth(control["index"])
                            .element_handle(timeout=limits.action_timeout_ms)
                        )
                    if before_action is not None and not await _resolved(before_action(action)):
                        return _stopped(result, "policy_denied")
                    if await is_cancelled():
                        return _stopped(result, "cancelled")
                    if pace_action is not None and not await pace(action):
                        return _stopped(result, "cancelled")
                    if action.kind == "click":
                        if element is None or not await element.evaluate(
                            _REVALIDATE_JS,
                            {
                                "selector": _CONTROL_SELECTOR,
                                "page_url": snapshot["url"],
                                "label": control["label"],
                                "href": control["href"],
                                "nofollow": control["nofollow"],
                            },
                        ):
                            return _stopped(result, "action_error")
                        await element.click(timeout=limits.action_timeout_ms)
                    else:
                        if page.url != action.url:
                            return _stopped(result, "action_error")
                        await page.evaluate(
                            "() => window.scrollTo(0, document.scrollingElement.scrollHeight)"
                        )
                finally:
                    if element is not None:
                        await element.dispose()
                result.steps += 1
                if request_stop is not None:
                    return _stopped(result, request_stop)
                # Wait for a link-state change, not networkidle: long-polling and
                # analytics must not consume the whole navigation budget. The
                # bounded polling also permits prompt cancellation.
                previous = signature
                settle_deadline = loop.time() + limits.settle_ms / 1000
                observed_action = False
                while True:
                    if request_stop is not None:
                        return _stopped(result, request_stop)
                    if await is_cancelled():
                        return _stopped(result, "cancelled")
                    snapshot, new, signature = await observe()
                    if not observed_action:
                        result.observations += 1
                        observed_action = True
                    # An emptied listing is a loading transition, not
                    # completed pagination. Wait for newly revealed links or
                    # the settle budget before deciding which controls remain.
                    if new > 0:
                        break
                    remaining = settle_deadline - loop.time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.05, remaining))
                if signature != previous and signature in seen_states:
                    return _stopped(result, "loop_detected")
                seen_states.add(signature)
                no_progress = no_progress + 1 if new == 0 else 0
                if no_progress >= limits.max_no_progress:
                    return _stopped(result, "no_progress")
    except TimeoutError:
        return _stopped(result, "total_time_limit" if total_timer.expired() else "time_limit")
    except Exception:
        return _stopped(result, request_stop or "action_error")
    finally:
        # Policy callbacks may still be running when the collector times out or
        # is cancelled. Cancel them before releasing this temporary guard.
        try:
            pending = list(request_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            if route_installed:
                try:
                    await page.unroute("**/*", guard_document)
                except Exception:
                    pass  # A closed page has no remaining requests to guard.
