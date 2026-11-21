"""Applying a template mapping to a page (README 6.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from app.extraction.page import Page
from app.extraction.transforms import apply_transform, coerce
from app.core.url_patterns import compile_patterns, matches_any, url_policy

_NO = object()


@dataclass
class FieldOutcome:
    value: Any
    source_index: int | None
    errors: list[str] = field(default_factory=list)


@dataclass
class Extraction:
    template_id: str
    data: dict[str, Any]
    filled: dict[str, bool]
    source_hits: dict[str, int | None]
    errors: dict[str, list[str]]
    notes: dict[str, list[dict]] = field(default_factory=dict)  # which rule decided a price/number reading

    def missing(self, required: set[str]) -> list[str]:
        return sorted(name for name in required if not self.filled.get(name, False))


def _url_matches(patterns: list[str], url: str) -> bool:
    return matches_any(compile_patterns(patterns, "match.url_patterns"), url)


@url_policy
def template_matches(mapping: dict, page: Page, canonical: str) -> bool:
    match = mapping.get("match") or {}
    patterns = match.get("url_patterns") or []
    selector = match.get("selector")
    if not patterns and not selector:
        return False
    field = f"mappings.{mapping.get('template_id', 'unknown')}.match.url_patterns"
    if patterns and not matches_any(compile_patterns(patterns, field), canonical):
        return False
    if selector and not page.has_selector(selector):
        return False
    return True


@url_policy
def pick_template(mappings: list[dict], page: Page, canonical: str) -> dict | None:
    for mapping in sorted(mappings, key=lambda m: m["template_id"]):
        if template_matches(mapping, page, canonical):
            return mapping
    return None


def _unwrap(value: Any, multiple: bool) -> list[Any]:
    """Final values of a JSON-LD path result."""
    if isinstance(value, list):
        items = value if multiple else value[:1]
        out: list[Any] = []
        for item in items:
            out.extend(_unwrap(item, False))
        return out
    if isinstance(value, dict):
        if "@value" in value:
            return [value["@value"]]
        if "url" in value and not isinstance(value["url"], (dict, list)):
            return [value["url"]]
        if "@id" in value:
            return [value["@id"]]
        return []
    if value is None:
        return []
    return [value]


def _raw_path(node: Any, path: str) -> list[Any]:
    current = [node]
    for token in path.split("."):
        nxt: list[Any] = []
        for value in current:
            items = value if isinstance(value, list) and not token.isdigit() and token != "*" else [value]
            for v in items:
                if token == "*" and isinstance(v, list):
                    nxt.extend(v)
                elif token.isdigit() and isinstance(v, list) and int(token) < len(v):
                    nxt.append(v[int(token)])
                elif isinstance(v, dict) and token in v:
                    nxt.append(v[token])
        current = nxt
    return current


def jsonld_values(page: Page, schema_type: str, path: str, multiple: bool, microdata: bool = False) -> list[Any]:
    node = page.first_microdata_node(schema_type) if microdata else page.first_node(schema_type)
    if node is None:
        return []
    current: list[Any] = [node]
    for token in path.split("."):
        nxt: list[Any] = []
        for value in current:
            if token == "*":
                if isinstance(value, list):
                    nxt.extend(value)
                elif value is not None:
                    nxt.append(value)
                continue
            if token.isdigit():
                if isinstance(value, list):
                    idx = int(token)
                    if idx < len(value):
                        nxt.append(value[idx])
                elif isinstance(value, dict) and token in value:
                    nxt.append(value[token])
                continue
            # property name; an array met without index or * yields its first item (all for multiple)
            candidates = value if isinstance(value, list) else [value]
            if isinstance(value, list) and not multiple:
                candidates = value[:1]
            for cand in candidates:
                if isinstance(cand, dict) and token in cand:
                    nxt.append(cand[token])
        current = nxt
        if not current:
            return []
    out: list[Any] = []
    for value in current:
        out.extend(_unwrap(value, multiple))
    return out if multiple else out[:1]


def _inner_html(sel) -> str:
    root = sel.root
    if not isinstance(root, etree._Element):
        return str(root)
    parts = [root.text or ""]
    for child in root:
        parts.append(etree.tostring(child, encoding="unicode", method="html"))
    return "".join(parts)


def css_values(page: Page, selector: str, extract: str, attribute: str | None, multiple: bool, price: bool = False) -> list[Any]:
    """``price``: a ``price`` field's text — text nodes split into tags are joined so the amount
    survives (``24<sup>99</sup>`` is 24.99, not 2499; ``pricing.price_text``)."""
    try:
        matches = page.selector.css(selector)
    except Exception:  # invalid selector
        return []
    if not multiple:
        matches = matches[:1]
    out: list[Any] = []
    for m in matches:
        if extract == "text" and price and isinstance(m.root, etree._Element):
            from app.extraction.pricing import note, price_text

            text, joined = price_text(m.root)
            if joined:
                note("minor_units_joined", text)
            out.append(text)
        elif extract == "text":
            out.append("".join(m.xpath(".//text()").getall()) if isinstance(m.root, etree._Element) else str(m.root))
        elif extract == "html":
            out.append(_inner_html(m))
        elif extract == "attribute" and attribute:
            value = m.attrib.get(attribute) if isinstance(m.root, etree._Element) else None
            if value is not None:
                out.append(value)
    return out


def source_allowed(kind: str, extraction_mode: str) -> bool:
    if extraction_mode == "structured_data":
        return kind in ("jsonld", "microdata")
    if extraction_mode == "selectors":
        return kind in ("css", "meta", "spec_table")
    return True


def _clean_text(value: Any) -> str:
    return " ".join(str(value).split()).rstrip(":").strip() if value is not None else ""


def spec_table_pairs(page: Page, source: dict) -> dict[str, str]:
    """"Name → value" from repeated rows (``row_selector``; ``key_selector`` and
    ``value_selector`` inside each row). Later duplicates of a name do not overwrite."""
    out: dict[str, str] = {}
    try:
        rows = page.selector.css(source["row_selector"])
    except Exception:
        return out
    for row in rows[:500]:
        try:
            key = _clean_text("".join(row.css(source["key_selector"])[:1].xpath(".//text()").getall()))
            val = _clean_text("".join(row.css(source["value_selector"])[:1].xpath(".//text()").getall()))
        except Exception:
            continue
        if key and val and key not in out:
            out[key[:200]] = val[:2000]
    return out


def _pairs_from_structured(values: list[Any]) -> dict[str, str]:
    """``additionalProperty`` (schema.org PropertyValue: ``name``/``value``) as pairs."""
    out: dict[str, str] = {}
    for v in values:
        items = v if isinstance(v, list) else [v]
        for item in items:
            if isinstance(item, dict) and item.get("name") is not None and item.get("value") is not None:
                key = _clean_text(item["name"])
                if key and key not in out:
                    out[key[:200]] = _clean_text(item["value"])[:2000]
    return out


def _run_source(page: Page, source: dict, field_type: str, multiple: bool) -> list[Any]:
    kind = source["kind"]
    if kind == "spec_table" or field_type == "spec_table":
        if kind == "spec_table":
            pairs = spec_table_pairs(page, source)
        elif kind in ("jsonld", "microdata"):
            node = page.first_microdata_node(source["schema_type"]) if kind == "microdata" else page.first_node(source["schema_type"])
            pairs = _pairs_from_structured([v for v in _raw_path(node, source["path"])]) if node else {}
        else:
            pairs = {}
        return [pairs] if pairs else []
    if kind in ("jsonld", "microdata"):
        raw = jsonld_values(page, source["schema_type"], source["path"], multiple, microdata=kind == "microdata")
    elif kind == "css":
        raw = css_values(page, source["selector"], source["extract"], source.get("attribute"), multiple, price=field_type == "price")
    elif kind == "meta":
        value = page.meta_value(source["name"])
        raw = [] if value is None else [value]
    else:
        raw = []
    values: list[Any] = []
    for value in raw:
        if isinstance(value, bool) or (isinstance(value, (int, float)) and not source.get("transforms")):
            out = coerce(value, field_type, page.base_url)
        else:
            out = value if isinstance(value, str) else (str(int(value)) if isinstance(value, float) and value.is_integer() else str(value))
            for spec in source.get("transforms") or []:
                if out is None:
                    break
                if not isinstance(out, str):
                    out = str(int(out)) if isinstance(out, float) and out.is_integer() else str(out)
                out = apply_transform(out, spec, page.base_url)
            out = coerce(out, field_type, page.base_url)
        if out is not None:
            values.append(out)
    return values


def _join_spec(source: dict) -> dict | None:
    for t in source.get("transforms") or []:
        if t.get("op") == "join":
            return t
    return None


def extract_field(page: Page, spec: dict, extraction_mode: str) -> FieldOutcome:
    multiple = bool(spec.get("multiple"))
    errors: list[str] = []
    for idx, source in enumerate(spec.get("sources") or []):
        if not source_allowed(source["kind"], extraction_mode):
            continue
        join = _join_spec(source)
        try:
            values = _run_source(page, source, spec["type"], multiple or join is not None)
        except Exception as exc:  # a broken source must not break the page
            errors.append(f"source {idx}: {type(exc).__name__}")
            continue
        if values and join is not None:
            # every match of the source, joined into one string (e.g. all gallery images)
            seen: list[str] = []
            for v in values:
                s = str(v)
                if s not in seen:
                    seen.append(s)
            joined = join.get("separator", ", ").join(seen)
            return FieldOutcome([joined] if multiple else joined, idx, errors)
        if values:
            if multiple:
                deduped: list = []
                for v in values:
                    if v not in deduped:
                        deduped.append(v)
                values = deduped
            return FieldOutcome(values if multiple else values[0], idx, errors)
    return FieldOutcome([] if multiple else None, None, errors)


def apply_mapping(mapping: dict, page: Page, extraction_mode: str = "auto") -> Extraction:
    data: dict[str, Any] = {}
    filled: dict[str, bool] = {}
    hits: dict[str, int | None] = {}
    errors: dict[str, list[str]] = {}
    notes: dict[str, list[dict]] = {}
    from app.extraction.pricing import collect_notes, page_context

    with page_context(page):
        for spec in mapping.get("fields") or []:
            with collect_notes() as found:
                outcome = extract_field(page, spec, extraction_mode)
            if found and outcome.value not in (None, []):
                notes[spec["name"]] = found
            _set(spec, outcome, data, filled, hits, errors)
    return Extraction(mapping["template_id"], data, filled, hits, errors, notes)


def _set(spec: dict, outcome: FieldOutcome, data: dict, filled: dict, hits: dict, errors: dict) -> None:
    data[spec["name"]] = outcome.value
    filled[spec["name"]] = bool(outcome.value) if spec.get("multiple") else outcome.value is not None
    hits[spec["name"]] = outcome.source_index
    if outcome.errors:
        errors[spec["name"]] = outcome.errors


def required_set(mapping: dict | None, required_fields: list[str]) -> set[str]:
    req = set(required_fields or [])
    if mapping:
        req |= {f["name"] for f in mapping.get("fields") or [] if f.get("required")}
    return req


def _selector_parses(selector: str) -> bool:
    try:
        from cssselect import GenericTranslator

        GenericTranslator().css_to_xpath(selector)
        return True
    except Exception:
        return False


def passes_target_selector(page: Page, config: dict) -> bool:
    """With ``target_page_selector`` set, a page without it is never a target page,
    whatever template matches it by URL; a selector that does not parse gates nothing."""
    selector = config.get("target_page_selector")
    if not selector or not _selector_parses(selector):
        return True
    return page.has_selector(selector)


def looks_like_target(page: Page, config: dict, from_url_list: bool) -> bool:
    if from_url_list:
        return True
    selector = config.get("target_page_selector")
    if selector and page.has_selector(selector):
        return True
    wanted = set(config.get("structured_data_types") or [])
    return bool(wanted & (set(page.jsonld_types()) | set(page.microdata_types())))
