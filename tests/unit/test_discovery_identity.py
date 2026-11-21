"""Query identity and target hints work without provider-specific parameter names."""

import re

import pytest

from app.extraction.page import Page
from app.extraction.sampling import link_groups, url_groups
from app.jobs.canon import CanonRegistry


@pytest.mark.parametrize(
    "path,parameter",
    [("/part/rotor", "fit"), ("/record/album", "edition"), ("/item/blend", "revision")],
)
def test_arbitrary_identity_parameters_are_sampled(path, parameter):
    page = Page(
        f'<main><a href="{path}?{parameter}=a">A</a><a href="{path}?{parameter}=b">B</a><a href="/catalogue?page=2">Next</a></main>',
        "https://supplier.test/catalogue",
    )
    groups, stats = link_groups(page, "supplier.test")
    assert {u for g in groups for u in g["urls"]} == {
        f"https://supplier.test{path}?{parameter}=a",
        f"https://supplier.test{path}?{parameter}=b",
    }
    assert stats["variants_skipped"] == 1
    assert (
        sum(
            len(g["urls"])
            for g in url_groups([f"https://supplier.test{path}?{parameter}=a"])
        )
        == 1
    )


def test_explicit_target_hint_overrides_heuristic_facet_parameter_skip():
    page = Page(
        '<main><a href="/part/rotor?size=large">Part</a><a href="/category?size=large">Filter</a></main>',
        "https://supplier.test/",
    )
    groups, _ = link_groups(page, "supplier.test", include=[re.compile(r"/part/")])
    assert {u for g in groups for u in g["urls"]} == {
        "https://supplier.test/part/rotor?size=large"
    }


@pytest.mark.parametrize("parameter", ["fit", "edition", "revision"])
def test_declared_canonical_cannot_remove_product_identity_query(parameter):
    url = f"https://supplier.test/item?{parameter}=second"
    page = Page('<link rel="canonical" href="https://supplier.test/item">', url)
    assert CanonRegistry({}).declared(page, None, "supplier.test") == url


def test_tracking_query_is_still_removed_when_accepting_canonical():
    page = Page(
        '<link rel="canonical" href="https://supplier.test/item">',
        "https://supplier.test/item?utm_source=news",
    )
    assert (
        CanonRegistry({}).declared(page, None, "supplier.test")
        == "https://supplier.test/item"
    )


def test_runtime_keeps_hinted_identity_but_still_blocks_action_links():
    from types import SimpleNamespace
    from app.jobs.runner import JobRunner

    page = Page(
        '<a href="/part/rotor?size=large">Part</a><a href="/part/rotor?add-to-cart=1">Action</a>',
        "https://supplier.test/",
    )
    runner = SimpleNamespace(
        link_rules={"facet_params", "action_params"},
        _looks_like_target_url=lambda u: "/part/" in u,
    )
    links, skips = JobRunner._filter_links(runner, page, page.links())
    assert links == ["https://supplier.test/part/rotor?size=large"]
    assert skips == {"action_params": 1}
