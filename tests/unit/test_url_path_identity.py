"""URL-shape guesses group traps; they must never erase resource identity."""

from types import SimpleNamespace

import pytest

from app.core.linkrules import DynamicParams, key_url, trap_pattern
from app.core.urls import canonical_url
from app.jobs.runner import JobRunner

ORIGIN = "https://catalogue.example.test"


@pytest.mark.parametrize(
    ("paths", "trap"),
    [
        (
            [
                "/directory/ZX301AA-microfabrication",
                "/directory/ZX302BB-electromechanical",
                "/directory/ZX303CC-nanostructuralmaterials",
            ],
            "/directory/*",
        ),
        (
            [
                "/objects/a912b47d903e5f80c1d2e3f4567890ab",
                "/objects/b812b47d903e5f80c1d2e3f4567890cd",
            ],
            "/objects/*",
        ),
    ],
    ids=["catalogue-slugs", "opaque-resource-ids"],
)
def test_distinct_canonical_paths_keep_distinct_queue_identity(paths, trap):
    urls = [canonical_url(ORIGIN + path) for path in paths]
    runner = SimpleNamespace(dynamic=DynamicParams())

    assert [key_url(url) for url in urls] == urls
    assert len({JobRunner._ukey(runner, url, None) for url in urls}) == len(urls)
    assert {trap_pattern(url) for url in urls} == {trap}


def test_path_identity_preserves_learned_query_and_canonical_deduplication():
    left = ORIGIN + "/objects/a912b47d903e5f80c1d2e3f4567890ab"
    right = ORIGIN + "/objects/b812b47d903e5f80c1d2e3f4567890cd"
    first = canonical_url(left + "?edition=blue&nonce=one&utm_source=first")
    repeated = canonical_url(left + "?edition=blue&nonce=two&utm_source=second")
    alternate = canonical_url(left + "?edition=red&nonce=two")
    other_resource = canonical_url(right + "?edition=blue&nonce=one")
    runner = SimpleNamespace(dynamic=DynamicParams(["nonce"]))

    assert key_url(first, {"nonce"}) == left + "?edition=blue"
    assert JobRunner._ukey(runner, first, None) == JobRunner._ukey(
        runner, repeated, None
    )
    assert JobRunner._ukey(runner, first, None) != JobRunner._ukey(
        runner, alternate, None
    )
    assert JobRunner._ukey(runner, first, None) != JobRunner._ukey(
        runner, other_resource, None
    )


def test_opaque_root_paths_share_a_trap_budget_without_sharing_identity():
    urls = [
        ORIGIN + "/a912b47d903e5f80c1d2e3f4567890ab",
        ORIGIN + "/b812b47d903e5f80c1d2e3f4567890cd",
    ]

    assert [key_url(url) for url in urls] == urls
    assert {trap_pattern(url) for url in urls} == {"/<opaque>"}
