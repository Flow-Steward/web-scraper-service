"""Browsers of the image and our Crawlee browser plugins/controllers.

* Chromium: Crawlee's Playwright plugin with incognito contexts (one context per page,
  proxy per context) and our controller, which gives every context the browserforge
  fingerprint of its exit (``identity.py``: one per proxy account+tier and exit IP), so a
  clearance cookie keeps meeting the identity that earned it.
* Camoufox: our own plugin (launch through ``AsyncNewBrowser`` with the options the old
  code relied on: ``humanize``, ``disable_coop``) and controller, which creates each
  context with the Camoufox preset, fonts and voices of its exit (``identity.py``) and the
  exit's timezone/locale/geolocation/WebRTC IP (``geo.py``). Crawlee's fingerprint
  generator is never combined with Camoufox.
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

log = logging.getLogger("wss.browsers")


def _first(pattern: str) -> str | None:
    found = sorted(glob.glob(pattern))
    return found[-1] if found else None


@lru_cache(maxsize=1)
def browser_presence() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/.cache/ms-playwright"))
    chrome = _first(f"{base}/chromium-*/chrome-linux*/chrome")
    version = None
    if chrome:
        try:
            raw = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=20).stdout
            version = raw.strip().split()[-1] if raw.strip() else None
        except (OSError, subprocess.SubprocessError):
            version = None
    out["chromium"] = {"present": bool(chrome), "version": version, "path": chrome}
    cam_path = None
    cam_version = None
    try:
        from camoufox.pkgman import installed_verstr  # type: ignore

        cam_version = installed_verstr()
    except Exception:
        cam_version = None
    cam_bin = _first(os.path.expanduser("~/.cache/camoufox/browsers/*/*/camoufox-bin")) or _first(
        os.path.expanduser("~/.cache/camoufox/camoufox-bin")
    )
    if cam_bin:
        cam_path = cam_bin
        if not cam_version:
            cam_version = os.path.basename(os.path.dirname(cam_bin)).split("-")[0] + "-" + os.path.basename(os.path.dirname(cam_bin)).split("-")[1] if "-" in os.path.basename(os.path.dirname(cam_bin)) else None
    out["camoufox"] = {"present": bool(cam_bin), "version": cam_version, "path": cam_path}
    return out


def resolve_browser(name: str) -> str | None:
    presence = browser_presence()
    if name == "auto":
        if presence["camoufox"]["present"]:
            return "camoufox"
        if presence["chromium"]["present"]:
            return "chromium"
        return None
    return name if presence.get(name, {}).get("present") else None


WINDOW = (1280, 720)  # the browser window every launch uses (old code: browser.py 174-260)

BROWSER_OS = ("windows", "macos", "linux", "host")


def host_os_name() -> str:
    """The machine's own OS as a Camoufox OS name (``linux`` in the image)."""
    try:
        from camoufox.fingerprints import OS_NAMES
        from camoufox.utils import _host_os_key

        return OS_NAMES[_host_os_key()]
    except Exception:
        import platform

        return {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")
CANVAS_NOISE = ("auto", "off", "on")


@dataclass(frozen=True)
class Variant:
    """How Camoufox is launched (job settings ``browser_os`` / ``canvas_noise``).

    ``browser_os``: the OS the identity claims; ``host`` (the default since 2026-11-13; on a
    test site only the host-OS identity passed) = the machine's own OS (Linux in the
    image and on the server). ``canvas_noise``: ``auto`` = Camoufox's own rule (the canvas readback is covered
    with random data when the identity claims another OS or GPU than the host renders with —
    a Windows identity on the Linux image, as the service always launched it before), ``off`` =
    never covered (the page reads the host's real pixels whatever GPU the identity claims),
    ``on`` = always covered. Chromium ignores both."""

    browser_os: str = "host"
    canvas_noise: str = "auto"

    @classmethod
    def from_config(cls, config: dict | None) -> "Variant":
        cfg = config or {}
        os_ = cfg.get("browser_os") or "host"
        noise = cfg.get("canvas_noise") or "auto"
        return cls(os_ if os_ in BROWSER_OS else "host", noise if noise in CANVAS_NOISE else "auto")

    def os_name(self) -> str:
        """The Camoufox OS name the launch and the preset use."""
        return host_os_name() if self.browser_os == "host" else self.browser_os

    def noise_arg(self) -> bool | None:
        return {"auto": None, "off": False, "on": True}[self.canvas_noise]

    def key(self) -> str:
        """The OS segment of the identity key: variants never share an identity."""
        return f"{self.os_name()}~canvas-{self.canvas_noise}"

    def public(self) -> dict:
        return {"browser_os": self.browser_os, "canvas_noise": self.canvas_noise}


async def launch_camoufox(playwright, *, headless: Any, variant: Variant | None = None, **overrides: Any):
    """Camoufox as every part of the service launches it (jobs, analysis, samples, the report):
    ``humanize``, ``disable_coop``, the variant's OS and canvas mode, the 1280x720 window,
    ``enable_cache``. ``overrides``: the report's experiments (``humanize``, ``webgl_config``)."""
    from camoufox.async_api import AsyncNewBrowser

    variant = variant or Variant()
    options: dict[str, Any] = dict(headless=headless, humanize=2.0, disable_coop=True, i_know_what_im_doing=True,
                                   canvas_noise=variant.noise_arg(), os=variant.os_name(), window=WINDOW, enable_cache=True)
    options.update(overrides)
    from app.fetching.firefox_policy import FirefoxPolicyBridge

    bridge = await FirefoxPolicyBridge().start()
    browser = None
    try:
        options["addons"] = [*(options.get("addons") or []), bridge.addon_path]
        browser = await AsyncNewBrowser(playwright, **options)
        browser._wss_firefox_policy_bridge = bridge
        native_close = browser.close

        async def close_with_policy(**kwargs):
            # Revoke every binding before native contexts/browser begin cleanup.
            bridge.revoke_all()
            try:
                await native_close(**kwargs)
            finally:
                await bridge.close()

        browser.close = close_with_policy
        def disconnected(*_):
            bridge.begin_close()
        browser.on("disconnected", disconnected)
        # Pinned Camoufox initializes extension backgrounds only after its
        # first window exists. A private blank page starts that window without
        # issuing any document network request; discard it after readiness.
        bootstrap = await browser.new_context(service_workers="block")
        try:
            await bootstrap.new_page()
            await bridge.wait_ready()
        finally:
            await bootstrap.close()
        return browser
    except BaseException:
        try:
            if browser is not None:
                await browser.close()
        finally:
            await bridge.close()
        raise


def consistent_preset(os_name: str = "windows", ff_version: str | None = None):
    """A Camoufox fingerprint preset whose available screen holds the launch window, so the
    page never sees a window larger than its screen."""
    from camoufox.fingerprints import get_random_preset

    fallback = None
    for _ in range(30):
        try:
            preset = get_random_preset(os=os_name, ff_version=ff_version)
        except Exception:
            return None
        if preset is None:
            return None
        fallback = fallback or preset
        screen = preset.get("screen") or {}
        if int(screen.get("availWidth") or screen.get("width") or 0) >= WINDOW[0] and int(screen.get("availHeight") or screen.get("height") or 0) >= WINDOW[1]:
            return preset
    return fallback


def _make_plugins():
    """Import Crawlee lazily: the API layer must not depend on Playwright being importable."""
    from crawlee.browsers import PlaywrightBrowserController, PlaywrightBrowserPlugin
    from crawlee.browsers._playwright_browser_controller import ProxySettings  # type: ignore

    class SessionContexts:
        """One browser context per crawl session (sticky proxy session), reused for all its pages
        (a fresh incognito context per page re-met Cloudflare every few pages; samples
        already keep one cleared tab). Crawlee's ``new_page`` with ``use_incognito_pages`` makes a
        context per page; this keeps the session's context — its cookies, storage and cache —
        and opens the next page in it. At most ``MAX_CONTEXTS`` contexts stay open; an idle one
        is closed when the cap is reached (a retired session's)."""

        MAX_CONTEXTS = 8

        async def new_page(self, browser_new_context_options=None, proxy_info=None):  # type: ignore[override]
            from datetime import datetime, timezone

            if not self.has_free_capacity:
                raise ValueError("Cannot open more pages in this browser.")
            self._opening_pages_count += 1
            try:
                contexts = self.__dict__.setdefault("_wss_contexts", {})
                key = getattr(proxy_info, "session_id", None) or getattr(proxy_info, "url", None) or "default"
                ctx = contexts.get(key)
                if ctx is not None and getattr(ctx, "_wss_closed", False):
                    ctx = None
                if ctx is None:
                    await self._close_idle_contexts(contexts)
                    ctx = await self._create_browser_context(browser_new_context_options=browser_new_context_options, proxy_info=proxy_info)
                    ctx.on("close", lambda *_: setattr(ctx, "_wss_closed", True))
                    contexts[key] = ctx
                page = await ctx.new_page()
                page.on(event="close", f=self._on_page_close)
                self._pages.append(page)
                self._last_page_opened_at = datetime.now(timezone.utc)
                self._total_opened_pages += 1
            finally:
                self._opening_pages_count -= 1
            return page

        async def _close_idle_contexts(self, contexts: dict) -> None:
            if len(contexts) < self.MAX_CONTEXTS:
                return
            for key, ctx in list(contexts.items()):
                try:
                    idle = not ctx.pages
                except Exception:
                    idle = True
                if idle:
                    contexts.pop(key, None)
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                    if len(contexts) < self.MAX_CONTEXTS:
                        return

    class SessionFingerprintController(SessionContexts, PlaywrightBrowserController):
        """Chromium: the identity of the context's exit (identity.py), never one per page."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)

        async def _create_browser_context(self, browser_new_context_options=None, proxy_info=None):  # type: ignore[override]
            from app.fetching.geo import exit_geo
            from app.fetching.identity import chromium_context, identity_for

            options = self._filter_context_options(dict(browser_new_context_options or {}))
            if proxy_info:
                options["proxy"] = ProxySettings(
                    server=f"{proxy_info.scheme}://{proxy_info.hostname}:{proxy_info.port}",
                    username=proxy_info.username,
                    password=proxy_info.password,
                )
            # timezone, locale and geolocation of the exit IP (geo.py)
            proxy_url = getattr(proxy_info, "url", None) if proxy_info else None
            geo = await exit_geo(proxy_url)
            if geo is not None:
                options.update(geo.context_kwargs(camoufox=False))
            identity = await identity_for("chromium", host_os_name(), proxy_url, geo)
            options.setdefault("viewport", {"width": WINDOW[0], "height": WINDOW[1]})
            return await chromium_context(self._browser, identity, **options)

    class ChromiumPlugin(PlaywrightBrowserPlugin):
        def __init__(self, *, headless: bool, max_pages: int) -> None:
            super().__init__(
                browser_type="chromium",
                browser_launch_options={
                    "headless": headless,
                    "executable_path": None,
                    "chromium_sandbox": False,
                    "args": ["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"],
                },
                use_incognito_pages=True,
                max_open_pages_per_browser=max_pages,
            )

        async def new_browser(self):  # type: ignore[override]
            if not self._playwright:
                raise RuntimeError("plugin not initialised")
            opts = {k: v for k, v in self._browser_launch_options.items() if v is not None}
            browser = await self._playwright.chromium.launch(**opts)
            return SessionFingerprintController(
                browser,
                use_incognito_pages=True,
                max_open_pages_per_browser=self._max_open_pages_per_browser,
                header_generator=None,
            )

    class CamoufoxController(SessionContexts, PlaywrightBrowserController):
        """Camoufox: the identity of the context's exit (identity.py): one preset, fonts and
        voices per exit, the same as analysis and samples going out through it."""

        def __init__(self, *args: Any, host_rendered: dict, variant: Variant, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._host_rendered = host_rendered
            self._variant = variant

        async def _create_browser_context(self, browser_new_context_options=None, proxy_info=None):  # type: ignore[override]
            from app.fetching.geo import exit_geo
            from app.fetching.identity import camoufox_context, identity_for

            options = self._filter_context_options(dict(browser_new_context_options or {}))
            proxy = None
            if proxy_info:
                proxy = {"server": f"{proxy_info.scheme}://{proxy_info.hostname}:{proxy_info.port}"}
                if proxy_info.username:
                    proxy["username"] = proxy_info.username
                    proxy["password"] = proxy_info.password
            proxy_url = getattr(proxy_info, "url", None) if proxy_info else None
            # timezone, locale, geolocation and WebRTC IP of the exit IP (geo.py); with both
            # webrtc_ip and timezone_id given Camoufox makes no lookup of its own
            geo = await exit_geo(proxy_url)
            if geo is not None:
                options.update(geo.context_kwargs(camoufox=True))
            identity = None
            v = self._variant
            if not self._host_rendered.get("yes"):
                identity = await identity_for("camoufox", v.os_name(), proxy_url, geo, ff_version=self._browser.version.split(".", 1)[0],
                                              variant_key=v.key())
            try:
                return await camoufox_context(self._browser, identity, proxy=proxy, variant=v, **options)
            except ValueError:
                # a browser that renders as the host may refuse another identity; keep the host's
                self._host_rendered["yes"] = True
                return await camoufox_context(self._browser, None, proxy=proxy, variant=v, **options)

    class CamoufoxPlugin(PlaywrightBrowserPlugin):
        def __init__(self, *, headless: bool, max_pages: int, variant: Variant | None = None) -> None:
            super().__init__(browser_type="firefox", use_incognito_pages=True, max_open_pages_per_browser=max_pages)
            self._camoufox_headless = headless
            self._host_rendered: dict[str, bool] = {}
            self._variant = variant or Variant()

        async def new_browser(self):  # type: ignore[override]
            if not self._playwright:
                raise RuntimeError("plugin not initialised")
            headless: Any = self._camoufox_headless
            if not headless and not os.environ.get("DISPLAY"):
                headless = "virtual"
            browser = await launch_camoufox(self._playwright, headless=headless, variant=self._variant)
            return CamoufoxController(
                browser,
                use_incognito_pages=True,
                max_open_pages_per_browser=self._max_open_pages_per_browser,
                header_generator=None,
                host_rendered=self._host_rendered,
                variant=self._variant,
            )

    return ChromiumPlugin, CamoufoxPlugin


def make_browser_pool(browser: str, *, headless: bool, max_pages: int, variant: Variant | None = None):
    from crawlee.browsers import BrowserPool

    chromium_cls, camoufox_cls = _make_plugins()
    plugin = (camoufox_cls(headless=headless, max_pages=max_pages, variant=variant) if browser == "camoufox"
              else chromium_cls(headless=headless, max_pages=max_pages))
    return BrowserPool(plugins=[plugin], retire_browser_after_page_count=200)
