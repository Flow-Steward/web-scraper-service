"""Synthetic async table pagination; all fixture traffic stays intercepted."""
import asyncio
from copy import deepcopy

import pytest

from tests.integration.test_browser_navigation import listing_page, open_fixture
from tests.unit.test_table_sources import SOURCE, sample_service


def table_html(*, pages=2, identical=False, loading=False, empty=False, action=None, rows_per_page=1):
    rows = '<tr><td>Mock A</td><td>2011-09</td><td></td></tr>' * rows_per_page
    return f'''<h1>Mock part</h1><div id="processing" style="display:{'block' if loading else 'none'}">Processing</div>
    <table id="applications"><thead><tr><th>Model</th><th>From</th><th>To</th></tr></thead><tbody>{'<tr><td class="empty" colspan="3">No rows</td></tr>' if loading or empty else rows}</tbody></table>
    <nav id="pager" aria-label="Pagination"><span class="current">1</span><button type="button" class="next" {'disabled' if pages == 1 else ''}>Next page</button></nav>
    <script>let n=1;const rows={rows!r};const body=document.querySelector('tbody');const next=document.querySelector('.next');
    function finish() {{document.querySelector('#processing').style.display='none';body.innerHTML=rows;}}
    {'setTimeout(finish, 150);' if loading else ''}
    next.onclick=()=>{{{action if action else f'''document.querySelector('#processing').style.display='block';body.innerHTML='<tr><td class="empty" colspan="3">No rows</td></tr>';setTimeout(()=>{{n++;document.querySelector('.current').textContent=n;finish();{' ' if identical else "body.firstElementChild.firstElementChild.textContent='Mock '+n;"}next.disabled=n==={pages};}},80);'''}}};</script>'''


def api():
    from app.fetching.tables import collect_table
    return collect_table


async def test_loading_placeholder_is_not_empty_and_open_end_is_preserved(listing_page):
    await open_fixture(listing_page, table_html(pages=1, loading=True))
    result = await api()(listing_page, SOURCE)
    assert result['complete'] is True
    assert result['rows'] == [['Mock A', '2011-09', '']]
    assert result['pages'] == 1
    assert result['html'].count('<td></td>') == 1


async def test_all_eight_pages_preserve_identical_repeated_rows(listing_page):
    await open_fixture(listing_page, table_html(pages=8, identical=True, rows_per_page=15))
    result = await api()(listing_page, SOURCE)
    assert result['complete'] is True
    assert result['pages'] == 8
    assert result['rows'] == [['Mock A', '2011-09', '']] * 120
    assert result['html'].count('<td>Mock A</td>') == 120
    assert listing_page.url == 'https://catalogue.example.test/list'


async def test_true_empty_is_complete_only_after_loading_is_hidden(listing_page):
    await open_fixture(listing_page, table_html(pages=1, empty=True))
    result = await api()(listing_page, SOURCE)
    assert result['complete'] is True
    assert result['rows'] == []


@pytest.mark.parametrize(('changes', 'reason', 'row_count'), [({'max_pages': 1}, 'page_limit', 1), ({'max_rows': 1}, 'row_limit', 1), ({'max_bytes': 120}, 'byte_limit', 0), ({'max_seconds': 0.3}, 'time_limit', 1)])
async def test_limits_preserve_partial_evidence(listing_page, changes, reason, row_count):
    action = '/* deliberately never settles */' if 'max_seconds' in changes else None
    await open_fixture(listing_page, table_html(pages=8, action=action))
    result = await api()(listing_page, SOURCE | changes)
    assert result['complete'] is False
    assert result['reason'] == reason
    assert len(result['rows']) == row_count


async def test_loading_forever_does_not_prove_zero_applicability(listing_page):
    await open_fixture(listing_page, table_html(pages=1, empty=True))
    await listing_page.locator('#processing').evaluate("el => el.style.display='block'")
    result = await api()(listing_page, SOURCE | {'max_seconds': .2})
    assert result['complete'] is False
    assert result['rows'] == []
    assert result['reason'] == 'time_limit'


async def test_wrong_initial_page_cannot_claim_complete(listing_page):
    await open_fixture(listing_page, table_html(pages=2))
    await listing_page.locator('.current').evaluate("el => el.textContent='2'")
    result = await api()(listing_page, SOURCE)
    assert result['complete'] is False
    assert result['reason'] == 'page_sequence'


async def test_policy_denial_and_cancel_never_click_next(listing_page):
    await open_fixture(listing_page, table_html(pages=2))
    result = await api()(listing_page, SOURCE, before_action=lambda action: False)
    assert result['reason'] == 'policy_denied'
    assert result['pages'] == 1
    result = await api()(listing_page, SOURCE, cancelled=lambda: True)
    assert result['reason'] == 'cancelled'
    assert await listing_page.locator('.current').text_content() == '1'


@pytest.mark.parametrize(('action', 'reason'), [("location.href='/different-document'", 'document_navigation'), ("fetch('/write', {method:'POST',body:'x'})", 'unsupported_method'), ("navigator.sendBeacon('/write','x')", 'unsupported_method')])
async def test_table_actions_never_issue_documents_or_non_get_requests(listing_page, action, reason):
    await open_fixture(listing_page, table_html(pages=2, action=action))
    issued = []
    async def catch(route):
        issued.append(route.request.url)
        await route.fulfill(body='unexpected')
    await listing_page.route('**/different-document', catch)
    await listing_page.route('**/write', catch)
    result = await api()(listing_page, SOURCE | {'max_seconds': .5})
    assert result['complete'] is False
    assert result['reason'] == reason
    assert issued == []
    assert listing_page.url == 'https://catalogue.example.test/list'


async def test_private_request_guard_remains_chained(listing_page, monkeypatch):
    from app.fetching.oneoff import private_route_guard
    action = "fetch('https://internal.example.test/private').then(()=>{document.querySelector('.current').textContent='2'})"
    await open_fixture(listing_page, table_html(pages=2, action=action))
    issued = []
    async def hit(route):
        issued.append(route.request.url)
        await route.fulfill(body='private')
    async def is_private(url):
        return 'internal.example.test' in url
    monkeypatch.setattr('app.fetching.oneoff.url_is_private', is_private)
    await listing_page.route('**/private', hit)
    await listing_page.route('**/*', private_route_guard())
    result = await api()(listing_page, SOURCE | {'max_seconds': .4})
    assert result['complete'] is False
    assert issued == []


async def test_stale_next_is_never_retargeted_after_policy_callback(listing_page):
    await open_fixture(listing_page, table_html(pages=2))
    async def policy(action):
        await listing_page.locator('.next').evaluate("el=>{el.outerHTML='<button class=next onclick=\"window.wrongClick=true\">Next page</button>'}")
        return True
    result = await api()(listing_page, SOURCE, before_action=policy)
    assert result['complete'] is False
    assert result['reason'] == 'stale_control'
    assert await listing_page.evaluate('!!window.wrongClick') is False


async def test_multibyte_cells_respect_byte_limit(listing_page):
    await open_fixture(listing_page, table_html(pages=1))
    await listing_page.locator('tbody td').first.evaluate("el=>el.textContent='Ž🚙'.repeat(100)")
    result = await api()(listing_page, SOURCE | {'max_bytes': 350})
    assert result['complete'] is False
    assert result['reason'] == 'byte_limit'
    assert len(result['html'].encode()) <= 350
    assert result['rows'] == []


async def test_runtime_and_stored_reextraction_keep_complete_table(listing_page, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from crawlee import Request
    from app.core.db import loads
    from app.core.schemas import is_valid
    from tests.unit.test_proxy_auth_pause import make_run
    from tests.unit.test_table_sources import mapping
    await open_fixture(listing_page, table_html(pages=2))
    original = await listing_page.content()
    svc, runner = await make_run(tmp_path, monkeypatch, config_overrides={'fetch_mode':'browser', 'respect_robots':False, 'required_fields':[]})
    spec = mapping() | {'__version': 1}
    monkeypatch.setattr(runner, 'mappings_for', lambda site: [spec])
    async def allowed(*args, **kwargs):
        return True
    async def public(url):
        return False
    monkeypatch.setattr(runner, '_document_destination_allowed', allowed)
    monkeypatch.setattr('app.jobs.runner.url_is_private', public)
    request = Request.from_url(listing_page.url, user_data={'src':'list'})
    ctx = SimpleNamespace(request=request, proxy_info=None, page=listing_page)
    try:
        result = await runner._classify(ctx, original, request.url, 200, True, request.user_data)
        assert result['page_class'] == 'extracted'
        assert result['record']['data']['fitment_raw'].count('<tr>') == 3
        assert result['record']['table_coverage']['fitment_raw']['pages'] == 2
        assert is_valid('record.schema.json', result['record'])
        # Force another required field to be repaired using this captured source;
        # the stored original HTML has only the initial table page.
        runner._store_page(result | {'page_class':'incomplete', 'kvs_key':'fixture_html'}, 'stored', 'catalogue.example.test')
        row = svc.db.one('SELECT * FROM stored_pages WHERE run_id=? AND page_key=?', (runner.run_id, 'stored'))
        stored = loads(row['doc'])
        runner._reextract_one(row, stored, original, False)
        saved = loads(svc.db.one('SELECT line FROM records WHERE run_id=?', (runner.run_id,))['line'])
        assert saved['data']['fitment_raw'].count('<tr>') == 3
        assert saved['table_coverage']['fitment_raw']['pages'] == 2
        assert is_valid('record.schema.json', saved)
    finally:
        svc.db.close()


async def test_sample_capture_replays_offline_without_losing_variant_extras(listing_page, sample_service, monkeypatch):
    from types import SimpleNamespace
    from app.extraction.page import Page
    from app.extraction.distill import sample_doc
    from app.core.schemas import is_valid
    from app.setup_ops import SetupFetch, mapping_test
    from tests.unit.test_logic import settings
    from tests.unit.test_table_sources import mapping
    await open_fixture(listing_page, table_html(pages=2))
    original = await listing_page.content()
    page = Page(original, listing_page.url)
    sf = SetupFetch(SimpleNamespace(settings=settings(), samples=sample_service.samples), {'site_url':page.url, 'browser':'chromium','fetch_mode':'browser','respect_robots':False, 'max_requests_per_minute':60000}, {})
    sf._session = SimpleNamespace(page=listing_page, _native_document_policy=None, failure_for=lambda url: None)
    async def allowed(*args, **kwargs):
        return True
    monkeypatch.setattr(sf, '_destination_allowed', allowed)
    await sf.capture_tables(page, [SOURCE])
    doc = sample_doc(sample_id='smp_'+'0'*26, page=page, requested_url=None, source_key=None, fetched_at='2026-11-25T00:00:00Z', expires_at='2030-01-01T00:00:00Z', fetch={'fetch_mode':'browser','browser':'chromium','proxy_tier':None,'passed_at_step':None}, matched_template_id=None, suggested_template_id='product', structured_types=[], max_bytes=65536, extracted=None)
    assert doc['table_captures'][0]['complete'] is True
    assert is_valid('samples.schema.json#/$defs/sample', doc)
    sample_service.samples.save(doc, original, page.url)
    sample_service.samples.save_extra(doc['sample_id'], {'shop_json':{'https://parts.example.test/product.js':{'variants':[]}}})
    sample_service.samples.save_extra(doc['sample_id'], {'table_captures':page.table_captures})
    assert 'shop_json' in sample_service.samples.load_extra(doc['sample_id'])
    tested = await mapping_test(sample_service, {'sample_ids':[doc['sample_id']], 'mappings':[mapping()], 'required_fill_threshold':.9})
    assert tested['passed'] is True
    assert tested['samples'][0]['table_coverage']['fitment_raw']['row_count'] == 2


async def test_native_document_guard_remains_bound_while_collecting(listing_page):
    from app.fetching.document_policy import bind_document_policy
    from app.fetching.oneoff import private_route_guard
    await open_fixture(listing_page, table_html(pages=2))
    policy = await bind_document_policy(listing_page, 'chromium', listing_page.url)
    async def native_or_fixture(route):
        if route.request.is_navigation_request():
            await policy.request(route)
        else:
            await route.fallback()
    await listing_page.route('**/*', private_route_guard(then=native_or_fixture))
    try:
        result = await api()(listing_page, SOURCE)
        assert result['complete'] is True
        assert result['pages'] == 2
        assert listing_page._wss_native_document_policy is policy
        assert policy.errors == []
        assert not policy.guard.closed
    finally:
        await policy.close()


async def test_replaced_table_cannot_become_the_capture_target(listing_page):
    await open_fixture(listing_page, table_html(pages=2))
    async def policy(action):
        await listing_page.locator('#applications').evaluate("el=>{el.outerHTML=el.outerHTML.replace('Mock A','Unrelated model')}")
        return True
    result = await api()(listing_page, SOURCE, before_action=policy)
    assert result['complete'] is False
    assert result['reason'] == 'table_changed'
    assert result['rows'] == [['Mock A', '2011-09', '']]
