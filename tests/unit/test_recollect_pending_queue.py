"""Failed-run recovery includes work already admitted before a source cursor commit."""
import asyncio
import json

import pytest

from app.core.db import dumps
from app.core.errors import ApiError
from app.core.schemas import CONTRACT_DIR
from app.fetching.oneoff import Fetched
from app.jobs.runner import JobRunner, page_key, write_unextracted
from app.service import Service
from tests.conftest import platform, profile
from tests.unit.test_logic import settings

SITE = "https://catalogue.example.test"
PENDING = SITE + "/selected/item-2"
LISTING = SITE + "/catalogue/category-2"
DONE = SITE + "/selected/item-1"
BACKLOG = SITE + "/selected/item-3"


@pytest.fixture
def parent(tmp_path, monkeypatch):
    svc = Service(settings(data_dir=str(tmp_path), secrets_key="test-secret-key-0123456789", robots_override_allowed=True))
    monkeypatch.setattr("app.settings._settings", svc.settings)
    monkeypatch.setattr(svc.jobs, "wake", lambda: None)
    doc = json.loads((CONTRACT_DIR / "examples/get_run.response.json").read_text())
    doc.update(state="failed", result=None, outcome=None, stop_reason=None)
    doc["config"].update(site_url=SITE + "/catalogue/", discovery_mode="crawl", fetch_mode="http", single_page=False,
                         url_include=[r"/selected/item-[0-9]+$"], target_page_selector=None, use_proxy_tiers=[],
                         challenge_click=False, use_solver=False, respect_robots=False, required_fields=["title"], fields=["title"])
    doc["resolved"]["discovery_mode"] = "crawl"
    rid = doc["run_id"]
    svc.db.execute("INSERT INTO runs(run_id,idempotency_key,fingerprint,state,site,created_at,updated_at,doc,runtime) VALUES (?,?,?,?,?,?,?,?,?)",
                   (rid, "failed-parent", "f", "failed", "catalogue.example.test", doc["created_at"], doc["updated_at"], dumps(doc), "{}"))
    for url, key, source, status in ((DONE, None, "link", "done"), (PENDING, "a", "sitemap", "queued"),
                                     (PENDING, "b", "sitemap", "queued"), (LISTING, None, "link", "queued")):
        svc.db.execute("INSERT INTO urls(run_id,unique_key,url,source,source_key,status) VALUES (?,?,?,?,?,?)",
                       (rid, page_key(url, key), url, source, key, status))
    for url in (DONE, BACKLOG):
        svc.db.execute("INSERT INTO link_backlog(run_id,url,source,unique_key) VALUES (?,?,?,?)", (rid, url, "link", page_key(url, None)))
    yield svc, rid
    svc.db.close()


async def test_recollect_preserves_pending_keys_and_navigation_without_replaying_done(parent, monkeypatch):
    svc, pid = parent
    original = dict(svc.jobs._row(pid))
    status, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("queued-child")}, "queued-child")
    assert status == 201
    assert child["recollect_of"]["url_count"] == 4
    assert child["recollect_of"]["by_reason"] == {"stopped": 4}
    assert child["config"]["discovery_mode"] == "crawl"
    runner = JobRunner(svc, child["run_id"])
    class Queue:
        async def add_requests(self, requests, **kwargs):
            pass
    runner.rq = Queue()
    async def public(url):
        return False
    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    await runner._feed()
    queued = svc.db.all("SELECT url,source_key,source FROM urls WHERE run_id=?", (child["run_id"],))
    assert {(r["url"], r["source_key"], r["source"]) for r in queued} == {
        (PENDING, "a", "sitemap"), (PENDING, "b", "sitemap"), (LISTING, None, "link"), (BACKLOG, None, "link")}
    assert runner._outside_include(LISTING, {"src": "link"})
    assert dict(svc.jobs._row(pid)) == original
    again_status, again = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("queued-child")}, "queued-child")
    assert again_status == 200 and again["run_id"] == child["run_id"]


async def test_explicit_reasons_can_exclude_unfinished_queue(parent):
    svc, pid = parent
    with pytest.raises(ApiError) as caught:
        await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("filtered-child"), "reasons": ["fetch_failed"]}, "filtered-child")
    assert caught.value.code == "nothing_to_recollect"


async def test_combined_source_retry_retains_category_crawl(parent):
    svc, pid = parent
    row = svc.jobs._row(pid)
    doc = json.loads(row["doc"])
    doc["config"]["discovery_mode"] = doc["resolved"]["discovery_mode"] = "sitemap_and_crawl"
    root, leaf = SITE + "/sitemap.xml", SITE + "/product-pages.xml"
    runtime = {"sitemaps": [root], "sitemap_traversal": {"sources": {
        root: {"status": "complete", "page_offset": 0, "nested_offset": 1},
        leaf: {"status": "failed", "reason": "fetch_failed", "page_offset": 0, "nested_offset": 0}},
        "root_offset": 1, "entries_seen": 10}}
    svc.db.execute("UPDATE runs SET doc=?,runtime=? WHERE run_id=?", (dumps(doc), dumps(runtime), pid))
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("source-child")}, "source-child")
    assert child["recollect_of"]["url_count"] == 4
    assert child["config"]["discovery_mode"] == "sitemap_and_crawl"


async def test_new_links_skip_completed_ancestor_keys_but_explicit_retries_do_not(parent, monkeypatch):
    svc, pid = parent
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("links-child")}, "links-child")
    runner = JobRunner(svc, child["run_id"])
    class Queue:
        async def add_requests(self, requests, **kwargs):
            pass
    runner.rq = Queue()
    async def public(url):
        return False
    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    await runner.enqueue([{"url": DONE, "source": "link"}, {"url": DONE, "source": "link", "source_key": "new-key"}])
    rows = svc.db.all("SELECT source_key FROM urls WHERE run_id=?", (child["run_id"],))
    assert [r["source_key"] for r in rows] == ["new-key"]
    await runner.enqueue([{"url": DONE, "source": "list"}])
    assert svc.db.one("SELECT source FROM urls WHERE run_id=? AND source_key IS NULL", (child["run_id"],))["source"] == "list"


async def test_pending_refetch_backlog_survives_completed_original(parent):
    svc, pid = parent
    svc.db.execute("INSERT INTO link_backlog(run_id,url,source,unique_key) VALUES (?,?,?,?)",
                   (pid, DONE, "refetch", page_key(DONE, None) + "\\x1frefetch"))
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("refetch-child")}, "refetch-child")
    assert child["recollect_of"]["url_count"] == 5
    rt = json.loads(svc.jobs._row(child["run_id"])["runtime"])
    assert any(e["url"] == DONE and e["source"] == "list" for e in rt["recollect_pending"])


async def test_admitted_refetch_is_recovered_from_stored_page_pending_flag(parent):
    svc, pid = parent
    svc.db.execute("INSERT INTO stored_pages(run_id,page_key,url,template_id,page_class,has_html,doc) VALUES (?,?,?,?,?,?,?)",
                   (pid, page_key(DONE, None), DONE, "product", "incomplete", 0,
                    dumps({"url": DONE, "requested_url": DONE, "source_key": None, "refetch_pending": True, "needs_reextract": False})))
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("admitted-refetch-child")}, "admitted-refetch-child")
    assert child["recollect_of"]["url_count"] == 5
    rt = json.loads(svc.jobs._row(child["run_id"])["runtime"])
    assert any(e["url"] == DONE and e["source"] == "list" for e in rt["recollect_pending"])


async def test_recollect_reused_key_rejects_changed_private_recovery_intent(parent):
    svc, pid = parent
    write_unextracted(svc.db, pid, SITE + "/selected/item-4", SITE + "/selected/item-4", None,
                      "catalogue.example.test", "fetch_failed", None, 500, 1)
    body = {"profile": profile(), "platform": platform("intent-child"), "reasons": ["stopped"]}
    await svc.jobs.recollect(pid, body, "intent-child")
    with pytest.raises(ApiError) as caught:
        await svc.jobs.recollect(pid, dict(body, reasons=["stopped", "fetch_failed"]), "intent-child")
    assert caught.value.code == "idempotency_key_reused"


async def test_concurrent_recollect_keys_include_recovery_intent(parent, monkeypatch):
    svc, pid = parent
    write_unextracted(svc.db, pid, SITE + "/selected/item-4", SITE + "/selected/item-4", None,
                      "catalogue.example.test", "fetch_failed", None, 500, 1)
    create, gate, reached = svc.jobs.create, asyncio.Event(), 0
    async def together(*args, **kwargs):
        nonlocal reached
        reached += 1
        if reached == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 2)
        return await create(*args, **kwargs)
    monkeypatch.setattr(svc.jobs, "create", together)
    body = {"profile": profile(), "platform": platform("concurrent-intent-child"), "reasons": ["stopped"]}
    results = await asyncio.gather(svc.jobs.recollect(pid, body, "concurrent-intent-child"),
                                   svc.jobs.recollect(pid, dict(body, reasons=["stopped", "fetch_failed"]), "concurrent-intent-child"),
                                   return_exceptions=True)
    assert sum(isinstance(r, ApiError) and r.code == "idempotency_key_reused" for r in results) == 1
    assert sum(isinstance(r, tuple) and r[0] == 201 for r in results) == 1


async def test_sitemap_retry_does_not_drop_another_source_key(parent, monkeypatch):
    svc, pid = parent
    svc.db.execute("UPDATE urls SET source_key='a',unique_key=? WHERE run_id=? AND url=?", (page_key(DONE, "a"), pid, DONE))
    svc.db.execute("DELETE FROM link_backlog WHERE run_id=? AND url=?", (pid, DONE))
    row = svc.jobs._row(pid)
    doc = json.loads(row["doc"])
    doc["config"]["discovery_mode"] = doc["resolved"]["discovery_mode"] = "sitemap"
    root = SITE + "/sitemap.xml"
    rt = {"sitemaps": [root], "sitemap_traversal": {"sources": {
        root: {"status": "failed", "reason": "fetch_failed", "page_offset": 0, "nested_offset": 0}}, "root_offset": 1, "entries_seen": 0}}
    svc.db.execute("UPDATE runs SET doc=?,runtime=? WHERE run_id=?", (dumps(doc), dumps(rt), pid))
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("sitemap-key-child")}, "sitemap-key-child")
    runner = JobRunner(svc, child["run_id"])
    class Queue:
        async def add_requests(self, requests, **kwargs):
            pass
    runner.rq = Queue()
    async def fetched(url):
        assert url == root
        return Fetched(url, url, 200, f'<urlset><url><loc>{DONE}</loc></url></urlset>'.encode())
    async def public(url):
        return False
    monkeypatch.setattr(runner, "aux_fetch", fetched)
    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    await runner._feed_sitemaps()
    admitted = svc.db.all("SELECT url,source_key FROM urls WHERE run_id=?", (child["run_id"],))
    assert [(r["url"], r["source_key"]) for r in admitted] == [(DONE, None)]


async def test_recovery_seed_keeps_the_configured_url_budget(parent):
    svc, pid = parent
    svc.settings.limits["max_url_list_entries"] = 3
    with pytest.raises(ApiError) as caught:
        await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("limited-child")}, "limited-child")
    assert caught.value.code == "validation_failed"
    assert caught.value.fields[0]["code"] == "limit_exceeded"
    assert svc.db.scalar("SELECT COUNT(*) FROM runs") == 1


async def test_recovery_replay_retains_url_guards(parent, monkeypatch):
    svc, pid = parent
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("guarded-child")}, "guarded-child")
    runner = JobRunner(svc, child["run_id"])
    class Queue:
        async def add_requests(self, requests, **kwargs):
            pass
    runner.rq = Queue()
    async def changed_to_private(url):
        return url == PENDING
    monkeypatch.setattr("app.jobs.runner.url_is_private", changed_to_private)
    await runner._feed()
    queued = svc.db.all("SELECT url FROM urls WHERE run_id=? AND status='queued'", (child["run_id"],))
    assert {r["url"] for r in queued} == {LISTING, BACKLOG}
    assert runner.counters()["urls_skipped"]["private_address"] == 2


async def test_same_recovery_intent_replays_legacy_fingerprint_and_rotated_profile(parent):
    svc, pid = parent
    body = {"profile": profile(), "platform": platform("legacy-child")}
    _, first = await svc.jobs.recollect(pid, body, "legacy-child")
    svc.db.execute("UPDATE runs SET fingerprint='legacy-fingerprint' WHERE run_id=?", (first["run_id"],))
    status, again = await svc.jobs.recollect(pid, dict(body, profile=profile("rotated")), "legacy-child")
    assert status == 200 and again["run_id"] == first["run_id"]


@pytest.mark.parametrize("fed,queued_prefix", [(0, False), (100, False), (100, True)])
async def test_failed_recovery_preserves_unadmitted_seed_suffix(parent, monkeypatch, fed, queued_prefix):
    svc, pid = parent
    for i in range(10, 256):
        url, key = SITE + f"/selected/item-{i}", str(i)
        svc.db.execute("INSERT INTO urls(run_id,unique_key,url,source,source_key,status) VALUES (?,?,?,?,?,?)",
                       (pid, page_key(url, key), url, "sitemap", key, "queued"))
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("interrupted-child")}, "interrupted-child")
    row = svc.jobs._row(child["run_id"])
    doc, rt = json.loads(row["doc"]), json.loads(row["runtime"])
    seed = rt["recollect_pending"]
    assert len(seed) == 250
    for i, entry in enumerate(seed[:fed]):
        svc.db.execute("INSERT INTO urls(run_id,unique_key,url,source,source_key,status) VALUES (?,?,?,?,?,?)",
                       (child["run_id"], page_key(entry["url"], entry.get("source_key")), entry["url"], entry["source"],
                        entry.get("source_key"), "queued" if queued_prefix and i == fed - 1 else "done"))
    rt["recollect_pending_fed"] = fed
    doc["state"] = "failed"
    svc.db.execute("UPDATE runs SET state='failed',doc=?,runtime=? WHERE run_id=?", (dumps(doc), dumps(rt), child["run_id"]))
    original = dict(svc.jobs._row(child["run_id"]))
    _, grandchild = await svc.jobs.recollect(child["run_id"], {"profile": profile(), "platform": platform("grandchild")}, "grandchild")
    expected = seed[fed:] + ([seed[fed - 1]] if queued_prefix else [])
    assert grandchild["recollect_of"]["url_count"] == len(expected)
    assert grandchild["config"]["discovery_mode"] == "crawl"
    runner = JobRunner(svc, grandchild["run_id"])

    class Queue:
        async def add_requests(self, requests, **kwargs):
            pass

    async def public(url):
        return False

    runner.rq = Queue()
    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    await runner._feed()
    queued = svc.db.all("SELECT url,source,source_key FROM urls WHERE run_id=?", (grandchild["run_id"],))
    assert {(r["url"], r["source"], r["source_key"]) for r in queued} == {
        (e["url"], e["source"], e.get("source_key")) for e in expected}
    assert dict(svc.jobs._row(child["run_id"])) == original


async def test_unadmitted_recovery_seed_keeps_url_budget(parent):
    svc, pid = parent
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("unfed-child")}, "unfed-child")
    svc.db.execute("UPDATE runs SET state='failed' WHERE run_id=?", (child["run_id"],))
    svc.settings.limits["max_url_list_entries"] = 3
    with pytest.raises(ApiError) as caught:
        await svc.jobs.recollect(child["run_id"], {"profile": profile(), "platform": platform("limited-grandchild")}, "limited-grandchild")
    assert caught.value.code == "validation_failed"
    assert caught.value.fields[0]["code"] == "limit_exceeded"
    assert svc.db.scalar("SELECT COUNT(*) FROM runs") == 2


async def test_explicit_reasons_can_exclude_unadmitted_recovery_seed(parent):
    svc, pid = parent
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("unfed-child")}, "unfed-child")
    svc.db.execute("UPDATE runs SET state='failed' WHERE run_id=?", (child["run_id"],))
    with pytest.raises(ApiError) as caught:
        await svc.jobs.recollect(child["run_id"], {"profile": profile(), "platform": platform("filtered-grandchild"),
                                                "reasons": ["fetch_failed"]}, "filtered-grandchild")
    assert caught.value.code == "nothing_to_recollect"


@pytest.mark.parametrize("lane", ["url_list", "sitemap_retry"])
async def test_failed_feeder_preserves_other_unadmitted_inputs(parent, monkeypatch, lane):
    svc, pid = parent
    svc.db.execute("DELETE FROM urls WHERE run_id=?", (pid,))
    svc.db.execute("DELETE FROM link_backlog WHERE run_id=?", (pid,))
    doc = json.loads(svc.jobs._row(pid)["doc"])
    entries = [{"url": SITE + f"/selected/item-{2000+i}", "key": f"{i:04d}"} for i in range(250)]
    root, leaf = SITE + "/sitemap.xml", SITE + "/other-pages.xml"
    if lane == "url_list":
        raw = entries.copy()
        raw.insert(150, dict(entries[0], url=entries[0]["url"].replace("catalogue.example.test", "CATALOGUE.EXAMPLE.TEST")))
        raw.insert(1, entries[0].copy())
        doc["config"].update(discovery_mode="url_list", urls=raw)
        rt = {"list_fed": 100}
    else:
        doc["config"].update(discovery_mode="sitemap", urls=[], sitemap_url=root)
        rt = {"sitemap_retry_urls": entries, "sitemap_retry_fed": 100, "sitemap_retry_roots": [root],
              "sitemap_traversal": {"sources": {root: {"status": "complete", "page_offset": 0, "nested_offset": 1},
                                                 leaf: {"status": "pending", "page_offset": 0, "nested_offset": 0}},
                                    "root_offset": 1, "entries_seen": 0}}
    doc["resolved"]["discovery_mode"] = doc["config"]["discovery_mode"]
    svc.db.execute("UPDATE runs SET doc=?,runtime=? WHERE run_id=?", (dumps(doc), dumps(rt), pid))
    for e in entries[:100]:
        svc.db.execute("INSERT INTO urls(run_id,unique_key,url,source,source_key,status) VALUES (?,?,?,?,?,'done')",
                       (pid, page_key(e["url"], e["key"]), e["url"], "list", e["key"]))
    original = dict(svc.jobs._row(pid))
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("input-child")}, "input-child")
    assert child["recollect_of"]["url_count"] == 150
    if lane == "sitemap_retry":
        assert child["recollect_of"]["source_count"] == 1
    runner = JobRunner(svc, child["run_id"])
    runner.doc["resolved"]["discovery_mode"] = child["config"]["discovery_mode"]
    requests = []

    class Queue:
        async def add_requests(self, batch, **kwargs):
            requests.extend(batch)

    async def public(url):
        return False

    async def empty_source(url):
        assert url in (root, leaf)
        return Fetched(url, url, 200, b"<urlset/>")

    runner.rq = Queue()
    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    monkeypatch.setattr("app.jobs.discovery.url_is_private", public)
    monkeypatch.setattr(runner, "aux_fetch", empty_source)
    await runner._feed()
    queued = svc.db.all("SELECT url,source,source_key FROM urls WHERE run_id=?", (child["run_id"],))
    assert {(x["url"], x["source"], x["source_key"]) for x in queued} == {
        (e["url"], "list", e["key"]) for e in entries[100:]}
    assert len(requests) == 150
    assert runner.counters()["urls_skipped"]["duplicate"] == 0
    assert dict(svc.jobs._row(pid)) == original


@pytest.mark.parametrize("explicit_failure", [False, True])
async def test_overlapping_seed_and_url_list_keep_completed_work_skipped(parent, explicit_failure):
    svc, pid = parent
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("overlap-child")}, "overlap-child")
    row = svc.jobs._row(child["run_id"])
    doc, rt = json.loads(row["doc"]), json.loads(row["runtime"])
    seed = rt["recollect_pending"]
    doc["config"].update(discovery_mode="url_list", urls=[{"url": e["url"], "key": e.get("source_key")} for e in seed])
    doc["resolved"]["discovery_mode"] = "url_list"
    rt.update(recollect_pending_fed=len(seed), list_fed=0)
    for e in seed:
        svc.db.execute("INSERT INTO urls(run_id,unique_key,url,source,source_key,status) VALUES (?,?,?,?,?,'done')",
                       (child["run_id"], page_key(e["url"], e.get("source_key")), e["url"], e["source"], e.get("source_key")))
    svc.db.execute("UPDATE runs SET state='failed',doc=?,runtime=? WHERE run_id=?", (dumps(doc), dumps(rt), child["run_id"]))
    if explicit_failure:
        write_unextracted(svc.db, child["run_id"], PENDING, PENDING, "a", "catalogue.example.test", "fetch_failed", None, 500, 1)
        _, grandchild = await svc.jobs.recollect(child["run_id"], {"profile": profile(), "platform": platform("overlap-grandchild")}, "overlap-grandchild")
        assert grandchild["recollect_of"]["url_count"] == 1
        assert grandchild["config"]["urls"] == [{"url": PENDING, "key": "a"}]
    else:
        with pytest.raises(ApiError) as caught:
            await svc.jobs.recollect(child["run_id"], {"profile": profile(), "platform": platform("overlap-grandchild")}, "overlap-grandchild")
        assert caught.value.code == "nothing_to_recollect"


async def test_feeder_failure_preserves_follower_backlog_for_recovery(parent, monkeypatch):
    """Real task lifecycle: admission spans a feeder failure before supervisor teardown."""
    from app.core.url_patterns import UrlPatternError

    svc, pid = parent
    _, child = await svc.jobs.recollect(pid, {"profile": profile(), "platform": platform("racing-child")}, "racing-child")
    runner = JobRunner(svc, child["run_id"])
    admission_started, failed, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    for i in range(501):
        url = SITE + f"/selected/item-{1000+i}"
        svc.db.execute("INSERT INTO link_backlog(run_id,url,source,unique_key,forefront) VALUES (?,?, 'link',?,0)",
                       (runner.run_id, url, page_key(url, None)))

    class Queue:
        async def add_requests(self, requests, **kwargs):
            admission_started.set()
            await failed.wait()

    class Crawler:
        async def run(self, requests):
            await failed.wait()

        def stop(self, *args):
            failed.set()

    async def prepare():
        runner.doc["state"] = "running"
        runner.rt["discovery_resolved"] = True
        runner._persist()

    async def empty_items(offset, limit):
        return []

    async def public(url):
        return False

    enqueue, drain = runner.enqueue, runner._drain_backlog

    async def controlled_enqueue(entries, **kwargs):
        if kwargs.get("feed", (None,))[0] == "recollect_pending_fed":
            await admission_started.wait()
            raise UrlPatternError("url_include", "fixture", "pattern_timeout")
        return await enqueue(entries, **kwargs)

    async def observed_drain():
        await drain()
        if runner.state == "failed":
            drained.set()

    runner.rq = Queue()
    monkeypatch.setattr(runner, "_prepare", prepare)
    monkeypatch.setattr(runner, "_read_items", empty_items)
    monkeypatch.setattr(runner, "_build_crawler", Crawler)
    monkeypatch.setattr(runner, "enqueue", controlled_enqueue)
    monkeypatch.setattr(runner, "_drain_backlog", observed_drain)
    monkeypatch.setattr(svc.settings, "supervisor_interval_seconds", 5.0)
    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    runner.task = asyncio.create_task(runner._main())
    svc.jobs.runners[runner.run_id] = runner
    try:
        await asyncio.wait_for(drained.wait(), 3)
        assert runner.state == "failed"
        assert svc.db.scalar("SELECT COUNT(*) FROM link_backlog WHERE run_id=?", (runner.run_id,)) == 1
    finally:
        runner.task.cancel()
        await asyncio.gather(runner.task, return_exceptions=True)
    assert all(t is None or t.done() for t in (runner.feeder_task, runner.follower_task, runner.crawler_task))
    original = dict(svc.jobs._row(runner.run_id))
    _, grandchild = await svc.jobs.recollect(runner.run_id, {"profile": profile(), "platform": platform("racing-grandchild")}, "racing-grandchild")
    assert grandchild["recollect_of"]["url_count"] == 505  # 500 queued + 1 backlog + 4 seed entries
    assert dict(svc.jobs._row(runner.run_id)) == original
