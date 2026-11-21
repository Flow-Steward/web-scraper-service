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
