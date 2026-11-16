"""Price status of a record, price ranges and the default currency (contract README 6.5).

A null price must never be ambiguous. Every record of a template with a ``price`` field carries
``price_status``:

* ``shown`` — the price field has a value;
* ``range`` — the source read a range ("24,00 € – 54,00 €"): the field holds the lowest value
  and the record's ``price_max`` the highest;
* ``on_request`` — no price, and the page says the price is given on request (an empty price
  element and request wording, or a schema.org offer without a price);
* ``missing`` — no price and no sign why.

Currency: ``default_currency`` (the ``currency`` transform's parameter) is a last resort. When a
currency field's value came from it, the page's own currency (schema.org ``priceCurrency``,
``product:price:currency`` meta, a symbol or code in the product's price block) replaces it; when the page
names none, the default stays and the record says ``currency_from_default: true``; when the record
has no price, the field is emptied (a currency of nothing says something the page does not).

The page's own signal (``page_signal``) is also given with samples (``price_on_page``) and drives
the mapping test's ``price_not_extracted`` failure.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

PRICE_STATUSES = ("shown", "range", "on_request", "missing")
RANGE_MARK = re.compile(r"[–—]|\s-\s|\bthrough\b|\bto\b|\bbis\b|\biki\b|\bдо\b|\blīdz\b", re.I)
REQUEST_WORDS = re.compile(
    r"price\s+(on|upon)\s+request|on\s+request|request\s+a\s+(quote|price)|ask\s+for\s+(a\s+|the\s+)?price|call\s+for\s+(a\s+|the\s+)?price"
    r"|contact\s+us\s+(for|to\s+get|about)\s+(a\s+|the\s+)?price|to\s+find\s+out\s+the\s+price|price\s+and\s+condition\s+of\s+a\s+product"
    r"|preis\s+auf\s+anfrage|auf\s+anfrage|prix\s+sur\s+demande|cena\s+pēc\s+pieprasījuma|kaina\s+pagal\s+užklausą|цена\s+по\s+запросу"
    r"|hinta\s+pyydettäessä|pris\s+på\s+förfrågan|prezzo\s+su\s+richiesta|precio\s+a\s+consultar|cena\s+na\s+(zapytanie|dotaz)",
    re.I)
PRICE_ELEMENTS = ".price, [itemprop='price'], .woocommerce-Price-amount, .product-price, .price-box, .product__price"
# an element that is a price by its markup, whatever its text shows of the currency
AMOUNT_MARKUP = re.compile(r"\b(woocommerce-Price-amount|amount|price-item|money)\b", re.I)
CURRENCY_HINT = re.compile(r"[€£¥₽₴₹₺₩₪₫₦$]|\b(zł|kč|ft|chf|lei|лв|руб|kr)\b|\b[A-Z]{3}\b", re.I)
NUMBER = re.compile(r"\d[\d.,\s  ]*")


def _price_xpath() -> str:
    from cssselect import GenericTranslator

    return GenericTranslator().css_to_xpath(PRICE_ELEMENTS, prefix="descendant-or-self::")


PRICE_XPATH = _price_xpath()


class PriceRange(float):
    """The lowest price of a range; ``max_value`` the highest (``transforms.price``)."""

    max_value: float

    def __new__(cls, low: float, high: float) -> "PriceRange":
        obj = super().__new__(cls, low)
        obj.max_value = high
        return obj


class DefaultCurrency(str):
    """A currency that came from ``default_currency``, not from the page."""


def split_range(text: str) -> tuple[str, str] | None:
    """The two sides of a range ("24,00 € – 54,00 €", "Price range: 24 € through 54 €"): the
    text before and after the first range mark, each holding a number; else None."""
    for m in RANGE_MARK.finditer(text):
        left, right = text[: m.start()], text[m.end():]
        if re.search(r"\d", left) and re.search(r"\d", right):
            return left, right
        if re.search(r"\d", left):
            return None  # a mark after the only number: not a range
    return None


def _root(page):
    try:
        root = page.selector.root
    except Exception:
        return None
    return root if root is not None and hasattr(root, "xpath") else None


def _price_elements(scope) -> list:
    if scope is None:
        return []
    try:
        return list(scope.xpath(PRICE_XPATH))
    except Exception:
        return []


def _text_of(el) -> str:
    try:
        return price_text(el)[0]
    except Exception:
        return ""


def _content_root(page):
    """The page's own content for the price checks: the product summary, else the main region,
    else the body (the same order as the distilled sample). A header's mini cart ("0,00 €") or
    a footer's wording is not the product's price."""
    from app.extraction.distill import MAIN_XPATH, SUMMARY_XPATH

    root = _root(page)
    if root is None:
        return None
    for xp in SUMMARY_XPATH + MAIN_XPATH:
        try:
            found = root.xpath(xp)
        except Exception:
            continue
        if found:
            return found[0]
    body = root.find(".//body") if hasattr(root, "find") else None
    return body if body is not None else root


def _visible_words(el) -> str:
    if el is None:
        return ""
    try:
        return " ".join(el.xpath(".//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript)]"))
    except Exception:
        return ""


def _offer_nodes(page) -> list[dict]:
    out = []
    nodes = list(getattr(page, "jsonld_nodes", []) or []) + list(getattr(page, "microdata_nodes", []) or [])
    for node in nodes:
        if not isinstance(node, dict):
            continue
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        names = {re.split(r"[/#:]", t.rstrip("/"))[-1] for t in types if isinstance(t, str)}
        if not names & {"Product", "ProductGroup"}:
            continue
        offers = node.get("offers")
        for offer in offers if isinstance(offers, list) else [offers]:
            if isinstance(offer, dict):
                out.append(offer)
    return out


def page_signal(page) -> str:
    """What the page itself shows about its price: ``shown``, ``range``, ``on_request`` or
    ``missing`` (schema.org offers first, then the price elements, then the wording)."""
    offers_without_price = False
    for offer in _offer_nodes(page):
        low, high = offer.get("lowPrice"), offer.get("highPrice")
        if low not in (None, "") and high not in (None, "") and str(low) != str(high):
            return "range"
        if offer.get("price") not in (None, "") or low not in (None, ""):
            return "shown"
        offers_without_price = True
    content = _content_root(page)
    elements = _price_elements(content)
    empty_element = False
    for el in elements[:20]:
        text = _text_of(el)
        if not re.search(r"\d", text):
            empty_element = True
            continue
        markup = " ".join([el.get("class") or "", el.get("itemprop") or ""] + [str(c) for c in el.xpath(".//@class")[:10]])
        if CURRENCY_HINT.search(text) or AMOUNT_MARKUP.search(markup) or el.get("itemprop") == "price":
            numbers = {re.sub(r"[\s  ]", "", n).rstrip(".,") for n in NUMBER.findall(text)}
            if len(numbers) >= 2 and split_range(text):
                return "range"
            return "shown"
    words = _visible_words(content)
    if REQUEST_WORDS.search(words) and (empty_element or offers_without_price or not elements):
        return "on_request"
    return "missing"


def page_currency(page) -> str | None:
    """The currency the page names: schema.org ``priceCurrency``, ``product:price:currency`` /
    ``og:price:currency`` meta, else an ISO code or an unambiguous symbol in a price element of
    the product's own content."""
    from app.extraction.transforms import ISO_CODES, currency

    for offer in _offer_nodes(page):
        code = offer.get("priceCurrency")
        if isinstance(code, str) and code.strip().upper() in ISO_CODES:
            return code.strip().upper()
    for name in ("product:price:currency", "og:price:currency"):
        try:
            value = page.meta_value(name)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip().upper() in ISO_CODES:
            return value.strip().upper()
    for el in _price_elements(_content_root(page))[:20]:  # the product's own price block, not a header's mini cart
        text = _text_of(el)
        if re.search(r"\d", text):
            code = currency(text)
            if code:
                return code
    return None


class PriceView:
    """What one page shows of its price, looked at only when a record needs it (a record with a
    price needs no signal; a currency not from the default needs no page currency)."""

    def __init__(self, page=None, signal: str | None = None, currency: str | None = None) -> None:
        self._page = page
        self._signal = signal
        self._currency = currency
        self._currency_known = page is None

    @property
    def signal(self) -> str:
        if self._signal is None:
            try:
                self._signal = page_signal(self._page) if self._page is not None else "missing"
            except Exception:
                self._signal = "missing"
        return self._signal

    @property
    def sale(self) -> dict | None:
        if not hasattr(self, "_sale"):
            try:
                self._sale = sale_prices(self._page) if self._page is not None else None
            except Exception:
                self._sale = None
        return self._sale

    @property
    def currency(self) -> str | None:
        if not self._currency_known:
            self._currency_known = True
            try:
                self._currency = page_currency(self._page)
            except Exception:
                self._currency = None
        return self._currency

    def __repr__(self) -> str:
        return f"PriceView(signal={self.signal!r}, currency={self.currency!r})"


def price_view(page, template: dict | None = None) -> PriceView:
    """``template`` given: nothing is looked at when it has no price or currency field."""
    if template is not None and price_field(template) is None and not currency_fields(template):
        return PriceView(None, "missing", None)
    return PriceView(page)


def price_field(template: dict) -> str | None:
    """The field ``price_status`` speaks of: a ``price``-type field named ``price``, else the
    first ``price``-type field."""
    names = [f["name"] for f in template.get("fields") or [] if f.get("type") == "price"]
    if not names:
        return None
    return "price" if "price" in names else names[0]


def currency_fields(template: dict) -> list[str]:
    return [f["name"] for f in template.get("fields") or [] if f.get("type") == "currency"]


def annotate(template: dict, data: dict, view: PriceView | None) -> dict[str, Any]:
    """Applies the range and default-currency rules to one record's ``data`` (in place) and
    returns the record's own keys: ``price_status``, ``price_max`` (a range),
    ``currency_from_default`` (true when used). Empty for a template without a price field."""
    name = price_field(template)
    out: dict[str, Any] = {}
    for fname, value in list(data.items()):
        if isinstance(value, PriceRange) and fname != name:
            data[fname] = float(value)  # a range in another price field: its lowest value
    has_price = False
    variant_regular = data.pop(VARIANT_REGULAR, None)
    if name is not None:
        value = data.get(name)
        if isinstance(value, PriceRange):
            out = {"price_status": "range", "price_max": float(value.max_value)}
            data[name] = float(value)
            has_price = True
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out = {"price_status": "shown"}
            has_price = True
        else:
            out = {"price_status": "on_request" if view is not None and view.signal == "on_request" else "missing"}
        if has_price:
            current = float(data[name])
            regular = None
            if isinstance(variant_regular, (int, float)):
                regular = float(variant_regular)  # the variant's own regular price (Woo display_regular_price, Shopify compare_at_price)
            elif view is not None and view.sale is not None and abs(view.sale["current"] - current) <= max(0.011, 0.001 * current):
                regular = view.sale["regular"]  # the page's sale, when this record's price is its current price
            if regular is not None and regular > current + 0.005:
                out["regular_price"] = regular
                out["on_sale"] = True
    for cur in currency_fields(template):
        value = data.get(cur)
        if not isinstance(value, DefaultCurrency):
            continue
        if name is not None and not has_price:
            data[cur] = None  # no price on the record: no currency either
        elif view is not None and view.currency:
            data[cur] = view.currency  # the page names its currency: the default was not needed
        else:
            data[cur] = str(value)
            out["currency_from_default"] = True
    return out


def plain(data: dict) -> dict:
    """``data`` with the marker types turned into plain values (samples' ``extracted``, a test's
    example values) when no record keys are written."""
    for k, v in list(data.items()):
        if isinstance(v, PriceRange):
            data[k] = float(v)
        elif isinstance(v, DefaultCurrency):
            data[k] = str(v)
    return data


# ---------------------------------------------------------------- price text split into tags, separators, machine prices
# Shops split a price into tags: <span class="int">24</span><sup>99</sup>, <span class="price-whole">24</span>
# <span class="price-fraction">99</span>, 24<small>,99</small> €, €<span>1.234</span><span>,56</span>. Joining the
# text nodes with "" reads 2499. ``price_text`` decides the join per text node.
MINOR_TAGS = {"sup", "small"}
MINOR_WORDS = ("dec", "frac", "cent", "minor", "kop")


def _minor_marked(el, stop) -> bool:
    """``el`` (or an ancestor below ``stop``) is a ``sup``/``small`` or names the minor units in a class or id
    (``price-fraction``, ``a-price-decimal``, ``cents``, ``kop``, ``ct``)."""
    node = el
    while node is not None and node is not stop:
        if isinstance(node.tag, str):
            if node.tag.lower() in MINOR_TAGS:
                return True
            for value in (node.get("class") or "", node.get("id") or ""):
                for token in re.split(r"[-_\s]+", value.lower()):
                    if token == "ct" or any(token.startswith(w) for w in MINOR_WORDS):
                        return True
        node = node.getparent()
    return False


STRUCK_TAGS = {"del", "s", "strike"}
STRUCK_WORDS = {"old", "was", "compare", "strike", "strikethrough", "crossed"}
HIDDEN_WORDS = ("screen-reader", "sr-only", "visually-hidden", "visuallyhidden", "screenreader")


def _struck(el, stop) -> bool:
    """``el`` or an ancestor up to ``stop`` (included) is a struck-through old price: ``del``/``s``/``strike``,
    ``text-decoration: line-through``, a class or id such as ``price-old``, ``old-price`` (Magento), ``was``,
    ``compare-at`` (Shopify), ``price--compare``, ``strikethrough``."""
    node = el
    while node is not None:
        if isinstance(node.tag, str):
            if node.tag.lower() in STRUCK_TAGS or "line-through" in (node.get("style") or "").lower():
                return True
            for value in (node.get("class") or "", node.get("id") or ""):
                low = value.lower()
                if "line-through" in low or set(re.split(r"[-_\s]+", low)) & STRUCK_WORDS:
                    return True
        if node is stop:
            break
        node = node.getparent()
    return False


def _hidden(el, stop) -> bool:
    node = el
    while node is not None:
        if isinstance(node.tag, str) and any(w in (node.get("class") or "").lower() for w in HIDDEN_WORDS):
            return True
        if node is stop:
            break
        node = node.getparent()
    return False


def _join(nodes: list, el) -> tuple[str, bool]:
    out = ""
    joined = False
    for t in nodes:
        piece = str(t)
        if not piece:
            continue
        owner = None if getattr(t, "is_tail", False) else t.getparent()
        head, tail = piece.lstrip(), out.rstrip()
        if tail and head and tail[-1].isdigit() and head[0].isdigit():
            digits = re.match(r"\d+", head).group(0)
            if owner is not None and len(digits) <= 2 and _minor_marked(owner, el):
                whole = re.search(r"[\d.,]*$", tail).group(0)
                out = tail + ("," if "." in whole else ".") + head
                joined = True
                continue
            out = tail + " / " + head  # separate amounts: never one digit group
            continue
        out += piece
    return " ".join(out.split()), joined


def price_parts(el) -> tuple[str, str, bool]:
    """(current text, struck-through old text, minor units joined) of a price element. Text nodes
    split into tags are joined so the amount survives: a trailing 1–2 digit node in ``sup``/``small``
    or a minor-units class after a digit gets a decimal mark (the one the whole part does not use),
    a node that starts with ``,`` or ``.`` is joined as it is, other digit-after-digit nodes stay
    apart. When the element holds both a struck-through amount and another one (a sale), the
    struck-through text and screen-reader copies leave the current text."""
    nodes = []
    for t in el.xpath(".//text()[not(ancestor::script) and not(ancestor::style)]"):
        owner = t.getparent()
        if getattr(t, "is_tail", False):
            owner = owner.getparent() if owner is not None and owner is not el else el
        nodes.append((t, _struck(owner, el) if owner is not None else False, _hidden(owner, el) if owner is not None else False))
    has_digit = re.compile(r"\d")
    struck = [t for t, st, _h in nodes if st]
    current = [t for t, st, hid in nodes if not st and not hid]
    if any(has_digit.search(str(t)) for t in struck) and any(has_digit.search(str(t)) for t in current):
        cur, joined = _join(current, el)
        old, _ = _join(struck, el)
        return cur, old, joined
    text, joined = _join([t for t, _s, _h in nodes], el)
    return text, "", joined


def price_text(el) -> tuple[str, bool]:
    """The current price text of a price element (``price_parts``) and whether minor units were joined."""
    cur, _old, joined = price_parts(el)
    return cur, joined


# The decimal mark of an ambiguous "1.234" / "1,234" (one mark, three digits after it) follows the page's language.
DOT_LANGS = {"en", "ja", "zh", "ko", "he", "th", "hi", "ms", "ta", "ur", "ga", "mt", "fil", "tl"}
COMMA_LANGS = {"de", "fr", "es", "it", "nl", "pt", "ru", "uk", "be", "lt", "lv", "et", "pl", "cs", "sk", "sl", "hr", "sr", "bs", "bg",
               "ro", "hu", "da", "sv", "nb", "nn", "no", "fi", "is", "el", "tr", "az", "ka", "hy", "kk", "id", "vi", "ca", "eu", "gl"}
DOT_REGIONS = {"ch", "li"}  # de-CH, it-CH, fr-CH write CHF 1'234.50
DOT_CURRENCIES = {"USD", "GBP", "AUD", "CAD", "NZD", "JPY", "CNY", "INR", "HKD", "SGD", "MXN", "KRW", "THB", "PHP", "MYR", "ILS", "CHF"}
COMMA_CURRENCIES = {"EUR", "RUB", "UAH", "PLN", "CZK", "HUF", "SEK", "NOK", "DKK", "BRL", "TRY", "RON", "BGN", "ISK"}
_DECIMAL_HINT: ContextVar[list | None] = ContextVar("wss_decimal_hint", default=None)  # [page, hint, computed]
_PRICE_NOTES: ContextVar[list | None] = ContextVar("wss_price_notes", default=None)


def _lang_mark(tag: str | None) -> str | None:
    if not tag:
        return None
    parts = re.split(r"[-_]", tag.strip().lower())
    if len(parts) > 1 and parts[1] in DOT_REGIONS:
        return "dot"
    if parts[0] in DOT_LANGS:
        return "dot"
    if parts[0] in COMMA_LANGS:
        return "comma"
    return None


def page_decimal(page) -> tuple[str, str] | None:
    """(``dot``|``comma``, the rule that decided): ``html[lang]``, else ``og:locale``, else the page's currency."""
    try:
        lang = (page.selector.xpath("/html/@lang").get() or "").strip()
    except Exception:
        lang = ""
    mark = _lang_mark(lang)
    if mark:
        return mark, f"page_language:{lang}"
    try:
        loc = page.meta_value("og:locale")
    except Exception:
        loc = None
    mark = _lang_mark(loc)
    if mark:
        return mark, f"og_locale:{loc}"
    try:
        cur = page_currency(page)
    except Exception:
        cur = None
    if cur in DOT_CURRENCIES:
        return "dot", f"currency:{cur}"
    if cur in COMMA_CURRENCIES:
        return "comma", f"currency:{cur}"
    return None


def decimal_hint() -> tuple[str, str] | None:
    """The current page's decimal mark (looked at only when an ambiguous number needs it)."""
    slot = _DECIMAL_HINT.get()
    if slot is None:
        return None
    if not slot[2]:
        slot[2] = True
        try:
            slot[1] = page_decimal(slot[0])
        except Exception:
            slot[1] = None
    return slot[1]


def note(rule: str, text: str) -> None:
    """Records which rule decided a price/number reading (the mapping test shows it per field)."""
    notes = _PRICE_NOTES.get()
    if notes is not None and len(notes) < 20:
        notes.append({"rule": rule, "text": text[:200]})


@contextmanager
def page_context(page):
    """While a mapping is applied to ``page``: the page's decimal mark for ambiguous numbers."""
    token = _DECIMAL_HINT.set([page, None, False])
    try:
        yield
    finally:
        _DECIMAL_HINT.reset(token)


@contextmanager
def collect_notes():
    notes: list = []
    token = _PRICE_NOTES.set(notes)
    try:
        yield notes
    finally:
        _PRICE_NOTES.reset(token)


# Machine-readable prices: what the page states for programs, to cross-check a price read from visible text.
def machine_price(page) -> tuple[float, str] | None:
    """(price, source): JSON-LD/microdata offer ``price`` or ``lowPrice``, ``meta[property=product:price:amount]``,
    an ``[itemprop=price][content]``, then ``data-price``/``data-product-price`` in the product's own content."""
    from app.extraction.transforms import number

    def num(value) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str) and value.strip():
            got = number(value.strip(), "dot") if re.fullmatch(r"\s*[-+]?\d+(\.\d+)?\s*", value) else number(value)
            return got
        return None

    for offer in _offer_nodes(page):
        for key in ("price", "lowPrice"):
            got = num(offer.get(key))
            if got is not None:
                return got, f"schema.org offers.{key}"
    for name in ("product:price:amount", "og:price:amount"):
        try:
            got = num(page.meta_value(name))
        except Exception:
            got = None
        if got is not None:
            return got, f"meta {name}"
    root = _root(page)
    content = _content_root(page)
    if root is not None:
        for el in root.xpath("//*[@itemprop='price'][@content]")[:1]:
            got = num(el.get("content"))
            if got is not None:
                return got, "itemprop=price content"
    if content is not None:
        for attr in ("data-price", "data-product-price", "data-price-amount"):
            for el in content.xpath(f".//*[@{attr}]")[:1]:
                got = num(el.get(attr))
                if got is not None:
                    return got, attr
    return None


def prices_differ(mapped: float, machine: float, source: str) -> bool:
    """More than rounding: 0.011 or 0.1 % of the larger value; a ``data-*`` attribute in minor units (×100) agrees."""
    tol = max(0.011, 0.001 * max(abs(mapped), abs(machine)))
    if abs(mapped - machine) <= tol:
        return False
    if source.startswith("data-") and abs(mapped * 100 - machine) <= tol * 100:
        return False
    return True


# ---------------------------------------------------------------- a sale: the current price and the old (regular) one
VARIANT_REGULAR = "\x00regular_price"  # a variant's own regular price, carried in a record's data until annotate()
SALE_HINT = re.compile(r"<del\b|<s\b|<strike\b|line-through|strikethrough|listprice|old-price|price-old|price--old|was-price|price-was"
                       r"|compare|special-price|class=[\"'][^\"']*\b(?:was|old)\b", re.I)


def _spec_regular(offer: dict) -> float | None:
    from app.extraction.transforms import number

    specs = offer.get("priceSpecification")
    for spec in specs if isinstance(specs, list) else [specs]:
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("priceType") or "")
        if "StrikethroughPrice" in kind or "ListPrice" in kind:
            value = spec.get("price")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            if isinstance(value, str) and value.strip():
                return number(value.strip(), "dot") if re.fullmatch(r"\s*\d+(\.\d+)?\s*", value) else number(value)
    return None


def sale_prices(page) -> dict | None:
    """``{current, regular, source}`` when the page shows a sale: schema.org ``offers.price`` with a
    ``priceSpecification`` of ``priceType`` StrikethroughPrice/ListPrice first, else the product's
    price block with a struck-through amount (``del``/``s``/``strike``, line-through, ``old-price``,
    ``price-old``, ``was``, ``compare-at`` …) beside the current one. None without a sale."""
    from app.extraction.transforms import price as parse_price

    for offer in _offer_nodes(page):
        regular = _spec_regular(offer)
        current = offer.get("price") if offer.get("price") not in (None, "") else offer.get("lowPrice")
        try:
            current = float(current) if current not in (None, "") else None
        except (TypeError, ValueError):
            current = None
        if regular is not None and current is not None and regular > current:
            return {"current": current, "regular": regular, "source": "schema.org priceSpecification"}
    try:
        html = page.html
    except Exception:
        html = ""
    if not SALE_HINT.search(html or ""):
        return None
    with page_context(page):
        for el in _price_elements(_content_root(page))[:20]:
            cur, old, _joined = price_parts(el)
            if not old:
                continue
            c, o = parse_price(cur), parse_price(old)
            if c is not None and o is not None and float(o) > float(c):
                return {"current": float(c), "regular": float(o), "source": "struck-through price"}
    return None


def price_details(page) -> dict:
    """Samples' ``price_details``: what the page states for programs (``machine_price``,
    ``machine_source``) and a sale (``current``, ``regular_price``, ``on_sale``, ``sale_source``)."""
    out: dict[str, Any] = {}
    try:
        stated = machine_price(page)
    except Exception:
        stated = None
    if stated is not None:
        out["machine_price"], out["machine_source"] = stated
    try:
        sale = sale_prices(page)
    except Exception:
        sale = None
    if sale is not None:
        out.update(current=sale["current"], regular_price=sale["regular"], on_sale=True, sale_source=sale["source"])
    return out
