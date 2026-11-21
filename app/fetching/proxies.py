"""Proxy tiers: provider presets, sticky session ids and our own escalation.

Escalation is ours, not Crawlee's ``tiered_proxy_urls``: a host moves to the next
selected tier only on a block (never on an ordinary fetch error), and the move is kept
in the job record for the rest of the job (README 3.2).

Username formats of the presets (see README "Proxy presets" for verification status):

* oxylabs:   ``customer-<user>-cc-<CC>-city-<city>-sessid-<id>-sesstime-<minutes>``
* bright_data: ``<brd-customer-…-zone-…>-country-<cc>-city-<city>-session-<id>``
* decodo:    ``user-<user>-country-<cc>-city-<city>-session-<id>-sessionduration-<minutes>``
* generic:   ``scheme://user:password@host:port``, geo fields ignored
* custom_list: one of ``proxy_urls`` per session (stable for the session)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from urllib.parse import quote, urlsplit

PRESET_ENDPOINTS = {
    "oxylabs": ("http", "pr.oxylabs.io", 7777),
    "bright_data": ("http", "brd.superproxy.io", 33335),
    "decodo": ("http", "gate.decodo.com", 7000),
}
VERIFIED_PRESETS: set[str] = {"generic", "custom_list"}


def _session_token(session_id: str | None, tier: int) -> str:
    raw = f"{session_id or 'nosession'}:{tier}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _city(city: str | None) -> str | None:
    if not city:
        return None
    return re.sub(r"[^a-z0-9]+", "_", city.strip().lower()).strip("_") or None


def build_username(tier: dict, session_id: str | None) -> str | None:
    user = tier.get("username")
    if tier.get("auth_mode") != "user_password" or not user:
        return None
    provider = tier.get("provider")
    sticky = tier.get("session_mode") == "sticky"
    sid = _session_token(session_id, tier["tier"]) if sticky else None
    minutes = tier.get("sticky_minutes") or 10
    country = tier.get("country")
    city = _city(tier.get("city"))
    if provider == "oxylabs":
        base = user if user.startswith("customer-") else f"customer-{user}"
        parts = [base]
        if country:
            parts.append(f"cc-{country.upper()}")
        if city:
            parts.append(f"city-{city}")
        if sid:
            parts += [f"sessid-{sid}", f"sesstime-{min(int(minutes), 1440)}"]
        return "-".join(parts)
    if provider == "bright_data":
        parts = [user]
        if country:
            parts.append(f"country-{country.lower()}")
        if city:
            parts.append(f"city-{city.replace('_', '')}")
        if sid:
            parts.append(f"session-{sid}")
        return "-".join(parts)
    if provider == "decodo":
        base = user if user.startswith("user-") else f"user-{user}"
        parts = [base]
        if country:
            parts.append(f"country-{country.lower()}")
        if city:
            parts.append(f"city-{city}")
        if sid:
            parts += [f"session-{sid}", f"sessionduration-{min(int(minutes), 1440)}"]
        return "-".join(parts)
    return user  # generic


_SCOPES: "OrderedDict[str, str]" = OrderedDict()  # proxy URL -> tier scope (identity.py)
_SCOPES_MAX = 20000


def identity_metadata(proxy_url: str) -> dict:
    """Public proxy routing identity, excluding credential and token fields."""
    parsed = urlsplit(proxy_url)
    return {"endpoint": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
            "username": parsed.username or ""}


def tier_scope(tier: dict) -> str:
    """The proxy account and tier a URL belongs to, as a short hash of the tier's definition
    (never the password): one part of the browser identity key (identity.py)."""
    public = {k: tier.get(k) for k in ("tier", "provider", "protocol", "host", "port", "username", "country", "city")}
    urls = tier.get("proxy_urls")
    public["proxy_urls"] = [identity_metadata(url) for url in urls] if urls is not None else None
    material = json.dumps(public, sort_keys=True, default=str)
    return f"t{tier.get('tier')}-" + hashlib.sha256(material.encode()).hexdigest()[:12]


def scope_of(url: str | None) -> str | None:
    return _SCOPES.get(url) if url else None


def _remember(url: str, tier: dict) -> str:
    _SCOPES[url] = tier_scope(tier)
    _SCOPES.move_to_end(url)
    while len(_SCOPES) > _SCOPES_MAX:
        _SCOPES.popitem(last=False)
    return url


def tier_url(tier: dict, session_id: str | None) -> str:
    provider = tier.get("provider")
    if provider == "custom_list":
        urls = tier.get("proxy_urls") or []
        if not urls:
            raise ValueError("custom_list without proxy_urls")
        idx = int(hashlib.sha256((session_id or "").encode()).hexdigest(), 16) % len(urls)
        return _remember(urls[idx], tier)
    scheme, host, port = tier.get("protocol"), tier.get("host"), tier.get("port")
    if provider in PRESET_ENDPOINTS:
        d_scheme, d_host, d_port = PRESET_ENDPOINTS[provider]
        scheme, host, port = scheme or d_scheme, host or d_host, port or d_port
    if not (scheme and host and port):
        raise ValueError("proxy tier without protocol/host/port")
    username = build_username(tier, session_id)
    if username is not None:
        auth = f"{quote(username, safe='')}:{quote(tier.get('password') or '', safe='')}@"
    else:
        auth = ""
    return _remember(f"{scheme}://{auth}{host}:{port}", tier)


def redact(url: str | None) -> str:
    if not url:
        return ""
    return re.sub(r"//[^@/]*@", "//***@", url)


class TierPlan:
    """Selected tiers in ascending order and the escalation state per host."""

    def __init__(self, tiers: list[dict], host_level: dict[str, int] | None = None) -> None:
        self.tiers = sorted(tiers, key=lambda t: t["tier"])
        self.host_level: dict[str, int] = dict(host_level or {})

    @property
    def enabled(self) -> bool:
        return bool(self.tiers)

    def level(self, host: str) -> int:
        return min(self.host_level.get(host, 0), max(len(self.tiers) - 1, 0))

    def tier_for(self, host: str) -> dict | None:
        if not self.tiers:
            return None
        return self.tiers[self.level(host)]

    def can_escalate(self, host: str) -> bool:
        return bool(self.tiers) and self.level(host) < len(self.tiers) - 1

    def escalate(self, host: str) -> bool:
        if not self.can_escalate(host):
            return False
        self.host_level[host] = self.level(host) + 1
        return True

    def at_highest(self, host: str | None = None) -> bool:
        if not self.tiers:
            return True
        if host is None:
            return all(self.level(h) >= len(self.tiers) - 1 for h in self.host_level) and bool(self.host_level) or len(self.tiers) == 1
        return self.level(host) >= len(self.tiers) - 1

    def url_for(self, host: str, session_id: str | None) -> str | None:
        tier = self.tier_for(host)
        return tier_url(tier, session_id) if tier else None
