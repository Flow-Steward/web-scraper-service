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
async def test_offline_mapping_test_requires_matching_complete_capture(sample_service, captured):
    from app.extraction.tables import source_key
    from app.setup_ops import mapping_test
    sid = 'smp_table'
    sample_service.samples.save({'sample_id': sid, 'url': 'https://parts.example.test/mock', 'expires_at': '2030-01-01T00:00:00Z', 'variant_sources': []}, '<h1>Mock part</h1>', 'https://parts.example.test/mock')
    if captured:
        sample_service.samples.save_extra(sid, {'table_captures': {source_key(SOURCE): capture()}})
    result = await mapping_test(sample_service, {'sample_ids': [sid], 'mappings': [mapping()], 'required_fill_threshold': .9})
    assert result['passed'] is captured
    assert result['samples'][0]['table_coverage']['fitment_raw']['complete'] is captured
    if captured:
        assert result['samples'][0]['data']['fitment_raw'].count('<td>Mock A</td>') == 2
    else:
        assert result['samples'][0]['missing_required'] == ['fitment_raw']


def test_sample_table_capture_extras_merge_with_variant_json(sample_service):
    from app.extraction.tables import source_key
    sid = 'smp_table'
    sample_service.samples.save_extra(sid, {'shop_json': {'https://parts.example.test/mock.js': {'variants': []}}})
    sample_service.samples.save_extra(sid, {'table_captures': {source_key(SOURCE): capture()}})
    assert 'shop_json' in sample_service.samples.load_extra(sid)
    assert 'table_captures' in sample_service.samples.load_extra(sid)


async def test_runtime_http_table_source_fails_closed_and_preserves_capture_reason(tmp_path, monkeypatch):
    from crawlee import Request
    from app.jobs.runner import JobRunner
    from tests.unit.test_proxy_auth_pause import make_run
    svc, runner = await make_run(tmp_path, monkeypatch, config_overrides={'urls': [{'url': 'https://target.test/one'}], 'required_fields': []})
    spec = mapping() | {'__version': 1}
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
        assert result['page_class'] == 'incomplete'
        assert result['missing_fields'] == ['fitment_raw']
        assert result['table_coverage']['fitment_raw']['reason'] == 'browser_required'
        runner._store_page(result, 'table-key', 'target.test')
        from app.core.db import loads
        stored = loads(svc.db.one('SELECT doc FROM stored_pages WHERE run_id=? AND page_key=?', (runner.run_id, 'table-key'))['doc'])
        assert stored['table_captures']
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
