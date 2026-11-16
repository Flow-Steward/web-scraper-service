"""The fixed HTML clean-up of the ``html`` field type (README 6.1); not configurable."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lxml import etree, html as lxml_html
from lxml_html_clean import Cleaner

_CLEANER = Cleaner(
    scripts=True,
    javascript=True,  # also removes on* event attributes and javascript: links
    comments=True,
    style=True,  # <style> elements and style attributes
    inline_style=True,
    links=False,
    meta=True,
    page_structure=False,
    processing_instructions=True,
    embedded=True,  # object/embed/applet
    frames=True,  # iframe/frame/frameset
    forms=True,
    annoying_tags=False,
    remove_unknown_tags=False,
    safe_attrs_only=False,
    kill_tags=["noscript", "template", "svg", "canvas", "button", "select", "textarea", "input", "link", "base", "meta"],
)
# attributes that carry a URL: kept only as an absolute http(s) URL
URL_ATTRS = ("href", "src", "poster", "background", "cite", "longdesc", "action", "formaction", "data", "usemap", "codebase")
DROP_ATTRS = ("ping", "srcset", "imagesrcset", "xmlns")
TRACKING_ATTR = re.compile(r"^data-(gtm|ga|track)", re.I)


def _strip_utm(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def clean_html(fragment: str, base_url: str | None = None) -> str:
    if fragment is None:
        return ""
    text = str(fragment).strip()
    if not text:
        return ""
    try:
        container = lxml_html.fragment_fromstring(text, create_parent="div")
    except (etree.ParserError, ValueError):
        return ""
    try:
        container = _CLEANER.clean_html(container)
    except (etree.ParserError, ValueError, TypeError):
        return ""
    from app.core.urls import absolute

    for el in container.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            low = attr.lower()
            if TRACKING_ATTR.match(low) or low.startswith("on"):
                del el.attrib[attr]
        for attr in DROP_ATTRS:
            if el.get(attr) is not None:
                del el.attrib[attr]
        for attr in URL_ATTRS:
            value = el.get(attr)
            if value is None:
                continue
            resolved = absolute(base_url, value) if base_url else (value if re.match(r"^https?://", value, re.I) else None)
            if resolved is None:
                del el.attrib[attr]
            else:
                el.set(attr, _strip_utm(resolved) if attr == "href" else resolved)
    parts = [container.text or ""]
    for child in container:
        parts.append(etree.tostring(child, encoding="unicode", method="html"))
    out = "".join(parts).strip()
    return re.sub(r"\n\s*\n+", "\n", out)
