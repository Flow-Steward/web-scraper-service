"""A table sample must come from the same approved document as its base fields."""
from types import SimpleNamespace

import pytest

from app.extraction.mapping import apply_mapping
from app.extraction.page import Page
from app.extraction.tables import source_key
from app.setup_ops import SetupFetch
from tests.unit.test_logic import settings
from tests.unit.test_table_sources import SOURCE, capture, mapping


@pytest.mark.parametrize('current,status,final_url,html,reason', [
    (False, 200, 'https://parts.example.test/other', '<h1>Other mock part</h1>', 'document_changed'),
    (False, 200, 'https://parts.example.test/mock', '<article><h1>Original mock part</h1></article>', 'document_changed'),
    (False, 500, 'https://parts.example.test/mock', '<h1>Original mock part</h1>', 'table_http_failed'),
    (True, 200, 'https://parts.example.test/mock', '<article><h1>Original mock part</h1></article>', 'document_changed'),
    (True, 500, 'https://parts.example.test/mock', '<h1>Original mock part</h1>', 'table_http_failed'),
    (True, 200, 'https://parts.example.test/mock', '<h1>Original mock part</h1>', None),
    (False, 200, 'https://parts.example.test/mock', '<h1>Original mock part</h1>', None),
])
async def test_table_capture_validates_acquired_and_current_document(monkeypatch, current, status, final_url, html, reason):
    original = '<h1>Original mock part</h1>'
    page = Page(original, 'https://parts.example.test/mock', 200)
    calls, navigations = [], []
    policy = SimpleNamespace(raise_if_denied=lambda:None, document_status=lambda fallback:status)
    async def content():
        return html
    tab = SimpleNamespace(url=page.url if current else 'about:blank', content=content)
    async def get(url, **kwargs):
        navigations.append(url)
        tab.url = final_url
        return status, html, final_url, None
    session = SimpleNamespace(page=tab, get=get, failure_for=lambda url:None, _native_document_policy=policy)
    sf = SetupFetch(SimpleNamespace(settings=settings(), samples=None), {'site_url':page.url,'browser':'chromium','fetch_mode':'browser'}, {})
    sf._session = session
    async def collect(*args, **kwargs):
        calls.append(True)
        return capture()
    monkeypatch.setattr('app.fetching.tables.collect_table', collect)
    await sf.capture_tables(page, [SOURCE])
    acquired = page.table_captures[source_key(SOURCE)]
    assert acquired['complete'] is (reason is None)
    assert acquired['reason'] == (reason or 'finished')
    assert bool(calls) is (reason is None)
    assert navigations == ([] if current else [page.url])
    assert page.html == original and page.url == 'https://parts.example.test/mock'
    assert apply_mapping(mapping(), page).data['title'] == 'Original mock part'


@pytest.mark.parametrize('stage', ['before', 'after_navigation', 'after_current_read'])
async def test_table_capture_never_downgrades_native_denial(monkeypatch, stage):
    page = Page('<h1>Original mock part</h1>', 'https://parts.example.test/mock')
    denied = stage == 'before'
    def raise_if_denied():
        if denied:
            raise RuntimeError('synthetic native security denial')
    policy = SimpleNamespace(raise_if_denied=raise_if_denied, document_status=lambda fallback:200)
    async def content():
        nonlocal denied
        denied = True
        return page.html
    tab = SimpleNamespace(url=page.url if stage == 'after_current_read' else 'about:blank', content=content)
    async def get(url, **kwargs):
        nonlocal denied
        denied = True
        tab.url = url
        return 200, page.html, url, None
    session = SimpleNamespace(page=tab, get=get, failure_for=lambda url:None, _native_document_policy=policy)
    sf = SetupFetch(SimpleNamespace(settings=settings(), samples=None), {'site_url':page.url,'browser':'chromium','fetch_mode':'browser'}, {})
    sf._session = session
    async def unexpected(*args, **kwargs):
        pytest.fail('a native security denial must prevent table collection')
    monkeypatch.setattr('app.fetching.tables.collect_table', unexpected)
    with pytest.raises(RuntimeError, match='native security denial'):
        await sf.capture_tables(page, [SOURCE])


@pytest.mark.parametrize('current', [False, True])
async def test_expected_table_and_pager_hydration_preserves_outside_structure_check(monkeypatch, current):
    original = '<h1>Original mock part</h1><table id="applications"><tbody></tbody></table><nav id="pager"><span class="current">1</span><button class="next">Next</button></nav>'
    hydrated = '<h1>Original mock part</h1><table id="applications"><thead><tr><th>Model</th></tr></thead><tbody><tr><td>Mock A</td></tr></tbody></table><nav id="pager"><span class="current"><strong>1</strong></span><button class="next disabled"><span>Next</span></button></nav>'
    page = Page(original, 'https://parts.example.test/mock', 200)
    calls, navigations = [], []
    policy = SimpleNamespace(raise_if_denied=lambda:None, document_status=lambda fallback:200)
    async def content():
        return hydrated
    tab = SimpleNamespace(url=page.url if current else 'about:blank', content=content)
    async def get(url, **kwargs):
        navigations.append(url)
        tab.url = url
        return 200, hydrated, url, None
    sf = SetupFetch(SimpleNamespace(settings=settings(), samples=None), {'site_url':page.url,'browser':'chromium','fetch_mode':'browser'}, {})
    sf._session = SimpleNamespace(page=tab, get=get, failure_for=lambda url:None, _native_document_policy=policy)
    async def collect(*args, **kwargs):
        calls.append(True)
        return capture()
    monkeypatch.setattr('app.fetching.tables.collect_table', collect)
    await sf.capture_tables(page, [SOURCE])
    assert page.table_captures[source_key(SOURCE)]['complete'] is True
    assert calls == [True] and navigations == ([] if current else [page.url])
    assert page.html == original
