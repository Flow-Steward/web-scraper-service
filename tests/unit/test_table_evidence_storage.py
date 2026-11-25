"""Private table archives use one bounded KVS blob and durable quota reservations."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.core.db import loads
from app.extraction.tables import source_key
from app.jobs.runner import JobRunner
from tests.unit.test_proxy_auth_pause import make_run
from tests.unit.test_table_sources import SOURCE, capture


def outcome(url='https://target.test/one', complete=False):
    return {'url':url, 'page_class':'incomplete', 'template_id':'product', 'status':200,
            'table_captures':{source_key(SOURCE):capture(complete)},
            'table_coverage':{'fitment_raw':{'complete':complete,'reason':'finished' if complete else 'page_limit','pages':2,'row_count':2,'bytes':100}},
            'record':{'data':{'fitment_raw':capture()['html'] if complete else None}}}


async def test_private_archive_is_one_reference_charged_once_across_restart(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    await runner._open_storages()
    svc.settings.limits['max_result_bytes'] = 2000
    async def store():
        return runner.kvs
    ctx = SimpleNamespace(get_key_value_store=store)
    first = outcome()
    try:
        await runner._archive_table_evidence(ctx, first)
        assert 'table_captures' not in first
        assert first['table_evidence']['retained'] is True
        used = runner.rt['table_evidence_bytes']
        assert 0 < used <= 2000
        repeated = outcome()
        await runner._archive_table_evidence(ctx, repeated)
        assert runner.rt['table_evidence_bytes'] == used
        assert repeated['table_evidence']['key'] == first['table_evidence']['key']
        restored = JobRunner(svc, runner.run_id)
        assert restored.rt['table_evidence_bytes'] == used
        second = outcome('https://target.test/two')
        await restored._archive_table_evidence(ctx, second)
        assert second['table_evidence']['retained'] is False
        assert second['table_evidence']['reason'] == 'storage_limit'
        assert restored.rt['table_evidence_bytes'] == used
        runner._store_page(first | {'kvs_key':'fixture_html'}, 'private-evidence', 'target.test')
        doc = loads(svc.db.one('SELECT doc FROM stored_pages WHERE run_id=? AND page_key=?',(runner.run_id,'private-evidence'))['doc'])
        assert 'table_captures' not in doc
        assert doc['table_evidence']['key'] == first['table_evidence']['key']
        loaded = runner._stored_table_captures_sync(doc)
        assert loaded[source_key(SOURCE)]['rows'] == [['Mock A','2011-09',''],['Mock A','2011-09','']]
        runner.cleanup_crawlee_storage()
        assert runner._stored_table_captures_sync(doc)[source_key(SOURCE)]['rows']
        await runner.kvs.set_value(first['table_evidence']['key'], None)
        missing = runner._stored_table_captures_sync(doc)
        assert missing[source_key(SOURCE)]['complete'] is False
        assert missing[source_key(SOURCE)]['reason'] == 'evidence_unavailable'
    finally:
        svc.db.close()


async def test_archive_exhaustion_never_erases_known_complete_field(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    svc.settings.limits['max_result_bytes'] = 0
    known = outcome(complete=True)
    original = known['record']['data']['fitment_raw']
    try:
        await runner._archive_table_evidence(SimpleNamespace(), known)
        assert known['table_evidence']['reason'] == 'storage_limit'
        assert known['record']['data']['fitment_raw'] == original
        assert known['table_coverage']['fitment_raw']['complete'] is True
        assert 'table_captures' not in known
        known['table_captures'] = {source_key(SOURCE):capture()}
        known['page_class'] = 'extracted'
        await runner._archive_table_evidence(SimpleNamespace(), known)
        assert known['table_evidence']['reason'] == 'record_complete'
        assert known['record']['data']['fitment_raw'] == original
    finally:
        svc.db.close()


async def test_repair_sample_reuses_archive_reference_without_blob_copy(tmp_path, monkeypatch):
    from app.setup_ops import mapping_test
    from tests.unit.test_table_sources import mapping
    svc, runner = await make_run(tmp_path, monkeypatch)
    await runner._open_storages()
    async def store():
        return runner.kvs
    item = outcome()
    try:
        await runner.kvs.set_value('fixture_html', '<h1>Mock part</h1>', content_type='text/html')
        await runner._archive_table_evidence(SimpleNamespace(get_key_value_store=store), item)
        runner._store_page(item | {'kvs_key':'fixture_html'}, 'repair-evidence', 'target.test')
        row = svc.db.one('SELECT * FROM stored_pages WHERE run_id=? AND page_key=?', (runner.run_id,'repair-evidence'))
        sample = runner._sample_from_stored(row, 'product', None)
        extra = svc.samples.load_extra(sample['sample_id'])
        assert 'table_captures' not in extra
        assert extra['table_evidence']['key'] == item['table_evidence']['key']
        result = await mapping_test(svc, {'sample_ids':[sample['sample_id']], 'mappings':[mapping()], 'required_fill_threshold':.9})
        assert result['samples'][0]['table_coverage']['fitment_raw']['reason'] == 'page_limit'
    finally:
        svc.db.close()


async def test_sample_archive_limit_does_not_mutate_known_values(tmp_path, monkeypatch):
    from app.setup_ops import SetupFetch
    from app.extraction.page import Page
    svc, runner = await make_run(tmp_path, monkeypatch)
    sf = SetupFetch(svc, {'browser':'chromium'}, {})
    svc.settings.limits['max_result_bytes'] = 0
    page = Page('<h1>Mock part</h1>', 'https://parts.example.test/mock')
    page.table_captures = {source_key(SOURCE):capture()}
    sample = {'sample_id':'smp_optional_archive', 'extracted':{'fitment_raw':capture()['html']}}
    try:
        sf.save_table_sample(sample, page)
        extra = svc.samples.load_extra(sample['sample_id'])
        assert 'table_captures' not in extra
        assert extra['table_evidence']['reason'] == 'storage_limit'
        assert sample['extracted']['fitment_raw'] == capture()['html']
        assert sample['table_evidence']['retained'] is False
    finally:
        svc.db.close()


async def test_interrupted_archive_reservation_survives_restart_without_double_charge(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    await runner._open_storages()
    svc.settings.limits['max_result_bytes'] = 2000
    real_store = runner.kvs
    class Interrupted:
        async def get_value(self, key):
            return await real_store.get_value(key)
        async def set_value(self, key, value, **kwargs):
            if not key.endswith('_reservation'):
                raise OSError('synthetic private archive interruption')
            return await real_store.set_value(key, value, **kwargs)
    async def interrupted_store():
        return Interrupted()
    async def recovered_store():
        return real_store
    first = outcome()
    try:
        await runner._archive_table_evidence(SimpleNamespace(get_key_value_store=interrupted_store), first)
        used = runner.rt['table_evidence_bytes']
        assert first['table_evidence']['reason'] == 'write_failed'
        assert used > 0
        restored = JobRunner(svc, runner.run_id)
        retried = outcome()
        await restored._archive_table_evidence(SimpleNamespace(get_key_value_store=recovered_store), retried)
        assert retried['table_evidence']['retained'] is True
        assert restored.rt['table_evidence_bytes'] == used
        assert restored._stored_table_captures_sync({'table_evidence':retried['table_evidence']})[source_key(SOURCE)]['rows']
    finally:
        svc.db.close()


def test_public_sample_table_preview_has_one_utf8_budget_and_full_counts():
    import json
    from app.extraction.distill import sample_doc
    from app.extraction.page import Page
    page = Page('<h1>Mock part</h1>', 'https://parts.example.test/mock')
    page.table_captures = {f'{index:064x}':capture() | {'rows':[['界'*100]*64 for _ in range(10)],'headers':['界'*100]*64} for index in range(55)}
    doc = sample_doc(sample_id='smp_preview', page=page, requested_url=None, source_key=None,
                     fetched_at='2026-11-25T00:00:00Z', expires_at='2030-01-01T00:00:00Z',
                     fetch={}, matched_template_id='product', suggested_template_id='product',
                     structured_types=[], max_bytes=1000, extracted=None)
    previews = doc['table_captures']
    assert len(json.dumps(previews, ensure_ascii=False).encode()) <= 65536
    assert len(previews) == 55
    assert all(item['row_count'] == 10 and item['column_count'] == 64 for item in previews)
    assert all(item['preview_truncated'] is True and 'rows' not in item and 'headers' not in item for item in previews)
    assert all(len(cell.encode()) <= 256 for item in previews for row in item['rows_preview'] for cell in row)


async def test_public_sample_omits_known_table_values_when_private_archive_is_full(tmp_path, monkeypatch):
    import json
    from app.extraction.distill import sample_doc
    from app.extraction.page import Page
    from app.setup_ops import SetupFetch
    svc, runner = await make_run(tmp_path, monkeypatch)
    svc.settings.limits['max_result_bytes'] = 0
    page = Page('<h1>Mock part</h1>', 'https://parts.example.test/mock')
    page.table_captures = {source_key(SOURCE):capture()}
    known = {'title':'Mock part','fitment_raw':'<table>'+'PRIVATE_FULL_TABLE'*10000+'</table>'}
    try:
        sample = sample_doc(sample_id='smp_omitted_table', page=page, requested_url=None, source_key=None,
                            fetched_at='2026-11-25T00:00:00Z', expires_at='2030-01-01T00:00:00Z', fetch={},
                            matched_template_id='product', suggested_template_id='product', structured_types=[],
                            max_bytes=1000, extracted=known, table_fields=['fitment_raw'])
        sf = SetupFetch(svc, {'browser':'chromium'}, {})
        sf.save_table_sample(sample, page)
        svc.samples.save(sample, page.html, page.url)
        stored, _, _ = svc.samples.load(sample['sample_id'])
        assert stored['extracted'] == {'title':'Mock part'}
        assert 'PRIVATE_FULL_TABLE' not in json.dumps(stored)
        assert sample['table_fields_omitted'] == ['fitment_raw']
        assert sample['extracted'] == {'title':'Mock part'}
        assert 'PRIVATE_FULL_TABLE' not in json.dumps(sample)
        assert len(json.dumps(sample).encode()) <= 65536
        assert svc.samples.load_extra(sample['sample_id'])['table_evidence']['reason'] == 'storage_limit'
        assert known['fitment_raw'].count('PRIVATE_FULL_TABLE') == 10000
    finally:
        svc.db.close()
