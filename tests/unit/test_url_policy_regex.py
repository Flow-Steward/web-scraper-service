"""Configurable URL regexes retain Python syntax and fail explicitly within budgets."""

import json
import re
import subprocess
import sys
import time
from dataclasses import replace

import pytest


def test_pathological_url_filter_is_bounded_and_reports_its_field():
    source = r'''
import json
from app.core.errors import ApiError
from app.jobs.discovery import filter_urls
print("ready", flush=True)
try:
    filter_urls(["https://example.test/" + "a" * 1950 + "!"], [r"(a+)+$"], [])
except ApiError as exc:
    print(json.dumps({"code": exc.code, "fields": exc.fields}), flush=True)
else:
    print(json.dumps({"code": "silently_finished"}), flush=True)
'''
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=2)
    except subprocess.TimeoutExpired:
        pytest.fail("URL regex exceeded the externally bounded subprocess deadline")
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout.splitlines()[-1])
    assert answer["code"] == "validation_failed"
    assert answer["fields"][0]["field"] == "url_include[0]"
    assert answer["fields"][0]["code"] == "pattern_timeout"
    assert time.monotonic() - started < 2


@pytest.mark.parametrize(
    "pattern,url,want",
    [(r"/item/(?P<id>\w+)/(?P=id)$", "https://example.test/item/abc/abc", True),
     (r"(?<=/item/)abc(?=\?)", "https://example.test/item/abc?edition=2", True),
     (r"/item/(\w+)/\1$", "https://example.test/item/abc/other", False)],
)
def test_python_backreferences_and_lookaround_are_preserved(pattern, url, want):
    from app.core.url_patterns import compile_patterns, matches_any

    assert matches_any(compile_patterns([pattern], "url_include"), url) is want


def test_compiled_python_ascii_flags_are_translated_by_name():
    from app.core.url_patterns import matches_any

    assert matches_any([re.compile(r"/\w+$", re.ASCII)], "https://example.test/café") is False
    assert matches_any([re.compile(r"/\w+$")], "https://example.test/café") is True


def test_invalid_python_syntax_is_explicit_even_if_other_engine_accepts_it():
    from app.core.url_patterns import UrlPatternError, compile_patterns

    with pytest.raises(UrlPatternError) as caught:
        compile_patterns([r"(?|a|b)"], "url_include")
    assert caught.value.fields[0]["field"] == "url_include[0]"
    assert caught.value.fields[0]["code"] == "pattern_invalid"


def test_bytes_patterns_are_refused_as_url_configuration():
    from app.core.url_patterns import UrlPatternError, compile_patterns

    with pytest.raises(UrlPatternError):
        compile_patterns([re.compile(b"item")], "url_include")


def test_ordinary_large_catalogue_is_not_truncated_by_regex_budgets():
    from app.jobs.discovery import filter_urls

    urls = [f"https://catalogue.example.test/item/{index}" for index in range(100_000)]
    assert filter_urls(urls, [r"/item/", r"/release/"], [r"/blocked/"]) == urls


def timed_pattern(policy, pattern, clock, seconds):
    original = pattern._compiled

    class TimedSearch:
        def search(self, *args, **kwargs):
            result = original.search(*args, **kwargs)
            clock[0] += seconds
            return result

    return replace(pattern, _compiled=TimedSearch())


@pytest.mark.parametrize("pause_before_search", [True, False])
def test_scheduling_pause_does_not_consume_matching_budget(pause_before_search):
    from app.core import url_patterns as policy

    pattern = policy.compile_patterns([r"/product/\d+$"], "url_include")[0]
    original = pattern._compiled

    class PausedSearch:
        def search(self, *args, **kwargs):
            # A process can be descheduled on either side of the C engine call.
            # Keep a real engine search and its normal timeout in this regression.
            if pause_before_search:
                time.sleep(0.06)
            result = original.search(*args, **kwargs)
            if not pause_before_search:
                time.sleep(0.06)
            return result

    paused = replace(pattern, _compiled=PausedSearch())
    with policy.url_pattern_batch():
        assert policy.matches_any([paused], "https://catalogue.example.test/product/123")


def test_shared_url_budget_is_not_reset_between_policy_checks(monkeypatch):
    from app.core import url_patterns as policy

    clock = [0.0]
    monkeypatch.setattr(policy, "_clock", lambda: clock[0])
    include = [timed_pattern(policy, p, clock, 0.015) for p in policy.compile_patterns(["absent", "item"], "url_include")]
    exclude = [timed_pattern(policy, p, clock, 0.015) for p in policy.compile_patterns(["absent", "item"], "url_exclude")]
    with policy.url_pattern_budget():
        assert policy.matches_any(include, "https://example.test/item")
        # This includes a successful fourth match after the shared allowance:
        # it must raise instead of returning True and hiding the timeout.
        with pytest.raises(policy.UrlPatternError) as caught:
            policy.matches_any(exclude, "https://example.test/item")
    assert caught.value.fields[0]["field"] == "url_exclude[1]"


def test_bulk_budget_is_shared_across_urls_but_not_separate_batches(monkeypatch):
    from app.core import url_patterns as policy

    clock = [0.0]
    monkeypatch.setattr(policy, "_clock", lambda: clock[0])
    monkeypatch.setattr(policy, "BATCH_SECONDS", 0.035)
    patterns = [timed_pattern(policy, p, clock, 0.015) for p in policy.compile_patterns(["item"], "url_include")]
    with policy.url_pattern_batch():
        assert policy.matches_any(patterns, "https://example.test/item/first")
        clock[0] += 10.0  # unrelated parsing/time between matches consumes no regex budget
        assert policy.matches_any(patterns, "https://example.test/item/second")
        with pytest.raises(policy.UrlPatternError):
            policy.matches_any(patterns, "https://example.test/item/third")
    # CPU matching budgets do not leak to a later operation/network observation.
    assert policy.matches_any(patterns, "https://example.test/item/fourth")
