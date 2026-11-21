"""Variants from data: one record per variant of a product page.

A mapping's ``variants`` block lists variant sources, tried in order; the first that yields at
least one variant wins:

* ``embedded_json`` — JSON inside the page: an element's attribute (WooCommerce's
  ``form.variations_form[data-product_variations]``) or an element's text (a Shopify theme's
  ``<script type="application/json">`` product block), with ``path`` to the variant list;
  optional ``children_path`` selects each parent's children, exposing its original
  object at ``parent.raw`` without merging parent values into child values;
* ``shop_json`` — the shop's own product JSON, one light request per product through the same
  session, tier, politeness and robots rules as a page: ``shopify`` = ``/products/<handle>.js``;
  ``woocommerce`` = Store API ``/wp-json/wc/store/v1/products/<id>`` (lists the variation
  ids) and one more request for the variations (``?type=variation&include=…``);
* ``jsonld`` / ``microdata`` — ``offers`` as a list, or a ``ProductGroup``'s ``hasVariant``.

Every source's raw variants are normalised to one shape: ``id``, ``title``, ``options``
(option name → value), ``price``, ``regular_price``, ``currency``, ``sku``, ``availability``,
``in_stock``, ``weight``, ``description``, ``image`` and ``raw`` (the source's own object).
The block's ``fields`` say which record field takes which normalised value (``path``) or
option (``option``); without ``fields``, the mapping's fields named like a normalised key
(``price``, ``currency``, ``sku``, ``availability``, ``weight``, ``description``, ``image``,
``image_url``) take it.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

from app.fetching.proxy_auth import ProxyAuthenticationRequired

FetchJson = Callable[[str], Awaitable[Any]]

NORMAL_KEYS = ("id", "title", "options", "price", "regular_price", "currency", "sku", "barcode", "availability", "in_stock",
               "weight", "description", "image")
DEFAULT_FIELD_KEYS = {"price": "price", "currency": "currency", "sku": "sku", "barcode": "barcode", "gtin": "barcode",
                      "availability": "availability", "weight": "weight", "description": "description", "image": "image",
                      "image_url": "image"}
TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------- small helpers
def json_path(value: Any, path: str | None) -> list[Any]:
    """Values at a dot path (``variants``, ``product.variants``, ``offers.*``); a number picks a
    list item, ``*`` all items; a list met without either is walked item by item."""
    current = [value]
    for token in [t for t in (path or "").split(".") if t]:
        nxt: list[Any] = []
        for v in current:
            if token == "*":
                nxt.extend(v if isinstance(v, list) else [v])
            elif token.isdigit() and isinstance(v, list):
                i = int(token)
                if i < len(v):
                    nxt.append(v[i])
            elif isinstance(v, dict) and token in v:
                nxt.append(v[token])
            elif isinstance(v, list):
                nxt.extend(item[token] for item in v if isinstance(item, dict) and token in item)
        current = nxt
    return current


def as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = htmllib.unescape(TAG_RE.sub(" ", str(value)))
    s = " ".join(s.split())
    return s or None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _last(value: Any) -> str | None:
    """``https://schema.org/InStock`` -> ``InStock``."""
    if value is None:
        return None
    return re.split(r"[/#]", str(value).rstrip("/"))[-1] or None


def _option_label(key: str) -> str:
    """WooCommerce ``attribute_pa_colour`` -> ``colour``."""
    k = re.sub(r"^attribute_", "", key)
    return re.sub(r"^pa_", "", k)


def _image(value: Any) -> str | None:
    if isinstance(value, dict):
        for k in ("full_src", "src", "url", "contentUrl"):
            if value.get(k):
                return str(value[k])
        return None
    if isinstance(value, list):
        for v in value:
            got = _image(v)
            if got:
                return got
        return None
    return str(value) if value else None


def _flat(v: dict) -> dict:
    out = {k: v.get(k) for k in NORMAL_KEYS}
    out["options"] = {str(k): ("" if val is None else str(val)) for k, val in (v.get("options") or {}).items()}
    out["raw"] = v.get("raw")
    return out


# ---------------------------------------------------------------- normalisers
def from_woocommerce(item: dict) -> dict:
    in_stock = item.get("is_in_stock")
    return _flat({
        "id": item.get("variation_id"), "title": None,
        "options": {_option_label(k): v for k, v in (item.get("attributes") or {}).items()},
        "price": _num(item.get("display_price")), "regular_price": _num(item.get("display_regular_price")),
        "currency": None, "sku": item.get("sku") or None,
        "availability": None if in_stock is None else ("InStock" if in_stock else "OutOfStock"), "in_stock": in_stock,
        "weight": item.get("weight") or None, "description": _text(item.get("variation_description")),
        "image": _image(item.get("image")), "raw": item,
    })


def _shopify_price(value: Any, divisor: float = 100.0) -> float | None:
    """Shopify's ``/products/<handle>.js`` and theme product JSON give prices in minor units
    (1099 = 10.99); a decimal string (``/products/<handle>.json``) is already in major units."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value / divisor
    s = str(value)
    if re.fullmatch(r"\d+", s):
        return int(s) / divisor
    return _num(s)


def from_shopify(item: dict, product: dict | None, divisor: float = 100.0) -> dict:
    names = []
    for opt in (product or {}).get("options") or []:
        names.append(opt.get("name") if isinstance(opt, dict) else str(opt))
    options = {}
    for i in range(1, 4):
        val = item.get(f"option{i}")
        if val is None:
            continue
        name = names[i - 1] if i - 1 < len(names) and names[i - 1] else f"option{i}"
        options[name] = val
    if list(options.values()) == ["Default Title"]:
        options = {}
    available = item.get("available")
    grams = item.get("grams") or item.get("weight")  # /products/<handle>.js: ``weight`` in grams
    image = item.get("featured_image") or (item.get("featured_media") or {}).get("preview_image")
    return _flat({
        "id": item.get("id"), "title": item.get("title") or item.get("public_title"), "options": options,
        "price": _shopify_price(item.get("price"), divisor), "regular_price": _shopify_price(item.get("compare_at_price"), divisor),
        "currency": None, "sku": item.get("sku") or None, "barcode": item.get("barcode") or None,
        "availability": None if available is None else ("InStock" if available else "OutOfStock"), "in_stock": available,
        "weight": f"{grams} g" if isinstance(grams, (int, float)) and grams else None,
        "description": None, "image": _image(image), "raw": item,
    })


def _offer_options(offer: dict) -> dict:
    item = offer.get("itemOffered") if isinstance(offer.get("itemOffered"), dict) else {}
    fmt = item.get("musicReleaseFormat") or offer.get("musicReleaseFormat")
    if fmt:
        label = _last(fmt) or ""
        return {"format": re.sub(r"Format$", "", label) or label}
    for key in ("color", "size", "material", "pattern"):
        if item.get(key) or offer.get(key):
            return {key: str(item.get(key) or offer.get(key))}
    name = item.get("name") or offer.get("name")
    return {"variant": str(name)} if name else {}


def from_offer(offer: dict) -> dict:
    item = offer.get("itemOffered") if isinstance(offer.get("itemOffered"), dict) else {}
    availability = _last(offer.get("availability"))
    return _flat({
        "id": offer.get("@id") or offer.get("sku") or item.get("sku"), "title": item.get("name") or offer.get("name"),
        "options": _offer_options(offer), "price": _num(offer.get("price") or (offer.get("priceSpecification") or {}).get("price")),
        "regular_price": None, "currency": offer.get("priceCurrency") or (offer.get("priceSpecification") or {}).get("priceCurrency"),
        "sku": offer.get("sku") or item.get("sku"), "barcode": offer.get("gtin13") or offer.get("gtin") or item.get("gtin13") or item.get("gtin"),
        "availability": availability,
        "in_stock": None if availability is None else availability in ("InStock", "LimitedAvailability", "OnlineOnly", "PreOrder"),
        "weight": None, "description": _text(item.get("description") or offer.get("description")),
        "image": _image(item.get("image") or offer.get("image")), "raw": offer,
    })


def from_variant_product(node: dict, varies_by: list[str]) -> dict:
    offers = node.get("offers")
    offer = offers[0] if isinstance(offers, list) and offers else (offers if isinstance(offers, dict) else {})
    keys = [_last(v) for v in varies_by] or ["color", "size", "material", "pattern"]
    options = {k: str(node[k]) for k in keys if k and node.get(k) is not None and not isinstance(node.get(k), (dict, list))}
    weight = node.get("weight")
    if isinstance(weight, dict):
        weight = " ".join(str(weight[k]) for k in ("value", "unitText", "unitCode") if weight.get(k) is not None) or None
    availability = _last(offer.get("availability"))
    return _flat({
        "id": node.get("@id") or node.get("sku"), "title": node.get("name"), "options": options,
        "price": _num(offer.get("price")), "regular_price": None, "currency": offer.get("priceCurrency"),
        "sku": node.get("sku") or offer.get("sku"), "barcode": node.get("gtin13") or node.get("gtin"), "availability": availability,
        "in_stock": None if availability is None else availability in ("InStock", "LimitedAvailability", "OnlineOnly", "PreOrder"),
        "weight": weight, "description": _text(node.get("description")), "image": _image(node.get("image")), "raw": node,
    })


def from_store_api(variation: dict, options: dict) -> dict:
    prices = variation.get("prices") or {}
    minor = int(prices.get("currency_minor_unit") or 0)

    def money(v):
        n = _num(v)
        return None if n is None else n / (10 ** minor)

    in_stock = variation.get("is_in_stock")
    return _flat({
        "id": variation.get("id"), "title": variation.get("name"), "options": options,
        "price": money(prices.get("price")), "regular_price": money(prices.get("regular_price")),
        "currency": prices.get("currency_code"), "sku": variation.get("sku") or None,
        "availability": None if in_stock is None else ("InStock" if in_stock else "OutOfStock"), "in_stock": in_stock,
        "weight": variation.get("weight") or None, "description": _text(variation.get("description") or variation.get("short_description")),
        "image": _image(variation.get("images")), "raw": variation,
    })


def from_generic(item: dict) -> dict:
    opts = item.get("options") if isinstance(item.get("options"), dict) else item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    return _flat({
        "id": item.get("id"), "title": item.get("title") or item.get("name"), "options": opts,
        "price": _num(item.get("price")), "regular_price": _num(item.get("regular_price") or item.get("compare_at_price")),
        "currency": item.get("currency"), "sku": item.get("sku"), "availability": item.get("availability"),
        "in_stock": item.get("in_stock", item.get("available")), "weight": item.get("weight"),
        "description": _text(item.get("description")), "image": _image(item.get("image")), "raw": item,
    })


def detect_format(items: list) -> str:
    sample = next((i for i in items if isinstance(i, dict)), {})
    if "variation_id" in sample or ("attributes" in sample and "display_price" in sample):
        return "woocommerce"
    if any(f"option{i}" in sample for i in (1, 2, 3)):
        return "shopify"
    return "generic"


def normalise(items: list, fmt: str, root: Any = None, src: dict | None = None) -> list[dict]:
    """``src`` options: ``price_divisor`` (prices in minor units: 100 for cents/pence; Shopify
    JSON defaults to 100, other formats to 1), ``currency`` (a fixed currency code when the
    data has none, e.g. Shopify's product JSON)."""
    src = src or {}
    items = [i for i in items if isinstance(i, dict)]
    fmt = detect_format(items) if fmt in (None, "", "auto") else fmt
    product = root if isinstance(root, dict) else None
    divisor = float(src.get("price_divisor") or (100 if fmt == "shopify" else 1))
    if fmt == "woocommerce":
        out = [from_woocommerce(i) for i in items]
    elif fmt == "shopify":
        out = [from_shopify(i, product, divisor) for i in items]
    else:
        out = [from_generic(i) for i in items]
    if fmt != "shopify" and divisor != 1:
        for v in out:
            for k in ("price", "regular_price"):
                if isinstance(v.get(k), (int, float)):
                    v[k] = v[k] / divisor
    if src.get("currency"):
        for v in out:
            v["currency"] = v.get("currency") or src["currency"]
    return out


# ---------------------------------------------------------------- sources
def _element_json(page, selector: str, attribute: str | None) -> list[Any]:
    out = []
    try:
        els = page.selector.css(selector)
    except Exception:
        return out
    for el in els[:5]:
        raw = el.attrib.get(attribute) if attribute else "".join(el.xpath(".//text()").getall())
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            try:
                out.append(json.loads(htmllib.unescape(raw)))
            except ValueError:
                continue
    return out


def woocommerce_labels(page) -> dict[str, tuple[str, dict[str, str]]]:
    """WooCommerce keeps slugs in ``data-product_variations`` (``attribute_pa_lens-cover-markings:
    "eu"``); the shop's words are in the variation form: ``<label for="pa_lens-cover-markings">``
    and ``<select name="attribute_pa_lens-cover-markings"><option value="eu">EU sticker</option>``.
    Returns option key (as ``_option_label`` gives it) -> (label, {slug: shown value})."""
    out: dict[str, tuple[str, dict[str, str]]] = {}
    try:
        selects = page.selector.css('select[name^="attribute_"]')
    except Exception:
        return out
    for sel in selects:
        key = _option_label(sel.attrib.get("name", ""))
        if not key:
            continue
        label = None
        sid = sel.attrib.get("id")
        if sid:
            # the label's own text ("Type:"), not a theme's echo of the chosen value inside it
            own = " ".join("".join(page.selector.css(f'label[for="{sid}"]').xpath("./text()").getall()).split()).rstrip(": ").strip()
            label = own or " ".join("".join(page.selector.css(f'label[for="{sid}"]').xpath(".//text()").getall()).split()) or None
        values = {}
        for opt in sel.css("option"):
            val = opt.attrib.get("value")
            if val:
                values[val] = " ".join("".join(opt.xpath(".//text()").getall()).split()) or val
        out[key] = (label or key, values)
    return out


def _apply_woo_labels(page, variants: list[dict]) -> None:
    labels = woocommerce_labels(page)
    if not labels:
        return
    for v in variants:
        shown = {}
        for k, val in (v.get("options") or {}).items():
            label, values = labels.get(k, (k, {}))
            shown[label] = values.get(val, val) if val else val
        v["options"] = shown


def from_embedded(page, src: dict) -> list[dict]:
    for doc in _element_json(page, src["selector"], src.get("attribute")):
        found = json_path(doc, src.get("path"))
        items = [
            item
            for value in found
            for item in (value if isinstance(value, list) else [value])
        ]
        root = (
            doc
            if not src.get("path") or "." not in src["path"]
            else json_path(doc, src["path"].rsplit(".", 1)[0])[:1]
        )
        root = root[0] if isinstance(root, list) and root else root
        woo = src.get("format") == "woocommerce"
        if src.get("children_path"):
            variants = []
            for parent in items:
                if not isinstance(parent, dict):
                    continue
                children = [
                    child
                    for value in json_path(parent, src["children_path"])
                    for child in as_list(value)
                    if isinstance(child, dict)
                ]
                # Keep incomplete parents visible to required-field diagnostics.
                group = normalise(
                    children or [{}], src.get("format") or "auto", parent, src
                )
                for variant in group:
                    variant["parent"] = {"raw": parent}
                variants.extend(group)
                woo = woo or detect_format(children) == "woocommerce"
        else:
            variants = normalise(
                as_list(items),
                src.get("format") or "auto",
                root if isinstance(root, dict) else doc,
                src,
            )
            woo = woo or detect_format(as_list(items)) == "woocommerce"
        if variants and woo:
            _apply_woo_labels(page, variants)
        if variants:
            return variants
    return []


def from_structured(page, src: dict) -> list[dict]:
    """``offers`` as a list (two or more: one offer alone is the product's price) or a
    ``ProductGroup``'s ``hasVariant``, from the first node of ``schema_type``."""
    from app.extraction.page import _type_names

    nodes = page.jsonld_nodes if src["kind"] == "jsonld" else page.microdata_nodes
    for node in nodes:
        if src["schema_type"] not in _type_names(node):
            continue
        items = [i for v in json_path(node, src["path"]) for i in (v if isinstance(v, list) else [v]) if isinstance(i, dict)]
        if not items:
            continue
        if src["path"].split(".")[-1] == "hasVariant":
            varies = [str(v) for v in as_list(node.get("variesBy"))]
            out = [from_variant_product(i, varies) for i in items]
        elif len(items) < 2:
            return []
        else:
            out = [from_offer(i) for i in items]
        if src.get("currency"):
            for v in out:
                v["currency"] = v.get("currency") or src["currency"]
        return out
    return []


def shopify_handle(url: str) -> str | None:
    m = re.search(r"/products/([^/?#]+)", urlsplit(url).path)
    return m.group(1) if m else None


WOO_ID_RES = (re.compile(r"\bpostid-(\d+)\b"), re.compile(r"[?&]p=(\d+)"))


def woocommerce_product_id(page) -> str | None:
    sel = page.selector
    for css in ("input[name=add-to-cart]::attr(value)", "button[name=add-to-cart]::attr(value)", "[data-product_id]::attr(data-product_id)",
                "[data-product-id]::attr(data-product-id)"):
        try:
            v = sel.css(css).get()
        except Exception:
            v = None
        if v and v.strip().isdigit():
            return v.strip()
    body = " ".join(sel.css("body::attr(class)").getall())
    m = WOO_ID_RES[0].search(body)
    if m:
        return m.group(1)
    for href in sel.css("link[rel=shortlink]::attr(href)").getall():
        m = WOO_ID_RES[1].search(href)
        if m:
            return m.group(1)
    return None


def shop_json_urls(page, platform: str) -> list[str]:
    base = f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}"
    if platform == "shopify":
        handle = shopify_handle(page.url)
        return [urljoin(base, f"/products/{handle}.js")] if handle else []
    if platform == "woocommerce":
        pid = woocommerce_product_id(page)
        return [urljoin(base, f"/wp-json/wc/store/v1/products/{pid}")] if pid else []
    return []


async def from_shop_json(page, src: dict, fetch_json: FetchJson | None) -> list[dict]:
    if fetch_json is None:
        return []
    platform = src.get("platform")
    urls = shop_json_urls(page, platform)
    if not urls:
        return []
    doc = await fetch_json(urls[0])
    if not isinstance(doc, dict):
        return []
    if platform == "shopify":
        return normalise(as_list(doc.get("variants")), "shopify", doc, src)
    if platform == "woocommerce":
        listed = [v for v in as_list(doc.get("variations")) if isinstance(v, dict) and v.get("id") is not None]
        if not listed:
            return []
        options = {str(v["id"]): {str(a.get("name")): str(a.get("value")) for a in as_list(v.get("attributes")) if isinstance(a, dict)} for v in listed}
        ids = ",".join(str(v["id"]) for v in listed[:100])
        base = urls[0].rsplit("/products/", 1)[0]
        more = await fetch_json(f"{base}/products?type=variation&include={ids}&per_page=100")
        details = {str(v.get("id")): v for v in as_list(more) if isinstance(v, dict)}
        out = []
        for v in listed[:100]:
            detail = details.get(str(v["id"]), {"id": v["id"]})
            out.append(from_store_api(detail, options.get(str(v["id"]), {})))
        if src.get("currency"):
            for v in out:
                v["currency"] = v.get("currency") or src["currency"]
        return out
    return []


async def resolve(page, block: dict | None, fetch_json: FetchJson | None) -> tuple[list[dict], dict | None]:
    """(variants, {kind, index}) of the first source that yields variants; ([], None) if none."""
    if not block:
        return [], None
    for idx, src in enumerate(block.get("sources") or []):
        try:
            if src["kind"] == "embedded_json":
                variants = from_embedded(page, src)
            elif src["kind"] == "shop_json":
                variants = await from_shop_json(page, src, fetch_json)
            elif src["kind"] in ("jsonld", "microdata"):
                variants = from_structured(page, src)
            else:
                variants = []
        except ProxyAuthenticationRequired:
            raise
        except Exception:
            variants = []
        if variants:
            return variants, {"kind": src["kind"], "index": idx}
    return [], None


def resolve_in_page(page, block: dict | None) -> tuple[list[dict], dict | None]:
    """``resolve`` without requests: the in-page sources only (re-extraction of stored pages)."""
    if not block:
        return [], None
    for idx, src in enumerate(block.get("sources") or []):
        try:
            if src["kind"] == "embedded_json":
                variants = from_embedded(page, src)
            elif src["kind"] in ("jsonld", "microdata"):
                variants = from_structured(page, src)
            else:
                variants = []
        except Exception:
            variants = []
        if variants:
            return variants, {"kind": src["kind"], "index": idx}
    return [], None


# ---------------------------------------------------------------- records
def _variant_value(variant: dict, spec: dict, page=None, *, multiple: bool = False) -> Any:
    if spec.get("selector_template"):
        from app.extraction.mapping import css_values

        template = spec["selector_template"]
        if page is None or not re.fullmatch(r"(?:[^{}]|\{(?:id|sku)\})+", template):
            return None
        missing = False

        def substitute(match: re.Match) -> str:
            nonlocal missing
            value = variant.get(match[1])
            if (
                value is None
                or isinstance(value, (dict, list, bool))
                or str(value) == ""
            ):
                missing = True
                return ""
            # Hex escapes are valid in quoted attributes and identifier contexts.
            return "".join(f"\\{ord(char):x} " for char in str(value))

        selector = re.sub(r"\{(id|sku)\}", substitute, template)
        if missing or len(selector) > 2000:
            return None
        values = css_values(
            page, selector, spec.get("extract", "text"), spec.get("attribute"), multiple
        )
        if not values:
            return None
        return values if multiple else values[0]
    if spec.get("option"):
        want = spec["option"].lower()
        for k, v in (variant.get("options") or {}).items():
            if k.lower() == want:
                return v
        return None
    got = json_path(variant, spec.get("path"))
    return got[0] if got else None


def variant_field_specs(mapping: dict) -> list[dict]:
    block = mapping.get("variants") or {}
    if block.get("fields"):
        return list(block["fields"])
    names = {f["name"] for f in mapping.get("fields") or []}
    return [{"name": n, "path": k} for n, k in DEFAULT_FIELD_KEYS.items() if n in names]


def variant_field_names(mapping: dict) -> list[str]:
    """Record fields beyond ``fields`` that only variants fill (present, null, on every record)."""
    names = [f["name"] for f in mapping.get("fields") or []]
    return [s["name"] for s in (mapping.get("variants") or {}).get("fields") or [] if s["name"] not in names]


def apply_variant(
    mapping: dict, data: dict, variant: dict, base_url: str, *, page=None
) -> tuple[dict, dict]:
    """The record data of one variant (page fields, the variant's values over them) and the
    per-field fill of the variant fields."""
    from app.extraction.transforms import apply_transform, coerce

    def coerce_item(value, spec, ftype):
        if value is None or isinstance(value, (dict, list)):
            return None
        if spec.get("transforms"):
            s = (
                value
                if isinstance(value, str)
                else (
                    str(int(value))
                    if isinstance(value, float) and value.is_integer()
                    else str(value)
                )
            )
            for transform in spec["transforms"]:
                if s is None:
                    break
                s = apply_transform(
                    s if isinstance(s, str) else str(s), transform, base_url
                )
            value = s
        return coerce(value, ftype, base_url)

    types = {f["name"]: f["type"] for f in mapping.get("fields") or []}
    multiple = {f["name"] for f in mapping.get("fields") or [] if f.get("multiple")}
    out = dict(data)
    filled: dict[str, bool] = {}
    for spec in variant_field_specs(mapping):
        value = _variant_value(variant, spec, page, multiple=spec["name"] in multiple)
        ftype = spec.get("type") or types.get(spec["name"], "text")
        if isinstance(value, list) and spec["name"] in multiple:
            items = [coerce_item(item, spec, ftype) for item in value]
            items = [item for item in items if item is not None]
            value = items or (
                [] if not value and spec.get("fallback_to_page") is False else None
            )
        else:
            value = coerce_item(value, spec, ftype)
            if value is not None and spec["name"] in multiple:
                value = [value]
        filled[spec["name"]] = (
            bool(value) if isinstance(value, list) else value is not None
        )
        if (
            value is not None
            or spec["name"] not in out
            or spec.get("fallback_to_page") is False
        ):
            out[spec["name"]] = value
    regular, price = _num(variant.get("regular_price")), _num(variant.get("price"))
    if regular is not None and price is not None and regular > price:
        from app.extraction.pricing import VARIANT_REGULAR

        out[VARIANT_REGULAR] = regular  # the record's regular_price / on_sale (pricing.annotate takes it out)
    return out, filled


def variant_meta(variant: dict, index: int, count: int, kind: str) -> dict:
    meta: dict[str, Any] = {"index": index, "count": count, "source": kind,
                            "options": {k[:100]: v[:500] for k, v in (variant.get("options") or {}).items()}}
    if variant.get("id") is not None:
        meta["id"] = str(variant["id"])[:200]
    return meta


# ---------------------------------------------------------------- detection for samples
def _script_selector(el) -> str:
    """Address the actual script, including ordinary scripts before JSON blocks."""
    ident = el.attrib.get("id")
    if (
        ident
        and re.fullmatch(r"[A-Za-z][\w-]*", ident)
        and len(el.root.getroottree().xpath("//*[@id=$ident]", ident=ident)) == 1
    ):
        return f'script[type="application/json"]#{ident}'
    parts = []
    node = el.root
    while node is not None and isinstance(node.tag, str):
        siblings = [s for s in node.itersiblings(preceding=True) if s.tag == node.tag]
        parts.append(f"{node.tag}:nth-of-type({len(siblings) + 1})")
        node = node.getparent()
    return " > ".join(reversed(parts))


def _raw_example(value: dict) -> tuple[dict, list[str]]:
    """Small raw values and scalar paths for the agent, never executable rules."""
    remaining = 30
    paths: list[str] = []

    def visit(obj: dict | list, prefix: str, depth: int) -> Any:
        nonlocal remaining
        mapping = isinstance(obj, dict)
        out: Any = {} if mapping else []
        # Preserve parent identity and flags before nested details spend the
        # shared scalar budget. Sorting is stable within each priority.
        items = (
            sorted(obj.items(), key=lambda pair: isinstance(pair[1], (dict, list)))
            if mapping
            else enumerate(obj[:2])
        )
        for key, item in items:
            if remaining <= 0:
                break
            if mapping and not re.fullmatch(r"[A-Za-z@_][A-Za-z0-9@_:-]*", key):
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            if len(path) > 300:
                continue
            if isinstance(item, (dict, list)):
                if depth >= 3:
                    if not mapping:
                        break  # Never pack later items into a different source index.
                    continue
                preview = visit(item, path, depth + 1)
                if mapping and not preview:
                    continue
            elif item is None or isinstance(item, (str, int, float, bool)):
                preview = item[:180] if isinstance(item, str) else item
                paths.append(path)
                remaining -= 1
            else:
                if not mapping:
                    break
                continue
            if mapping:
                out[key] = preview
            else:
                out.append(preview)  # Empty containers retain the original array index.
        return out

    return visit(value, "", 0), paths


def detect_candidates(page) -> list[dict]:
    """Bounded JSON-array evidence; the agent decides whether it represents variants.

    These arrays are deliberately separate from confirmed ``detect_sources`` results:
    application state can also contain navigation, images and unrelated products.
    """
    candidates: list[dict] = []
    visited = 0
    used_chars = 0
    output_chars = 0
    try:
        scripts = page.selector.css('script[type="application/json"]')[:20]
    except Exception:
        return candidates

    def append(candidate: dict) -> None:
        nonlocal output_chars
        size = len(json.dumps(candidate, ensure_ascii=False))
        if (
            len(candidates) < 12
            and output_chars + size + 2 + 2 * len(candidates) <= 32768
        ):
            candidates.append(candidate)
            output_chars += size

    def walk(value: Any, path: str, depth: int, selector: str) -> None:
        nonlocal visited, output_chars
        if visited >= 2000 or depth > 8 or len(candidates) >= 12:
            return
        visited += 1
        if isinstance(value, list):
            if (
                value
                and len(value) <= 10000
                and all(isinstance(v, dict) for v in value)
            ):
                examples, paths, parent_paths = [], set(), []
                for row in value[:2]:
                    example, row_paths = _raw_example(row)
                    examples.append(example)
                    paths.update(row_paths)
                    parent_paths.extend(row_paths)
                source = {
                    "kind": "embedded_json",
                    "selector": selector,
                    "format": "generic",
                }
                if path:
                    source["path"] = path
                if paths and path not in ("variants", "product.variants"):
                    candidate = {
                        "source": source,
                        "count": len(value),
                        "raw_examples": examples,
                        "raw_paths": sorted(paths)[:40],
                        "evidence": f"Array of {len(value)} objects at {path or '<root>'}; inspect raw examples to decide whether these are variants.",
                    }
                    append(candidate)
                child_keys = dict.fromkeys(
                    key
                    for row in value[:5]
                    for key, child in row.items()
                    if re.fullmatch(r"[A-Za-z@_][A-Za-z0-9@_:-]*", key)
                    and len(key) <= 300
                    and isinstance(child, list)
                    and any(isinstance(item, dict) for item in child)
                )
                for key in list(child_keys)[:3]:
                    children, count = [], 0
                    for row in value:
                        selected = [
                            v for v in as_list(row.get(key)) if isinstance(v, dict)
                        ]
                        count += max(1, len(selected))
                        children.extend(selected[: max(0, 2 - len(children))])
                    if not children or count > 10000:
                        continue
                    child_examples, child_paths = [], []
                    for child in children:
                        example, found_paths = _raw_example(child)
                        child_examples.append(example)
                        child_paths.extend(found_paths)
                    append(
                        {
                            "source": {**source, "children_path": key},
                            "count": count,
                            "parent_count": len(value),
                            "raw_examples": child_examples,
                            "raw_paths": sorted(set(child_paths))[:40],
                            "parent_examples": examples,
                            "context_paths": [
                                f"raw.{p}"
                                for p in dict.fromkeys(child_paths)
                                if len(p) <= 296
                            ][:20]
                            + [
                                f"parent.raw.{p}"
                                for p in sorted(
                                    dict.fromkeys(parent_paths), key=lambda p: "." in p
                                )
                                if len(p) <= 289
                            ][:20],
                            "evidence": f"Child objects at {path or '<root>'} -> {key}; inspect parent and child examples before choosing variant granularity. Parent fields use parent.raw paths.",
                        }
                    )
            for i, item in enumerate(value[:5]):
                walk(item, f"{path}.{i}" if path else str(i), depth + 1, selector)
        elif isinstance(value, dict):
            for key, item in value.items():
                if visited >= 2000 or len(candidates) >= 12:
                    break
                if re.fullmatch(r"[A-Za-z@_][A-Za-z0-9@_:-]*", key):
                    child = f"{path}.{key}" if path else key
                    if len(child) <= 300:
                        walk(item, child, depth + 1, selector)

    for el in scripts:
        raw = "".join(el.xpath(".//text()").getall())
        if len(raw) > 524288 or used_chars + len(raw) > 1048576:
            continue
        used_chars += len(raw)
        selector = _script_selector(el)
        if len(selector) > 500:
            continue
        try:
            doc = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        walk(doc, "", 0, selector)
        if visited >= 2000 or len(candidates) >= 12:
            break
    return candidates


def detect_sources(page) -> list[dict]:
    """Variant sources visible in the page itself (for samples: the AI chooses from these)."""
    found: list[dict] = []
    for sel, attr in (("form.variations_form", "data-product_variations"), ("[data-product_variations]", "data-product_variations")):
        src = {"kind": "embedded_json", "selector": sel, "attribute": attr, "format": "woocommerce"}
        v = from_embedded(page, src)
        if v:
            found.append(_described(src, v))
            break
    try:
        scripts = page.selector.css('script[type="application/json"]')
    except Exception:
        scripts = []
    for i, el in enumerate(scripts[:20]):
        try:
            doc = json.loads("".join(el.xpath(".//text()").getall()) or "null")
        except ValueError:
            continue
        for path in ("variants", "product.variants"):
            items = json_path(doc, path)
            items = items[0] if len(items) == 1 and isinstance(items[0], list) else []
            if items and isinstance(items[0], dict):
                selector = _script_selector(el)
                src = {"kind": "embedded_json", "selector": selector, "path": path, "format": "auto"}
                v = from_embedded(page, src)
                if v:
                    found.append(_described(src, v))
                break
    for kind, nodes in (("jsonld", page.jsonld_nodes), ("microdata", page.microdata_nodes)):
        from app.extraction.page import _type_names

        for node in nodes:
            types = _type_names(node)
            if "ProductGroup" in types and isinstance(node.get("hasVariant"), list):
                src = {"kind": kind, "schema_type": "ProductGroup", "path": "hasVariant"}
                v = from_structured(page, src)
                if v:
                    found.append(_described(src, v))
            if "Product" in types and isinstance(node.get("offers"), list) and len(node["offers"]) > 1:
                src = {"kind": kind, "schema_type": "Product", "path": "offers"}
                v = from_structured(page, src)
                if v:
                    found.append(_described(src, v))
    return found


def page_platforms(page) -> list[str]:
    low = page.html[:400_000].lower()
    out = []
    if shopify_handle(page.url) and ("cdn.shopify.com" in low or "shopify" in low):
        out.append("shopify")
    if ("wp-content" in low or "woocommerce" in low) and woocommerce_product_id(page):
        out.append("woocommerce")
    return out


def _described(src: dict, variants: list[dict]) -> dict:
    first = variants[0]
    return {"source": src, "count": len(variants),
            "option_names": sorted({k for v in variants for k in (v.get("options") or {})})[:10],
            "example": {k: first.get(k) for k in ("options", "price", "currency", "sku", "availability", "weight", "image") if first.get(k) not in (None, {}, "")},
            "raw_keys": sorted((first.get("raw") or {}).keys())[:40] if isinstance(first.get("raw"), dict) else []}


def variant_rows(
    mapping: dict,
    page_data: dict,
    variants: list[dict],
    kind: str | None,
    base_url: str,
    *,
    page=None,
) -> list[tuple[dict, dict | None, dict]]:
    """(data, variant meta or None, per-variant-field fill) per record of a page: one row per
    variant, or one row without variant meta when the page has no variants."""
    base = dict(page_data)
    for name in variant_field_names(mapping):
        base.setdefault(name, None)
    if not variants:
        for spec in variant_field_specs(mapping):
            if spec.get("fallback_to_page") is False:
                base[spec["name"]] = None
        return [(base, None, {})]
    rows = []
    for i, v in enumerate(variants):
        data, filled = apply_variant(mapping, base, v, base_url, page=page)
        meta = variant_meta(v, i, len(variants), kind or "") if len(variants) > 1 or v.get("options") else None
        rows.append((data, meta, filled))
    return rows
