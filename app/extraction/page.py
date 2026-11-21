"""A fetched page as the extraction code sees it: HTML, JSON-LD nodes, meta, base URL."""

from __future__ import annotations

import hashlib
import json
import re
from functools import cached_property
from typing import Any

from lxml import etree
from parsel import Selector


def _type_names(node: dict) -> list[str]:
    types = node.get("@type")
    if types is None:
        return []
    if not isinstance(types, list):
        types = [types]
    out = []
    for t in types:
        if isinstance(t, str):
            out.append(re.split(r"[/#:]", t.rstrip("/"))[-1])
    return out


def flatten_jsonld(docs: list[Any]) -> list[dict]:
    """Every JSON-LD node in document order (pre-order), ``@graph`` and nested nodes included."""
    out: list[dict] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "@graph" in value and isinstance(value["@graph"], list):
                rest = {k: v for k, v in value.items() if k != "@graph"}
                if any(k not in ("@context", "@id") for k in rest):
                    out.append(value)
                for item in value["@graph"]:
                    walk(item)
                return
            if "@type" in value:
                out.append(value)
            for key, child in value.items():
                if key in ("@context",):
                    continue
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for doc in docs:
        walk(doc)
    return out


def _parse_jsonld_scripts(html: str) -> list[Any]:
    try:
        import extruct

        data = extruct.extract(html, syntaxes=["json-ld"], uniform=False, errors="ignore")
        return list(data.get("json-ld") or [])
    except Exception:  # extruct raises on broken markup; fall back to a plain scan
        docs = []
        for raw in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
            try:
                docs.append(json.loads(raw.strip()))
            except ValueError:
                continue
        return docs


def _microdata_to_ld(value: Any) -> Any:
    """extruct's microdata item (``type``, ``properties``) as a JSON-LD-like node (``@type``
    and the properties), nested items too — so ``microdata`` sources share the ``jsonld`` path
    rules."""
    if isinstance(value, list):
        return [_microdata_to_ld(v) for v in value]
    if isinstance(value, dict) and "properties" in value and ("type" in value or "id" in value):
        node: dict[str, Any] = {}
        types = value.get("type")
        if types:
            node["@type"] = types
        if value.get("id"):
            node["@id"] = value["id"]
        for k, v in (value.get("properties") or {}).items():
            node[k] = _microdata_to_ld(v)
        if "value" in value and not isinstance(value["value"], (dict, list)) and "name" not in node:
            node["@value"] = value["value"]
        return node
    return value


def _parse_microdata(html: str) -> list[Any]:
    try:
        import extruct

        data = extruct.extract(html, syntaxes=["microdata"], uniform=False, errors="ignore")
        return [_microdata_to_ld(i) for i in data.get("microdata") or []]
    except Exception:
        return []


class Page:
    def __init__(
        self,
        html: str,
        url: str,
        status: int | None = 200,
        *,
        extra_links: list[str] | None = None,
        extra_anchors: list[tuple[str, str | None, bool]] | None = None,
    ) -> None:
        self.html = html or ""
        self.url = url
        self.status = status
        self.extra_links = list(extra_links or [])
        self.extra_anchors = [tuple(anchor) for anchor in extra_anchors or []]

    @cached_property
    def selector(self) -> Selector:
        return Selector(text=self.html or "<html></html>")

    @cached_property
    def jsonld_docs(self) -> list[Any]:
        return _parse_jsonld_scripts(self.html) if "ld+json" in self.html else []

    @cached_property
    def jsonld_nodes(self) -> list[dict]:
        return flatten_jsonld(self.jsonld_docs)

    def jsonld_types(self) -> list[str]:
        seen: list[str] = []
        for node in self.jsonld_nodes:
            for t in _type_names(node):
                if t not in seen:
                    seen.append(t)
        return seen

    def first_node(self, schema_type: str) -> dict | None:
        for node in self.jsonld_nodes:
            if schema_type in _type_names(node):
                return node
        return None

    @cached_property
    def microdata_nodes(self) -> list[dict]:
        """schema.org microdata (``itemscope``/``itemprop``) as JSON-LD-like nodes."""
        if "itemscope" not in self.html and "itemtype" not in self.html:
            return []
        return flatten_jsonld(_parse_microdata(self.html))

    def microdata_types(self) -> list[str]:
        seen: list[str] = []
        for node in self.microdata_nodes:
            for t in _type_names(node):
                if t not in seen:
                    seen.append(t)
        return seen

    def first_microdata_node(self, schema_type: str) -> dict | None:
        for node in self.microdata_nodes:
            if schema_type in _type_names(node):
                return node
        return None

    @cached_property
    def meta(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for el in self.selector.css("meta"):
            content = el.attrib.get("content")
            if content is None:
                continue
            for attr in ("name", "property", "itemprop"):
                key = el.attrib.get(attr)
                if key and key not in out:
                    out[key] = content
        return out

    def meta_value(self, name: str) -> str | None:
        for el in self.selector.css("meta"):
            if el.attrib.get("name") == name and el.attrib.get("content") is not None:
                return el.attrib["content"]
        for el in self.selector.css("meta"):
            if el.attrib.get("property") == name and el.attrib.get("content") is not None:
                return el.attrib["content"]
        return None

    @cached_property
    def base_url(self) -> str:
        from app.core.urls import absolute

        href = self.selector.css("base::attr(href)").get()
        if href:
            resolved = absolute(self.url, href)
            if resolved:
                return resolved
        return self.url

    @cached_property
    def title(self) -> str | None:
        title = self.selector.css("title::text").get()
        return " ".join(title.split()) if title else None

    def has_selector(self, css: str) -> bool:
        try:
            return bool(self.selector.css(css))
        except Exception:  # invalid selector
            return False

    @cached_property
    def visible_text_chars(self) -> int:
        try:
            body = self.selector.xpath("//body")
            if not body:
                return 0
            texts = body.xpath(".//text()[not(ancestor::script) and not(ancestor::style) and not(ancestor::noscript) and not(ancestor::template)]").getall()
        except Exception:
            return 0
        return len(" ".join(" ".join(texts).split()))

    def links(self) -> list[str]:
        from app.core.urls import absolute

        out = []
        for href in self.selector.css("a[href]::attr(href)").getall():
            resolved = absolute(self.base_url, href)
            if resolved:
                out.append(resolved.split("#", 1)[0])
        return list(dict.fromkeys(out + self.extra_links))

    def anchors(self) -> list[tuple[str, str | None, bool]]:
        """Every ``<a href>``: (raw href, absolute http(s) URL without fragment or None, rel nofollow)."""
        from app.core.urls import absolute

        out = []
        for a in self.selector.css("a[href]"):
            href = a.attrib.get("href", "")
            rel = (a.attrib.get("rel") or "").lower().split()
            resolved = absolute(self.base_url, href)
            out.append((href, resolved.split("#", 1)[0] if resolved else None, "nofollow" in rel))
        seen = set(out)
        for anchor in self.extra_anchors:
            if anchor not in seen:
                out.append(anchor)
                seen.add(anchor)
        return out

    def canonical_link(self) -> str | None:
        """The page's ``<link rel="canonical" href>`` as an absolute URL, or None."""
        from app.core.urls import absolute

        for el in self.selector.css("link[href]"):
            if "canonical" in (el.attrib.get("rel") or "").lower().split():
                return absolute(self.base_url, el.attrib.get("href", ""))
        return None

    @cached_property
    def signature(self) -> str:
        """Hash of the DOM skeleton (tag names and classes of the first levels of <body>)."""
        try:
            root = etree.fromstring(self.html.encode("utf-8", "replace"), etree.HTMLParser())
        except (etree.XMLSyntaxError, ValueError):
            root = None
        parts: list[str] = []
        if root is not None:
            body = root.find(".//body")
            if body is not None:
                def walk(el, depth: int) -> None:
                    if depth > 4 or not isinstance(el.tag, str) or el.tag in ("script", "style", "noscript"):
                        return
                    cls = ".".join(sorted(c for c in (el.get("class") or "").split() if not re.search(r"\d", c))[:3])
                    parts.append(f"{depth}{el.tag}.{cls}")
                    for child in el:
                        walk(child, depth + 1)

                walk(body, 0)
        return hashlib.sha256("|".join(sorted(set(parts))).encode()).hexdigest()[:16]
