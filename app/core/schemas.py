"""The contract's JSON Schemas, loaded once; request validation with field-coded errors."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contract"
BASE = "https://contracts.flowsteward.org/web-scraper/v1/"


@lru_cache(maxsize=1)
def registry() -> Registry:
    resources = []
    for path in sorted((CONTRACT_DIR / "schemas").glob("*.schema.json")):
        doc = json.loads(path.read_text("utf-8"))
        resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def validator(ref: str) -> Draft202012Validator:
    """``ref`` like ``job_config.schema.json#/$defs/validate_request``."""
    return Draft202012Validator({"$ref": BASE + ref}, registry=registry())


def _path(parts) -> str:
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            name = re.sub(r"[^a-z0-9_]", "_", str(part).lower()) or "_"
            if not re.match(r"[a-z_]", name):
                name = "_" + name
            out += ("." if out else "") + name
    return out


def _leaf_errors(err: ValidationError) -> list[ValidationError]:
    if err.validator in ("anyOf", "oneOf", "allOf") and err.context:
        # prefer the branch with the deepest errors
        best = max(err.context, key=lambda e: (len(e.absolute_path), e.validator != "type"))
        if len(best.absolute_path) > len(err.absolute_path):
            return _leaf_errors(best)
        if all(e.validator == "type" for e in err.context):
            return [err]
        return [err]
    return [err]


def _code(err: ValidationError) -> str:
    if err.validator == "additionalProperties":
        return "unknown_field"
    if err.validator == "required":
        return "required"
    if err.validator == "type":
        return "invalid_type"
    if err.validator in ("anyOf", "oneOf") and err.context and all(e.validator == "type" for e in err.context):
        return "invalid_type"
    return "invalid_value"


# enum values removed before the first release, with the replacement named in the error
REMOVED_VALUES = {
    "storm_proxies": "use provider 'generic' (host, port, IP allow-list or user/password) for a Storm Proxies gateway",
}


def schema_errors(ref: str, instance: Any, prefix: str = "") -> list[dict[str, Any]]:
    from app.core.errors import field_error

    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for top in validator(ref).iter_errors(instance):
        for err in _leaf_errors(top):
            parts = list(err.absolute_path)
            code = _code(err)
            if err.validator == "additionalProperties" and isinstance(err.instance, dict):
                allowed = set((err.schema or {}).get("properties", {}))
                extras = [k for k in err.instance if k not in allowed] or ["_"]
                for extra in extras:
                    field = _path(([prefix] if prefix else []) + parts + [extra])
                    key = (field, code)
                    if key not in seen:
                        seen.add(key)
                        found.append(field_error(field, code, f"unknown field '{extra}'"[:300]))
                continue
            if err.validator == "required" and isinstance(err.instance, dict):
                missing = re.findall(r"'([^']+)' is a required property", err.message)
                parts = parts + missing[:1]
            field = _path(([prefix] if prefix else []) + parts) or (prefix or "body")
            key = (field, code)
            if key in seen:
                continue
            seen.add(key)
            # Messages never echo the instance: it may be a secret.
            if err.validator == "enum" and err.instance in REMOVED_VALUES:
                message = f"{field}: '{err.instance}' was removed; {REMOVED_VALUES[err.instance]}"
            elif code == "required":
                message = f"{field} is required"
            elif code == "invalid_type":
                expected = err.validator_value if err.validator == "type" else "another type"
                message = f"{field} has the wrong type (expected {expected})"
            else:
                rule = err.validator
                bound = err.validator_value if rule in ("minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "enum", "const", "pattern") else None
                if rule == "pattern" or (isinstance(bound, (list, dict)) and len(str(bound)) > 200):
                    bound = None
                message = f"{field} violates {rule}" + (f" {bound}" if bound is not None else "")
            found.append(field_error(field, code, message))
    return found


def is_valid(ref: str, instance: Any) -> bool:
    return validator(ref).is_valid(instance)


def assert_valid(ref: str, instance: Any) -> None:
    validator(ref).validate(instance)
