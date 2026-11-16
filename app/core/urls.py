"""Canonical URL (README 7.3), site key (README 1) and ``record_id`` (README 7.4)."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

import idna

UNRESERVED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
SUB_DELIMS = set("!$&'()*+,;=")
PATH_CHARS = UNRESERVED | SUB_DELIMS | set(":@/")
QUERY_CHARS = PATH_CHARS | set("?")
HEX = set("0123456789abcdefABCDEF")
TRACKING_EXACT = {
    "gclid", "dclid", "fbclid", "msclkid", "yclid", "gbraid", "wbraid", "_ga", "_gl",
    "mc_cid", "mc_eid", "srsltid",
}
# session and one-time parameters: a new value on every visit, never part of the page's address
SESSION_PARAMS = {
    "sid", "phpsessid", "jsessionid", "sessionid", "session_id", "oscsid", "zenid", "cfid", "cftoken", "form_key", "uenc",
    "___store", "___from_store", "___sid",
}
PATH_SESSION = re.compile(r";(jsessionid|phpsessid|sid|sessionid)=[^/?#;]*", re.I)
DEFAULT_PORTS = {"http": 80, "https": 443}


class InvalidUrl(ValueError):
    pass


ASCII_HOST = re.compile(r"^[a-z0-9.-]+$")


@lru_cache(maxsize=4096)
def _host_ascii(host: str) -> str:
    """IDNA to ASCII, cached: hosts repeat on every link of a crawl (IDNA checks were
    the largest cost of a page). A plain lower-case ASCII host without ``xn--`` labels is its
    own ASCII form."""
    lowered = host.lower()
    if ASCII_HOST.match(lowered) and "xn--" not in lowered and ".." not in lowered and not lowered.startswith("."):
        return lowered.rstrip(".")
    return _host_ascii_slow(host)


def _host_ascii_slow(host: str) -> str:
    host = host.rstrip(".")
    if not host:
        raise InvalidUrl("empty host")
    if host.startswith("["):
        return host.lower()
    try:
        ascii_host = idna.encode(host, uts46=True, transitional=False).decode("ascii")
    except idna.IDNAError:
        # Hosts with underscores or other non-IDNA labels: lower-case as-is.
        ascii_host = host.encode("ascii", "strict").decode() if host.isascii() else host
    return ascii_host.lower().rstrip(".")


def normalise_percent(text: str, allowed: set[str]) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "%" and i + 2 < n and text[i + 1] in HEX and text[i + 2] in HEX:
            byte = int(text[i + 1 : i + 3], 16)
            char = chr(byte)
            if byte < 128 and char in UNRESERVED:
                out.append(char)
            else:
                out.append("%" + text[i + 1 : i + 3].upper())
            i += 3
            continue
        if ch in allowed:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
        i += 1
    return "".join(out)


def remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4."""
    inp = path
    out: list[str] = []
    while inp:
        if inp.startswith("../"):
            inp = inp[3:]
        elif inp.startswith("./"):
            inp = inp[2:]
        elif inp.startswith("/./"):
            inp = "/" + inp[3:]
        elif inp == "/.":
            inp = "/"
        elif inp.startswith("/../"):
            inp = "/" + inp[4:]
            if out:
                out.pop()
        elif inp == "/..":
            inp = "/"
            if out:
                out.pop()
        elif inp in (".", ".."):
            inp = ""
        else:
            start = 1 if inp.startswith("/") else 0
            idx = inp.find("/", start)
            if idx == -1:
                out.append(inp)
                inp = ""
            else:
                out.append(inp[:idx])
                inp = inp[idx:]
    return "".join(out)


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("utm_") or lowered in TRACKING_EXACT or lowered in SESSION_PARAMS


def _split_netloc(url: str):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise InvalidUrl("scheme must be http or https")
    host = parts.hostname
    if host is None:
        raise InvalidUrl("missing host")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidUrl("invalid port") from exc
    # urlsplit lower-cases hostname already; IDNA-encode the original-case one
    raw_netloc = parts.netloc.rsplit("@", 1)[-1]
    if raw_netloc.startswith("["):
        raw_host = raw_netloc[: raw_netloc.index("]") + 1]
    else:
        raw_host = raw_netloc.rsplit(":", 1)[0] if ":" in raw_netloc else raw_netloc
    return scheme, _host_ascii(raw_host), port, parts


@lru_cache(maxsize=65536)
def canonical_url(url: str) -> str:
    """Cached: a crawl meets the same link on many pages (pure function of the string)."""
    return _canonical_url(url)


def _canonical_url(url: str) -> str:
    url = url.strip()
    scheme, host, port, parts = _split_netloc(url)
    netloc = host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{port}"
    path = PATH_SESSION.sub("", parts.path or "/") or "/"
    path = remove_dot_segments(normalise_percent(path, PATH_CHARS))
    if not path:
        path = "/"
    pairs: list[tuple[str, str]] = []
    for piece in parts.query.split("&"):
        if not piece:
            continue
        name, sep, value = piece.partition("=")
        name = normalise_percent(name, QUERY_CHARS)
        value = normalise_percent(value, QUERY_CHARS)
        if _is_tracking(name):
            continue
        pairs.append((name, value))
    pairs.sort()
    query = "&".join(f"{n}={v}" for n, v in pairs)
    return f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")


@lru_cache(maxsize=65536)
def site_key(url: str) -> str:
    scheme, host, port, _ = _split_netloc(url.strip())
    if host.startswith("www."):
        host = host[4:]
    if port is not None and port != DEFAULT_PORTS[scheme]:
        return f"{host}:{port}"
    return host


def host_of(url: str) -> str:
    return _split_netloc(url)[1]


def record_id(canonical: str, template_id: str, item_index: int = 0, source_key: str | None = None) -> str:
    material = "\x1f".join([canonical, template_id, str(item_index), source_key or ""])
    return "rec_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def is_http_url(url: str) -> bool:
    try:
        _split_netloc(url)
        return True
    except (InvalidUrl, ValueError, UnicodeError):
        return False


def absolute(base: str, href: str) -> str | None:
    href = (href or "").strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    try:
        joined = urljoin(base, href)
    except ValueError:
        return None
    if not re.match(r"^https?://", joined, re.I):
        return None
    return joined


def result_file_name(site: str, run_id: str) -> str:
    # The contract pattern ^[a-z0-9.-]+_run_… has no ':'; a site key with a port uses '-'.
    return f"{site.replace(':', '-')}_{run_id}.jsonl"


def run_file_names(site: str, run_id: str) -> dict[str, str]:
    """Every file a run may keep in ``results/``: the result as JSON Lines and as CSV (README 7.1, 7.5)."""
    base = f"{site.replace(':', '-')}_{run_id}"
    return {"result": f"{base}.jsonl", "result_csv": f"{base}.csv"}
