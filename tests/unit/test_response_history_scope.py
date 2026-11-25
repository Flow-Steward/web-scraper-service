"""Explicitly selected products and intermediate listings can share URL shapes."""
import json
from types import SimpleNamespace

from crawlee import Request
import pytest

from app.core.db import dumps
from app.core.schemas import CONTRACT_DIR
from app.extraction.sampling import url_shape
from app.jobs.runner import JobRunner
from app.service import Service
from tests.unit.test_logic import settings

ORIGIN = "https://catalogue.example.test"
LISTING = ORIGIN + "/lamps-12"
PRODUCT = ORIGIN + "/lamps-12/item-99"
BODY = '<!doctype html><html><body><h1 class="product">Lamp</h1>' + '<p>Long normal product description.</p>' * 300 + '</body></html>'
MAPPING = {"schema_version": 1, "template_id": "product", "__version": 1,
           "match": {"selector": "h1.product"}, "fields": [
               {"name": "title", "type": "text", "required": True,
                "sources": [{"kind": "css", "selector": "h1.product", "extract": "text"}]}]}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    svc = Service(settings(data_dir=str(tmp_path), secrets_key="test-secret-key-0123456789"))
    monkeypatch.setattr("app.settings._settings", svc.settings)
    doc = json.loads((CONTRACT_DIR / "examples/get_run.response.json").read_text())
    doc.update(state="running", finished_at=None, outcome=None, stop_reason=None, result=None)
    doc["config"].update(single_page=False, site_url=LISTING, fetch_mode="http", max_retries=0,
                         url_include=[r"/item-[0-9]+$"], target_page_selector=None,
                         crawl_include=[], required_fields=["title"])
    doc["resolved"]["discovery_mode"] = "crawl"
    svc.db.execute("INSERT INTO runs(run_id,idempotency_key,fingerprint,state,site,created_at,updated_at,doc,runtime) VALUES (?,?,?,?,?,?,?,?,?)",
                   (doc["run_id"], "response-history", "f", "running", doc["site"], doc["created_at"], doc["updated_at"], dumps(doc), "{}"))
    item = JobRunner(svc, doc["run_id"])
    item.link_rules = set()
    monkeypatch.setattr(item, "mappings_for", lambda site: [MAPPING])
    async def public(url):
        return False
    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    yield item
    svc.db.close()


async def classify(runner, url, html):
    request = Request.from_url(url, user_data={"src": "link"})
    return await runner._classify(SimpleNamespace(request=request, proxy_info=None), html, url, 200, False, request.user_data)


async def warm_products(runner):
    assert url_shape(PRODUCT) == url_shape(LISTING)
    for i in range(5):
        result = await classify(runner, ORIGIN + f"/lamps-12/item-{i}", BODY)
        assert result["page_class"] == "extracted"


@pytest.mark.parametrize("small", [False, True])
async def test_explicit_non_target_listing_keeps_discovered_links(runner, small):
    await warm_products(runner)
    listing = '<html><body><h1>Catalogue</h1><a href="/lamps-12/item-99">Item</a>' + ('' if small else '<p>Normal catalogue description.</p>' * 300) + '</body></html>'
    result = await classify(runner, LISTING, listing)
    assert result["page_class"] == "not_target"
    assert result["links"] == [PRODUCT]
    assert not result.get("soft_error")


async def test_navigation_history_does_not_disable_selected_product_lost_marks(runner):
    await warm_products(runner)
    listing = BODY.replace('class="product"', 'class="listing"')
    result = await classify(runner, LISTING, listing)
    assert result["page_class"] == "not_target"
    result = await classify(runner, PRODUCT, listing)
    assert result["page_class"] == "fetch_failed"
    assert result["soft_error"] == "lost_template"


async def test_selected_url_missing_its_required_selector_still_retries(runner):
    await warm_products(runner)
    runner.config["target_page_selector"] = "h1.product"
    result = await classify(runner, PRODUCT, BODY.replace('class="product"', 'class="listing"'))
    assert result["soft_error"] == "lost_template"


async def test_genuinely_empty_excluded_listing_is_still_rejected(runner):
    await warm_products(runner)
    result = await classify(runner, LISTING, "")
    assert result["page_class"] == "fetch_failed"
    assert result["soft_error"] == "empty"
