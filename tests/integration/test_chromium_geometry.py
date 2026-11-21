"""Deterministic browser geometry from the production identity/context pipeline."""
from copy import deepcopy
import os
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from app.fetching import identity


@pytest.mark.asyncio
async def test_rejected_geometry_never_reaches_browser_context(monkeypatch):
    good = deepcopy(identity.chromium_generator("linux").generate())
    good.screen.width = good.screen.availWidth = 1366
    good.screen.height = good.screen.availHeight = 768
    good.screen.outerWidth, good.screen.outerHeight = 1280, 720
    good.screen.innerWidth = good.screen.innerHeight = 0
    bad = deepcopy(good)
    bad.screen.outerWidth, bad.screen.outerHeight = 1374, 772
    draws = iter([bad, good])
    monkeypatch.setattr(identity, "chromium_generator", lambda os: SimpleNamespace(generate=lambda: next(draws)))
    ident = identity.new_chromium_identity("203.0.113.10", "linux")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel="chromium", headless=True, args=["--no-sandbox"],
            executable_path=os.environ.get("WSS_TEST_CHROMIUM_PATH"),
        )
        try:
            context = await identity.chromium_context(
                browser, ident, viewport={"width": 1280, "height": 720}, locale="de-DE"
            )
            page = await context.new_page()
            view = await page.evaluate("""() => ({
                screen: [screen.width, screen.height],
                available: [screen.availWidth, screen.availHeight],
                window: [outerWidth, outerHeight],
                viewport: [innerWidth, innerHeight],
                language: navigator.language,
            })""")

            assert view["screen"] == view["available"] == [1366, 768]
            assert view["window"] == view["viewport"] == [1280, 720]
            assert view["language"] == "de-DE"
            assert identity.preset_id_of(page) == ident.preset_id
            assert ident.exit_ip == "203.0.113.10"
        finally:
            await browser.close()
