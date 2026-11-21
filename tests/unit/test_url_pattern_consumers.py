"""URL policy timeouts remain actionable at API, sampler and runner boundaries."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core.db import dumps
from app.core.schemas import CONTRACT_DIR, validator
from app.core.url_patterns import UrlPatternError, compile_patterns, matches_any
from app.extraction.page import Page
from app.jobs.runner import JobRunner
from app.service import Service
from tests.unit.test_logic import settings

BAD_URL = "https://catalogue.example.test/" + "a" * 1950 + "!"
BAD_PATTERN = r"(a+)+$"


@pytest.fixture
def runner(tmp_path, monkeypatch):
    svc = Service(settings(data_dir=str(tmp_path), secrets_key="test-secret-key-0123456789"))
    monkeypatch.setattr("app.settings._settings", svc.settings)
    doc = json.loads((CONTRACT_DIR / "examples/get_run.response.json").read_text())
    doc.update(state="running", finished_at=None, outcome=None, stop_reason=None, result=None)
    doc["config"].update(single_page=True, site_url=BAD_URL, max_records=1, url_include=[BAD_PATTERN])
    doc["resolved"]["discovery_mode"] = "crawl"
    doc["counters"]["records_extracted"] = 0
    svc.db.execute(
        "INSERT INTO runs(run_id,idempotency_key,fingerprint,state,site,created_at,updated_at,doc,runtime) VALUES (?,?,?,?,?,?,?,?,?)",
        (doc["run_id"], "test-key", "f", "running", doc["site"], doc["created_at"], doc["updated_at"], dumps(doc), "{}"),
    )
    item = JobRunner(svc, doc["run_id"])
    yield item
    svc.db.close()


def assert_failed_with_pattern(runner):
    assert runner.state == "failed"
    summary = runner.svc.jobs.summary(runner.run_id)
    assert summary["error"]["code"] == "pattern_timeout"
    assert "url_include[0]" in summary["error"]["message"]
    assert not list(validator("run_summary.schema.json").iter_errors(summary))
    events = runner.svc.events.for_run(runner.run_id)
    assert len(events) == 1 and events[0]["event_type"] == "run_failed"
    payload = runner.svc.events.payload(events[0]["event_id"])
    assert not list(validator("event.schema.json").iter_errors(payload))


async def test_feeder_policy_error_fails_run_instead_of_completing_empty(runner, monkeypatch):
    async def enqueue(*args, **kwargs):
        matches_any(runner.include, BAD_URL)

    monkeypatch.setattr(runner, "enqueue", enqueue)
    await runner._feed()
    assert_failed_with_pattern(runner)
    assert not runner.rt.get("feeder_done")


async def test_pre_navigation_policy_error_is_global_and_not_retried(runner):
    from crawlee import Request

    runner.config["single_page"] = False
    ctx = SimpleNamespace(request=Request.from_url(BAD_URL, user_data={"src": "start"}), proxy_info=None)
    with pytest.raises(UrlPatternError) as caught:
        await runner._pre_nav(ctx)
    assert ctx.request.no_retry is True
    await runner._failed(ctx, caught.value)  # Crawlee's failure callback must not duplicate the failure.
    assert_failed_with_pattern(runner)


async def test_follower_policy_error_fails_run_instead_of_retrying(runner, monkeypatch):
    async def items(*args):
        return [{"kind": "page"}]

    def apply_item(item):
        matches_any(runner.include, BAD_URL)

    monkeypatch.setattr(runner, "_read_items", items)
    monkeypatch.setattr(runner, "_apply_item", apply_item)
    try:
        await asyncio.wait_for(runner._follow(), timeout=0.5)
    except asyncio.TimeoutError:
        pytest.fail("Follower retried a URL pattern policy failure")
    assert_failed_with_pattern(runner)
    assert runner.rt.get("follower_offset", 0) == 0


def test_sampler_scope_mapping_and_runner_use_bounded_patterns(runner):
    from app.core.scope import crawl_patterns, in_scope
    from app.extraction.mapping import template_matches
    from app.extraction.sampling import link_groups

    page = Page(f'<main><a href="{BAD_URL}">Item</a></main>', "https://catalogue.example.test/")
    checks = [
        lambda: in_scope(crawl_patterns({"crawl_include": [BAD_PATTERN]}), BAD_URL),
        lambda: template_matches({"template_id": "item", "match": {"url_patterns": [BAD_PATTERN]}}, page, BAD_URL),
        lambda: link_groups(page, "catalogue.example.test", compile_patterns([BAD_PATTERN], "url_exclude")),
        lambda: runner._passes_filters(BAD_URL),
    ]
    for check in checks:
        with pytest.raises(UrlPatternError) as caught:
            check()
        assert caught.value.fields[0]["code"] == "pattern_timeout"


async def test_samples_http_timeout_is_actionable_and_next_request_responds(runner, monkeypatch):
    from fastapi import FastAPI
    from app.api import routes
    from app.api.http import ApiMiddleware
    from app import setup_ops
    from app.extraction.sampling import ProbePlan

    body = json.loads((CONTRACT_DIR / "examples/post_samples.request.json").read_text())
    body["config"].update(site_url="https://catalogue.example.test/", discovery_mode="crawl", fetch_mode="http", url_include=[BAD_PATTERN])
    body.pop("urls", None)
    body["config"]["sample_count"] = 1

    async def discover(*args, **kwargs):
        return ProbePlan([], limit=1, tail=[BAD_URL])

    async def fetched(*args, **kwargs):
        return Page("<main><h1>Item</h1></main>", BAD_URL), {}, None

    async def public(*args):
        return False

    monkeypatch.setattr(setup_ops, "_discover_candidates", discover)
    monkeypatch.setattr(setup_ops, "fetch_page", fetched)
    monkeypatch.setattr(setup_ops, "url_is_private", public)
    monkeypatch.setattr(routes, "get_service", lambda: runner.svc)
    monkeypatch.setattr("app.fetching.browsers.browser_presence", lambda: {
        "chromium": {"present": False, "version": None}, "camoufox": {"present": False, "version": None}
    })
    app = FastAPI()
    app.add_middleware(ApiMiddleware)
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://service.test", headers={"Authorization": "Bearer t"}) as client:
        response = await asyncio.wait_for(client.post("/v1/samples", json=body), timeout=2)
        assert response.status_code == 422, response.text
        error = response.json()
        assert error["error"]["fields"][0]["code"] == "pattern_timeout"
        assert "url_include[0]" in error["error"]["message"]
        assert not list(validator("error.schema.json").iter_errors(error))
        health = await asyncio.wait_for(client.get("/health"), timeout=2)
        assert health.status_code == 200


async def test_enqueue_rejected_urls_share_sync_budget_and_reset_before_dns(runner, monkeypatch):
    from dataclasses import replace
    from app.core import url_patterns as policy

    clock = [0.0]
    monkeypatch.setattr(policy, "_clock", lambda: clock[0])
    monkeypatch.setattr(policy, "BATCH_SECONDS", 0.05)
    pattern = compile_patterns([r"/keep/"], "url_include")[0]
    original = pattern._compiled

    class TimedSearch:
        def search(self, *args, **kwargs):
            result = original.search(*args, **kwargs)
            clock[0] += 0.015
            return result

    runner.include = [replace(pattern, _compiled=TimedSearch())]
    runner.exclude = []
    entries = [{"url": f"https://catalogue.example.test/skip/{i}", "source": "sitemap"} for i in range(4)]
    with pytest.raises(UrlPatternError):
        await runner.enqueue(entries)

    calls = []
    progress = []
    async def sibling():
        progress.append("scheduled")

    sibling_task = asyncio.create_task(sibling())
    async def private(url):
        # No regex context may leak into a DNS/network await.
        assert policy._batch_budget.get() is None
        assert progress, "cached DNS must not reset the batch without yielding to another task"
        clock[0] += 10
        calls.append(url)
        return True

    monkeypatch.setattr("app.jobs.runner.url_is_private", private)
    await runner.enqueue(entries[:2] + [{"url": "https://catalogue.example.test/keep/1", "source": "sitemap"}] + entries[2:])
    await sibling_task
    assert len(calls) == 1


def timed_policy(monkeypatch, pattern=r"/item/"):
    """Use the real engine with deterministic matching CPU accounting."""
    from app.core import url_patterns as policy

    clock = [0.0]
    original_compile = policy._compile
    monkeypatch.setattr(policy, "_clock", lambda: clock[0])
    monkeypatch.setattr(policy, "BATCH_SECONDS", 0.035)

    class TimedSearch:
        def __init__(self, compiled):
            self.compiled = compiled

        def search(self, *args, **kwargs):
            result = self.compiled.search(*args, **kwargs)
            clock[0] += 0.015
            return result

    monkeypatch.setattr(policy, "_compile", lambda raw, flags: TimedSearch(original_compile(raw, flags)))
    return policy.compile_patterns([pattern], "url_include")


async def test_backlog_prioritization_shares_one_sync_matching_budget(runner, monkeypatch):
    from app.core.urls import site_key

    patterns = timed_policy(monkeypatch)
    runner._target_patterns[site_key("https://catalogue.example.test/")] = patterns
    runner.crawler_task = SimpleNamespace(done=lambda: False)
    for index in range(4):
        runner.svc.db.execute(
            "INSERT INTO link_backlog(run_id,url,source,forefront) VALUES (?,?,?,0)",
            (runner.run_id, f"https://catalogue.example.test/item/{index}", "link"),
        )
    enqueued = []
    async def enqueue(entries, **kwargs):
        enqueued.extend(entries)
        runner._shutdown = True

    monkeypatch.setattr(runner, "enqueue", enqueue)
    with pytest.raises(UrlPatternError):
        await runner._drain_backlog()
    assert not enqueued
    assert len(runner.svc.db.all("SELECT seq FROM link_backlog WHERE run_id=?", (runner.run_id,))) == 4


async def test_reextraction_shares_budget_and_rolls_back_all_page_updates(runner, monkeypatch):
    from app.core import url_patterns as policy
    from app.core.db import loads

    timed_policy(monkeypatch, r"/different-section/")
    mapping = {"template_id": "item", "match": {"url_patterns": [r"/different-section/"]}}
    monkeypatch.setattr(runner, "mappings_for", lambda site: [mapping])
    read = []
    async def html(key):
        assert policy._batch_budget.get() is None  # storage awaits precede the bounded transaction
        read.append(key)
        return "<main><h1>Item</h1></main>"

    runner.kvs = SimpleNamespace(get_value=html)
    for index in range(4):
        url = f"https://catalogue.example.test/item/{index}"
        doc = {"url": url, "kvs_key": f"page-{index}", "needs_reextract": True}
        runner.svc.db.execute(
            "INSERT INTO stored_pages(run_id,page_key,url,template_id,page_class,has_html,doc) VALUES (?,?,?,?,?,1,?)",
            (runner.run_id, f"page-{index}", url, "item", "no_template", dumps(doc)),
        )
    with pytest.raises(UrlPatternError):
        await runner._reextract_some()
    assert len(read) == 4
    rows = runner.svc.db.all("SELECT doc FROM stored_pages WHERE run_id=?", (runner.run_id,))
    assert all(loads(row["doc"])["needs_reextract"] for row in rows)
