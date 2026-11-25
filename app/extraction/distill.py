"""Samples: distilled HTML and the sample object (``samples.schema.json``)."""

from __future__ import annotations

import re
from typing import Any

from lxml import etree, html as lxml_html

KEEP_ATTRS = {"id", "class", "itemprop", "itemtype", "itemscope", "href", "src", "alt", "content", "name", "property",
              "value", "for", "role"}  # value/for since a variant select's option slugs and its label
DROP_TAGS = {"script", "style", "svg", "noscript", "template", "iframe", "link", "meta", "canvas"}


LONG_DATA_CHARS = 400  # a data-* value longer than this is shortened
DATA_KEEP_CHARS = 160
# where a page's own content starts, most specific first: the product summary (title,
# price, variant form), then the main content region; header, menus and footer come after
SUMMARY_XPATH = [
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' summary ') and ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' product ') or starts-with(@id, 'product-')]]",
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' entry-summary ')]",
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' product-info-main ')]",
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' product__info ')]",
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' product-single__meta ')]",
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' product-summary ')]",
    "//*[@itemscope and contains(@itemtype, 'schema.org/Product')]",
]
MAIN_XPATH = [
    "//main", "//*[@role='main']", "//*[@id='main']", "//*[@id='content']", "//*[@id='primary']",
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' site-content ')]", "//article",
]


def _path_of(el) -> str:
    """``div#content.site-content > div.summary.entry-summary``: where a section sits in the page,
    so selectors written against the distilled sample still fit the page."""
    parts = []
    node = el
    while node is not None and isinstance(node.tag, str) and node.tag not in ("html",):
        label = node.tag
        if node.get("id"):
            label += "#" + node.get("id")
        classes = [c for c in (node.get("class") or "").split() if re.fullmatch(r"[A-Za-z_][\w-]*", c)][:3]
        label += "".join("." + c for c in classes)
        parts.append(label)
        node = node.getparent()
    parts.reverse()
    return " > ".join(parts[-6:])


def _first(body, xpaths: list[str]):
    for xp in xpaths:
        for el in body.xpath(xp):
            if el is not body and el.getparent() is not None:
                return el
    return None


def _serialise(el) -> str:
    return etree.tostring(el, encoding="unicode", method="html", with_tail=False)


def _swap_out(el, note: str, *, tail_already_given: bool = False):
    """Replaces ``el`` by a comment for one serialisation; returns a function that puts it back."""
    parent = el.getparent()
    marker = etree.Comment(f" {note} ")
    marker.tail, keep_tail = (None if tail_already_given else el.tail), el.tail
    parent.replace(el, marker)

    def restore() -> None:
        parent.replace(marker, el)
        el.tail = keep_tail

    return restore


def _heading_regions(body) -> list:
    """Prioritize local content when no known summary/main landmark exists."""
    excluded_roles = {"banner", "navigation", "contentinfo", "dialog"}

    def excluded(el) -> bool:
        return (
            el.tag in {"header", "nav", "footer"}
            or el.get("role") in excluded_roles
            or el.get("hidden") is not None
            or (el.get("aria-hidden") or "").lower() == "true"
            or bool(
                re.search(
                    r"(?:display\s*:\s*none|visibility\s*:\s*hidden)\b",
                    el.get("style") or "",
                    re.I,
                )
            )
        )

    def link_menu(el) -> bool:
        if len(el.xpath(".//a[@href]")) <= 40:
            return False
        if el.tag in {"table", "dl"} or el.xpath(".//table | .//dl"):
            return (
                False  # Fitment/specification tables can legitimately have many links.
            )
        other_text = " ".join(el.xpath(".//text()[not(ancestor::a)]")).strip()
        return len(other_text) < 500

    for heading in body.xpath(".//h1"):
        if not "".join(heading.itertext()).strip():
            continue
        if any(excluded(el) for el in [heading, *heading.iterancestors()]):
            continue
        if len(heading.xpath("preceding::a[@href]")) <= 40:
            return []  # Keep ordinary small pages in their original presentation.
        region = heading
        for depth, parent in enumerate(heading.iterancestors()):
            if depth >= 16 or parent is body or parent.tag == "html":
                break
            # Do not pull a large site menu into a plain-div product region.
            outside_links = len(parent.xpath(".//a[@href]")) - len(
                region.xpath(".//a[@href]")
            )
            if outside_links > 40:
                break
            region = parent
        return [region] + [
            el
            for el in region.itersiblings()
            if isinstance(el.tag, str) and not excluded(el) and not link_menu(el)
        ]
    return []


def distill(html: str, max_bytes: int, pointers: dict[str, str] | None = None) -> tuple[str, bool]:
    """The page for the mapping agent within ``max_bytes`` (README 2, ``distilled_html``).

    Scripts, styles, SVG, comments and hidden inputs out; attributes reduced. Now the page's
    own content comes first, each section after a ``<!-- wss:section … -->`` comment naming it and
    its place in the page: the product summary (title, price, variant form), then the main
    content, then the rest of the body (header, menus, footer) — a heavy header no longer pushes
    the price out of the budget. A ``data-*`` value longer than 400 characters is shortened;
    ``pointers`` (attribute name → where its data is parsed, e.g. ``variant_sources[0]``) is named
    in the shortened value."""
    if not html:
        return "", False
    try:
        root = lxml_html.document_fromstring(html)
    except (etree.ParserError, ValueError):
        return "", False
    body = root.find(".//body")
    if body is None:
        body = root
    etree.strip_elements(body, etree.Comment, etree.ProcessingInstruction, with_tail=False)
    for el in list(body.iter()):
        if el is body or not isinstance(el.tag, str):
            continue
        if el.tag in DROP_TAGS or (el.tag == "input" and (el.get("type") or "").lower() == "hidden"):
            el.drop_tree()
    summary = _first(body, SUMMARY_XPATH)
    main = _first(body, MAIN_XPATH)
    fallback = _heading_regions(body) if summary is None and main is None else []
    pointers = pointers or {}
    for el in body.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            if attr not in KEEP_ATTRS and not attr.startswith("data-"):
                del el.attrib[attr]
            elif attr.startswith("data-") and len(el.attrib[attr]) > LONG_DATA_CHARS:
                value = el.attrib[attr]
                where = f"; parsed in {pointers[attr]}" if attr in pointers else ""
                el.set(attr, f"{value[:DATA_KEEP_CHARS]}… [{len(value)} characters cut{where}]")
    if main is not None and summary is not None and (main is summary or summary in main.iterancestors()):
        main = None  # the "main" region lies inside the summary: the summary alone
    sections: list[str] = []
    if summary is not None:
        sections.append(f"<!-- wss:section product-summary at {_path_of(summary)} -->" + _serialise(summary))
    if main is not None:
        nested = summary is not None and main in summary.iterancestors()
        restore = _swap_out(summary, "wss: product summary, given above") if nested else None
        sections.append(f"<!-- wss:section main-content at {_path_of(main)} -->" + _serialise(main))
        if restore:
            restore()
    for el in fallback:
        sections.append(
            f"<!-- wss:section content-from-heading at {_path_of(el)} -->"
            + etree.tostring(el, encoding="unicode", method="html", with_tail=True)
        )
    if sections:
        given = fallback or [el for el in (main, summary) if el is not None]
        if main is not None and summary is not None and main in summary.iterancestors():
            given = [main]
        restores = [
            _swap_out(el, "wss: section given above", tail_already_given=bool(fallback))
            for el in given
        ]
        rest = [body.text or ""] + [etree.tostring(child, encoding="unicode", method="html") for child in body]
        for restore in reversed(restores):
            restore()
        sections.append("<!-- wss:section rest-of-page (header, menus, footer) -->" + "".join(rest))
        text = "".join(sections)
    else:
        text = "".join([body.text or ""] + [etree.tostring(child, encoding="unicode", method="html") for child in body])
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r">\s+<", "><", text)
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    cut = data[:max_bytes].decode("utf-8", errors="ignore")
    return cut, True


def variant_pointers(sources: list[dict]) -> dict[str, str]:
    """``data-*`` attribute → ``variant_sources[i]`` for the in-page variant sources read from an
    attribute (WooCommerce ``data-product_variations``)."""
    out: dict[str, str] = {}
    for i, found in enumerate(sources or []):
        attr = ((found or {}).get("source") or {}).get("attribute")
        if attr and attr.startswith("data-") and attr not in out:
            out[attr] = f"variant_sources[{i}]"
    return out


def table_capture_previews(captures: dict) -> list[dict]:
    """Public hints share one 64 KiB budget; only private evidence is replayable."""
    import json

    def text(value):
        return value.encode("utf-8")[:256].decode("utf-8", errors="ignore") if isinstance(value, str) else ""

    previews = []
    originals = []
    for key, captured in list(captures.items())[:55]:
        rows, headers = captured.get("rows") or [], captured.get("headers") or []
        previews.append({"source_key": key, "complete": captured["complete"], "reason": captured["reason"],
                         "pages": captured["pages"], "bytes": captured["bytes"], "row_count": len(rows),
                         "column_count": max([len(headers), *(len(row) for row in rows)], default=0),
                         "headers_preview": [], "rows_preview": [], "preview_truncated": bool(rows or headers)})
        originals.append((rows, headers))
    for index, (rows, headers) in enumerate(originals):
        header_preview = [text(value) for value in headers[:16]]
        row_preview = [[text(value) for value in row[:16]] for row in rows[:3]]
        previous = previews[index]
        candidate = previous | {"headers_preview": header_preview, "rows_preview": row_preview,
                                "preview_truncated": header_preview != headers or row_preview != rows}
        previews[index] = candidate
        # ASCII escaping is an upper bound for both UTF-8 API and SQLite JSON.
        if len(json.dumps(previews, ensure_ascii=True).encode()) > 65536:
            previews[index] = previous
    return previews


def sample_doc(
    *,
    sample_id: str,
    page,
    requested_url: str | None,
    source_key: str | None,
    fetched_at: str,
    expires_at: str,
    fetch: dict,
    matched_template_id: str | None,
    suggested_template_id: str,
    structured_types: list[str],
    max_bytes: int,
    extracted: dict | None,
    table_fields: list[str] | None = None,
) -> dict[str, Any]:
    from app.core.urls import canonical_url
    from app.extraction.pricing import page_signal, price_details
    from app.extraction.variants import detect_candidates, detect_sources

    try:
        in_page = detect_sources(page)  # the in-page variant sources; shop JSON is added by the caller
    except Exception:
        in_page = []
    distilled, truncated = distill(page.html, max_bytes, variant_pointers(in_page))
    try:
        url = canonical_url(page.url)
    except ValueError:
        url = page.url
    doc: dict[str, Any] = {
        "sample_id": sample_id,
        "url": url,
        "http_status": page.status or 200,
        "fetched_at": fetched_at,
        "expires_at": expires_at,
        "fetch": fetch,
        "template_hint": {
            "matched_template_id": matched_template_id,
            "suggested_template_id": suggested_template_id,
            "signature": page.signature,
        },
        "title": page.title,
        "distilled_html": distilled,
        "distilled_html_truncated": truncated,
        "jsonld": [n for n in page.jsonld_nodes if isinstance(n, dict)],
        "meta": page.meta,
        "extracted": {name: value for name, value in extracted.items() if name not in (table_fields or [])} if extracted is not None else None,
        "variant_sources": in_page,
        "variant_candidates": detect_candidates(page),
        "price_on_page": page_signal(page),
    }
    if table_fields:
        doc["table_fields_omitted"] = list(dict.fromkeys(table_fields))
    if getattr(page, "table_captures", None):
        doc["table_captures"] = table_capture_previews(page.table_captures)
    details = price_details(page)
    if details:
        doc["price_details"] = details
    if requested_url and requested_url != url:
        doc["requested_url"] = requested_url
    if source_key:
        doc["source_key"] = source_key
    return doc


def suggest_template_id(url: str, taken: set[str]) -> str:
    from urllib.parse import urlsplit

    path = urlsplit(url).path
    segments = [s for s in path.split("/") if s]
    base = "page"
    if len(segments) >= 2:
        base = segments[0]
    elif segments:
        base = "page"
    base = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_") or "page"
    if not re.match(r"^[a-z]", base):
        base = "t_" + base
    base = base[:56]
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}_{n}"
        n += 1
    return candidate
