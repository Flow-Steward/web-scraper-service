"""Explicit target selection must constrain heuristic escapes from a walk scope."""
from types import SimpleNamespace, MethodType

import pytest

from app.core.url_patterns import compile_patterns
from app.extraction.sampling import url_shape
from app.jobs.runner import JobRunner
from app.jobs.discovery import target_patterns
from app.core.url_patterns import matches_any

ORIGIN = "https://supplier.test"


def runner(hint, include):
    other = ORIGIN + "/catalogue/fuel-13"
    mappings = [{"template_id": "product", "match": {"url_patterns": [r"/.+-[0-9]+$"]}}] if hint == "mapping" else []
    instance = SimpleNamespace(config={"url_include": include}, include=compile_patterns(include, "url_include"),
        _target_patterns={}, _target_shapes={url_shape(other)} if hint == "shape" else set(),
        mappings_for=lambda site: mappings)
    instance._looks_like_target_url = MethodType(JobRunner._looks_like_target_url, instance)
    return instance


@pytest.mark.parametrize("hint", ["mapping", "shape"])
def test_explicit_selection_blocks_unrelated_scope_escape(hint):
    instance = runner(hint, [r"/products/selected-[0-9]+$"])
    scope = compile_patterns([r"/catalogue/chosen(?:[/?#]|$)"], "crawl_include")
    assert not JobRunner._scope_allows_link(instance, scope, ORIGIN + "/catalogue/fuel-13", True)
    assert not instance._looks_like_target_url(ORIGIN + "/catalogue/fuel-13")


def test_walk_navigation_and_explicit_targets_outside_it_remain_allowed():
    instance = runner("mapping", [r"/products/selected-[0-9]+$"])
    scope = compile_patterns([r"/catalogue/chosen(?:[/?#]|$)"], "crawl_include")
    assert JobRunner._scope_allows_link(instance, scope, ORIGIN + "/catalogue/chosen?page=2", True)
    assert JobRunner._scope_allows_link(instance, scope, ORIGIN + "/products/selected-9", True)


@pytest.mark.parametrize("hint", ["mapping", "shape"])
def test_without_explicit_selection_existing_target_hints_still_work(hint):
    instance = runner(hint, [])
    scope = compile_patterns([r"/catalogue/chosen(?:[/?#]|$)"], "crawl_include")
    assert JobRunner._scope_allows_link(instance, scope, ORIGIN + "/catalogue/fuel-13", True)


def test_setup_target_patterns_do_not_widen_an_explicit_selection():
    mapping = {"template_id": "product", "match": {"url_patterns": [r"/.+-[0-9]+$"]}}
    patterns = target_patterns([mapping], [r"/products/selected-[0-9]+$"])
    assert not matches_any(patterns, ORIGIN + "/catalogue/fuel-13")
    assert matches_any(patterns, ORIGIN + "/products/selected-9")


@pytest.mark.parametrize("path, expected", [("/catalogue/fuel-13", False), ("/products/selected-9", True)])
def test_backlog_and_trap_target_hint_uses_explicit_selection_before_shapes(path, expected):
    instance = runner("shape", [r"/products/selected-[0-9]+$"])
    assert JobRunner._targetish(instance, ORIGIN + path) is expected
