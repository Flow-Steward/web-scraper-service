"""Challenge and block detection after every page load.

A *challenge* is a page that stands in for the requested one until the visitor passes a
check (Cloudflare's "Just a moment…" interstitial, the managed-challenge Turnstile on an
otherwise empty page). A *hard block* is a refusal ("Sorry, you have been blocked",
error 1020). Ordinary pages often carry Cloudflare scripts or widgets and must stay
ordinary:

* JavaScript detections / Bot Fight Mode inject ``/cdn-cgi/challenge-platform/scripts/jsd/…``
  into normal pages — not a challenge;
* a Turnstile, reCAPTCHA or hCaptcha widget in a contact form, a newsletter box or a login
  form (``data-sitekey``) — not a challenge;
* the words "access denied" or "request blocked" in product text — not a block.

Strong interstitial markers (any status, any size): ``_cf_chl_opt`` / ``__cf_chl_`` tokens,
the orchestrate script ``/cdn-cgi/challenge-platform/h/<x>/orchestrate/…``,
``cf-browser-verification``. The interstitial titles ("Just a moment...", "Attention
Required!") and the generic phrases ("checking your browser", "verify you are human", …)
count only on an error status (401/403/429/503) or a small page. A Turnstile frame or
widget counts as a challenge only on a small page or together with an interstitial marker.
Block phrases count only with an error status, or (the unmistakable Cloudflare ones) on a
small page. The bare word ``cloudflare`` is never a marker.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

STRONG_MARKERS = (
    "_cf_chl_opt",
    "__cf_chl_",
    "cf-browser-verification",
    "cf_chl_prog",
)
ORCHESTRATE_RE = re.compile(r"/cdn-cgi/challenge-platform/h/[a-z0-9]+/orchestrate/", re.I)
TITLE_RE = re.compile(r"<title[^>]*>\s*(just a moment|attention required|one more step|please wait)[^<]*</title>", re.I)
GENERIC_MARKERS = (
    "checking your browser",
    "checking if the site connection is secure",
    "verify you are human",
    "verifying you are human",
    "needs to review the security of your connection",
    "enable javascript and cookies to continue",
    "ddos protection by",
)
# block phrases that only a Cloudflare / WAF block page contains
CF_BLOCK_MARKERS = (
    "sorry, you have been blocked",
    "you have been blocked",
    "error code: 1020",
    "error 1020",
    "error 1015",
    "error 1010",
    "error 1012",
    "error 1006",
    "error 1005",
    "cf-error-details",
)
GENERIC_BLOCK_MARKERS = ("access denied", "request blocked", "forbidden")
CAPTCHA_MARKERS = ("g-recaptcha", "h-captcha", "hcaptcha.com", "recaptcha/api")
TURNSTILE_IFRAME_RE = re.compile(r"<iframe[^>]+src=[\"'][^\"']*challenges\.cloudflare\.com[^\"']*", re.I)
TURNSTILE_WIDGET_RE = re.compile(r"class=[\"'][^\"']*\bcf-turnstile\b", re.I)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
FORM_RE = re.compile(r"<form\b.*?</form>", re.I | re.S)
VISIBLE_INPUT_RE = re.compile(r"<(textarea|select)\b|<input\b(?![^>]*type=[\"']?hidden)", re.I)
SMALL_PAGE_CHARS = 1500


def _without_form_widgets(html: str) -> str:
    """A Turnstile inside a form with fields of its own (contact, login, newsletter) protects
    that form, not the page; such forms are left out of the Turnstile check."""
    return FORM_RE.sub(lambda m: "" if VISIBLE_INPUT_RE.search(m.group(0)) else m.group(0), html)
ERROR_STATUSES = (401, 403, 429, 503)


@dataclass(frozen=True)
class Verdict:
    kind: str  # none | challenge | hard_block
    challenge: str | None = None  # turnstile | interstitial | other_captcha

    @property
    def is_challenge(self) -> bool:
        return self.kind == "challenge"

    @property
    def is_block(self) -> bool:
        return self.kind == "hard_block"


NONE = Verdict("none")


def text_len(html: str) -> int:
    return len(" ".join(TAG_RE.sub(" ", SCRIPT_RE.sub(" ", html)).split()))


def detect_html(html: str, status: int | None = None) -> Verdict:
    html = html or ""
    low = html.lower()
    small = text_len(html) < SMALL_PAGE_CHARS
    error_status = status in ERROR_STATUSES
    strong = any(m in low for m in STRONG_MARKERS) or bool(ORCHESTRATE_RE.search(html))
    titled = bool(TITLE_RE.search(html)) and (small or error_status)
    generic = any(m in low for m in GENERIC_MARKERS) and (small or error_status)
    gate = _without_form_widgets(html)
    turnstile = bool(TURNSTILE_IFRAME_RE.search(gate) or TURNSTILE_WIDGET_RE.search(gate))
    cf_block = any(m in low for m in CF_BLOCK_MARKERS)
    if strong or titled or generic or (turnstile and small):
        if cf_block and not turnstile and not strong:
            return Verdict("hard_block")
        return Verdict("challenge", "turnstile" if turnstile else "interstitial")
    if cf_block and (error_status or small):
        return Verdict("hard_block")
    if error_status and small:
        if any(m in low for m in CAPTCHA_MARKERS):
            return Verdict("challenge", "other_captcha")
        if status in (401, 403) or any(m in low for m in GENERIC_BLOCK_MARKERS):
            return Verdict("hard_block")
    return NONE


async def page_html(page, attempts: int = 3) -> str | None:
    """``page.content()``, retried while a navigation (e.g. the challenge's own redirect) runs."""
    for i in range(attempts):
        try:
            return await page.content()
        except Exception:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                await asyncio.sleep(0.5 * (i + 1))
    return None


class PageNotLoaded(Exception):
    """The page did not finish loading within the bound (a still-loading page,
    title "Loading https://…", was judged a challenge and ended ``blocked``)."""


async def looks_loaded(page) -> bool:
    """The document is there: not about:blank, not Firefox's "Loading <url>" placeholder title,
    content readable (not in the middle of a navigation)."""
    try:
        url = page.url or ""
    except Exception:
        return False
    if not url or url == "about:blank":
        return False
    try:
        title = (await page.title()) or ""
    except Exception:
        return False
    if title.startswith(("Loading http", "Loading…", "Loading...")) and len(title) > 8:
        return False
    try:
        await page.content()
    except Exception:
        return False
    return True


async def settle(page, seconds: float = 20.0) -> bool:
    """Waits (bounded) until the page has loaded before anything judges it. True when it
    has; False after ``seconds`` — the caller treats that as a failed load, never as a
    challenge."""
    import time

    deadline = time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        try:
            await page.wait_for_load_state("load", timeout=int(max(0.5, min(5.0, left)) * 1000))
        except Exception:
            pass
        if await looks_loaded(page):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.5)


async def detect_page(page, status: int | None = None) -> Verdict:
    """Detection on a live browser page: the markup, then the challenge frame and widget.

    A page whose content cannot be read even after retries is still navigating (a challenge
    redirecting itself) and is reported as a challenge, never as an ordinary page."""
    html = await page_html(page)
    if html is None:
        return Verdict("challenge", "interstitial")
    verdict = detect_html(html, status)
    if verdict.kind != "none":
        return verdict
    try:
        # a widget inside a form is that form's protection, not a gate in front of the page
        frame_el = await page.query_selector('iframe[src*="challenges.cloudflare.com"]:not(form iframe)')
        widget = await page.query_selector(".cf-turnstile:not(form .cf-turnstile)")
    except Exception:
        frame_el = widget = None
    if (frame_el is not None or widget is not None) and text_len(html) < SMALL_PAGE_CHARS:
        return Verdict("challenge", "turnstile")
    return NONE
