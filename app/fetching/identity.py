"""One browser identity per exit.

A real visitor does not change GPU, screen, platform or core count between two page loads
from one IP: analysis once claimed an NVIDIA GPU and the samples of the same request, on the
same sticky exit IP, an AMD one. The service now keeps ONE fingerprint identity per

    (browser, spoofed OS, proxy account and tier, exit IP)

— the exit IP as ``geo.exit_geo`` resolves it; while it is unknown, per sticky proxy session
(the proxy URL carries the session) — and without a proxy one per host IP. Analysis,
samples, the job contexts (both browsers) and the protection report all take their identity
from here, so every context that goes out through one exit looks like the same machine.

* Camoufox: a bundled real preset (navigator UA/platform/cores, screen, WebGL) whose screen
  holds the launch window, plus a salt that fixes the fonts and speech voices Camoufox draws
  for it (without a salt Camoufox draws a new font list for every context).
* Chromium: one browserforge fingerprint (Chrome on Windows, a screen that holds the window).

The "proxy account and tier" part is a hash of the tier's definition (provider, endpoint,
user name, geo fields, tier number; never the password), registered by
``proxies.tier_url`` for every URL it builds. The environment of the exit (timezone,
locale, geolocation, WebRTC IP: ``geo.py``) is applied on top of the identity as before.
Unknown-exit session keys also use only the endpoint and session-bearing username.
Legacy keys derived from credential-bearing URLs are discarded on cache load.
The first cache upgrade also redraws legacy tier-scoped identities: their old
hashes cannot distinguish safe account scopes from secret-bearing custom lists.
Direct and endpoint-only identities survive; subsequent upgrades retain public keys.

Identities live in memory and in ``<data dir>/identities.json`` (rewritten atomically when a
new identity is drawn; at most ``MAX_ENTRIES``, least recently used dropped), so they
survive a restart and the report script — a separate process on the same volume — reuses
the identity the service gave that exit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("wss.identity")

MAX_ENTRIES = 1000
FILE_NAME = "identities.json"


def _h(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


@dataclass
class Identity:
    preset_id: str
    browser: str  # camoufox | chromium
    os: str
    preset: dict | None = None  # Camoufox: the bundled preset
    salt: int | None = None  # Camoufox: fixes the font and voice draw of the preset
    fingerprint: dict | None = None  # Chromium: browserforge fingerprint as a dict
    exit_ip: str | None = None
    created_at: str = ""
    last_used: float = 0.0
    extra: dict = field(default_factory=dict)


def key_for(browser: str, os_name: str, proxy_url: str | None, geo) -> str:
    """The identity key of a context: browser, OS (for Camoufox the launch variant: OS and
    canvas mode, ``browsers.Variant.key``), proxy account+tier, exit."""
    from app.fetching.proxies import scope_of

    ip = getattr(geo, "ip", None) if geo is not None else None
    if proxy_url:
        scope = scope_of(proxy_url) or ("proxy-" + _public_proxy_id(proxy_url))
        where = f"ip:{ip}" if ip else "session-public-v1:" + _public_proxy_id(proxy_url, session=True, n=16)
    else:
        scope = "direct"
        where = f"ip:{ip}" if ip else "host"
    return f"{browser}|{os_name}|{scope}|{where}"


def _public_proxy_id(proxy_url: str, *, session: bool = False, n: int = 12) -> str:
    """Digest public routing identity; never include password, path or query."""
    from app.fetching.proxies import identity_metadata

    metadata = identity_metadata(proxy_url)
    endpoint = metadata["endpoint"]
    # Endpoint-only keys preserve existing known-IP identities. A formatted
    # username distinguishes sticky sessions while a password rotation does not.
    public = json.dumps([endpoint, metadata["username"]], separators=(",", ":")) if session else endpoint
    return hashlib.sha256(public.encode("utf-8"), usedforsecurity=False).hexdigest()[:n]


def _coherent_chromium_geometry(fingerprint: dict | None) -> bool:
    """Reject sampled windows larger than their sampled screen; zero is unknown.

    Browserforge injects positive outer dimensions independently of the actual
    viewport. Screen constraints alone do not exclude incoherent window samples.
    """
    try:
        screen = fingerprint["screen"]
        return all(
            screen[size] > 0
            and (not screen.get(outer) or screen[outer] <= screen[size])
            for size, outer in (("width", "outerWidth"), ("height", "outerHeight"))
        )
    except (KeyError, TypeError):
        return False


class IdentityStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._mem: dict[str, Identity] = {}
        self._loaded = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ storage
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            legacy_format = data.get("version", 1) == 1
            removed_legacy = legacy_format
            for key, raw in (data.get("identities") or {}).items():
                parts = key.split("|")
                legacy_scope = legacy_format and len(parts) == 4 and re.fullmatch(r"t\d+-[0-9a-f]{12}", parts[2])
                if parts[-1].startswith("session:") or legacy_scope:
                    removed_legacy = True
                    continue
                try:
                    ident = Identity(**raw)
                    # Old persisted samples are replaced before creating a new
                    # context. Existing contexts keep their original identity.
                    if ident.browser == "chromium" and not _coherent_chromium_geometry(ident.fingerprint):
                        continue
                    self._mem[key] = ident
                except TypeError:
                    continue
            if removed_legacy:
                self._save()
        except (OSError, ValueError) as exc:
            log.warning("identities file unreadable (%s); new identities are drawn", type(exc).__name__)

    def _save(self) -> None:
        if self.path is None:
            return
        if len(self._mem) > MAX_ENTRIES:
            for key in sorted(self._mem, key=lambda k: self._mem[k].last_used)[: len(self._mem) - MAX_ENTRIES]:
                self._mem.pop(key, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"version": 2, "identities": {k: asdict(v) for k, v in self._mem.items()}}))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("identities file not written (%s); identities stay in memory", type(exc).__name__)

    # ------------------------------------------------------------------ access
    def get(self, key: str) -> Identity | None:
        with self._lock:
            self._load()
            return self._mem.get(key)

    def get_or_create(self, key: str, make) -> Identity:
        """The identity of ``key``; ``make()`` draws a new one the first time."""
        import time

        with self._lock:
            self._load()
            found = self._mem.get(key)
            if found is not None:
                found.last_used = time.time()
                return found
            ident = make()
            ident.last_used = time.time()
            self._mem[key] = ident
            self._save()
            log.info("new browser identity %s for %s", ident.preset_id, _public_key(key))
            return ident

    def find_by_exit(self, browser: str, os_name: str, ip: str) -> Identity | None:
        """``os_name``: the same OS segment as in ``key_for`` (a ``Variant.key`` for Camoufox)."""
        """The most recently used identity any scope gave this exit IP (the report script)."""
        with self._lock:
            self._load()
            hits = [v for k, v in self._mem.items() if k.startswith(f"{browser}|{os_name}|") and k.endswith(f"|ip:{ip}")]
            return max(hits, key=lambda v: v.last_used) if hits else None


def _public_key(key: str) -> str:
    """The key for a log line (hashes and an IP only, nothing secret)."""
    return key


def tag(context, preset_id: str | None, env: dict | None = None):
    """Marks a context with its identity and launch variant, for the protection trace."""
    try:
        context._wss_preset_id = preset_id
        context._wss_env = dict(env or {})
    except Exception:
        pass
    return context


def env_of(page) -> dict:
    """``browser_os``, ``canvas_noise``, ``canvas_readback``, ``webgl_readback`` of the page's context."""
    try:
        return dict(getattr(page.context, "_wss_env", None) or {})
    except Exception:
        return {}


# A known fill read back on about:blank: "exact" = the pixels drawn, "noise" = anything else
# (Camoufox covers the canvas with random data when the identity claims another OS or GPU
# than the host renders with), "unavailable" = no context/readback at all.
READBACK_JS = """
() => {
  const want = [51, 102, 153, 255];
  const same = (d) => { for (let i = 0; i < d.length; i += 4) { for (let k = 0; k < 4; k++) { if (Math.abs(d[i + k] - want[k]) > 1) return false; } } return true; };
  const out = {};
  try {
    const c = document.createElement('canvas'); c.width = 8; c.height = 8;
    const x = c.getContext('2d');
    if (!x) { out.canvas = 'unavailable'; } else {
      x.fillStyle = 'rgb(51,102,153)'; x.fillRect(0, 0, 8, 8);
      out.canvas = same(x.getImageData(0, 0, 8, 8).data) ? 'exact' : 'noise';
    }
  } catch (e) { out.canvas = 'unavailable'; }
  try {
    const c = document.createElement('canvas'); c.width = 4; c.height = 4;
    const gl = c.getContext('webgl', {preserveDrawingBuffer: true});
    if (!gl) { out.webgl = 'unavailable'; } else {
      gl.clearColor(51 / 255, 102 / 255, 153 / 255, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      const px = new Uint8Array(16); gl.readPixels(0, 0, 2, 2, gl.RGBA, gl.UNSIGNED_BYTE, px);
      out.webgl = same(px) ? 'exact' : 'noise';
    }
  } catch (e) { out.webgl = 'unavailable'; }
  return out;
}
"""
_readback_cache: dict[tuple, dict] = {}


async def readback(context, cache_key: tuple) -> dict:
    """Canvas 2D and WebGL readback of ``context``, measured once per identity and variant on
    a blank page of that context before it navigates anywhere (cached for the process)."""
    hit = _readback_cache.get(cache_key)
    if hit is not None:
        return hit
    out = {"canvas_readback": "unavailable", "webgl_readback": "unavailable"}
    page = None
    try:
        page = await context.new_page()
        got = await page.evaluate(READBACK_JS)
        if isinstance(got, dict):
            out = {"canvas_readback": got.get("canvas") or "unavailable", "webgl_readback": got.get("webgl") or "unavailable"}
            _readback_cache[cache_key] = out
    except Exception as exc:
        log.info("canvas self-check failed (%s)", type(exc).__name__)
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
    return out


def preset_id_of(page) -> str | None:
    try:
        return getattr(page.context, "_wss_preset_id", None)
    except Exception:
        return None


_store: IdentityStore | None = None


def store() -> IdentityStore:
    global _store
    if _store is None:
        try:
            from app.settings import get_settings

            path: Path | None = Path(get_settings().data_dir) / FILE_NAME
        except Exception:
            path = None
        _store = IdentityStore(path)
    return _store


def reset_for_tests(path: Path | None) -> IdentityStore:
    global _store
    _store = IdentityStore(path)
    return _store


# ====================================================================== drawing
def _now() -> str:
    from app.core.timeutil import now_iso

    return now_iso()


def new_camoufox_identity(os_name: str, ff_version: str | None, exit_ip: str | None) -> Identity:
    """A preset of ``os_name`` (``windows``, ``macos``, ``linux``) whose screen holds the window."""
    from app.fetching.browsers import consistent_preset

    preset = consistent_preset(os_name, ff_version)
    salt = secrets.randbits(63)
    material = json.dumps(preset, sort_keys=True, default=str) + f"|{salt}"
    return Identity(preset_id="fp_" + _h(material, 16), browser="camoufox", os=os_name, preset=preset, salt=salt,
                    exit_ip=exit_ip, created_at=_now())


def new_chromium_identity(exit_ip: str | None, os_name: str | None = None) -> Identity:
    """Chrome on the machine's own OS (a Windows claim on the Linux host failed a
    managed challenge on a test site, the host-OS identity passed)."""
    from app.fetching.browsers import host_os_name

    os_name = os_name or host_os_name()
    generator = chromium_generator(os_name)
    for _ in range(32):
        fp = asdict(generator.generate())
        if _coherent_chromium_geometry(fp):
            break
    else:
        raise ValueError("Could not generate a coherent Chromium fingerprint after 32 attempts")
    return Identity(preset_id="fp_" + _h(json.dumps(fp, sort_keys=True, default=str), 16), browser="chromium", os=os_name,
                    fingerprint=fp, exit_ip=exit_ip, created_at=_now())


_generators: dict[str, Any] = {}


def chromium_generator(os_name: str = "linux"):
    """Chrome on a desktop of ``os_name`` whose screen holds the 1280x720 window."""
    gen = _generators.get(os_name)
    if gen is None:
        from crawlee.fingerprint_suite import DefaultFingerprintGenerator, HeaderGeneratorOptions, ScreenOptions

        from app.fetching.browsers import WINDOW

        gen = _generators[os_name] = DefaultFingerprintGenerator(
            header_options=HeaderGeneratorOptions(browsers=["chrome"], operating_systems=[os_name], devices=["desktop"]),
            screen_options=ScreenOptions(min_width=WINDOW[0], min_height=WINDOW[1]),
        )
    return gen


async def identity_for(browser: str, os_name: str, proxy_url: str | None, geo, *, ff_version: str | None = None,
                       variant_key: str | None = None) -> Identity:
    """``variant_key`` (Camoufox: OS and canvas mode) replaces the OS in the key, so launch
    variants never share an identity; ``os_name`` is the OS the preset is drawn for."""
    key = key_for(browser, variant_key or os_name, proxy_url, geo)
    ip = getattr(geo, "ip", None) if geo is not None else None
    if browser == "camoufox":
        make = lambda: new_camoufox_identity(os_name, ff_version, ip)  # noqa: E731
    else:
        make = lambda: new_chromium_identity(ip, os_name)  # noqa: E731
    st = store()
    if st.get(key) is not None:
        return st.get_or_create(key, make)
    # drawing reads bundled data files: off the event loop; a concurrent draw for the same
    # key loses to the first one stored
    ident = await asyncio.get_running_loop().run_in_executor(None, make)
    return st.get_or_create(key, lambda: ident)


# ====================================================================== contexts
def camoufox_fingerprint(identity: Identity, ff_version: str, *, os_name: str | None = None, webgl_config=None,
                         webrtc_ip: str | None = None) -> dict:
    """Camoufox's per-context fingerprint for ``identity``: the preset's navigator, screen and
    WebGL, and the fonts and voices of its salt (the same for every context of the identity)."""
    from camoufox.fingerprints import from_preset, generate_context_fingerprint

    pinned = from_preset(identity.preset, ff_version, salt=identity.salt)
    overrides = {k: pinned[k] for k in ("fonts", "voices") if k in pinned}
    return generate_context_fingerprint(preset=identity.preset, os=os_name, ff_version=ff_version, webrtc_ip=webrtc_ip,
                                        webgl_config=webgl_config, config_overrides=overrides)


async def camoufox_context(browser, identity: Identity | None, *, proxy: dict | None, variant=None, **kwargs: Any):
    """``camoufox_context_raw`` plus the canvas self-check, both recorded on the context."""
    context = await camoufox_context_raw(browser, identity, proxy=proxy, **kwargs)
    env = variant.public() if variant is not None else {}
    pid = getattr(context, "_wss_preset_id", None)
    env.update(await readback(context, ("camoufox", pid, variant.key() if variant is not None else "")))
    return tag(context, pid, env)


async def camoufox_context_raw(browser, identity: Identity | None, *, proxy: dict | None, **kwargs: Any):
    """A Camoufox context with ``identity`` — Camoufox's ``AsyncNewContext`` with the identity's
    font/voice salt (``AsyncNewContext`` itself would draw new fonts for every context).
    ``kwargs``: Playwright context options plus ``webrtc_ip``/``geolocation`` (geo.py).
    Raises ValueError like ``AsyncNewContext`` on a browser that renders as the host."""
    from camoufox.async_api import AsyncNewContext

    # Service workers bypass Playwright request routes, including safety guards.
    kwargs["service_workers"] = "block"
    if identity is None or not identity.preset:
        return tag(await AsyncNewContext(browser, proxy=proxy, **kwargs), None)
    try:
        from camoufox.async_api import _resolve_proxy_geo
        from camoufox.utils import context_identity, new_context_options
    except ImportError:  # another Camoufox release: its own context, fonts drawn by it
        return tag(await AsyncNewContext(browser, preset=identity.preset, proxy=proxy, **kwargs), identity.preset_id)
    kw = dict(kwargs)
    ff = browser.version.split(".", 1)[0]
    webrtc_ip = kw.pop("webrtc_ip", None)
    geolocation = kw.pop("geolocation", None)
    if proxy and (not webrtc_ip or "timezone_id" not in kw):
        exit_ip, tz = await _resolve_proxy_geo(proxy)
        webrtc_ip = webrtc_ip or exit_ip
        kw.setdefault("timezone_id", tz)
    os_name, webgl_config = context_identity(browser, None, identity.preset)
    fp = await asyncio.get_running_loop().run_in_executor(
        None, lambda: camoufox_fingerprint(identity, ff, os_name=os_name, webgl_config=webgl_config, webrtc_ip=webrtc_ip))
    context = await browser.new_context(**new_context_options(fp, kw, proxy, geolocation))
    await context.add_init_script(fp["init_script"])
    return tag(context, identity.preset_id)


def chromium_fingerprint(identity: Identity, locale: str | None):
    """The identity's browserforge fingerprint, its language set to the exit's locale (the
    fingerprint would otherwise send its own Accept-Language and navigator.language)."""
    import copy

    from browserforge.fingerprints import Fingerprint, NavigatorFingerprint, ScreenFingerprint, VideoCard

    if not _coherent_chromium_geometry(identity.fingerprint):
        raise ValueError("A coherent Chromium fingerprint is required before creating a context")
    raw = copy.deepcopy(identity.fingerprint or {})
    nav = dict(raw.pop("navigator"))
    if locale:
        base = locale.split("-", 1)[0]
        nav["language"] = locale
        nav["languages"] = [locale] + ([base] if base != locale else [])
        headers = dict(raw.get("headers") or {})
        headers["Accept-Language"] = f"{locale},{base};q=0.9" if base != locale else locale
        raw["headers"] = headers
    screen = ScreenFingerprint(**raw.pop("screen"))
    card = raw.pop("videoCard", None)
    return Fingerprint(screen=screen, navigator=NavigatorFingerprint(**nav), videoCard=VideoCard(**card) if card else None, **raw)


async def chromium_context(browser, identity: Identity, **options: Any):
    """A Chromium context with the identity's fingerprint (browserforge's injector); its
    language follows ``options["locale"]`` (the exit's, geo.py) when given."""
    from browserforge.injectors.playwright import AsyncNewContext

    # Keep private-address and listing-navigation guards on every request.
    options["service_workers"] = "block"
    context = await AsyncNewContext(browser=browser, fingerprint=chromium_fingerprint(identity, options.get("locale")), **options)
    env = {"browser_os": identity.os, "canvas_noise": None}
    env.update(await readback(context, ("chromium", identity.preset_id, "")))
    return tag(context, identity.preset_id, env)
