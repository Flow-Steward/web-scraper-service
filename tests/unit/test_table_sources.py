"""Opt-in table captures preserve applicability and fail closed offline."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.extraction.mapping import apply_mapping
from app.extraction.page import Page

SOURCE = {"kind": "browser_table", "selector": "#applications", "row_selector": "tbody tr", "cell_selector": "td", "header_selector": "thead th", "loading_selector": "#processing", "empty_selector": "td.empty", "next_selector": "#pager .next", "current_page_selector": "#pager .current"}


def mapping():
    return {"schema_version": 1, "template_id": "product", "match": {"selector": "h1"}, "fields": [{"name": "title", "type": "text", "sources": [{"kind": "css", "selector": "h1", "extract": "text"}]}, {"name": "fitment_raw", "type": "html", "sources": [deepcopy(SOURCE)]}]}


def capture(complete=True):
    return {"complete": complete, "reason": "finished" if complete else "page_limit", "pages": 2, "rows": [["Mock A", "2011-09", ""], ["Mock A", "2011-09", ""]], "headers": ["Model", "From", "To"], "bytes": 100, "html": "<table><thead><tr><th>Model</th><th>From</th><th>To</th></tr></thead><tbody><tr><td>Mock A</td><td>2011-09</td><td></td></tr><tr><td>Mock A</td><td>2011-09</td><td></td></tr></tbody></table>"}


def test_captured_table_preserves_repeated_rows_months_and_blank_bounds():
    from app.extraction.tables import source_key
    page = Page('<h1>Mock part</h1><div id="spec-year">2023</div>', 'https://parts.example.test/mock')
    page.table_captures = {source_key(SOURCE): capture()}
    ext = apply_mapping(mapping(), page)
    assert ext.data['fitment_raw'].count('<td>Mock A</td>') == 2
    assert ext.data['fitment_raw'].count('<td></td>') == 2
    assert '2011-09' in ext.data['fitment_raw']
    assert '2023' not in ext.data['fitment_raw']
    assert ext.data['title'] == 'Mock part'


@pytest.mark.parametrize('how', ['uncaptured', 'incomplete', 'different_source'])
def test_unavailable_table_never_falls_back_to_specification_year(how):
    from app.extraction.tables import source_key, table_failures
    page = Page('<h1>Mock part</h1><div id="spec-year">2023</div>', 'https://parts.example.test/mock')
    if how != 'uncaptured':
        source = deepcopy(SOURCE)
        if how == 'different_source':
            source['selector'] = '#other'
        page.table_captures = {source_key(source): capture(how != 'incomplete')}
    spec = mapping()
    spec['fields'][1]['sources'].append({'kind': 'css', 'selector': '#spec-year', 'extract': 'text'})
    ext = apply_mapping(spec, page)
    assert ext.data['fitment_raw'] is None
    assert table_failures(spec, page)['fitment_raw']['complete'] is False
    assert ext.data['title'] == 'Mock part'


def test_contract_accepts_only_bounded_html_table_sources():
    from app.core.schemas import is_valid
    assert is_valid('mapping.schema.json', mapping())
    for change in ({'max_pages': 65}, {'max_bytes': 0}, {'max_seconds': 121}, {'next_selector': '#pager .next', 'current_page_selector': None}):
        value = mapping()
        value['fields'][1]['sources'][0].update(change)
        assert not is_valid('mapping.schema.json', value)
    value = mapping()
    value['fields'][1]['type'] = 'number'
    assert not is_valid('mapping.schema.json', value)


from tests.unit.test_mapping_row_coverage import sample_service


@pytest.mark.parametrize('captured', [False, True])
@pytest.mark.parametrize('required', [False, True])
async def test_offline_mapping_test_requires_matching_complete_capture(sample_service, captured, required):
    from app.extraction.tables import source_key
    from app.setup_ops import mapping_test
    sid = 'smp_table'
    sample_service.samples.save({'sample_id': sid, 'url': 'https://parts.example.test/mock', 'expires_at': '2030-01-01T00:00:00Z', 'variant_sources': []}, '<h1>Mock part</h1>', 'https://parts.example.test/mock')
    if captured:
        sample_service.samples.save_extra(sid, {'table_captures': {source_key(SOURCE): capture()}})
    spec = mapping()
    spec['fields'][1]['required'] = required
    result = await mapping_test(sample_service, {'sample_ids': [sid], 'mappings': [spec], 'required_fill_threshold': .9})
    assert result['passed'] is (captured or not required)
    assert result['samples'][0]['table_coverage']['fitment_raw']['complete'] is captured
    if captured:
        assert result['samples'][0]['data']['fitment_raw'].count('<td>Mock A</td>') == 2
    else:
        assert result['samples'][0]['missing_required'] == (['fitment_raw'] if required else [])
        assert any(warning['code'] == 'table_incomplete' for warning in result['warnings'])


def test_sample_table_capture_extras_merge_with_variant_json(sample_service):
    from app.extraction.tables import source_key
    sid = 'smp_table'
    sample_service.samples.save_extra(sid, {'shop_json': {'https://parts.example.test/mock.js': {'variants': []}}})
    sample_service.samples.save_extra(sid, {'table_captures': {source_key(SOURCE): capture()}})
    assert 'shop_json' in sample_service.samples.load_extra(sid)
    assert 'table_captures' in sample_service.samples.load_extra(sid)


@pytest.mark.parametrize('required', [False, True])
async def test_runtime_http_table_source_fails_closed_and_preserves_capture_reason(tmp_path, monkeypatch, required):
    from crawlee import Request
    from app.jobs.runner import JobRunner
    from tests.unit.test_proxy_auth_pause import make_run
    svc, runner = await make_run(tmp_path, monkeypatch, config_overrides={'urls': [{'url': 'https://target.test/one'}], 'required_fields': []})
    spec = mapping() | {'__version': 1}
    spec['fields'][1]['required'] = required
    monkeypatch.setattr(runner, 'mappings_for', lambda site: [spec])
    monkeypatch.setattr('app.jobs.runner.url_is_private', lambda url: _false())
    request = Request.from_url('https://target.test/one', user_data={'src':'list'})
    async def saved(*args, **kwargs):
        pass
    async def store():
        return SimpleNamespace(set_value=saved)
    ctx = SimpleNamespace(request=request, proxy_info=None, get_key_value_store=store)
    try:
        body = '<!doctype html><html><body><h1>Mock part</h1>' + '<p>Ordinary product detail.</p>' * 40 + '</body></html>'
        result = await runner._classify(ctx, body, request.url, 200, False, request.user_data)
        assert result['page_class'] == ('incomplete' if required else 'extracted')
        assert result.get('missing_fields', []) == (['fitment_raw'] if required else [])
        assert result['table_coverage']['fitment_raw']['reason'] == 'browser_required'
        if not required:
            assert result['record']['data']['fitment_raw'] is None
            assert result['record']['table_coverage']['fitment_raw']['complete'] is False
            runner._apply_item(result)
            assert runner.rt['table_partial'] is True
            assert any(error['code'] == 'table_partial' for error in runner.doc['errors'])
        runner._store_page(result | {'page_class':'incomplete', 'kvs_key':'fixture_html'}, 'table-key', 'target.test')
        from app.core.db import loads
        stored = loads(svc.db.one('SELECT doc FROM stored_pages WHERE run_id=? AND page_key=?', (runner.run_id, 'table-key'))['doc'])
        assert 'table_captures' not in stored
        assert stored['table_coverage']['fitment_raw']['reason'] == 'browser_required'
    finally:
        svc.db.close()


async def _false():
    return False


def test_malformed_complete_capture_cannot_satisfy_optional_table():
    from app.extraction.tables import source_key, table_failures
    page = Page('<h1>Mock part</h1>', 'https://parts.example.test/mock')
    page.table_captures = {source_key(SOURCE): {'complete':True, 'reason':'finished', 'pages':1}}
    assert table_failures(mapping(), page)['fitment_raw']['complete'] is False
    assert apply_mapping(mapping(), page).data['fitment_raw'] is None


async def test_runtime_table_collection_honors_page_timeout(monkeypatch):
    from app.jobs.runner import JobRunner
    seen = []
    async def collect(page, source, **kwargs):
        seen.append(source['max_seconds'])
        return capture()
    monkeypatch.setattr('app.fetching.tables.collect_table', collect)
    runner = SimpleNamespace(config={'page_timeout_seconds':15, 'max_duration_minutes':10}, running_seconds=lambda:0)
    page = Page('<h1>Mock part</h1>', 'https://parts.example.test/mock')
    await JobRunner._collect_tables(runner, SimpleNamespace(page=object()), page, mapping(), True, {})
    assert 0 < seen[0] <= 15


@pytest.mark.parametrize('bad', [{"complete": False}, {"complete": False, "reason": "time_limit", "pages": 0, "bytes": 0, "rows": None, "headers": [], "html": ""}, {"complete": False, "reason": "time_limit", "pages": True, "bytes": 0, "rows": [], "headers": [], "html": ""}])
def test_malformed_incomplete_capture_fails_closed_without_coverage_crash(bad):
    from app.extraction.tables import source_key, table_failures
    page = Page('<h1>Mock part</h1>', 'https://parts.example.test/mock')
    page.table_captures = {source_key(SOURCE): bad}
    failure = table_failures(mapping(), page)['fitment_raw']
    assert failure['complete'] is False
    assert failure['reason'] == 'malformed_capture'
    assert apply_mapping(mapping(), page).data['fitment_raw'] is None


@pytest.mark.parametrize(('limits', 'reason'), [({'max_rows': 2}, 'aggregate_row_limit'), ({'max_pages': 2}, 'aggregate_page_limit')])
def test_page_capture_budget_preserves_existing_evidence_and_skips_exhausted_source(limits, reason):
    from app.extraction.tables import TableCaptureBudget, source_key
    other = SOURCE | {'selector': '#other'}
    captures = {source_key(SOURCE): capture()}
    budget = TableCaptureBudget(captures, [SOURCE, other], **limits)
    bounded, stopped = budget.bounds(other)
    assert bounded is None
    assert stopped == reason
    budget.retain(other, {'complete': False, 'reason': stopped, 'pages': 0, 'rows': [], 'headers': [], 'bytes': 0, 'html': ''})
    assert captures[source_key(SOURCE)]['complete'] is True
    assert captures[source_key(other)]['reason'] == reason
    assert set(captures) == {source_key(SOURCE), source_key(other)}


def test_page_capture_budget_bounds_actual_serialized_bytes_and_original_keys():
    import json
    from app.extraction.tables import TableCaptureBudget, source_key
    other = SOURCE | {'selector': '#other', 'max_bytes': 4194304}
    third = SOURCE | {'selector': '#third'}
    captures = {source_key(SOURCE): capture()}
    budget = TableCaptureBudget(captures, [SOURCE, other, third], max_bytes=4096)
    bounded, reason = budget.bounds(other)
    assert reason is None
    assert 0 < bounded['max_bytes'] < 4096
    budget.retain(other, capture() | {'html': '<table>' + 'Ž' * 3000 + '</table>'}, bounded)
    assert len(json.dumps(captures, ensure_ascii=False, separators=(',', ':')).encode()) <= 4096
    assert captures[source_key(other)]['complete'] is False
    assert captures[source_key(other)]['reason'] == 'aggregate_byte_limit'
    assert captures[source_key(SOURCE)]['rows'] == [['Mock A', '2011-09', ''], ['Mock A', '2011-09', '']]
    assert source_key(bounded) not in captures
    assert source_key(third) in captures


async def test_runtime_aggregate_row_limit_stops_later_collector_calls(tmp_path, monkeypatch):
    from app.jobs.runner import JobRunner
    from app.extraction.tables import source_key
    from tests.unit.test_proxy_auth_pause import make_run
    first = SOURCE | {'max_rows':10000}
    second = SOURCE | {'selector':'#second', 'max_rows':10000}
    spec = mapping()
    spec['fields'][1]['sources'] = [first]
    spec['fields'].append({'name':'other_fitment', 'type':'html', 'sources':[second]})
    calls = []
    async def collect(page, source, **kwargs):
        calls.append(source)
        return capture() | {'pages':1, 'rows':[['Mock A']] * 10000, 'bytes':200000}
    monkeypatch.setattr('app.fetching.tables.collect_table', collect)
    svc, runner = await make_run(tmp_path, monkeypatch)
    try:
        page = Page('<h1>Mock part</h1>', 'https://parts.example.test/mock')
        await JobRunner._collect_tables(runner, SimpleNamespace(page=object()), page, spec, True, {})
        assert len(calls) == 1
        assert page.table_captures[source_key(second)]['reason'] == 'aggregate_row_limit'
        assert len(page.table_captures[source_key(first)]['rows']) == 10000
    finally:
        svc.db.close()


async def test_setup_active_and_proposed_sources_share_page_and_byte_budgets(monkeypatch):
    from app.setup_ops import SetupFetch
    from app.extraction.tables import source_key
    from tests.unit.test_logic import settings
    first = SOURCE | {'max_pages':64}
    second = SOURCE | {'selector':'#proposed', 'max_pages':64}
    page = Page('<h1>Mock part</h1>', 'https://parts.example.test/mock')
    page.table_captures = {source_key(first): capture() | {'pages':64}}
    sf = SetupFetch(SimpleNamespace(settings=settings(), samples=None), {'site_url':page.url, 'browser':'chromium', 'fetch_mode':'browser'}, {})
    sf._session = SimpleNamespace(page=SimpleNamespace(url=page.url), failure_for=lambda url:None)
    async def unexpected(*args, **kwargs):
        raise AssertionError('exhausted aggregate budget must prevent a collector call')
    monkeypatch.setattr('app.fetching.tables.collect_table', unexpected)
    await sf.capture_tables(page, [first, second, second])
    assert page.table_captures[source_key(second)]['reason'] == 'aggregate_page_limit'
    assert page.table_captures[source_key(first)]['pages'] == 64


@pytest.mark.parametrize('caller', ['runtime', 'setup'])
async def test_aggregate_byte_budget_prevents_later_source_collection(tmp_path, monkeypatch, caller):
    import json
    from app.extraction.tables import source_key
    from app.jobs.runner import JobRunner
    from app.setup_ops import SetupFetch
    from tests.unit.test_logic import settings
    from tests.unit.test_proxy_auth_pause import make_run
    sources = [SOURCE | {'selector': '#first', 'max_bytes':4194304}, SOURCE | {'selector':'#second', 'max_bytes':4194304}]
    page = Page('<h1>Mock part</h1>', 'https://parts.example.test/mock')
    calls = []
    async def collect(browser_page, source, **kwargs):
        calls.append(source)
        return capture() | {'bytes':source['max_bytes'], 'html':'<table>' + 'x' * (source['max_bytes'] - 100) + '</table>'}
    monkeypatch.setattr('app.fetching.tables.collect_table', collect)
    if caller == 'setup':
        sf = SetupFetch(SimpleNamespace(settings=settings(), samples=None), {'site_url':page.url, 'browser':'chromium', 'fetch_mode':'browser'}, {})
        async def content():
            return page.html
        policy = SimpleNamespace(raise_if_denied=lambda:None, document_status=lambda fallback:200)
        sf._session = SimpleNamespace(page=SimpleNamespace(url=page.url, content=content), failure_for=lambda url:None, _native_document_policy=policy)
        await sf.capture_tables(page, sources)
    else:
        svc, runner = await make_run(tmp_path, monkeypatch)
        spec = mapping()
        spec['fields'][1]['sources'] = [sources[0]]
        spec['fields'].append({'name':'other_fitment', 'type':'html', 'sources':[sources[1]]})
        try:
            await JobRunner._collect_tables(runner, SimpleNamespace(page=object()), page, spec, True, {})
        finally:
            svc.db.close()
    assert len(calls) == 1
    assert page.table_captures[source_key(sources[0])]['complete'] is True
    assert page.table_captures[source_key(sources[1])]['reason'] == 'aggregate_byte_limit'
    assert len(json.dumps(page.table_captures, ensure_ascii=False, separators=(',', ':')).encode()) <= 4194304


async def test_offline_replay_applies_aggregate_limits_to_stored_captures(sample_service):
    from app.extraction.tables import source_key
    from app.setup_ops import mapping_test
    first = SOURCE | {'selector':'#stored-first', 'max_bytes':4194304}
    second = SOURCE | {'selector':'#stored-second', 'max_bytes':4194304}
    spec = mapping()
    spec['fields'][1]['sources'] = [first]
    spec['fields'].append({'name':'other_fitment', 'type':'html', 'required':True, 'sources':[second]})
    sid = 'smp_stored_tables'
    sample_service.samples.save({'sample_id':sid, 'url':'https://parts.example.test/mock', 'expires_at':'2030-01-01T00:00:00Z', 'variant_sources':[]}, '<h1>Mock part</h1>', 'https://parts.example.test/mock')
    large = capture() | {'bytes':2500000, 'html':'<table>'+'x'*2500000+'</table>'}
    sample_service.samples.save_extra(sid, {'table_captures':{source_key(first):large, source_key(second):large}})
    result = await mapping_test(sample_service, {'sample_ids':[sid], 'mappings':[spec], 'required_fill_threshold':.9})
    assert result['passed'] is False
    assert result['samples'][0]['table_coverage']['fitment_raw']['complete'] is True
    assert result['samples'][0]['table_coverage']['other_fitment']['reason'] == 'aggregate_byte_limit'


@pytest.mark.parametrize('scoped_inventory', [False, True])
async def test_optional_table_failure_keeps_products_and_reports_partial_result(tmp_path, monkeypatch, scoped_inventory):
    from app.core.db import dumps
    from tests.unit.test_proxy_auth_pause import make_run
    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.rt['table_partial'] = True
    runner.doc.setdefault('errors', []).append({'code':'table_partial', 'message':'Optional table data is incomplete', 'severity':'warning', 'at':'2026-11-25T00:00:00Z'})
    if scoped_inventory:
        runner.rt['sitemap_feeding_started'] = True
        runner.rt['sitemap_traversal'] = {
            'sources': {'https://target.test/catalogue.xml': {'status': 'complete'}},
            'source_scope': True,
            'excluded_source_count': 1,
            'excluded_sources': ['https://target.test/other.xml'],
        }
    record = {'record_id':'rec_'+'0'*32, 'source_url':'https://target.test/one', 'site':'target.test', 'template_id':'product', 'mapping_version':1, 'item_index':0, 'extracted_at':'2026-11-25T00:00:00Z', 'data':{'title':'Mock part','fitment_raw':None}, 'table_coverage':{'fitment_raw':{'complete':False,'reason':'time_limit','pages':1,'row_count':1,'bytes':100}}}
    runner._write_record(record, None)
    try:
        await svc.jobs._complete_doc(runner.run_id, runner.doc, runner.rt, None)
        summary = svc.jobs.summary(runner.run_id)
        assert summary['outcome'] == 'partial'
        assert summary['completeness']['pages_collected'] == 1
        assert summary['completeness']['not_collected'] == 0
        assert 'optional table data is incomplete' in summary['completeness']['sentence'].lower()
        if scoped_inventory:
            assert summary['sitemap_discovery']['complete'] is True
            assert 'configured sitemap sources, not the whole website' in summary['completeness']['sentence']
    finally:
        svc.db.close()


@pytest.mark.parametrize('name,variant_fields,conflict', [
    ('fitment_raw',[{'name':'fitment_raw','path':'description'}],True),
    ('description',None,True),
    ('fitment_raw',[{'name':'sku','path':'sku'}],False),
    ('fitment_raw',None,False),
])
def test_table_mapping_rejects_ambiguous_variant_override(name, variant_fields, conflict):
    from app.setup_ops import validate_mapping_rules
    from tests.unit.test_generic_mapping_validation import service
    spec = mapping()
    spec['fields'][1]['name'] = name
    spec['variants'] = {'sources':[{'kind':'jsonld'}]}
    if variant_fields is not None:
        spec['variants']['fields'] = variant_fields
    errors = validate_mapping_rules(spec, service(), 'mapping')
    assert any(error['code'] == 'ambiguous_table_variant' for error in errors) is conflict
    if conflict:
        assert any(error['field'] == 'mapping.fields[1].sources' for error in errors)
    # The same variant override is still valid for all existing source kinds.
    spec['fields'][1]['sources'] = [{'kind':'css','selector':'.description','extract':'html'}]
    assert not any(error['code'] == 'ambiguous_table_variant' for error in validate_mapping_rules(spec, service(), 'mapping'))


def test_explicit_sample_table_sources_replace_active_candidates():
    from app.setup_ops import _sample_table_sources
    active = mapping()
    active['fields'][1]['sources'][0]['max_pages'] = 2
    candidate = SOURCE | {'max_pages':8}
    assert _sample_table_sources(active, {'table_sources':[candidate]}) == [candidate]
    assert _sample_table_sources(active, {'table_sources':[]}) == []
    assert _sample_table_sources(active, {}) == active['fields'][1]['sources']
