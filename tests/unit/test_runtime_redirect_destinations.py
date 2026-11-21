"""Runtime redirect policy retains walk/target scope, robots, and cancellation."""
from types import SimpleNamespace
import re

import pytest

from app.jobs.runner import JobRunner

ORIGIN = "https://catalogue.example.test"


def runner():
    async def robots(url):
        return not url.endswith("blocked")

    return SimpleNamespace(
        _shutdown=False, rt={}, state="running", running_seconds=lambda: 0,
        config={"max_duration_minutes": 1},
        _crawl_scope=lambda: [re.compile(r"/list(?:/|\?|$)")],
        _looks_like_target_url=lambda url: "/item/" in url,
        exclude=[re.compile(r"/list/excluded$")],
        link_rules={"sensitive_path", "action_params", "facet_params"}, robots_allowed=robots,
    )


@pytest.mark.parametrize("target, allowed", [
    ("/list/next?edition=mono", True), ("/item/record?edition=mono", True), ("/item/record?color=red", True),
    ("/list/excluded", False), ("/other", False), ("/list/blocked", False),
    ("/cart?add-to-cart=42", False), ("https://other.example.test/list", False),
])
async def test_runtime_redirect_uses_link_admission_policy(target, allowed):
    instance = runner()
    url = target if target.startswith("http") else ORIGIN + target
    assert await JobRunner._document_destination_allowed(instance, ORIGIN + "/list", url) is allowed


async def test_runtime_redirect_cancellation_and_auxiliary_scope_exemption():
    instance = runner()
    assert await JobRunner._document_destination_allowed(
        instance, ORIGIN + "/list", ORIGIN + "/sitemap.xml", auxiliary=True,
    )
    instance.rt["stop_reason"] = "cancelled"
    assert not await JobRunner._document_destination_allowed(
        instance, ORIGIN + "/list", ORIGIN + "/list/next",
    )


async def test_runtime_robots_cache_miss_preserves_configured_headers(monkeypatch):
    from app.fetching.oneoff import Fetched
    from app.jobs.runner import JobRunner

    seen = []
    async def robots(origin, fetch):
        await fetch(origin + "/robots.txt")
        return SimpleNamespace(is_allowed=lambda url: False, get_crawl_delay=lambda: None)
    async def fetch(url, **kwargs):
        seen.append((url, kwargs))
        return Fetched(url, url, 200)
    monkeypatch.setattr("app.jobs.discovery.robots_from", robots)
    instance = SimpleNamespace(config={"headers": {"X-Fixture": "retained"}}, robots={},
        throttle=SimpleNamespace(set_crawl_delay=lambda *a: None),
        _redirect_http_client=SimpleNamespace(fetch_auxiliary=fetch))
    assert not await JobRunner._http_redirect_robots_allowed(instance, ORIGIN + "/list/blocked")
    assert seen == [(ORIGIN + "/robots.txt", {"headers": {"X-Fixture": "retained"}})]


def test_runtime_late_policy_error_is_not_accepted_as_document_success():
    from app.fetching.destinations import DestinationDenied
    from app.jobs.runner import raise_document_policy_error

    error = DestinationDenied(ORIGIN + "/cart")
    def rejected():
        raise error
    ctx = SimpleNamespace(request=SimpleNamespace(no_retry=False),
        page=SimpleNamespace(_wss_native_document_policy=SimpleNamespace(errors=[error], raise_if_denied=rejected)))
    with pytest.raises(DestinationDenied):
        raise_document_policy_error(ctx)
    assert ctx.request.no_retry is True
