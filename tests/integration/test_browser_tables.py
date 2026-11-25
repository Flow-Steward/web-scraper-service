"""Synthetic async table pagination; all fixture traffic stays intercepted."""
import asyncio
from copy import deepcopy

import pytest

from tests.integration.test_browser_navigation import listing_page as base_listing_page, open_fixture as base_open_fixture
import pytest_asyncio
from tests.unit.test_table_sources import SOURCE, sample_service
from tests.integration.test_native_table_resource_policy import resource_page, native_resources, table_worker_isolation
from tests.integration.test_challenge_form_post import challenge_document


@pytest_asyncio.fixture
async def listing_page(base_listing_page, monkeypatch):
    from tests.unit.test_logic import settings
    monkeypatch.setattr('app.settings._settings', settings())
    base_listing_page.context._wss_table_owned = True
    yield base_listing_page
    policy = getattr(base_listing_page, '_wss_native_document_policy', None)
    if policy is not None:
        try:
            await policy.close()
        except Exception:
            pass


async def open_fixture(page, html, url='https://catalogue.example.test/list'):
    from app.fetching.document_policy import bind_document_policy
    await bind_document_policy(page, 'chromium', url)
    await base_open_fixture(page, html, url)


SHARED_WORKER_PROBE = """() => new Promise(resolve => {
    try {
        const script = new Blob(["onconnect=e=>e.ports[0].postMessage('ran')"],{type:'text/javascript'});
        const worker = new SharedWorker(URL.createObjectURL(script));
        worker.onerror = () => resolve('blocked');
        worker.port.onmessage = () => resolve('ran');
        worker.port.start();
        setTimeout(() => resolve('timeout'),500);
    } catch (_) { resolve('blocked'); }
})"""


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


@pytest.mark.parametrize(('changes', 'reason', 'row_count'), [({'max_pages': 1}, 'page_limit', 1), ({'max_rows': 1}, 'row_limit', 1), ({'max_bytes': 120}, 'byte_limit', 0), ({'max_seconds': 1.5}, 'time_limit', 1)])
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
    result = await api()(listing_page, SOURCE | {'max_seconds': 2})
    assert result['complete'] is False
    assert result['reason'] in {reason, 'table_document_navigation', 'table_request_denied'}
    assert issued == []
    assert listing_page.url == 'https://catalogue.example.test/list' or listing_page._wss_native_document_policy.errors


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
    result = await api()(listing_page, SOURCE | {'max_seconds': 2})
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
    await runner._open_storages()
    spec = mapping() | {'__version': 1}
    monkeypatch.setattr(runner, 'mappings_for', lambda site: [spec])
    async def allowed(*args, **kwargs):
        return True
    async def public(url):
        return False
    monkeypatch.setattr(runner, '_document_destination_allowed', allowed)
    monkeypatch.setattr('app.jobs.runner.url_is_private', public)
    request = Request.from_url(listing_page.url, user_data={'src':'list'})
    async def kvs():
        return runner.kvs
    ctx = SimpleNamespace(request=request, proxy_info=None, page=listing_page, get_key_value_store=kvs)
    try:
        result = await runner._classify(ctx, original, request.url, 200, True, request.user_data)
        assert result['page_class'] == 'extracted'
        assert result['record']['data']['fitment_raw'].count('<tr>') == 3
        assert result['record']['table_coverage']['fitment_raw']['pages'] == 2
        assert is_valid('record.schema.json', result['record'])
        # Force another required field to be repaired using this captured source;
        # the stored original HTML has only the initial table page.
        stored_outcome = result | {'page_class':'incomplete', 'kvs_key':'fixture_html'}
        await runner._archive_table_evidence(ctx, stored_outcome)
        runner._store_page(stored_outcome, 'stored', 'catalogue.example.test')
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


@pytest.mark.parametrize('malformed', ['header_width', 'row_width', 'cell_tag', 'header_span'])
async def test_malformed_table_shape_never_claims_complete(listing_page, malformed):
    await open_fixture(listing_page, table_html(pages=1))
    source = SOURCE.copy()
    if malformed == 'header_width':
        await listing_page.locator('tbody td').last.evaluate('el=>el.remove()')
    elif malformed == 'header_span':
        await listing_page.locator('thead th').first.evaluate("el=>el.setAttribute('colspan','2')")
    elif malformed == 'row_width':
        source.pop('header_selector')
        await listing_page.locator('tbody').evaluate("el=>el.insertAdjacentHTML('beforeend','<tr><td>Mock B</td><td>2023</td></tr>')")
    else:
        source['cell_selector'] = 'td span'
        await listing_page.locator('tbody td').evaluate_all("cells=>cells.forEach(el=>el.innerHTML='<span>'+el.textContent+'</span>')")
    result = await api()(listing_page, source)
    assert result['complete'] is False
    assert result['reason'] in {'column_mismatch', 'unsupported_row'}


async def test_early_terminal_pager_waits_for_pending_get_rows(listing_page):
    action = "document.querySelector('.current').textContent='2';next.disabled=true;fetch('/table-data').then(r=>r.text()).then(data=>body.innerHTML=data)"
    await open_fixture(listing_page, table_html(pages=2, action=action))
    async def delayed(route):
        await asyncio.sleep(.6)
        await route.fulfill(content_type='text/html', body='<tr><td>Mock B</td><td>2018-02</td><td></td></tr>')
    await listing_page.route('**/table-data', delayed)
    result = await api()(listing_page, SOURCE)
    assert result['complete'] is True
    assert result['rows'] == [['Mock A','2011-09',''], ['Mock B','2018-02','']]


async def test_popup_first_form_post_cannot_escape_table_context_gate(listing_page):
    action = "const f=document.createElement('form');f.method='POST';f.target='_blank';f.action='/write';document.body.append(f);f.submit()"
    await open_fixture(listing_page, table_html(pages=2, action=action))
    hits = []
    async def hit(route):
        hits.append(route.request.method)
        await route.fulfill(body='write reached')
    await listing_page.context.route('**/write', hit)
    result = await api()(listing_page, SOURCE | {'max_seconds':2})
    assert result['complete'] is False
    assert hits == []


async def test_delayed_post_remains_blocked_after_terminal_capture_returns(listing_page):
    action = "document.querySelector('.current').textContent='2';next.disabled=true;setTimeout(()=>fetch('/write',{method:'POST',body:'x'}),1500)"
    await open_fixture(listing_page, table_html(pages=2, action=action))
    hits = []
    async def hit(route):
        hits.append(route.request.method)
        await route.fulfill(body='write reached')
    await listing_page.route('**/write', hit)
    result = await api()(listing_page, SOURCE)
    assert result['complete'] is True
    await listing_page.wait_for_timeout(1700)
    assert hits == []


async def test_native_lease_collect_reset_preserves_cookie_and_next_document(resource_page, native_resources):
    from app.fetching.document_policy import NativeDocumentPolicy
    page, guard, engine = resource_page
    origin, _, _, _ = native_resources
    page.context._wss_table_owned = True
    policy = NativeDocumentPolicy(guard)
    page._wss_native_document_policy = policy
    page.on('close', policy.begin_close)
    await page.context.add_cookies([{'name':'lease_keep','value':'yes','url':origin}])
    original_context = page.context
    await page.set_content(table_html(pages=2))
    try:
        result = await api()(page, SOURCE)
        assert result['complete'] is True, (result, guard.errors)
        old_lease = policy._table_lease
        await policy.rebind(origin + '/next')
        assert old_lease.released and policy._table_lease is None
        await page.goto(origin + '/next')
        assert page.context is original_context
        assert not page.is_closed()
        assert any(cookie['name'] == 'lease_keep' and cookie['value'] == 'yes' for cookie in await page.context.cookies())
        assert page.url == origin + '/next'
    finally:
        await policy.close()


async def test_firefox_unarmed_table_lease_allows_authorized_clearance_form(resource_page, native_resources, challenge_document, monkeypatch):
    from app.fetching.document_policy import NativeDocumentPolicy
    page, guard, engine = resource_page
    if engine != 'camoufox':
        pytest.skip('Only Firefox supports the explicitly authorized clearance form')
    challenge_origin, _, hits = challenge_document
    origin = native_resources[0]
    async def private(url):
        return not (url.startswith(origin + '/') or url.startswith(challenge_origin + '/'))
    for module in ('app.fetching.oneoff', 'app.fetching.chromium_destinations', 'app.fetching.firefox_policy'):
        monkeypatch.setattr(module + '.url_is_private', private)
    page.context._wss_table_owned = True
    policy = NativeDocumentPolicy(guard)
    page._wss_native_document_policy = policy
    page.on('close', policy.begin_close)
    await page.set_content(table_html(pages=1))
    try:
        result = await api()(page, SOURCE)
        assert result['complete'] is True, (result, guard.errors)
        await policy.rebind(challenge_origin + '/product', challenge_post_allowed=True)
        try:
            await page.goto(challenge_origin + '/product')
        except Exception as exc:
            pytest.fail(f"{type(exc).__name__}; mode={getattr(guard, '_table_mode', None)} active={guard.active} errors={[(type(error).__name__, getattr(error, 'reason', str(error))) for error in guard.errors]} hits={hits}")
        await page.wait_for_function("document.title === 'Product'", timeout=10000)
        assert [hit for hit in hits if hit[0] == 'POST'] == [('POST','/product')]
        assert not guard.errors
    finally:
        await policy.close()


async def test_worker_policy_blocks_future_blob_network_and_firefox_shared_workers(resource_page, native_resources):
    from app.fetching.document_policy import NativeDocumentPolicy
    page, guard, engine = resource_page
    _, _, hits, _ = native_resources
    prefix = 'mw:' if engine == 'camoufox' else ''
    page.context._wss_table_owned = True
    if engine == 'camoufox':
        assert await page.evaluate(prefix + SHARED_WORKER_PROBE) == 'blocked'
        assert page.context.browser._wss_table_worker_isolation is True
    policy = NativeDocumentPolicy(guard)
    page._wss_native_document_policy = policy
    page.on('close', policy.begin_close)
    await page.set_content(table_html(pages=1))
    try:
        result = await api()(page, SOURCE)
        assert result['complete'] is True, (result, guard.errors)
        from playwright.async_api import Error
        try:
            state = await page.evaluate(prefix + '''() => new Promise(resolve => {
                try {
                    const script = new Blob(["fetch('/worker-write',{method:'POST'});postMessage('ran')"],{type:'text/javascript'});
                    const worker = new Worker(URL.createObjectURL(script));
                    worker.onerror = () => resolve('blocked');
                    worker.onmessage = () => resolve('ran');
                    setTimeout(() => resolve('timeout'),500);
                } catch (_) { resolve('blocked'); }
            })''')
            assert state == 'blocked'
        except Error:
            # Firefox can emit a worker event even for a CSP-blocked worker.
            # The exclusive lease then destroys its transport before any work.
            assert page.is_closed()
            assert any(getattr(error, 'reason', None) == 'unsupported_worker' for error in policy.errors)
        assert not [hit for hit in hits if hit[0] == 'POST']
    finally:
        await policy.close()


async def test_oneoff_dedicated_firefox_launch_really_isolates_workers(monkeypatch, native_resources):
    import os
    from app.fetching.browsers import browser_presence, launch_camoufox
    from app.fetching.oneoff import BrowserSession
    from tests.unit.test_logic import settings
    binary = os.environ.get('CAMOUFOX_EXECUTABLE_PATH') or browser_presence().get('camoufox', {}).get('path')
    if not binary:
        pytest.skip('Camoufox is not installed in this environment')
    monkeypatch.setattr('app.settings._settings', settings())
    monkeypatch.setattr('camoufox.utils.warn_if_package_outdated', lambda:None)
    async def no_geo(*args, **kwargs):
        return None
    async def context(browser, *args, **kwargs):
        return await browser.new_context(service_workers='block')
    async def local_launch(playwright, **kwargs):
        kwargs['headless'] = True
        return await launch_camoufox(playwright, executable_path=binary, fingerprint_preset={
            'navigator':{'platform':'MacIntel','userAgent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 Firefox/158.0','hardwareConcurrency':4},
            'screen':{'width':1440,'height':900,'availWidth':1440,'availHeight':860,'colorDepth':24}}, **kwargs)
    monkeypatch.setattr('app.fetching.geo.exit_geo', no_geo)
    monkeypatch.setattr('app.fetching.identity.identity_for', no_geo)
    monkeypatch.setattr('app.fetching.identity.camoufox_context', context)
    monkeypatch.setattr('app.fetching.browsers.launch_camoufox', local_launch)
    session = BrowserSession('camoufox', table_dedicated=True)
    try:
        await session.start()
        assert session.context._wss_table_owned is True
        assert session._browser._wss_table_worker_isolation is True
        await session.get(native_resources[0] + '/page')
        assert await session.page.evaluate('mw:' + SHARED_WORKER_PROBE) == 'blocked'
    finally:
        await session.close()


@pytest.mark.parametrize('required', [False, True])
async def test_native_table_http500_is_optional_unknown_or_required_failure(resource_page, native_resources, tmp_path, monkeypatch, required):
    from types import SimpleNamespace
    from crawlee import Request
    from app.fetching.document_policy import NativeDocumentPolicy
    from tests.unit.test_proxy_auth_pause import make_run
    from tests.unit.test_table_sources import mapping
    page, guard, engine = resource_page
    page.context._wss_table_owned = True
    policy = NativeDocumentPolicy(guard)
    page._wss_native_document_policy = policy
    page.on('close', policy.begin_close)
    action = "document.querySelector('.current').textContent='2';next.disabled=true;fetch('/http500').catch(()=>null)"
    await page.set_content(table_html(pages=2, action=action))
    original = await page.content()
    svc, runner = await make_run(tmp_path, monkeypatch, config_overrides={'fetch_mode':'browser','respect_robots':False,'required_fields':[]})
    await runner._open_storages()
    spec = mapping() | {'__version':1}
    spec['fields'][1]['required'] = required
    monkeypatch.setattr(runner, 'mappings_for', lambda site:[spec])
    async def allowed(*args, **kwargs):
        return True
    async def public(url):
        return False
    monkeypatch.setattr(runner, '_document_destination_allowed', allowed)
    monkeypatch.setattr('app.jobs.runner.url_is_private', public)
    async def kvs():
        return runner.kvs
    request = Request.from_url(page.url, user_data={'src':'list'})
    ctx = SimpleNamespace(page=page,request=request,proxy_info=None,get_key_value_store=kvs)
    try:
        result = await runner._classify(ctx, original, page.url, 200, True, request.user_data)
        assert guard.table_load_failure in {'table_http_failed','table_request_failed'}
        assert guard.errors == []
        assert result['page_class'] == ('incomplete' if required else 'extracted')
        assert result['table_coverage']['fitment_raw']['complete'] is False
        assert result['table_coverage']['fitment_raw']['reason'] == guard.table_load_failure
        if not required:
            assert result['record']['data']['fitment_raw'] is None
            assert result['record']['data']['title'] == 'Mock part'
    finally:
        await policy.close()
        svc.db.close()


@pytest.mark.parametrize('caller', ['runtime', 'setup'])
async def test_overlapping_table_sources_restart_guarded_original_document(listing_page, tmp_path, monkeypatch, caller):
    from types import SimpleNamespace
    from crawlee import Request
    from app.extraction.page import Page
    from app.extraction.tables import source_key
    from app.setup_ops import SetupFetch
    from tests.unit.test_proxy_auth_pause import make_run
    from tests.unit.test_table_sources import mapping
    await open_fixture(listing_page, table_html(pages=8))
    original = await listing_page.content()
    url = listing_page.url
    sources = [SOURCE | {'max_pages':2}, SOURCE | {'max_pages':8}]
    product = Page(original, url)
    svc, runner = await make_run(tmp_path, monkeypatch, config_overrides={'fetch_mode':'browser','respect_robots':False,'required_fields':[]})
    async def allowed(*args, **kwargs):
        return True
    try:
        if caller == 'runtime':
            spec = mapping() | {'__version':1}
            spec['fields'][1]['sources'] = [sources[0]]
            spec['fields'].append({'name':'all_applications','type':'html','sources':[sources[1]]})
            monkeypatch.setattr(runner, '_document_destination_allowed', allowed)
            request = Request.from_url(url)
            ctx = SimpleNamespace(page=listing_page, request=request)
            await runner._collect_tables(ctx, product, spec, True, {})
            actions = runner.rt['navigation_actions']
        else:
            sf = SetupFetch(svc, {'site_url':url,'browser':'chromium','fetch_mode':'browser','respect_robots':False}, {})
            policy = listing_page._wss_native_document_policy
            async def get(target, **kwargs):
                policy.raise_if_denied()
                await policy.rebind(target, kwargs.get('destination_policy'))
                response = await listing_page.goto(target)
                return response.status, await listing_page.content(), listing_page.url, None
            sf._session = SimpleNamespace(page=listing_page, context=listing_page.context, _browser=listing_page.context.browser,
                                          _native_document_policy=policy, get=get, failure_for=lambda url:None)
            monkeypatch.setattr(sf, '_destination_allowed', allowed)
            await sf.capture_tables(product, sources)
            actions = sf._navigation_actions
        assert product.html == original
        assert product.table_captures[source_key(sources[0])]['reason'] == 'page_limit'
        complete = product.table_captures[source_key(sources[1])]
        assert complete['complete'] is True, complete
        assert complete['pages'] == 8 and len(complete['rows']) == 8
        assert actions == 9  # One first traversal, one reset, seven final traversal actions.
    finally:
        svc.db.close()


@pytest.mark.parametrize('blocked_by', ['action_budget', 'security_denial'])
async def test_table_restart_cannot_bypass_budget_or_clear_security_denial(listing_page, blocked_by):
    await open_fixture(listing_page, table_html(pages=8))
    first = await api()(listing_page, SOURCE | {'max_pages':2})
    assert first['reason'] == 'page_limit'
    policy = listing_page._wss_native_document_policy
    if blocked_by == 'security_denial':
        await listing_page.evaluate("fetch('/write',{method:'POST'}).catch(()=>null)")
        assert policy.errors
    resets = []
    async def restart():
        resets.append(True)
        return True
    result = await api()(listing_page, SOURCE | {'max_pages':8}, restart_document=restart,
                         before_action=lambda action: blocked_by != 'action_budget')
    assert result['complete'] is False
    assert resets == []
    if blocked_by == 'action_budget':
        assert result['reason'] == 'policy_denied'
    else:
        assert policy.errors
        with pytest.raises(Exception):
            policy.raise_if_denied()
