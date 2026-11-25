"""Declared sitemap source selection keeps raw scan accounting and guarded traversal."""
import copy

import pytest

from app.core.config import apply_rules, merge_layers
from app.core.schemas import schema_errors
from app.jobs import discovery
from tests.unit.test_logic import settings, BROWSERS
from tests.unit.test_sitemap_completeness import ROOT, A, B, collect, fake_runner, index, urlset


@pytest.fixture(autouse=True)
def public(monkeypatch):
    async def allowed(url):
        return False
    monkeypatch.setattr(discovery, "url_is_private", allowed)
    monkeypatch.setattr("app.core.config.url_is_private", allowed)


async def test_declared_child_scope_skips_unrelated_sources_but_keeps_root():
    product = "https://supplier.example/product/1"
    bodies = {ROOT: index(A, B), A: urlset(product), B: RuntimeError("excluded source fetched")}
    urls, state = await collect(bodies, source_include=[r"/a\.xml$"], source_exclude=[r"/sitemap\.xml$"])
    assert urls == [product]
    report = discovery.sitemap_report(state)
    assert report["complete"] is True
    assert report["scope"] == "configured_sources"
    assert report["excluded_source_count"] == 1
    assert report["excluded_sources"] == [B]
    assert report["entries_seen"] == 1
    assert B not in state["sources"]
    assert not schema_errors("run_summary.schema.json#/$defs/sitemap_discovery", report)


async def test_source_exclude_wins_and_intermediate_indexes_must_match():
    nested = "https://supplier.example/nested.xml"
    bodies = {ROOT: index(nested, A), nested: index(B), A: urlset("https://supplier.example/allowed"), B: urlset("https://supplier.example/hidden")}
    urls, state = await collect(bodies, source_include=[r"/(?:a|b)\.xml$"], source_exclude=[r"/a\.xml$"])
    assert urls == []
    report = discovery.sitemap_report(state)
    assert report["excluded_source_count"] == 2
    assert report["complete"] is True


async def test_selected_products_after_irrelevant_locations_need_explicit_raw_budget():
    runner = fake_runner({ROOT: index(A, B), A: urlset(*[f"https://supplier.example/other/{i}" for i in range(6)], "https://supplier.example/product/1"), B: urlset("https://supplier.example/product/2")})
    runner.config.update(max_sitemap_entries=8, sitemap_source_include=[r"/[ab]\.xml$"], url_include=[r"/product/"])
    products = []
    async def enqueue(entries, **kwargs):
        products.extend(e["url"] for e in entries if "/product/" in e["url"])
    runner.enqueue = enqueue
    await runner.feed()
    assert products == ["https://supplier.example/product/1", "https://supplier.example/product/2"]
    assert runner.doc["sitemap_discovery"]["entries_seen"] == 8
    assert runner.doc["sitemap_discovery"]["complete"] is True
    assert not runner.rt["sitemap_partial"]

    limited = fake_runner({ROOT: urlset(*[f"https://supplier.example/other/{i}" for i in range(6)], "https://supplier.example/product/1")})
    limited.config["max_sitemap_entries"] = 6
    limited.enqueue = enqueue
    await limited.feed()
    assert limited.doc["sitemap_discovery"]["entries_seen"] == 6
    assert limited.rt["sitemap_partial"]
    assert limited.rt["sitemap_traversal"]["sources"][ROOT]["page_offset"] == 6
    snapshot = copy.deepcopy(limited.rt)
    await limited.feed()
    assert limited.rt == snapshot


async def test_config_preserves_explicit_budget_and_service_ceiling():
    cfg, _ = merge_layers({"site_url": "https://supplier.example/", "max_sitemap_entries": 900000, "sitemap_source_include": [r"/products_\d+\.xml$"]}, {}, {})
    assert cfg["max_sitemap_entries"] == 900000
    assert cfg["sitemap_source_include"] == [r"/products_\d+\.xml$"]
    _, errors, _ = await apply_rules(cfg, {}, settings(), BROWSERS)
    assert not errors
    svc = settings()
    svc.limits["max_sitemap_entries"] = 800000
    _, errors, _ = await apply_rules(cfg, {}, svc, BROWSERS)
    assert any(e["field"] == "max_sitemap_entries" and e["limit"] == 800000 for e in errors)
    cfg["sitemap_source_exclude"] = ["("]
    _, errors, _ = await apply_rules(cfg, {}, svc, BROWSERS)
    assert any(e["field"] == "sitemap_source_exclude[0]" and e["code"] == "pattern_invalid" for e in errors)
    assert merge_layers({}, {}, {})[0]["max_sitemap_entries"] == 100000


async def test_complete_feeder_supersedes_limited_probe_for_outcome():
    runner = fake_runner({ROOT: urlset("https://supplier.example/product/1")})
    runner.rt.update(sitemap_probe_partial=True, sitemap_probe={"complete": False, "capped": True, "cap_reason": "entry_limit"})
    async def enqueue(entries, **kwargs):
        pass
    runner.enqueue = enqueue
    await runner.feed()
    assert runner.doc["sitemap_discovery"]["complete"] is True
    assert not runner.rt["sitemap_partial"]
    assert runner.rt["sitemap_probe"]["capped"] is True


async def test_excluded_sources_do_not_spend_source_file_budget_and_samples_are_bounded():
    rejected = [f"https://user:secret@supplier.example/skip-{i}.xml?token=secret" for i in range(30)]
    urls, state = await collect({ROOT: index(*rejected, A), A: urlset("https://supplier.example/product/1")}, max_files=2, source_include=[r"/a\.xml$"])
    assert urls == ["https://supplier.example/product/1"]
    report = discovery.sitemap_report(state)
    assert report["source_files"] == 2 and report["complete"]
    assert report["excluded_source_count"] == 30
    assert len(report["excluded_sources"]) == 20
    assert "secret" not in str(report)
    assert not schema_errors("run_summary.schema.json#/$defs/sitemap_discovery", report)


async def test_included_private_child_still_never_reaches_fetch(monkeypatch):
    calls = []
    async def private(url):
        return url == A
    async def host_private(host):
        return True
    async def fetch(url):
        calls.append(url)
        return discovery.Fetched(url, url, 200, index(A))
    monkeypatch.setattr(discovery, "url_is_private", private)
    monkeypatch.setattr(discovery, "host_is_private", host_private)
    state = {}
    assert [u async for u, _ in discovery.iter_sitemaps([ROOT], fetch, state=state, source_include=[r"/a\.xml$"])] == []
    assert calls == [ROOT]
    assert discovery.sitemap_report(state)["refused_sources"] == [{"url": A, "reason": "private_address_refused"}]


async def test_pathological_source_filter_fails_with_its_field():
    from app.core.url_patterns import UrlPatternError
    bad = "https://supplier.example/" + "a" * 1950 + "!"
    with pytest.raises(UrlPatternError) as caught:
        await collect({ROOT: index(bad)}, source_include=[r"(a+)+$"])
    assert caught.value.fields[0]["field"] == "sitemap_source_include[0]"
    assert caught.value.fields[0]["code"] == "pattern_timeout"


@pytest.mark.parametrize("budget", [0, -1, 1000001, True])
def test_raw_budget_schema_refuses_unbounded_or_invalid_values(budget):
    assert schema_errors("job_config.schema.json#/$defs/job_settings", {"max_sitemap_entries": budget})


async def test_one_feeder_can_scan_beyond_default_only_with_explicit_finite_budget():
    irrelevant = [f"https://supplier.example/other/{i}" for i in range(100001)]
    body = urlset(*irrelevant, "https://supplier.example/product/last")
    products = []
    async def enqueue(entries, **kwargs):
        products.extend(e["url"] for e in entries if "/product/" in e["url"])
    default = fake_runner({ROOT: body})
    default.enqueue = enqueue
    await default.feed()
    assert products == []
    assert default.doc["sitemap_discovery"]["entries_seen"] == 100000
    assert not default.doc["sitemap_discovery"]["complete"]

    explicit = fake_runner({ROOT: body})
    explicit.config["max_sitemap_entries"] = 110000
    explicit.enqueue = enqueue
    await explicit.feed()
    assert products == ["https://supplier.example/product/last"]
    assert explicit.doc["sitemap_discovery"]["entries_seen"] == 100002
    assert explicit.doc["sitemap_discovery"]["complete"]
