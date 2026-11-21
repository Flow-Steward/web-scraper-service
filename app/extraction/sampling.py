"""Choosing sample candidates and explaining what a samples call did.

Two live calls on a Cloudflare-protected shop ran for about four minutes and
answered only "no target page": a site-wide sitemap of 100 000 URLs whose first entries are
not products, and a category page whose first 30 links are the header menu. So:

* links of a page are ranked like a visitor scans a listing: links in product cards / with
  an image first, links in the header, navigation, footer, breadcrumbs last, and URL variants
  (pagination, sort and filters) after everything else;
* sitemap URLs are spread over their URL shapes (round robin, largest shape first) instead
  of taking the first N;
* every candidate tried is recorded (how it was loaded, what came of it, the product signals
  on it) and the answer gets ``explanation``: counts, URL-shape groups, pages, seconds, a
  plain summary and the next hint to give the operator.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any

from app.core.url_patterns import UrlPattern, matches_any, url_batch, url_pattern_budget

VARIANT_RE = re.compile(
    r"[?&](page|p|pg|paged|sort|sorting|order|orderby|dir|filter|filters|limit|per_page|perpage|view|mode|price|color|colour|size|brand)"
    r"(\[[^\]]*\])?=|/page/\d+/?$|/(filter|filters|sort)/", re.I)
CHROME_XPATH = ("ancestor::*[self::nav or self::header or self::footer or self::aside"
                " or contains(translate(@class,'NAVMEUFOTHDBRC','navmeufothdbrc'),'nav')"
                " or contains(translate(@class,'NAVMEUFOTHDBRC','navmeufothdbrc'),'menu')"
                " or contains(translate(@class,'NAVMEUFOTHDBRC','navmeufothdbrc'),'footer')"
                " or contains(translate(@class,'NAVMEUFOTHDBRC','navmeufothdbrc'),'header')"
                " or contains(translate(@class,'NAVMEUFOTHDBRC','navmeufothdbrc'),'breadcrumb')"
                " or contains(@id,'nav') or contains(@id,'menu') or contains(@id,'footer') or contains(@id,'header')]")
CARD_XPATH = ("ancestor::*[contains(translate(@class,'PRODUCTIEMCAR','productiemcar'),'product')"
              " or contains(translate(@class,'PRODUCTIEMCAR','productiemcar'),'item')"
              " or contains(translate(@class,'PRODUCTIEMCAR','productiemcar'),'card')][position() <= 4]")


def is_variant(url: str) -> bool:
    """Known listing controls; arbitrary query keys can identify real products."""
    return bool(VARIANT_RE.search(url))


MARKER_RE = re.compile(r"^.*?-([a-z]{0,3})(\d+)(\.[a-z0-9]{1,5})?$", re.I)


def url_shape(url: str) -> str:
    """The shape of a URL for grouping links: short plain words kept
    (``lt``, ``ru``, ``produkt``), slugs as ``*`` with a run of slug segments collapsed into one,
    numbers as ``<n>``, and the last segment's id marker kept: a category tree
    ``/a-c1/b-c2/leaf-c3`` and ``/lt/leaf-c3`` give ``/*-c<n>`` and ``/lt/*-c<n>``, a product
    ``/front-lamp-dep551-p1023042`` gives ``/*-p<n>``."""
    from urllib.parse import urlsplit

    segs = [s for s in urlsplit(url).path.split("/") if s]
    if not segs:
        return "/"
    out: list[str] = []
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        m = MARKER_RE.match(seg) if last else None
        if seg.isdigit():
            tok = "<n>"
        elif m:
            tok = f"*-{m.group(1).lower()}<n>{(m.group(3) or '').lower()}"
        elif "-" not in seg and "_" not in seg and not any(ch.isdigit() for ch in seg) and len(seg) <= 20:
            tok = seg.lower()
        else:
            tok = "*"
        if tok.startswith("*") and out and out[-1] == "*":
            out.pop()  # a run of slug segments (a category tree) is one wildcard
        out.append(tok)
    return "/" + "/".join(out)


def shape_of(url: str) -> str:
    return url_shape(url)


CHROME_TAGS = {"nav", "header", "footer", "aside"}
CHROME_CLASS = ("nav", "menu", "footer", "header", "breadcrumb")
CHROME_ID = ("nav", "menu", "footer", "header")
CARD_CLASS = ("product", "item", "card")


def anchor_flags(page) -> list[tuple[str, bool, bool]]:
    """Every ``<a href>`` in document order with (inside header/nav/footer/menu chrome, inside a
    product card or holding an image). One walk of the tree with the flags inherited from the
    ancestors: the same answer as ``CHROME_XPATH``/``CARD_XPATH`` per link, without two XPath
    queries per link (they were a third of a page's processing)."""
    root = getattr(page.selector, "root", None)
    if root is None or not hasattr(root, "iterchildren"):
        out = []
        for a in page.selector.css("a[href]"):
            try:
                out.append((a.attrib.get("href", ""), bool(a.xpath(CHROME_XPATH)), bool(a.css("img") or a.xpath(CARD_XPATH))))
            except Exception:
                out.append((a.attrib.get("href", ""), False, False))
        return out
    out: list[tuple[str, bool, bool]] = []
    stack = [(root, False, False)]
    while stack:
        el, chrome, card = stack.pop()
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "a" and el.get("href") is not None:
            has_img = any(True for _ in el.iter("img"))
            out.append((el.get("href"), chrome, card or has_img))
        cls = (el.get("class") or "").lower() if tag else ""
        ident = el.get("id") or "" if tag else ""
        child_chrome = chrome or tag in CHROME_TAGS or any(w in cls for w in CHROME_CLASS) or any(w in ident for w in CHROME_ID)
        child_card = card or any(w in cls for w in CARD_CLASS)
        children = [c for c in el.iterchildren() if isinstance(c.tag, str)]
        for child in reversed(children):
            stack.append((child, child_chrome, child_card))
    return out


@url_batch
def link_groups(page, site: str, exclude: list | None = None, include: list | None = None) -> tuple[list[dict], dict]:
    """The page's same-site links grouped by URL shape, for probing one link per shape first.
    Each group: ``shape``, ``urls`` (links inside product cards / around an image first, menu
    links last), ``links``, ``in_menus`` (share of its links inside header/nav/footer/menu
    containers). Groups are ordered content first, then smallest first (leaf pages such as
    products are fewer than a site-wide menu's categories). Query/pagination/sort/filter
    variants are left out. Stats: ``hrefs``, ``in_site``, ``other_hosts``, ``variants_skipped``."""
    from app.core.urls import absolute, canonical_url, site_key

    try:
        start = canonical_url(page.url)
    except ValueError:
        start = page.url
    info: "OrderedDict[str, dict]" = OrderedDict()
    stats = {"hrefs": 0, "in_site": 0, "other_hosts": 0, "variants_skipped": 0}
    flags = anchor_flags(page)

    def observed_key(href: str) -> str | None:
        resolved = absolute(page.base_url, href)
        if not resolved:
            return None
        try:
            return canonical_url(resolved.split("#", 1)[0])
        except ValueError:
            return resolved.split("#", 1)[0]

    # The browser collector also observes the initial DOM. Supplement it with
    # new URLs only: re-adding a menu link as unmarked content loses its flags.
    observed = {observed_key(href) for href, _, _ in flags}
    for href in getattr(page, "extra_links", []):
        key = observed_key(href)
        if key is not None and key not in observed:
            flags.append((href, False, False))
            observed.add(key)
    deferred = {
        observed_key(href) for href in getattr(page, "deferred_links", [])
    }
    for href, chrome, card in flags:
        stats["hrefs"] += 1
        resolved = absolute(page.base_url, href)
        if not resolved:
            continue
        try:
            if site_key(resolved) != site:
                stats["other_hosts"] += 1
                continue
            c = canonical_url(resolved.split("#", 1)[0])
        except ValueError:
            continue
        with url_pattern_budget():
            if c == start or matches_any(exclude or [], c):
                continue
            if is_variant(c) and c not in deferred and not matches_any(include or [], c):
                stats["variants_skipped"] += 1
                continue
        cur = info.get(c)
        if cur is None:
            info[c] = {"chrome": chrome, "card": card}
        else:  # a link that also appears in the content counts as content
            cur["chrome"] = cur["chrome"] and chrome
            cur["card"] = cur["card"] or card
    stats["in_site"] = len(info)
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for url, meta in info.items():
        shape = url_shape(url)
        g = groups.setdefault(shape, {"shape": shape, "urls": [], "chrome": 0})
        g["urls"].append((url, meta))
        g["chrome"] += meta["chrome"]
    out = []
    for g in groups.values():
        order = sorted(range(len(g["urls"])), key=lambda i: (g["urls"][i][1]["chrome"], not g["urls"][i][1]["card"], i))
        n = len(g["urls"])
        out.append({"shape": g["shape"], "urls": [g["urls"][i][0] for i in order], "links": n, "in_menus": round(g["chrome"] / n, 2)})
    out.sort(key=lambda g: (g["in_menus"] >= 0.5, g["links"]))
    return out, stats


@url_batch
def url_groups(urls: list[str], include: list | None = None) -> list[dict]:
    """Sitemap URLs grouped by URL shape, largest group first (no page context to judge
    menus by); query variants left out."""
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    for u in urls:
        if not is_variant(u) or matches_any(include or [], u):
            groups.setdefault(url_shape(u), []).append(u)
    return sorted(({"shape": k, "urls": v, "links": len(v), "in_menus": 0.0} for k, v in groups.items()), key=lambda g: -g["links"])


def ranked_links(page, site: str, exclude: list | None = None) -> list[str]:
    """The links of ``link_groups`` in probing order: one per shape first, then round robin."""
    groups, _ = link_groups(page, site, exclude)
    return ProbePlan(groups, limit=10 ** 6, misses=10 ** 6).drain()


def spread_by_shape(urls: list[str], limit: int) -> list[str]:
    """Up to ``limit`` URLs taken round robin over their URL shapes (largest shape first),
    query variants left out — a site-wide sitemap does not hand over its first N entries only."""
    return ProbePlan(url_groups(urls), limit=limit, misses=10 ** 6).drain()


class ProbePlan:
    """Which candidate to try next: one link from each URL-shape group first (shape
    diversity), a group dropped after ``misses`` tried pages without a sample, and once a
    group yields a sample, the rest of that group before anything else; ``tail`` after all."""

    def __init__(self, groups: list[dict], *, limit: int, misses: int = 2, tail: list[str] | None = None) -> None:
        self.groups = [
            {
                "shape": g["shape"],
                "queue": list(g["urls"]),
                "tried": 0,
                "hits": 0,
                "misses": 0,
            }
            for g in groups
        ]
        self.limit, self.max_misses = limit, misses
        self.tail = list(tail or [])
        self.tried = 0
        self._of: dict[str, dict] = {}
        self._known = {u for g in self.groups for u in g["queue"]} | set(self.tail)
        self.depths: dict[str, int] = {}

    def has_unseen(self, groups: list[dict]) -> bool:
        """Whether this observation contributes any new traversal candidate."""
        return any(u not in self._known for group in groups for u in group["urls"])

    @url_batch
    def extend(
        self,
        groups: list[dict],
        parent: str,
        *,
        max_depth: int = 4,
        preferred: list[UrlPattern] | None = None,
    ) -> None:
        """Add another level, placing known targets ahead of pending intermediates."""
        depth = self.depths.get(parent, 1) + 1
        if depth > max_depth:
            return
        by_shape = {g["shape"]: g for g in self.groups}
        for group in groups:
            fresh = [u for u in group["urls"] if u not in self._known]
            if not fresh:
                continue
            g = by_shape.get(group["shape"])
            if g is None:
                g = {
                    "shape": group["shape"],
                    "queue": [],
                    "tried": 0,
                    "hits": 0,
                    "misses": 0,
                }
                self.groups.append(g)
                by_shape[g["shape"]] = g
            g["misses"] = 0  # a new subtree may contain products of the same URL shape
            preferred_count = 0
            for url in fresh:
                if len(self._known) >= max(1000, self.limit * 100):
                    return
                self._known.add(url)
                self.depths[url] = depth
                if preferred and matches_any(preferred, url):
                    g["queue"].insert(preferred_count, url)
                    preferred_count += 1
                else:
                    g["queue"].append(url)

    def _take(self, g: dict) -> str | None:
        while g["queue"]:
            url = g["queue"].pop(0)
            if url not in self._of:
                self._of[url] = g
                g["tried"] += 1
                self.tried += 1
                return url
        return None

    def next(self) -> str | None:
        if self.tried >= self.limit:
            return None
        for g in self.groups:  # a group that yielded a sample comes first
            if g["hits"] and g["queue"]:
                url = self._take(g)
                if url:
                    return url
        while True:
            live = [g for g in self.groups if g["queue"] and g["misses"] < self.max_misses and not g["hits"]]
            if not live:
                break
            g = min(live, key=lambda g: (g["tried"], self.groups.index(g)))  # least tried first: one per shape
            url = self._take(g)
            if url:
                return url
        while self.tail:
            url = self.tail.pop(0)
            if url not in self._of:
                self._of[url] = {"shape": url_shape(url), "queue": [], "tried": 1, "hits": 0, "misses": 0}
                self.tried += 1
                return url
        return None

    def feedback(self, url: str, sample: bool) -> None:
        g = self._of.get(url)
        if g is not None:
            g["hits" if sample else "misses"] += 1

    def drain(self) -> list[str]:
        out = []
        while True:
            url = self.next()
            if url is None:
                return out
            out.append(url)


class Explain:
    """What a samples call tried, for the answer and one log line."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.discovery: dict[str, Any] = {"source": None}
        self.pages: list[dict] = []
        self.guess: dict | None = None
        self.variants: list[list[dict]] = []  # per sample: the variant sources found on it

    def page(self, url: str, how: str, outcome: str, reason: str | None = None, signals: list[str] | None = None, *,
             status: int | None = None, links: int | None = None, selector_found: bool | None = None) -> None:
        entry: dict[str, Any] = {"url": url[:2048], "loaded": how, "outcome": outcome, "shape": url_shape(url)}
        if status is not None:
            entry["status"] = status
        if selector_found is not None:
            entry["selector_found"] = selector_found
        if links is not None:
            entry["links"] = links
        if reason:
            entry["reason"] = reason[:300]
        if signals is not None:
            entry["product_signals"] = signals
        self.pages.append(entry)

    def mark_sample(self, url: str) -> None:
        for p in reversed(self.pages):
            if p["url"] == url[:2048]:
                p["outcome"] = "sample"
                return

    def _tried(self) -> list[dict]:
        return [p for p in self.pages if p["outcome"] != "start"]

    def counts(self) -> dict[str, int]:
        tried = self._tried()
        c = {"tried": len(tried), "http": 0, "browser": 0, "reused": 0, "not_loaded": 0, "samples": 0, "not_target": 0,
             "challenged": 0, "failed": 0, "skipped": 0, "probed": 0, "selector_found": 0}
        for p in tried:
            c[{"http": "http", "browser": "browser", "reused": "reused"}.get(p["loaded"], "not_loaded")] += 1
            key = "samples" if p["outcome"] == "sample" else p["outcome"]
            if key in c:
                c[key] += 1
            c["selector_found"] += bool(p.get("selector_found"))
        return c

    def groups(self) -> list[dict]:
        from app.extraction.target_guess import SIGNAL_NAMES

        out: "OrderedDict[str, dict]" = OrderedDict()
        for p in self._tried():
            shape = p.get("shape") or url_shape(p["url"])
            g = out.setdefault(shape, {"shape": shape, "tried": 0, "samples": 0, "selector_found": 0, "with_product_signals": 0, "signals": set()})
            g["tried"] += 1
            g["samples"] += p["outcome"] == "sample"
            g["selector_found"] += bool(p.get("selector_found"))
            sig = p.get("product_signals") or []
            g["with_product_signals"] += bool(sig)
            g["signals"].update(sig)
        res = [{**{k: v for k, v in g.items() if k != "signals"}, "signals": sorted(g["signals"], key=list(SIGNAL_NAMES).index)}
               for g in out.values()]
        return res[:30]

    def hints(self, config: dict, sample_count: int) -> list[dict]:
        c = self.counts()
        hints: list[dict] = []
        if c["samples"] >= sample_count:
            return hints
        selector = config.get("target_page_selector")
        loaded = sum(p["outcome"] in ("sample", "not_target", "probed") for p in self._tried())
        best = None
        for g in self.groups():
            if g["with_product_signals"] and (best is None or g["with_product_signals"] > best["with_product_signals"]):
                best = g
        if selector and loaded and not c["selector_found"]:
            hints.append({"kind": "target_page_selector", "value": selector,
                          "message": f"none of the {loaded} pages loaded had the product page selector {selector}; open a product page and check it"})
        if best is not None and not c["samples"]:
            pattern = shape_to_pattern(best["shape"])
            hints.append({"kind": "url_include", "value": pattern,
                          "message": f"pages shaped {best['shape']} showed product signals ({', '.join(best['signals'])}); "
                                     f"set Only these URLs to {pattern} or the product page selector"})
        if not selector and not best and loaded:
            hints.append({"kind": "target_page_selector", "message": "set the product page selector (a CSS selector only product pages have)"})
        if self.discovery.get("source") == "sitemap" and self.discovery.get("start_is_deep"):
            hints.append({"kind": "discovery_mode", "value": "crawl",
                          "message": "the start URL is a category and the sitemap covers the whole site: set Discovery = crawl to sample from the category"})
        sp = self.discovery.get("start_page") or {}
        if sp and not sp.get("in_site") and sp.get("status") and sp["status"] < 400:
            hints.append({"kind": "fetch_mode", "value": "browser",
                          "message": "the start page had no usable links (perhaps rendered by JavaScript): set Fetch mode = browser"})
        if c["challenged"]:
            hints.append({"kind": "protection", "message": f"{c['challenged']} pages stayed behind a challenge; allow the click or set Fetch mode = browser"})
        return hints

    def summary(self, config: dict, sample_count: int) -> str:
        d, c = self.discovery, self.counts()
        src = d.get("source") or "-"
        parts = []
        sp = d.get("start_page") or {}
        if sp.get("hrefs") is not None:
            shapes = sp.get("shapes") or []
            listed = ", ".join(f"{g['shape']} ×{g['links']}" for g in shapes[:6]) + (", …" if len(shapes) > 6 else "")
            parts.append(f"start page ({sp.get('loaded')}, HTTP {sp.get('status')}): {sp['hrefs']} links, {sp.get('in_site', 0)} on this site "
                         f"in {d.get('shapes', len(shapes))} URL shapes ({listed})"
                         + (f", {sp['variants_skipped']} pagination/filter variants skipped" if sp.get("variants_skipped") else ""))
        elif sp:
            parts.append(f"start page not usable (HTTP {sp.get('status')})")
        if d.get("sitemap_urls") is not None:
            parts.append(f"sitemap: {d['sitemap_urls']} URLs{' (capped)' if d.get('sitemap_capped') else ''}"
                         + (", used after the start page's links" if src == "start_page+sitemap" else "")
                         + (", not used (Discovery = crawl)" if src == "crawl" else ""))
        if src in ("urls", "url_list"):
            parts.append(f"{d.get('candidates', 0)} given URLs")
        groups = self.groups()
        if groups:
            parts.append("probed: " + "; ".join(
                f"{g['shape']} {g['tried']}" + (f" (selector on {g['selector_found']})" if config.get("target_page_selector") else "")
                + (f" (product signals on {g['with_product_signals']})" if g["with_product_signals"] else "")
                + (f" → {g['samples']} samples" if g["samples"] else "")
                for g in groups[:8]) + (" …" if len(groups) > 8 else ""))
        parts.append(f"{c['tried']} pages tried ({c['http']} over HTTP, {c['browser']} in the browser, {c['reused']} already loaded)")
        outcome = [f"{c['samples']} samples"]
        for key, label in (("not_target", "not target pages"), ("challenged", "challenged"), ("failed", "failed"), ("skipped", "skipped")):
            if c[key]:
                outcome.append(f"{c[key]} {label}")
        parts.append(", ".join(outcome))
        if self.guess:
            parts.append(f"URL-shape guess: {self.guess.get('shape') or 'no group qualified'}")
        if self.variants:
            vs = self.variant_summary()
            parts.append("variant sources: " + ("; ".join(
                f"{_describe_variant_source(v['source'])} on {v['samples']} of {len(self.variants)} samples "
                f"({v['variants_min']}" + (f"–{v['variants_max']}" if v['variants_max'] != v['variants_min'] else "") + " variants)"
                for v in vs) if vs else f"none found on the {len(self.variants)} samples (one record per page)"))
        parts.append(f"{round(time.monotonic() - self.started)} s")
        text = "; ".join(parts) + "."
        hints = self.hints(config, sample_count)
        if hints:
            text += " Next: " + hints[0]["message"] + "."
        return text[:2000]

    def variant_summary(self) -> list[dict]:
        """Variant sources over the samples: each source, on how many samples, variant counts."""
        acc: "OrderedDict[str, dict]" = OrderedDict()
        for per_sample in self.variants:
            for src in per_sample:
                spec = {k: v for k, v in src.items() if k != "count"}
                key = repr(sorted(spec.items()))
                a = acc.setdefault(key, {"source": spec, "samples": 0, "variants_min": src["count"], "variants_max": src["count"]})
                a["samples"] += 1
                a["variants_min"] = min(a["variants_min"], src["count"])
                a["variants_max"] = max(a["variants_max"], src["count"])
        return list(acc.values())

    def public(self, config: dict, sample_count: int) -> dict:
        return {"summary": self.summary(config, sample_count), "discovery": {k: v for k, v in self.discovery.items() if v is not None},
                **({"variant_sources": self.variant_summary(), "samples_with_variant_sources": sum(1 for v in self.variants if v)}
                   if self.variants else {}),
                "counts": self.counts(), "groups": self.groups(), "pages": self.pages[:60],
                "seconds": round(time.monotonic() - self.started, 1), "hints": self.hints(config, sample_count),
                **({"guess": self.guess} if self.guess else {})}

    def log_line(self, site: str) -> str:
        """One INFO line per samples call: the counts, then every page loaded (URL, how, status,
        selector found, in-site links on it, outcome)."""
        d, c = self.discovery, self.counts()
        sp = d.get("start_page") or {}
        head = (f"samples site={site} source={d.get('source')} start_links={sp.get('hrefs', '-')}/{sp.get('in_site', '-')} "
                f"shapes={d.get('shapes', '-')} sitemap={d.get('sitemap_urls', '-')}{'(capped)' if d.get('sitemap_capped') else ''} "
                f"tried={c['tried']} http={c['http']} browser={c['browser']} reused={c['reused']} samples={c['samples']} "
                f"not_target={c['not_target']} challenged={c['challenged']} failed={c['failed']} skipped={c['skipped']} "
                f"selector_found={c['selector_found']} seconds={round(time.monotonic() - self.started)}")
        pages = " | ".join(
            f"{p['url']} {p['loaded']} {p.get('status', '-')} sel={'-' if p.get('selector_found') is None else ('yes' if p['selector_found'] else 'no')} "
            f"links={p.get('links', '-')} {p['outcome']}" for p in self.pages[:60])
        return f"{head} pages: {pages}" if pages else head


def _describe_variant_source(src: dict) -> str:
    kind = src.get("kind")
    if kind == "embedded_json":
        where = src.get("selector", "")
        return (f"JSON in the page ({where}" + (f" [{src['attribute']}]" if src.get("attribute") else "")
                + (f", path {src['path']}" if src.get("path") else "") + ")")
    if kind == "shop_json":
        return {"shopify": "Shopify product JSON (/products/<handle>.js)", "woocommerce": "WooCommerce Store API"}.get(src.get("platform"), "shop JSON")
    return f"{kind} {src.get('schema_type')}.{src.get('path')}"


def shape_to_pattern(shape: str) -> str:
    """A ``url_include`` pattern for a shape: ``/*-p<n>`` -> ``/[^/?#]*-p\\d+$``, ``/lt/*`` ->
    ``/lt/[^/?#]+$``; a wildcard before the last segment may span several segments."""
    toks = [t for t in shape.strip("/").split("/") if t]
    if not toks:
        return "/$"
    parts = []
    for i, t in enumerate(toks):
        last = i == len(toks) - 1
        if t == "<n>":
            parts.append(r"\d+")
        elif t == "*":
            parts.append(r"[^/?#]+" if last else r"[^?#]+")
        elif t.startswith("*-"):
            parts.append(r"[^/?#]*-" + re.escape(t[2:]).replace(re.escape("<n>"), r"\d+"))
        else:
            parts.append(re.escape(t))
    return "/" + "/".join(parts) + "$"
