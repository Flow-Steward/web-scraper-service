"""Which links a crawl follows (contract README 4.11).

Applied to a page's links before they enter the queue (sitemap and listed URLs are never
skipped by these rules). Every skip is counted by rule in ``counters.links_skipped``. A run
switches rules off with ``link_rules_off`` (job setting, so per site too).

Rules:

* ``scheme`` — not a web address: ``mailto:``, ``tel:``, ``javascript:``, messenger and app
  schemes (the old scraper's list plus more);
* ``messenger_social`` — links to messengers and share buttons on other hosts (never followed
  anyway: another host; counted so the skip is visible);
* ``sensitive_path`` — a path segment that is an account, cart, wishlist, compare, search,
  review-form, newsletter or print page. Whole segments only (``auth`` never matches
  ``authentic``); a segment may carry a file suffix (``login.php``); for the action words a
  segment that starts with the word and a separator matches too (``wishlist-add``);
* ``action_params`` — a query that performs an action (``add-to-cart``, ``form_key`` …);
* ``file_extension`` — documents, archives, media, images, fonts, css/js/json/xml/txt;
* ``nofollow`` — every anchor to the URL says ``rel="nofollow"``;
* ``facet_params`` — layered-navigation filters and sort/view switches in the query
  (``color``, ``size``, ``price``, ``filter_*`` …); pagination (``page``, ``p``) is kept;
* ``trap_budget``, ``dynamic_params``, ``canonical`` — stateful rules of the runner.

Session and one-time parameters are dropped from the canonical URL itself (``app.core.urls``).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

RULES = ("scheme", "messenger_social", "sensitive_path", "action_params", "file_extension", "nofollow", "facet_params",
         "trap_budget", "dynamic_params", "canonical")
RULE_WORDS = {
    "scheme": "not a web address (mailto:, tel:, javascript:, messenger and app links)",
    "messenger_social": "messenger, chat and share links to other hosts",
    "sensitive_path": "account, login, cart, checkout, wishlist, compare, search, review form, newsletter and print pages",
    "action_params": "links that perform an action (add to cart / wishlist / compare, form keys)",
    "file_extension": "files that are not pages (documents, archives, media, images, fonts, css/js/json/xml/txt)",
    "nofollow": "links marked rel=\"nofollow\"",
    "facet_params": "filter and sort variants of a listing (color=, size=, price=, orderby= …); pagination is kept",
    "trap_budget": "a URL pattern whose last pages found no new target page (suspected trap)",
    "dynamic_params": "a query parameter that takes a new random value on almost every link is ignored in the page key",
    "canonical": "a page whose canonical address was already collected is a duplicate",
}

SCHEMES = (
    "mailto", "tel", "javascript", "data", "blob", "file", "ftp", "sftp", "ws", "wss", "about", "chrome", "chrome-extension",
    "moz-extension", "view-source", "urn", "magnet", "sms", "whatsapp", "skype", "viber", "tg", "callto", "facetime", "maps",
    "geo", "intent", "market", "itms-apps", "itms-services", "fb-messenger", "sip", "webcal",
)
SOCIAL_HOSTS = ("wa.me", "api.whatsapp.com", "web.whatsapp.com", "chat.whatsapp.com", "t.me", "telegram.me", "telegram.org", "m.me",
                "viber.com", "invite.viber.com", "msng.link")
SHARE_PATHS = (("facebook.com", "/sharer"), ("facebook.com", "/share.php"), ("twitter.com", "/intent"), ("x.com", "/intent"),
               ("pinterest.com", "/pin/create"), ("linkedin.com", "/sharing"), ("linkedin.com", "/sharearticle"),
               ("reddit.com", "/submit"), ("vk.com", "/share.php"), ("tumblr.com", "/share"))
# whole path segments (the old scraper's SENSITIVE_PATH_KEYWORDS plus more)
SENSITIVE_SEGMENTS = {
    "login", "logout", "log-in", "sign-in", "signin", "sign-out", "signout", "signup", "sign-up", "register", "registration",
    "account", "my-account", "myaccount", "customer", "user", "auth", "oauth", "password", "forgot-password", "lost-password",
    "wishlist", "wish-list", "cart", "basket", "checkout", "onepage", "compare", "product_compare", "catalogsearch", "search",
    "review", "reviews-form", "write-review", "newsletter", "sendfriend", "send-to-friend", "print", "privacy-policy", "terms",
}
# words that also match as a segment prefix followed by a separator (wishlist-add, checkout_cart)
PREFIX_WORDS = ("login", "logout", "signin", "signup", "sign-in", "sign-up", "register", "my-account", "wishlist", "compare",
                "product_compare", "checkout", "catalogsearch", "sendfriend", "newsletter")
ACTION_PARAMS = {"add-to-cart", "add_to_cart", "add-to-wishlist", "add_to_wishlist", "add-to-compare", "add_to_compare",
                 "remove_item", "removed_item", "undo_item", "form_key", "uenc", "_wpnonce", "action", "wishlist_id"}
EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "rtf", "csv", "zip", "rar", "tar", "gz", "tgz", "7z", "bz2",
    "exe", "msi", "dmg", "pkg", "apk", "iso", "mp3", "mp4", "m4a", "m4v", "avi", "mov", "wmv", "flv", "webm", "ogg", "wav",
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "ico", "avif", "heic", "psd", "ai", "eps", "svg", "tif", "tiff", "raw",
    "woff", "woff2", "ttf", "eot", "otf", "css", "js", "json", "xml", "txt", "rss", "atom",
}
FACET_PARAMS = {
    "color", "colour", "size", "price", "brand", "material", "manufacturer", "dir", "order", "orderby", "order_by", "sort",
    "sort_by", "sortby", "limit", "mode", "view", "product_list_order", "product_list_dir", "product_list_limit",
    "product_list_mode", "min_price", "max_price", "rating_filter", "filter", "filters",
}
FACET_PREFIXES = ("filter_", "pa_", "query_type_")
PAGE_SUFFIX = re.compile(r"\.(html?|php|aspx?|jsp)$", re.I)


def _segment_hit(seg: str) -> bool:
    s = PAGE_SUFFIX.sub("", seg.lower())
    if s in SENSITIVE_SEGMENTS:
        return True
    return any(s.startswith(w) and len(s) > len(w) and s[len(w)] in "-_." for w in PREFIX_WORDS)


def raw_skip(href: str, enabled: set[str]) -> str | None:
    """The rule that drops a raw href before it is resolved (schemes), or None."""
    h = (href or "").strip().lower()
    m = re.match(r"^([a-z][a-z0-9+.-]*):", h)
    if m and m.group(1) not in ("http", "https") and "scheme" in enabled and m.group(1) in SCHEMES:
        return "scheme"
    return None


def offsite_rule(url: str, enabled: set[str]) -> str | None:
    if "messenger_social" not in enabled:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in SOCIAL_HOSTS:
        return "messenger_social"
    path = parts.path.lower()
    for h, p in SHARE_PATHS:
        if (host == h or host.endswith("." + h)) and path.startswith(p):
            return "messenger_social"
    return None


def url_skip(url: str, enabled: set[str]) -> str | None:
    """The rule that drops a same-site link, or None (rules in ``RULES`` order)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    segs = [s for s in parts.path.split("/") if s]
    if "sensitive_path" in enabled and any(_segment_hit(s) for s in segs):
        return "sensitive_path"
    names = [n.lower() for n, _ in parse_qsl(parts.query, keep_blank_values=True)]
    if "action_params" in enabled and any(n in ACTION_PARAMS for n in names):
        return "action_params"
    if "file_extension" in enabled and segs:
        last = segs[-1].lower()
        if "." in last and last.rsplit(".", 1)[1] in EXTENSIONS:
            return "file_extension"
    if "facet_params" in enabled and any(n in FACET_PARAMS or n.startswith(FACET_PREFIXES) for n in names):
        return "facet_params"
    return None


def enabled_rules(config: dict) -> set[str]:
    return set(RULES) - set(config.get("link_rules_off") or [])


# ---------------------------------------------------------------------- page keys
HASH_SEGMENT = re.compile(r"^[A-Za-z0-9_\-+,]{24,}={0,2}$")


def _hashlike(seg: str) -> bool:
    return bool(HASH_SEGMENT.match(seg)) and bool(re.search(r"\d", seg)) and bool(re.search(r"[A-Za-z]", seg)) and not re.search(r"[a-z]{3,}-[a-z]{3,}", seg)


def key_url(canonical: str, dynamic: set[str] | frozenset = frozenset()) -> str:
    """Keep canonical path identity, ignoring only learned dynamic query parameters.

    Opaque-looking path segments can identify distinct resources. Shape heuristics
    belong to trap grouping, never to the decision that a page was already seen.
    """
    parts = urlsplit(canonical)
    query = parts.query
    if dynamic and query:
        query = "&".join(
            p for p in query.split("&") if p.partition("=")[0].lower() not in dynamic
        )
    if query == parts.query:
        return canonical
    return f"{parts.scheme}://{parts.netloc}{parts.path}" + (
        f"?{query}" if query else ""
    )


PATH_FILTER = re.compile(r"^(color|colour|size|brand|material|price|manufacturer|pattern|width|height|length|finish)-[a-z0-9][a-z0-9_-]*$", re.I)
FILTER_IDS = re.compile(r"^\d+(-\d+)+$|^\d+$")


def path_filtered(segs: list[str]) -> bool:
    """Filters written in the path: ``color-red``, ``size-xl``, ``brand-x`` segments, or
    ``f/<ids joined by dashes>``."""
    for i, seg in enumerate(segs):
        if PATH_FILTER.match(seg):
            return True
        if seg.lower() == "f" and i + 1 < len(segs) and FILTER_IDS.match(segs[i + 1]):
            return True
    return False


def trap_pattern(canonical: str) -> str:
    """Group trap budgets by the first segment and query parameter names.

    Digits and opaque-looking first segments are normalized only for grouping.
    Depth is left out. A URL with a filter written in its path
    (``/lights/color-red/size-xl/``, ``/lights/f/12-40/``) counts under one site-wide pattern."""
    parts = urlsplit(canonical)
    segs = [s for s in parts.path.split("/") if s]
    names = sorted(
        {n.lower() for n, _ in parse_qsl(parts.query, keep_blank_values=True)}
    )
    query = ("?" + "&".join(names)) if names else ""
    if path_filtered(segs):
        return (
            "/<path-filters>" + query
        )  # every path filter of the site shares one budget
    first = (
        ("<opaque>" if _hashlike(segs[0]) else re.sub(r"\d+", "<n>", segs[0]))
        if segs
        else ""
    )
    first += "/*" if len(segs) > 1 else ""
    return f"/{first}" + query


PAGINATION_PARAMS = {"page", "p", "pg", "paged", "start", "offset", "pagenumber", "page_no"}
RANDOM_VALUE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_\-]{12,}$|^[0-9a-fA-F]{16,}$")


class DynamicParams:
    """Learns query parameters whose value changes on almost every link: seen on
    at least ``min_paths`` different paths, random-looking values, and a different value for most
    repeated sightings of one path. Pagination parameters are never learned."""

    def __init__(self, learned: list[str] | None = None, min_paths: int = 10) -> None:
        self.learned: set[str] = set(learned or [])
        self.min_paths = min_paths
        self._seen: dict[str, dict[str, set[str]]] = {}

    def observe(self, canonical: str) -> str | None:
        """Returns a parameter name when it has just been learned."""
        parts = urlsplit(canonical)
        if not parts.query:
            return None
        new = None
        for name, value in parse_qsl(parts.query, keep_blank_values=True):
            n = name.lower()
            if n in self.learned or n in PAGINATION_PARAMS or not RANDOM_VALUE.match(value or ""):
                continue
            paths = self._seen.setdefault(n, {})
            vals = paths.setdefault(parts.path, set())
            if len(vals) < 8:
                vals.add(value)
            if len(paths) > 2000:
                paths.pop(next(iter(paths)))
            repeated = [v for v in paths.values() if len(v) >= 2]
            if len(paths) >= self.min_paths and len(repeated) >= max(3, len(paths) // 4):
                self.learned.add(n)
                self._seen.pop(n, None)
                new = n
                break
        return new
