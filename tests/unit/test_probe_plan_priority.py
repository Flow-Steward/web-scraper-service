"""Known targets must not starve behind a large colliding navigation bucket."""

import re

from app.extraction.sampling import ProbePlan


def test_discovered_targets_preempt_pending_intermediates_in_the_same_shape():
    origin = "https://catalogue.example.test"
    producers = [f"{origin}/makers/producer-{i}" for i in range(40)]
    products = {
        producer: [f"{origin}/items/entry-{i}-{j}" for j in range(5)]
        for i, producer in enumerate(producers)
    }
    preferred = [re.compile(r"/items/")]

    def colliding_bucket(urls):
        # Shape grouping is supplied input to ProbePlan. Explicitly model the
        # observed collision independently of how a particular site spells URLs.
        return [{"shape": "/*", "urls": urls, "links": len(urls), "in_menus": 0.0}]

    plan = ProbePlan(colliding_bucket(producers), limit=30)
    probed = []
    samples = []
    while (url := plan.next()) is not None:
        probed.append(url)
        if any(pattern.search(url) for pattern in preferred):
            samples.append(url)
            plan.feedback(url, True)
            if len(samples) == 5:
                break
        else:
            # Every intermediate reveals fresh targets, just as a large live
            # catalogue does; its group therefore never dries out by accident.
            plan.extend(colliding_bucket(products[url]), url, preferred=preferred)
            plan.feedback(url, False)

    assert samples == [
        origin + "/items/entry-0-0",
        origin + "/items/entry-0-1",
        origin + "/items/entry-0-2",
        origin + "/items/entry-0-3",
        origin + "/items/entry-0-4",
    ]
    assert probed == [producers[0], *samples]
    assert plan.tried == 6


async def test_samples_reach_targets_before_exhausting_a_large_colliding_catalogue(
    tmp_path, monkeypatch
):
    from app.core.config import merge_layers
    from app.extraction.page import Page
    from app.extraction.sampling import url_shape
    from app.fetching.oneoff import Fetched
    from app.service import Service
    from app.setup_ops import SetupFetch, _samples

    from tests.conftest import profile
    from tests.unit.test_logic import settings

    svc = Service(
        settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567")
    )
    monkeypatch.setattr("app.settings._settings", svc.settings)
    origin = "https://catalogue.example.test"
    producers = [
        f"/supplier-directory/maker-{chr(97 + i // 26)}{chr(97 + i % 26)}"
        for i in range(40)
    ]
    products = {
        producer: [
            f"/product-catalogue/item-{chr(97 + i // 26)}{chr(97 + i % 26)}-{chr(97 + j)}"
            for j in range(5)
        ]
        for i, producer in enumerate(producers)
    }
    assert url_shape(origin + producers[0]) == url_shape(
        origin + products[producers[0]][0]
    )
    config, _ = merge_layers(
        {
            "site_url": origin + "/catalogue",
            "discovery_mode": "crawl",
            "fetch_mode": "http",
            "target_page_selector": ".sheet",
            "fields": ["title"],
            "url_include": ["/product-catalogue/"],
        },
        {},
        {},
    )
    pages = {
        "/catalogue": "<main>"
        + "".join(f'<a href="{path}">Maker</a>' for path in producers)
        + "</main>"
    }
    for producer, entries in products.items():
        pages[producer] = (
            "<main>"
            + "".join(f'<a href="{path}">Item</a>' for path in entries)
            + "</main>"
        )
        for entry in entries:
            pages[entry] = '<main class="sheet"><h1>Catalogue item</h1></main>'
    loaded = []

    async def fetch(self, url):
        return Fetched(url, url, 200, pages[url.removeprefix(origin)].encode())

    async def fetch_page(_svc, url, _config, sf):
        loaded.append(url)
        sf.last_how = "http"
        return Page(pages[url.removeprefix(origin)], url, 200), {"mode": "http"}, None

    async def public(_url):
        return False

    monkeypatch.setattr(SetupFetch, "fetch", fetch)
    monkeypatch.setattr("app.setup_ops.fetch_page", fetch_page)
    monkeypatch.setattr("app.setup_ops.url_is_private", public)
    sf = SetupFetch(svc, config, profile())
    try:
        result = await _samples(svc, {"config": config, "urls": []}, config, 5, sf)

        assert {sample["url"] for sample in result["samples"]} == {
            origin + path for path in products[producers[0]]
        }
        assert loaded == [
            origin + producers[0],
            *(origin + path for path in products[producers[0]]),
        ]
    finally:
        await sf.close()
        svc.db.close()
