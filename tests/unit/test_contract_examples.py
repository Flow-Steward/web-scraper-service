"""Every contract example validates against its schema, through our validator registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.core.schemas import CONTRACT_DIR, validator

STANDALONE = {
    "job_config.json": "job_config.schema.json",
    "record.json": "record.schema.json",
    "record.with_source_key.json": "record.schema.json",
    "record.variant.json": "record.schema.json",
    "mapping.variants.json": "mapping.schema.json",
    "mapping.product.json": "mapping.schema.json",
    "mapping.spare_part.json": "mapping.schema.json",
    "mapping_request.json": "mapping_request.schema.json",
    "mapping_test.request.json": "mapping_test.schema.json#/$defs/request",
    "mapping_test.response.json": "mapping_test.schema.json#/$defs/response",
    "samples.json": "samples.schema.json",
    "analysis.json": "analysis.schema.json",
    "run_summary.json": "run_summary.schema.json",
    "result_descriptor.json": "result_descriptor.schema.json",
    "connection_profile.json": "connection_profile.schema.json",
    "event.mapping_required.json": "event.schema.json",
    "event.run_completed.json": "event.schema.json",
}
STANDALONE.update({p.name: "error.schema.json" for p in (CONTRACT_DIR / "examples").glob("error.*.json")})


def _ref(schema_ref: str) -> str:
    return schema_ref.replace("./schemas/", "")


def openapi_examples() -> list[tuple[str, str]]:
    doc = yaml.safe_load((CONTRACT_DIR / "openapi.yaml").read_text())
    pairs = []

    def collect(content: dict) -> None:
        for media in content.values():
            schema = media.get("schema", {}).get("$ref")
            for ex in (media.get("examples") or {}).values():
                if schema and ex.get("externalValue"):
                    pairs.append((ex["externalValue"].replace("./examples/", ""), _ref(schema)))

    for ops in list(doc["paths"].values()) + list(doc.get("webhooks", {}).values()):
        for op in ops.values():
            if not isinstance(op, dict):
                continue
            if "requestBody" in op:
                collect(op["requestBody"].get("content", {}))
            for resp in op.get("responses", {}).values():
                if isinstance(resp, dict) and "content" in resp:
                    collect(resp["content"])
    return pairs


ALL = sorted(set(openapi_examples()) | set(STANDALONE.items()))


@pytest.mark.parametrize("example,ref", ALL)
def test_example_validates(example: str, ref: str) -> None:
    data = json.loads((CONTRACT_DIR / "examples" / example).read_text())
    errors = list(validator(ref).iter_errors(data))
    assert not errors, [e.message for e in errors][:5]


def test_every_example_file_is_checked() -> None:
    checked = {e for e, _ in ALL} | {"result.sample.jsonl", "webhook_body_signature.vector.json"}
    files = {p.name for p in (CONTRACT_DIR / "examples").iterdir()}
    assert files - checked == set(), files - checked


def test_result_sample_lines_are_records() -> None:
    v = validator("record.schema.json")
    for line in (CONTRACT_DIR / "examples" / "result.sample.jsonl").read_text().splitlines():
        assert not list(v.iter_errors(json.loads(line)))


def test_openapi_document_is_valid() -> None:
    from openapi_spec_validator import validate
    from openapi_spec_validator.readers import read_from_filename

    spec, url = read_from_filename(str(CONTRACT_DIR / "openapi.yaml"))
    validate(spec, base_uri=Path(CONTRACT_DIR / "openapi.yaml").as_uri())
