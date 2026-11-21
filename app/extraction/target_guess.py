"""Guessing the target pages of a site without hints (no JSON-LD, no mapping, no
``url_include``, no ``target_page_selector``), for ``POST /v1/samples``.

From the start page and one level of in-site links, candidate URLs are grouped by URL shape
(path depth; a segment is kept literally when many siblings share it, otherwise it is a
wildcard). The largest group of *leaf* pages wins: several siblings of the same shape whose
pages carry at most one main heading and clearly fewer in-site links than the listing that
linked to them. When no group qualifies the guess fails and the caller reports no samples.

Among the leaf groups, the one whose probed pages carry the most *product signals* wins
(on a shop the largest leaf group was "our portfolio / about us / blog
posts"): price-like text, an add-to-cart or quantity control, product microdata or
OpenGraph/product meta tags, an image gallery. Without any signal in any group the largest
leaf group is taken as before; the caller names the signals in its warning.
"""

from __future__ import annotations

import re
from app.core.url_patterns import matches_any, url_batch
from collections import Counter, defaultdict
from typing import Awaitable, Callable
from urllib.parse import urlsplit

MIN_GROUP = 3
MAX_LISTINGS = 6
MAX_GROUPS_PROBED = 6

_CUR = r"(?:[€£$¥₽₴₹]|\b(?:EUR|USD|GBP|PLN|SEK|NOK|DKK|CHF|CZK|HUF|RON|UAH|RUB|CAD|AUD)\b|zł|Kč|\bkr\b)"
PRICE_RE = re.compile(rf"{_CUR}\s?\d[\d.,\u00a0 ]{{0,12}}\d|\d[\d.,\u00a0 ]{{0,12}}\d\s?{_CUR}", re.I)
CART_RE = re.compile(
    r"add[\s-]?to[\s-]?(?:cart|basket|bag|trolley)|buy now|in den warenkorb|jetzt kaufen|ajouter au panier|añadir al carrito|"
    r"aggiungi al carrello|in winkelwagen|dodaj do koszyka|lägg i varukorgen|læg i kurv|legg i handlekurv|lisää koriin|"
    r"į krepšelį|pievienot grozam|lisa ostukorvi|в корзину|купить|do košíku|kosárba", re.I)
SIGNAL_NAMES = {
    "price": "price-like text",
    "cart": "an add-to-cart or quantity control",
    "markup": "product microdata or OpenGraph product tags",
    "gallery": "an image gallery",
}


def _segments(url: str) -> list[str]:
    path = urlsplit(url).path
    return [s for s in path.split("/") if s]


def group_by_shape(urls: list[str]) -> dict[str, list[str]]:
    by_depth: dict[int, list[tuple[str, list[str]]]] = defaultdict(list)
    for url in urls:
        segs = _segments(url)
        if segs:
            by_depth[len(segs)].append((url, segs))
    groups: dict[str, list[str]] = defaultdict(list)
    for depth, items in by_depth.items():
        counters = [Counter(segs[i] for _, segs in items) for i in range(depth)]
        threshold = max(2, int(0.3 * len(items)))
        for url, segs in items:
            shape = "/" + "/".join(seg if counters[i][seg] >= threshold else "*" for i, seg in enumerate(segs))
            if url not in groups[shape]:
                groups[shape].append(url)
    return dict(groups)


def _in_site_links(page, site: str) -> list[str]:
    from app.core.urls import canonical_url, site_key

    out: list[str] = []
    seen: set[str] = set()
    for link in page.links():
        try:
            if site_key(link) != site:
                continue
            c = canonical_url(link)
        except ValueError:
            continue
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _h1_count(page) -> int:
    try:
        return len(page.selector.css("h1"))
    except Exception:
        return 0


def product_signals(page) -> set[str]:
    """The product signals of a page (``SIGNAL_NAMES``)."""
    sel = page.selector
    found: set[str] = set()
    try:
        text = " ".join(t.strip() for t in sel.xpath("//body//text()[not(ancestor::script) and not(ancestor::style)]").getall() if t.strip())
    except Exception:
        text = ""
    try:
        if PRICE_RE.search(text) or sel.css("[itemprop=price], meta[property='product:price:amount'], meta[property='og:price:amount']"):
            found.add("price")
        controls = " ".join(sel.css("button::text, input[type=submit]::attr(value), a[class*=cart]::text, button::attr(aria-label)").getall())
        if CART_RE.search(controls) or CART_RE.search(text[:20000]) or sel.css(
                "input[name*=qty], input[name*=quantity], input[name*=Qty], input[name*=Quantity], form[action*=cart], form[action*=basket], "
                "[class*=add-to-cart], [class*=addtocart], [id*=add-to-cart]"):
            found.add("cart")
        og = (page.meta_value("og:type") or "").lower()
        if og in ("product", "og:product", "product.item") or sel.css("[itemtype*='schema.org/Product'], meta[property^='product:']") or any(
                t in ("Product", "Offer") for t in page.jsonld_types()):
            found.add("markup")
        gallery = sel.css("[class*=gallery] img, [class*=carousel] img, [class*=swiper] img, [class*=slick] img, [class*=product-image] img, "
                          "[class*=thumbnail] img, [class*=gallery][data-src], [class*=gallery] [data-src]")
        if gallery:
            found.add("gallery")
    except Exception:
        pass
    return found


def describe_signals(signals: list[str]) -> str:
    return ", ".join(SIGNAL_NAMES[s] for s in signals if s in SIGNAL_NAMES)


async def guess_targets(site_url: str, fetch_page: Callable[[str], Awaitable[object | None]], exclude: list) -> tuple[str, list[str]] | None:
    """Returns (url shape, candidate URLs of that shape, product signals found on its probed
    pages) or None. ``fetch_page(url)`` returns a ``Page`` or None."""
    from app.core.urls import canonical_url, site_key

    site = site_key(site_url)
    start = await fetch_page(site_url)
    if start is None:
        return None
    try:
        start_canonical = canonical_url(site_url)
    except ValueError:
        start_canonical = site_url

    def allowed(u: str) -> bool:
        return u != start_canonical and not matches_any(exclude, u)

    @url_batch
    def allowed_links(page):
        return [u for u in _in_site_links(page, site) if allowed(u)]

    parent_links: dict[str, int] = {}
    level0 = allowed_links(start)
    start_count = len(level0)
    for u in level0:
        parent_links.setdefault(u, start_count)
    pages: dict[str, object] = {}
    # one level deeper: a few level-0 pages, spread over their URL shapes (largest first)
    shapes0 = sorted(group_by_shape(level0).values(), key=len, reverse=True)
    picks: list[str] = []
    while len(picks) < MAX_LISTINGS and any(shapes0):
        for members in shapes0:
            if members and len(picks) < MAX_LISTINGS:
                picks.append(members.pop(0))
    for u in picks:
        page = await fetch_page(u)
        if page is None:
            continue
        pages[u] = page
        links = allowed_links(page)
        for v in links:
            parent_links[v] = max(parent_links.get(v, 0), len(links))
    groups = group_by_shape(list(parent_links))
    ranked = sorted(((shape, members) for shape, members in groups.items() if len(members) >= MIN_GROUP), key=lambda g: len(g[1]), reverse=True)
    leaf_groups: list[tuple[float, int, str, list[str], set[str]]] = []
    for order, (shape, members) in enumerate(ranked[:MAX_GROUPS_PROBED]):
        probe = [m for m in members if m in pages][:2] or members[:2]
        leaf_votes = 0
        scores: list[int] = []
        signals: set[str] = set()
        for m in probe:
            page = pages.get(m) or await fetch_page(m)
            if page is None:
                continue
            pages[m] = page
            own = len(allowed_links(page))
            parent = parent_links.get(m, start_count) or 1
            if _h1_count(page) <= 1 and own <= 0.5 * parent:
                leaf_votes += 1
            found = product_signals(page)
            scores.append(len(found))
            signals |= found
        if probe and leaf_votes == len(probe):
            leaf_groups.append((sum(scores) / max(len(scores), 1), -order, shape, members, signals))
    if not leaf_groups:
        return None
    # the most product-like leaf group; without any signal, the largest leaf group (rank order)
    best = max(leaf_groups, key=lambda g: (g[0], g[1]))
    if best[0] == 0:
        best = max(leaf_groups, key=lambda g: g[1])
    return best[2], best[3], sorted(best[4], key=list(SIGNAL_NAMES).index)
