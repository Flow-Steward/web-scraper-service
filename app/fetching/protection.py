"""Challenge handling inside the page: wait, model-free checkbox click, solver.

One route for every fetch of the service (jobs, analysis, samples, robots.txt, sitemaps):
``run_route`` = wait -> (the caller's next-proxy-tier hook) -> click (if allowed and
requested) -> solver (if allowed and requested), with the caller's caps and counters.

The click (live finding on a real managed challenge, 2026-11-13): Cloudflare's Turnstile
frame sits in a **closed shadow root**, so no CSS selector reaches it; ``page.frames``
lists it (``challenges.cloudflare.com/cdn-cgi/challenge-platform/…/turnstile/…``) and
``frame.frame_element().bounding_box()`` gives its box (about 300x65). The checkbox is
about 28 px from the frame's left edge, at its vertical middle. So the frame list is the
first method; the selectors stay as fall-backs. The click is made **once** per widget
(a second click while the widget verifies restarts the verification), then the page is
polled every second for up to ``click_wait_seconds``. As in the old ``human_click``
(``scraper/browser.py`` 508-606) the mouse is moved with a single ``mouse.move(x, y)`` so
Camoufox's ``humanize`` draws the path (Playwright's ``steps`` would draw a straight,
evenly timed line); Chromium has no humanize and gets a few steps. Small random offset,
hesitation, held button. No screenshot and no model.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.fetching.challenge import Verdict, detect_page

log = logging.getLogger("wss.protection")

FRAME_URL_KEYS = ("challenges.cloudflare.com", "/turnstile/", "challenge-platform", "turnstile")
TURNSTILE_IFRAME_SELECTORS = [
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="turnstile"]',
    'iframe[title*="Cloudflare"]',
    'iframe[title*="Widget"]',
]
WRAPPER_SELECTORS = [".cf-turnstile", "#cf-turnstile", '[class*="cf-turnstile"]']
CHECKBOX_OFFSET_PX = 28  # checkbox centre from the frame's left edge


async def wait_for_pass(page, seconds: float, poll: float = 1.0) -> bool:
    """Wait for the challenge to pass by itself (``_wait_for_turnstile_auto_solve``)."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=int(max(poll, 0.5) * 1000))
        except Exception:
            pass
        verdict = await detect_page(page)
        if verdict.kind == "none":
            return True
        if verdict.kind == "hard_block":
            return False
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll)


async def human_click(page, x: float, y: float, *, humanized: bool = True) -> None:
    """``human_click`` of the old code: offset, hesitation, held button. With Camoufox's
    ``humanize`` one ``mouse.move`` lets the browser draw the cursor path."""
    tx, ty = x + random.randint(-3, 3), y + random.randint(-3, 3)
    try:
        if humanized:
            await page.mouse.move(tx, ty)
        else:
            await page.mouse.move(tx, ty, steps=random.randint(8, 20))
        await asyncio.sleep(random.randint(50, 150) / 1000.0)
        await page.mouse.down()
        await asyncio.sleep(random.randint(50, 120) / 1000.0)
        await page.mouse.up()
    except Exception as exc:  # fall back to a plain click like the old code
        log.debug("human_click fallback: %s", type(exc).__name__)
        await page.mouse.click(tx, ty)


def _box_ok(box) -> bool:
    return bool(box) and box["width"] > 50 and box["height"] > 30


async def _frame_box(page):
    """The challenge frame's box via the page's frame list (reaches closed shadow roots)."""
    return (await _find_box(page, frames_only=True))[0]


async def _find_box(page, frames_only: bool = False):
    """(box, via): via = ``frames`` (the page's frame list), ``selector`` or ``none``."""
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for frame in frames:
        url = (frame.url or "").lower()
        if frame == page.main_frame or not any(k in url for k in FRAME_URL_KEYS):
            continue
        try:
            element = await frame.frame_element()
            box = await element.bounding_box() if element else None
        except Exception:
            box = None
        if _box_ok(box):
            return box, "frames"
    if frames_only:
        return None, "none"
    box = await _selector_box(page)
    return (box, "selector") if box is not None else (None, "none")


async def _selector_box(page):
    for selector in TURNSTILE_IFRAME_SELECTORS + WRAPPER_SELECTORS:
        try:
            el = await page.query_selector(selector)
            box = await el.bounding_box() if el else None
        except Exception:
            box = None
        if _box_ok(box):
            return box
    return None


async def click_checkbox(page, *, humanized: bool = True, wait_seconds: float = 15.0, poll: float = 1.0) -> str:
    """One click on the checkbox, then poll. Returns ``passed``, ``clicked`` (one click made,
    the challenge is still there after ``wait_seconds``) or ``frame_not_found``."""
    return (await click_checkbox_traced(page, humanized=humanized, wait_seconds=wait_seconds, poll=poll))["result"]


async def click_checkbox_traced(page, *, humanized: bool = True, wait_seconds: float = 15.0, poll: float = 1.0) -> dict:
    """As ``click_checkbox``, with what happened: how the frame was found, where the click
    went (page coordinates and offset in the frame), the outcome and its duration."""
    started = time.monotonic()
    box, via = await _find_box(page)
    if box is None:
        # the widget may not be there yet — let the page finish loading, look once more
        from app.fetching.challenge import settle

        await settle(page, 10.0)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        box, via = await _find_box(page)
    if box is None:
        return {"result": "frame_not_found", "frame_found_via": "none", "seconds": round(time.monotonic() - started, 1)}
    offset = min(CHECKBOX_OFFSET_PX, box["width"] / 2)
    x = box["x"] + offset
    y = box["y"] + box["height"] / 2
    await human_click(page, x, y, humanized=humanized)
    clicked_at = time.monotonic()
    out = {"result": "clicked", "frame_found_via": via, "x": round(x), "y": round(y),
           "frame_box": {k: round(box[k]) for k in ("x", "y", "width", "height")}}
    deadline = clicked_at + wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=int(poll * 1000))
        except Exception:
            pass
        verdict = await detect_page(page)
        if verdict.kind == "none":
            out["result"] = "passed"
            break
        if verdict.kind == "hard_block":
            out["result"] = "hard_block"
            break
    out["seconds"] = round(time.monotonic() - clicked_at, 1)
    return out


@dataclass
class RouteBudget:
    """What one route may use, and the counters it updates (a job's ``protection`` object,
    or a request-scoped one for analysis and samples)."""

    counters: dict
    click_allowed: bool = False
    clicks_limit: int = 0
    solver: object | None = None
    solves_limit: int = 0
    wait_seconds: float = 15.0
    click_wait_seconds: float = 15.0
    humanized: bool = True
    errors: list = field(default_factory=list)
    # why a step is not used at all (None = requested and allowed): not_requested, not_allowed_by_service, not_configured
    click_skip: str | None = None
    solver_skip: str | None = None

    def click_usable(self) -> bool:
        return self.click_allowed and self.counters.get("clicks_used", 0) < self.clicks_limit

    def solver_usable(self) -> bool:
        return self.solver is not None and self.counters.get("solves_used", 0) < self.solves_limit


async def run_route(page, budget: RouteBudget, *, after_wait: Callable[[], Awaitable[None]] | None = None,
                    trace: list | None = None) -> str | None:
    """Wait -> ``after_wait`` (a job moves the host to the next proxy tier there and gives the
    request back) -> one click -> solver. Returns the step that passed, or None. Each step
    tried (or skipped, with the reason) is appended to ``trace``."""
    steps = trace if trace is not None else []
    c = budget.counters
    started = time.monotonic()
    if await wait_for_pass(page, budget.wait_seconds):
        steps.append({"step": "wait", "outcome": "passed", "seconds": round(time.monotonic() - started, 1)})
        return "wait"
    steps.append({"step": "wait", "outcome": "timed_out", "seconds": round(time.monotonic() - started, 1)})
    if after_wait is not None:
        await after_wait()
    if budget.click_usable():
        detail = await click_checkbox_traced(page, humanized=budget.humanized, wait_seconds=budget.click_wait_seconds)
        result = detail.pop("result")
        step = {"step": "click", "outcome": {"passed": "passed", "clicked": "still_challenged"}.get(result, result), **detail}
        steps.append(step)
        if result == "frame_not_found":
            c["frame_not_found"] = c.get("frame_not_found", 0) + 1
        else:
            c["clicks_used"] = c.get("clicks_used", 0) + 1
            if result == "passed":
                return "click"
    else:
        steps.append({"step": "click", "outcome": "skipped", "reason": budget.click_skip or "limit_reached"})
    if budget.solver_usable():
        from app.fetching.solvers import SolverError

        sitekey = await site_key(page)
        if not sitekey:
            budget.errors.append(("debug", "no Turnstile sitekey on the challenge page; solver not called"))
            steps.append({"step": "solver", "outcome": "skipped", "reason": "no_sitekey"})
            return None
        c["solves_used"] = c.get("solves_used", 0) + 1
        t0 = time.monotonic()
        try:
            token = await budget.solver.solve_turnstile(page.url, sitekey)
            await inject_token(page, token)
            if await wait_for_pass(page, 15):
                steps.append({"step": "solver", "outcome": "passed", "seconds": round(time.monotonic() - t0, 1)})
                return "solver"
            steps.append({"step": "solver", "outcome": "still_challenged", "seconds": round(time.monotonic() - t0, 1)})
        except SolverError as err:
            budget.errors.append(("warning", err.message))
            steps.append({"step": "solver", "outcome": "error", "code": err.code, "seconds": round(time.monotonic() - t0, 1)})
    else:
        steps.append({"step": "solver", "outcome": "skipped", "reason": budget.solver_skip or "limit_reached"})
    return None


async def page_facts(page) -> tuple[list[str], str]:
    """Cookie names (never values) and the page title, for a trace."""
    try:
        cookies = sorted({c["name"] for c in await page.context.cookies()})
    except Exception:
        cookies = []
    try:
        title = (await page.title())[:200]
    except Exception:
        title = ""
    return cookies, title


ENV_JS = """
() => {
  const out = {};
  try { out.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) {}
  try { out.language = navigator.language; out.languages = Array.from(navigator.languages || []); } catch (e) {}
  try { out.platform = navigator.platform; out.user_agent = navigator.userAgent; } catch (e) {}
  try { out.screen = [screen.width, screen.height]; out.window = [window.outerWidth, window.outerHeight]; out.viewport = [window.innerWidth, window.innerHeight]; } catch (e) {}
  try { out.hardware_concurrency = navigator.hardwareConcurrency; } catch (e) {}
  try {
    const c = document.createElement('canvas'); const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      out.webgl_vendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR);
      out.webgl_renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    } else { out.webgl_vendor = null; out.webgl_renderer = null; }
  } catch (e) {}
  return out;
}
"""


async def page_environment(page) -> dict:
    """The browser's own view of the values a challenge checks (timezone, languages, platform,
    screen, WebGL). Nothing secret."""
    try:
        view = await page.evaluate(ENV_JS)
        return view if isinstance(view, dict) else {}
    except Exception:
        return {}


def trace_line(trace: dict) -> str:
    """One log line per challenge (no secrets: tier number, no proxy URL)."""
    parts = []
    for st in trace.get("steps", []):
        bit = f"{st['step']}:{st['outcome']}"
        if st.get("reason"):
            bit += f"({st['reason']})"
        if st.get("frame_found_via"):
            bit += f"[via={st['frame_found_via']}" + (f" at={st['x']},{st['y']}" if "x" in st else "") + "]"
        if st.get("seconds") is not None:
            bit += f"/{st['seconds']}s"
        parts.append(bit)
    env = (trace.get("environment") or {})
    exit_ = env.get("exit") or {}
    view = env.get("browser_view") or {}
    env_bit = (f" exit_ip={exit_.get('ip')} tz={exit_.get('timezone')}/{view.get('timezone')} locale={exit_.get('locale')}/{view.get('language')}"
               f" webgl={view.get('webgl_renderer')!r} preset={env.get('preset_id')} os={env.get('browser_os')}"
               f" canvas_noise={env.get('canvas_noise')} canvas={env.get('canvas_readback')}/{env.get('webgl_readback')}" if env else "")
    return (f"challenge {trace.get('url')} tier={trace.get('proxy_tier')} browser={trace.get('browser')}{env_bit} "
            f"steps={' '.join(parts) or '-'} passed={trace.get('passed_at_step')} cookies={','.join(trace.get('cookies') or []) or '-'} "
            f"title={trace.get('final_title')!r}")


INJECT_TOKEN_JS = """
(token) => {
  const fields = document.querySelectorAll('[name="cf-turnstile-response"], [name="g-recaptcha-response"], input[name*="turnstile"]');
  fields.forEach((f) => { f.value = token; });
  if (typeof window.turnstileCallback === 'function') { try { window.turnstileCallback(token); } catch (e) {} }
  if (window.cf_challenge_response !== undefined) { window.cf_challenge_response = token; }
  document.querySelectorAll('form').forEach((form) => {
    if (form.querySelector('[name*="turnstile"]')) { try { form.submit(); } catch (e) {} }
  });
  return fields.length;
}
"""


SITEKEY_IN_URL = re.compile(r"/(0x[0-9A-Za-z_-]{10,})/")


async def site_key(page) -> str | None:
    """The widget's ``data-sitekey``, else the key in the challenge frame's URL (a managed
    challenge renders its widget in a closed shadow root)."""
    try:
        el = await page.query_selector(".cf-turnstile, [data-sitekey]")
        if el:
            key = await el.get_attribute("data-sitekey")
            if key:
                return key
    except Exception:
        pass
    try:
        for frame in page.frames:
            m = SITEKEY_IN_URL.search(frame.url or "")
            if m:
                return m.group(1)
    except Exception:
        return None
    return None


async def inject_token(page, token: str) -> None:
    from playwright.async_api import Error as PlaywrightError

    try:
        await page.evaluate(INJECT_TOKEN_JS, token)
    except PlaywrightError as exc:  # the callback or the form submit may navigate away mid-evaluate
        log.debug("token injection ended with %s", type(exc).__name__)


def describe(verdict: Verdict) -> str:
    return verdict.challenge or verdict.kind


# ---------------------------------------------------------------- failed route, in words
CHALLENGE_WORDS = {"interstitial": "interstitial challenge", "turnstile": "Turnstile challenge", "other_captcha": "CAPTCHA challenge"}
SKIP_WORDS = {
    "click": {"not_requested": "no click requested", "not_allowed_by_service": "the click is not allowed by the service",
              "limit_reached": "the click limit was reached"},
    "solver": {"not_requested": "no solver requested", "not_allowed_by_service": "no solver allowed",
               "not_configured": "no solver configured", "limit_reached": "the solver limit was reached",
               "no_sitekey": "no site key found for a solver"},
}


def describe_steps(steps: list[dict]) -> str:
    parts = []
    for st in steps:
        name, outcome = st.get("step"), st.get("outcome")
        if outcome == "skipped":
            parts.append(SKIP_WORDS.get(name, {}).get(st.get("reason"), f"{name} skipped"))
        elif name == "wait":
            parts.append(f"waited {int(st.get('seconds') or 0)} s")
        elif name == "click":
            parts.append({"still_challenged": "clicked the checkbox once", "frame_not_found": "the checkbox was not found",
                          "hard_block": "clicked the checkbox once, then the site blocked the page"}.get(outcome, f"click {outcome}"))
        elif name == "solver":
            parts.append("a solver token did not pass" if outcome == "still_challenged" else
                         f"the solver failed ({st.get('code')})" if outcome == "error" else f"solver {outcome}")
        elif name == "next_proxy_tier":
            parts.append("moved to the next proxy tier")
    return ", ".join(parts) or "no step could be tried"


def describe_failure(failure: dict, tail: str) -> str:
    """"<host> shows a Cloudflare interstitial challenge that was not passed (waited 15 s,
    clicked the checkbox once, no solver allowed); proxy tier 1. <tail>" — for the operator."""
    trace = failure["trace"]
    words = CHALLENGE_WORDS.get(trace.get("challenge") or "", "challenge")
    if failure.get("vendor"):
        words = f"{failure['vendor']} {words}"
    article = "an" if words[0].lower() in "aeiou" else "a"
    tier = trace.get("proxy_tier")
    proxy = f"proxy tier {tier}" if tier else "no proxy"
    earlier = trace.get("passed_earlier")
    again = (f"; the host had passed a challenge earlier in this call ({earlier.get('step')}) and was challenged again"
             if isinstance(earlier, dict) else "")
    return (f"{failure['host']} shows {article} {words} that was not passed ({describe_steps(trace.get('steps') or [])}){again}; "
            f"{proxy}. {tail}")[:2000]
