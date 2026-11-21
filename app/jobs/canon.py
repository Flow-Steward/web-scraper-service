"""Duplicates by the page's canonical address (contract README 4.12).

After a page loads, its ``<link rel="canonical">`` (or the HTTP ``Link: <…>; rel="canonical"``
header) names the page's real address. Only a canonical on the same site counts. The first
page of a canonical address is collected and its records are keyed by that address; a later
page with the same canonical is a duplicate: not extracted again, its links not followed.
A canonical cannot erase or change the loaded URL's non-pagination query identity:
those queries may select a resource, edition or fit, so the loaded address remains
its identity. Tracking/session parameters were already removed during URL normalization.
Query-free path aliases still adopt their declared canonical.

A target page is compared with the first one before it is dropped: when the two products
differ (SKU, else title), the canonical is wrong for that page, which is then collected under
its own address. The run stops trusting canonicals (``rt["canon"]["distrusted"]``, a warning)
when different products share one canonical three times, when more than ``MAX_ALIASES``
addresses name one canonical, or when target pages name the home page as their canonical;
records keyed by a canonical so far are then keyed by their own address again.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from app.core.urls import absolute, canonical_url, record_id, site_key

IDENTITY_FIELDS = ("sku", "mpn", "gtin", "gtin13", "gtin12", "gtin8", "ean", "upc", "part_number", "article_number", "article",
                   "product_code", "code", "model", "title", "name")
MAX_MISMATCHES = 3
MAX_ALIASES = 20
MAX_HOME = 3
PROBATION_CAP = 1000
CONFIRM_AFTER = 200
PAGINATION = {"page", "p", "pg", "paged", "start", "offset"}
PAGE_PATH = re.compile(r"/page/\d+/?$")


def identity(data: dict | None) -> str | None:
    for name in IDENTITY_FIELDS:
        value = (data or {}).get(name)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return f"{name}:{str(value).strip().lower()}"
    return None


def link_header_canonical(value: str | None, base: str) -> str | None:
    """``Link: <https://x/p>; rel="canonical", <…>; rel="alternate"`` -> the canonical URL."""
    for part in (value or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;(.*)$', part)
        if m and re.search(r'rel\s*=\s*"?([^";]*\s)?canonical(\s[^";]*)?"?', m.group(2), re.I):
            return absolute(base, m.group(1))
    return None


def only_pagination_differs(url: str, canonical: str) -> bool:
    """A listing's page N naming page 1 as its canonical is not a duplicate."""
    a, b = urlsplit(url), urlsplit(canonical)
    pa = [(n, v) for n, v in parse_qsl(a.query, keep_blank_values=True) if n.lower() not in PAGINATION]
    pb = [(n, v) for n, v in parse_qsl(b.query, keep_blank_values=True) if n.lower() not in PAGINATION]
    path_a = PAGE_PATH.sub("/", a.path)
    path_b = PAGE_PATH.sub("/", b.path)
    return (a.netloc, path_a.rstrip("/"), sorted(pa)) == (b.netloc, path_b.rstrip("/"), sorted(pb))


class CanonRegistry:
    def __init__(self, rt: dict) -> None:
        self.state = rt.setdefault("canon", {"distrusted": None, "mismatches": 0, "home": 0, "keyed": 0, "probation": [], "confirmed": False})
        self.seen: dict[str, str | None] = {}
        self.aliases: dict[str, int] = {}
        self.loaded = False
        self.rewrite_pending = False

    @property
    def distrusted(self) -> str | None:
        return self.state.get("distrusted")

    def load(self, db, run_id: str) -> None:
        """After a restart: the canonicals of the records already collected."""
        if self.loaded:
            return
        import json

        for row in db.all("SELECT line FROM records WHERE run_id=?", (run_id,)):
            rec = json.loads(row["line"])
            self.seen.setdefault(rec["source_url"], identity(rec.get("data")))
        self.loaded = True

    def declared(self, page, link_header: str | None, site: str) -> str | None:
        """Use a same-site canonical unless it would discard the loaded query identity."""
        for raw in (page.canonical_link(), link_header_canonical(link_header, page.base_url)):
            if not raw:
                continue
            try:
                if site_key(raw) != site:
                    continue
                declared = canonical_url(raw)
                own = canonical_url(page.url)
                own_query = [
                    (n, v)
                    for n, v in parse_qsl(urlsplit(own).query, keep_blank_values=True)
                    if n.lower() not in PAGINATION
                ]
                declared_query = [
                    (n, v)
                    for n, v in parse_qsl(
                        urlsplit(declared).query, keep_blank_values=True
                    )
                    if n.lower() not in PAGINATION
                ]
                # An SEO canonical may name the parent product while a query selects
                # a distinct edition/fit/option. Tracking was already removed above.
                if own_query and sorted(own_query) != sorted(declared_query):
                    return own
                return declared
            except ValueError:
                continue
        return None

    def distrust(self, why: str) -> bool:
        if self.distrusted:
            return False
        self.state["distrusted"] = why[:300]
        self.rewrite_pending = True
        return True

    def note_alias(self, canonical: str, target: bool) -> str | None:
        """Counts addresses per canonical; answers a reason to stop trusting canonicals."""
        n = self.aliases.get(canonical, 0) + 1
        self.aliases[canonical] = n
        if n > MAX_ALIASES:
            return f"more than {MAX_ALIASES} different addresses name {canonical} as their canonical"
        if target and urlsplit(canonical).path in ("", "/"):
            self.state["home"] = int(self.state.get("home", 0)) + 1
            if self.state["home"] >= MAX_HOME:
                return f"{self.state['home']} product pages name the home page {canonical} as their canonical"
        return None

    def note_mismatch(self, canonical: str, mine: str | None, theirs: str | None) -> str | None:
        self.state["mismatches"] = int(self.state.get("mismatches", 0)) + 1
        if self.state["mismatches"] >= MAX_MISMATCHES:
            return f"different products share one canonical ({canonical}: {theirs} and {mine})"
        return None

    def keyed(self, records: list[dict], own: str) -> None:
        """Records keyed by a declared canonical, remembered until the canonicals are confirmed."""
        self.state["keyed"] = int(self.state.get("keyed", 0)) + 1
        if self.state.get("confirmed"):
            return
        if self.state["keyed"] >= CONFIRM_AFTER and not self.distrusted:
            self.state["confirmed"] = True
            self.state["probation"] = []
            return
        prob = self.state.setdefault("probation", [])
        if len(prob) < PROBATION_CAP:
            prob.extend([r["record_id"], own] for r in records)


def rekey(rec: dict, url: str, requested: str | None) -> dict:
    """The record keyed by ``url`` (source_url and record_id), ``requested_url`` when it differs."""
    out = dict(rec)
    out["source_url"] = url
    out["record_id"] = record_id(url, rec["template_id"], rec.get("item_index", 0), rec.get("source_key"))
    if requested and requested != url:
        out["requested_url"] = requested
    else:
        out.pop("requested_url", None)
    return out
