"""Terminate the actual browser between auxiliary and product setup requests."""

import pytest

from app.core.config import merge_layers
from app.core.errors import ApiError
from app.service import Service
from app.setup_ops import SetupFetch, _samples
from tests.conftest import PLAIN, profile
from tests.unit.test_logic import settings


@pytest.mark.parametrize(
    "closure_phase", ["before_navigation", "final_read", "collector"]
)
async def test_actual_browser_termination_survives_sampling_fallback(
    tmp_path, monkeypatch, fixture_world, closure_phase
):
    svc = Service(
        settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567")
    )
    monkeypatch.setattr("app.settings._settings", svc.settings)

    async def no_external_geo(*args, **kwargs):
        return None

    monkeypatch.setattr("app.fetching.geo.exit_geo", no_external_geo)
    if closure_phase == "final_read":
        from app.fetching.challenge import detect_page

        async def close_after_detection(page, status=None):
            verdict = await detect_page(page, status)
            await page.close()
            return verdict

        monkeypatch.setattr("app.fetching.challenge.detect_page", close_after_detection)
    elif closure_phase == "collector":
        from app.fetching.navigation import collect_listing_links

        async def close_during_observation(page, **kwargs):
            await page.close()
            return await collect_listing_links(page, **kwargs)

        monkeypatch.setattr(
            "app.fetching.navigation.collect_listing_links", close_during_observation
        )
    config, _ = merge_layers(
        {
            "site_url": PLAIN + "/produkt/leuchte-01",
            "fetch_mode": "browser",
            "browser": "chromium",
            "discovery_mode": "crawl",
            "sample_count": 1,
            "target_page_selector": "div.product-detail",
            "delay_min_seconds": 0,
            "delay_max_seconds": 0,
        },
        {},
        {},
    )
    sf = SetupFetch(svc, config, profile())
    try:
        session = await sf.session()
        auxiliary = await session.raw(PLAIN + "/robots.txt")
        assert auxiliary.status == 200
        if closure_phase == "before_navigation":
            await session._browser.close()
        with pytest.raises(ApiError) as failure:
            await _samples(svc, {"config": config, "urls": []}, config, 1, sf)
        assert failure.value.status == 503
        assert failure.value.details["reason"] == "browser_unavailable"
        if closure_phase == "before_navigation":
            assert failure.value.details["exception_type"] == "TargetClosedError"
        assert await sf.session() is None
    finally:
        await sf.close()
        svc.db.close()
