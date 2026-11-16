"""The browser environment follows the exit IP.

A context that talks through a Latvian exit IP must not report UTC, an English locale and
no geolocation. For every browser context the service creates (job, analysis, samples,
the protection report) the exit IP is resolved once per proxy session (the sticky
session's proxy URL is the cache key; without a proxy: the host's public IP, once per
process) through ``WSS_IP_ECHO_URL``; timezone, locale and coordinates come from the
echo's answer when it carries them, else from Camoufox's bundled GeoIP database (read
locally, never downloaded at run time). The result goes into the context as
``timezone_id``, ``locale`` (which also sets ``Accept-Language``), ``geolocation`` and, for
Camoufox, ``webrtc_ip``; the values used are reported in the protection trace.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass

log = logging.getLogger("wss.geo")

_cache: dict[str, tuple[float, "ExitGeo | None"]] = {}
_TTL = 6 * 3600.0
_locks: dict[str, asyncio.Lock] = {}


@dataclass
class ExitGeo:
    ip: str | None
    timezone: str | None
    locale: str | None
    latitude: float | None
    longitude: float | None
    country: str | None
    source: str  # echo | echo+geoip | unknown

    def public(self) -> dict:
        """What the trace shows: no proxy URL, nothing secret."""
        return asdict(self)

    def context_kwargs(self, camoufox: bool) -> dict:
        kw: dict = {}
        if self.timezone:
            kw["timezone_id"] = self.timezone
        if self.locale:
            kw["locale"] = self.locale
        if self.latitude is not None and self.longitude is not None:
            kw["geolocation"] = {"latitude": self.latitude, "longitude": self.longitude, "accuracy": 100.0}
            if not camoufox:
                kw["permissions"] = ["geolocation"]
        if camoufox and self.ip:
            kw["webrtc_ip"] = self.ip
        return kw


def _geoip_lookup(ip: str) -> dict:
    """Camoufox's bundled GeoIP database, read locally (no update, no download)."""
    try:
        import maxminddb
        from camoufox.geolocation import _find_in, get_mmdb_path, load_geoip_config

        config = load_geoip_config()
        path = get_mmdb_path("ipv6" if ":" in ip else "ipv4", config)
        if not path.exists():
            return {}
        with maxminddb.open_database(str(path)) as reader:
            resp = reader.get(ip)
        if not resp:
            return {}
        paths = config["paths"]
        iso = str(_find_in(resp, paths["iso_code"]) or "").upper() or None
        out = {
            "country": iso,
            "timezone": _find_in(resp, paths["timezone"]),
            "latitude": _find_in(resp, paths["latitude"]),
            "longitude": _find_in(resp, paths["longitude"]),
        }
        if iso:
            out["locale"] = _locale_for(iso)
        return {k: v for k, v in out.items() if v is not None}
    except Exception as exc:
        log.debug("geoip lookup failed: %s", type(exc).__name__)
        return {}


def _locale_for(country: str | None) -> str | None:
    """The country's main language as a locale, the same every time (``de-DE`` for DE,
    ``lv-LV`` for LV): the official language with the most speakers in Camoufox's bundled
    CLDR territory data. Camoufox's own ``SELECTOR.from_region`` draws a language at random
    by share of speakers (``en-DE`` or ``ru-LV`` now and then), which made one exit look
    like a different visitor from one call to the next."""
    if not country:
        return None
    region = country.upper()
    try:
        from camoufox.locales import SELECTOR, normalize_locale

        territory = SELECTOR.root.find(f"territory[@type='{region}']")
        if territory is None:
            return None
        rows = [(lp.get("type") or "", float(lp.get("populationPercent") or 0), lp.get("officialStatus"))
                for lp in territory.findall("languagePopulation")]
        rows = [r for r in rows if r[0]]
        official = [r for r in rows if r[2] in ("official", "de_facto_official")]
        if not rows:
            return None
        language = max(official or rows, key=lambda r: r[1])[0].replace("_", "-")
        return normalize_locale(f"{language}-{region}").as_string
    except Exception:
        return None


async def _echo(proxy_url: str | None, echo_url: str) -> dict:
    import httpx

    kwargs: dict = {"timeout": 15.0}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.get(echo_url)
    try:
        data = resp.json()
    except ValueError:
        data = {"ip": resp.text.strip()}
    return data if isinstance(data, dict) else {}


async def exit_geo(proxy_url: str | None) -> ExitGeo | None:
    """Exit IP and its place for this proxy session (cached); None when unknown."""
    from app.settings import get_settings

    key = proxy_url or "host"
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        settings = get_settings()
        result: ExitGeo | None = None
        from app.core.netguard import url_is_private

        if settings.ip_echo_url and not await url_is_private(settings.ip_echo_url):
            try:
                data = await _echo(proxy_url, settings.ip_echo_url)
                ip = data.get("ip") or data.get("query") or data.get("origin")
                ip = str(ip).split(",")[0].strip() if ip else None
                geo = {
                    "timezone": data.get("timezone"),
                    "country": data.get("countryCode") or data.get("country_code") or (data.get("country") if len(str(data.get("country") or "")) == 2 else None),
                    "latitude": data.get("lat") if data.get("lat") is not None else data.get("latitude"),
                    "longitude": data.get("lon") if data.get("lon") is not None else data.get("longitude"),
                }
                source = "echo"
                if ip and not (geo["timezone"] and geo["latitude"] is not None):
                    local = await asyncio.get_running_loop().run_in_executor(None, _geoip_lookup, ip)
                    for k, v in local.items():
                        if geo.get(k) is None:
                            geo[k] = v
                    if local:
                        source = "echo+geoip"
                result = ExitGeo(
                    ip=ip, timezone=geo.get("timezone"), locale=geo.get("locale") or _locale_for(geo.get("country")),
                    latitude=float(geo["latitude"]) if geo.get("latitude") is not None else None,
                    longitude=float(geo["longitude"]) if geo.get("longitude") is not None else None,
                    country=(geo.get("country") or None), source=source,
                )
            except Exception as exc:
                log.info("exit IP lookup failed (%s); the browser keeps its defaults", type(exc).__name__)
                result = None
        _cache[key] = (time.monotonic() + (_TTL if result else 300.0), result)
        return result


def cached(proxy_url: str | None) -> ExitGeo | None:
    hit = _cache.get(proxy_url or "host")
    return hit[1] if hit else None
