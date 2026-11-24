"""Actual Camoufox listing controls retain the native document guard.

Invented documents are fulfilled locally; no supplier or proxy is used.
"""
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from app.fetching.browsers import launch_camoufox
from app.fetching.document_policy import bind_document_policy
from app.fetching.navigation import NavigationLimits, collect_listing_links


@pytest_asyncio.fixture
async def camoufox_listing_page(monkeypatch):
    from tests.unit.test_logic import settings
    monkeypatch.setattr('app.settings._settings', settings())
    async with async_playwright() as playwright:
        browser = await launch_camoufox(playwright, headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            page = await context.new_page()
            yield page
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['anchor', 'anchor_tall', 'button'])
async def test_actual_camoufox_listing_actions(camoufox_listing_page, mode):
    page = camoufox_listing_page
    products = ''.join(f'<article><a href="/product/item-{i}">Item {i}</a></article>' for i in range(60))
    if mode.startswith('anchor'):
        control = '<ul class="pagination"><li><span>1</span></li><li><a href="/list?page=2">2</a></li><li><a href="/list?page=2">&gt;</a></li></ul>'
    else:
        control = '<button type="button" onclick="document.querySelector(\'main\').insertAdjacentHTML(\'beforeend\',\'<a href=&quot;/product/new&quot;>New</a>\');this.disabled=true">Load more products</button>'
    html = '<main>' + products + '</main>' + control
    if mode == 'anchor_tall':
        html = '<style>main{padding-top:300px;display:grid;grid-template-columns:repeat(4,1fr)}article{height:320px}</style>' + html
    requests = []
    async def document(route):
        requests.append(route.request.url)
        await route.fulfill(content_type='text/html', body=html)
    await page.route('**/*', document)
    policy = await bind_document_policy(page, 'camoufox', 'https://catalogue.example.test/list', timeout_s=10)
    await page.goto('https://catalogue.example.test/list')
    result = await collect_listing_links(page, limits=NavigationLimits(max_seconds=15, settle_ms=100))
    assert not policy.errors
    if mode.startswith('anchor'):
        assert result.stop_reason == 'navigation_deferred'
        assert 'https://catalogue.example.test/list?page=2' in result.deferred_links
        assert 'https://catalogue.example.test/list?page=2' not in requests
    else:
        assert result.stop_reason == 'controls_exhausted'
        assert 'https://catalogue.example.test/product/new' in result.links
