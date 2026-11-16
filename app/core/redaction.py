"""Secret redaction for everything the service stores, returns or logs (README 10).

Two layers:

* patterns: credentials inside any ``scheme://user[:password]@host`` URL (``http``, ``https``,
  ``socks4``, ``socks5``, ``socks5h`` and every other scheme, username-only included),
  ``Bearer``/``Basic`` authorisation values, and secret-looking keys in JSON, Python-repr
  dicts and ``key=value`` text;
* exact values: a job's own secrets (proxy usernames, passwords, full proxy URLs, the
  solver key, the signing secret) are masked wherever they appear, whatever surrounds them.
"""

from __future__ import annotations

import re
from typing import Iterable

MASK = "***"
_SECRET_KEYS = (
    "password|passwd|pwd|pass|api_key|apikey|api-key|clientkey|client_key|signing_secret|secret|token|access_token|"
    "username|user|proxy_urls|proxy_url|proxy|authorization|proxy-authorization|x-api-key"
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # credentials in any URL: scheme://user:pass@ and scheme://user@
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)[^/\s@'\"<>]+@"), r"\1***@"),
    # authorisation header values
    (re.compile(r"(?i)\b(bearer|basic|digest)\s+[A-Za-z0-9._~+/=-]{4,}"), r"\1 ***"),
    # "key": "value" / 'key': 'value' (JSON and Python repr)
    (re.compile(r"(?i)([\"']?(?:" + _SECRET_KEYS + r")[\"']?\s*:\s*)([\"'])(?:\\.|(?!\2).)*\2"), r"\1\2***\2"),
    # "key": [ ... ] (lists of proxy URLs)
    (re.compile(r"(?i)([\"']?proxy_urls[\"']?\s*:\s*)\[[^\]]*\]"), r"\1[***]"),
    # key=value in query strings, headers and log text
    (re.compile(r"(?i)\b((?:" + _SECRET_KEYS + r")\s*=\s*)[^\s&,;'\"]+"), r"\1***"),
]


def redact(text: str | None, extra_values: Iterable[str] = ()) -> str:
    if not text:
        return text or ""
    out = text
    for value in sorted({v for v in extra_values if v and len(v) >= 4}, key=len, reverse=True):
        out = out.replace(value, MASK)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def secret_values(secret: dict | None) -> list[str]:
    """Every secret string of a job's stored secret part (proxy tiers, solver) or webhook."""
    values: list[str] = []
    if not isinstance(secret, dict):
        return values
    for tier in secret.get("proxy_tiers") or []:
        for key in ("username", "password"):
            if tier.get(key):
                values.append(str(tier[key]))
        for url in tier.get("proxy_urls") or []:
            values.append(str(url))
            m = re.match(r"^[a-z0-9+.-]+://([^@/]*)@", str(url), re.I)
            if m:
                values += [part for part in m.group(1).split(":") if part]
    solver = secret.get("solver") or {}
    if solver.get("api_key"):
        values.append(str(solver["api_key"]))
    if secret.get("signing_secret"):
        values.append(str(secret["signing_secret"]))
    return values
