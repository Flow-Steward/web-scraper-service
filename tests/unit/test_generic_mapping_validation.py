from types import SimpleNamespace

from app.settings import LIMIT_DEFAULTS
from app.setup_ops import validate_mapping_rules


def service():
    return SimpleNamespace(
        settings=SimpleNamespace(limits={k: v[0] for k, v in LIMIT_DEFAULTS.items()})
    )


def mapping():
    return {
        "fields": [{"name": "title", "type": "text", "sources": []}],
        "variants": {"fields": [{"name": "code", "path": "sku"}]},
    }


def test_filter_rejects_unknown_extracted_field():
    value = mapping()
    value["record_filter"] = [{"field": "unknown", "op": "eq", "value": True}]
    assert any(
        e["field"] == "mapping.record_filter[0].field"
        for e in validate_mapping_rules(value, service(), "mapping")
    )


def test_variant_selector_template_rejects_invalid_css():
    value = mapping()
    value["variants"]["fields"] = [
        {
            "name": "description",
            "selector_template": "[data-code='{id}'",
            "extract": "text",
        }
    ]
    assert any(
        e["field"] == "mapping.variants.fields[0].selector_template"
        for e in validate_mapping_rules(value, service(), "mapping")
    )
