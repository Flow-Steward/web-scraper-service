"""Synthetic sitemap traversal: explicit incompleteness and committed per-source cursors."""
import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.fetching.oneoff import Fetched
from app.jobs import discovery

ROOT = 'https://supplier.example/sitemap.xml'
A = 'https://supplier.example/a.xml'
B = 'https://supplier.example/b.xml'


def urlset(*urls):
    return ('<urlset>' + ''.join(f'<url><loc>{u}</loc></url>' for u in urls) + '</urlset>').encode()


def index(*urls):
    return ('<sitemapindex>' + ''.join(f'<sitemap><loc>{u}</loc></sitemap>' for u in urls) + '</sitemapindex>').encode()


@pytest.fixture(autouse=True)
def public(monkeypatch):
    async def allowed(url):
        return False
    monkeypatch.setattr(discovery, 'url_is_private', allowed)


def fetcher(bodies):
    async def fetch(url):
        body = bodies.get(url)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, Fetched):
            return body
        return None if body is None else Fetched(url, url, 200, body)
    return fetch


async def collect(bodies, state=None, **kwargs):
    state = {} if state is None else state
    return [u async for u, _ in discovery.iter_sitemaps([ROOT], fetcher(bodies), state=state, **kwargs)], state


@pytest.mark.parametrize('body', [None, b'', b'<html>missing</html>', b'<urlset><url>', b'not a sitemap', RuntimeError('token=secret')])
async def test_failed_root_is_incomplete_and_retryable(body):
    urls, state = await collect({ROOT: body})
    assert urls == []
    report = discovery.sitemap_report(state)
    assert report['complete'] is False
    assert report['failed_sources'][0]['url'] == ROOT
    assert 'secret' not in str(report)
    assert (await collect({ROOT: urlset('https://supplier.example/product')}, state))[0] == ['https://supplier.example/product']


@pytest.mark.parametrize('body', [urlset(), index()])
async def test_valid_empty_is_complete(body):
    urls, state = await collect({ROOT: body})
    assert urls == []
    assert discovery.sitemap_report(state)['complete'] is True


async def test_failed_earlier_child_cannot_shift_successful_child_cursor():
    product_a, product_b = 'https://supplier.example/p/a', 'https://supplier.example/p/b'
    urls, state = await collect({ROOT: index(A, B), B: urlset(product_b)})
    assert urls == [product_b]
    assert discovery.sitemap_report(state)['complete'] is False
    urls, _ = await collect({ROOT: index(A, B), A: urlset(product_a), B: urlset(product_b)}, state)
    assert urls == [product_a]
    assert discovery.sitemap_report(state)['complete'] is True


async def test_entry_cap_counts_unfiltered_locations_and_survives_restart():
    bodies = {ROOT: urlset(*[f'https://supplier.example/{i}' for i in range(10)])}
    urls, state = await collect(bodies, max_entries=5)
    assert len(urls) == 5
    assert state['entries_seen'] == 5
    assert discovery.sitemap_report(state)['capped'] is True
    assert (await collect(bodies, copy.deepcopy(state), max_entries=5))[0] == []
    assert state['sources'][ROOT]['page_offset'] == 5


async def test_file_cap_keeps_index_cursor_and_bound():
    urls, state = await collect({ROOT: index(A, B), A: urlset(), B: urlset()}, max_files=2)
    assert urls == []
    assert len(state['sources']) <= 2
    assert discovery.sitemap_report(state)['complete'] is False
    retry = discovery.sitemap_retry_state(state)
    _, retry = await collect({ROOT: index(A, B), B: urlset()}, retry, max_files=2)
    assert discovery.sitemap_report(retry)['complete'] is True


async def test_complete_root_and_nested_child_controls():
    urls, state = await collect({ROOT: index(A), A: urlset('https://supplier.example/p')})
    assert urls == ['https://supplier.example/p']
    assert discovery.sitemap_report(state)['complete'] is True


async def test_changed_source_order_replays_an_incomplete_index_without_skipping_children():
    import hashlib

    product_map = 'https://supplier.example/sitemap-products-0.xml'
    item_a, item_b = 'https://supplier.example/a', 'https://supplier.example/p/1'
    root_body = index(A, product_map)
    # Persisted by an older version: the first source in original order was
    # admitted, then the source-file cap left the index unfinished.
    state = {'sources': {
        ROOT: {'status': 'pending', 'page_offset': 0, 'nested_offset': 1,
               'body_sha256': hashlib.sha256(root_body).hexdigest()},
        A: {'status': 'complete', 'page_offset': 1, 'nested_offset': 0},
    }, 'root_offset': 1}
    urls, state = await collect({ROOT: root_body, A: urlset(item_a), product_map: urlset(item_b)},
                                state, prefer_products=True)
    assert urls == [item_b]
    assert discovery.sitemap_report(state)['complete'] is True


async def test_product_preference_changes_order_without_dropping_other_sources():
    product_map = 'https://supplier.example/sitemap-products-0.xml'
    item_a, item_b = 'https://supplier.example/a', 'https://supplier.example/p/1'
    bodies = {ROOT: index(A, product_map), A: urlset(item_a), product_map: urlset(item_b)}
    assert (await collect(bodies))[0] == [item_a, item_b]
    urls, state = await collect(bodies, prefer_products=True)
    assert urls == [item_b, item_a]
    assert discovery.sitemap_report(state)['complete'] is True


async def test_product_file_admission_keeps_other_children_retryable():
    product_map = 'https://supplier.example/sitemap-products-0.xml'
    item_a, item_b = 'https://supplier.example/a', 'https://supplier.example/p/1'
    bodies = {ROOT: index(A, product_map), A: urlset(item_a), product_map: urlset(item_b)}
    urls, state = await collect(bodies, prefer_products=True, max_files=2)
    assert urls == [item_b]
    assert discovery.sitemap_report(state)['complete'] is False
    retry = discovery.sitemap_retry_state(state)
    urls, retry = await collect(bodies, retry, prefer_products=True, max_files=2)
    assert urls == [item_a]
    assert discovery.sitemap_report(retry)['complete'] is True


async def test_changed_preference_in_both_directions_cannot_skip_an_unseen_source():
    product_map = 'https://supplier.example/sitemap-products-0.xml'
    item_a, item_b = 'https://supplier.example/a', 'https://supplier.example/p/1'
    bodies = {ROOT: index(A, product_map), A: urlset(item_a), product_map: urlset(item_b)}
    urls, state = await collect(bodies, prefer_products=True, max_files=2)
    assert urls == [item_b]
    retry = discovery.sitemap_retry_state(state)
    urls, retry = await collect(bodies, retry, prefer_products=False, max_files=3)
    assert item_a in urls
    assert discovery.sitemap_report(retry)['complete'] is True


async def test_malformed_root_cannot_stop_a_valid_product_source_during_ranking():
    product_map = 'https://supplier.example/sitemap-products-0.xml'
    item = 'https://supplier.example/p/1'
    urls = [u async for u, _ in discovery.iter_sitemaps(
        ['https://[invalid/sitemap.xml', product_map], fetcher({product_map: urlset(item)}),
        prefer_products=True)]
    assert urls == [item]


async def test_product_roots_are_prioritized_before_file_admission_and_retry_does_not_replay():
    product_map = 'https://supplier.example/sitemap-products-0.xml'
    item_a, item_b = 'https://supplier.example/a', 'https://supplier.example/p/1'
    bodies = {A: urlset(item_a), product_map: urlset(item_b)}
    state = {}
    urls = [u async for u, _ in discovery.iter_sitemaps(
        [A, product_map], fetcher(bodies), state=state, prefer_products=True, max_files=1)]
    assert urls == [item_b]
    assert discovery.sitemap_report(state)['complete'] is False
    retry = discovery.sitemap_retry_state(state)
    urls = [u async for u, _ in discovery.iter_sitemaps(
        [A, product_map], fetcher(bodies), state=retry, prefer_products=True, max_files=1)]
    assert urls == [item_a]
    assert discovery.sitemap_report(retry)['complete'] is True


async def test_legacy_root_ordinal_cannot_skip_newly_prioritized_product_map():
    product_map = 'https://supplier.example/sitemap-products-0.xml'
    item_a, item_b = 'https://supplier.example/a', 'https://supplier.example/p/1'
    state = {'sources': {A: {'status': 'complete', 'page_offset': 1, 'nested_offset': 0}},
             'root_offset': 1, 'pending_root': product_map}
    urls = [u async for u, _ in discovery.iter_sitemaps(
        [A, product_map], fetcher({A: urlset(item_a), product_map: urlset(item_b)}),
        state=state, prefer_products=True)]
    assert urls == [item_b]
    assert discovery.sitemap_report(state)['complete'] is True


async def test_changed_root_list_replays_admission_instead_of_skipping_new_front_root():
    c = 'https://supplier.example/c.xml'
    item_a, item_b, item_c = ('https://supplier.example/a', 'https://supplier.example/b',
                              'https://supplier.example/c')
    bodies = {A: urlset(item_a), B: urlset(item_b), c: urlset(item_c)}
    state = {}
    urls = [u async for u, _ in discovery.iter_sitemaps(
        [A, B], fetcher(bodies), state=state, max_files=1)]
    assert urls == [item_a]
    retry = discovery.sitemap_retry_state(state)
    urls = [u async for u, _ in discovery.iter_sitemaps(
        [c, A, B], fetcher(bodies), state=retry, max_files=3)]
    assert item_c in urls and item_b in urls
    assert discovery.sitemap_report(retry)['complete'] is True


def fake_runner(bodies, rt=None):
    from app.jobs.runner import JobRunner
    runner = SimpleNamespace(
        rt={} if rt is None else rt,
        config={'site_url': 'https://supplier.example/'},
        doc={}, svc=SimpleNamespace(db=SimpleNamespace(tx=nullcontext)),
        _cut_or_stopping=lambda: False,
        _persist=lambda: None,
        aux_fetch=fetcher(bodies),
    )
    runner.rt.setdefault('sitemaps', [ROOT])
    runner._completed_ancestor_keys = lambda entries: JobRunner._completed_ancestor_keys(runner, entries)
    async def feed():
        await JobRunner._feed_sitemaps(runner)
    runner.feed = feed
    return runner


async def test_interrupted_enqueue_never_persists_unqueued_cursor():
    bodies = {ROOT: urlset(*[f'https://supplier.example/{i}' for i in range(110)])}
    runner = fake_runner(bodies)
    snapshots = []
    committed_urls = []
    async def interrupted(entries, **kwargs):
        snapshots.append(copy.deepcopy(runner.rt))  # supervisor persists during await
        assert 'sitemap_traversal' not in runner.rt
        raise RuntimeError('queue interrupted')
    runner.enqueue = interrupted
    with pytest.raises(RuntimeError):
        await runner.feed()
    assert 'sitemap_traversal' not in snapshots[0]
    async def enqueue(entries, **kwargs):
        committed_urls.extend(e['url'] for e in entries)
    runner.enqueue = enqueue
    await runner.feed()
    assert len(committed_urls) == 110
    assert runner.doc['sitemap_discovery']['complete'] is True


async def test_second_batch_interruption_replays_only_uncommitted_cursor():
    bodies = {ROOT: urlset(*[f'https://supplier.example/{i}' for i in range(110)])}
    runner = fake_runner(bodies)
    calls = 0
    async def enqueue(entries, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert runner.rt['sitemap_traversal']['entries_seen'] == 100
            raise RuntimeError('queue interrupted')
    runner.enqueue = enqueue
    with pytest.raises(RuntimeError):
        await runner.feed()
    resumed = fake_runner(bodies, copy.deepcopy(runner.rt))
    urls = []
    async def finish(entries, **kwargs):
        urls.extend(e['url'] for e in entries)
    resumed.enqueue = finish
    await resumed.feed()
    assert urls == [f'https://supplier.example/{i}' for i in range(100, 110)]


async def test_filtered_urls_after_budget_are_explicitly_partial(monkeypatch):
    monkeypatch.setattr(discovery, 'SITEMAP_CAP', 5)
    runner = fake_runner({ROOT: urlset(*[f'https://supplier.example/{"product" if i >= 5 else "listing"}/{i}' for i in range(10)])})
    matches = []
    async def enqueue(entries, **kwargs):
        matches.extend(e for e in entries if '/product/' in e['url'])
    runner.enqueue = enqueue
    await runner.feed()
    assert matches == []
    assert runner.rt['sitemap_partial'] is True
    assert runner.doc['sitemap_discovery']['cap_reason'] == 'entry_limit'
    before = copy.deepcopy(runner.rt['sitemap_traversal'])
    await runner.feed()
    assert runner.rt['sitemap_traversal'] == before


async def test_source_urls_in_report_strip_credentials_and_query():
    state = {'sources': {'https://user:password@supplier.example/x.xml?token=secret': {'status': 'failed', 'reason': 'fetch_failed'}}}
    assert discovery.sitemap_report(state)['failed_sources'] == [{'url': 'https://supplier.example/x.xml', 'reason': 'fetch_failed'}]


async def test_api_policy_errors_are_not_converted_to_source_fetch_failure():
    from app.core.errors import ApiError
    with pytest.raises(ApiError):
        await collect({ROOT: ApiError('browser_unavailable', 'not installed')})


async def test_private_source_refusal_is_neutral_and_never_fetched(monkeypatch):
    async def private(url):
        return True
    monkeypatch.setattr(discovery, 'url_is_private', private)
    monkeypatch.setattr(discovery, 'host_is_private', private)
    _, state = await collect({ROOT: RuntimeError('must not fetch')})
    report = discovery.sitemap_report(state)
    assert report['complete'] is True
    assert report['refused_sources'] == [{'url': ROOT, 'reason': 'private_address_refused'}]


async def test_proxy_auth_mid_sitemap_retries_pending_child_without_product_replay():
    from app.fetching.proxy_auth import ProxyAuthenticationRequired
    prior_urls = [f'https://supplier.example/p/{i}' for i in range(100)]
    runner = fake_runner({ROOT: index(A, B), A: urlset(*prior_urls), B: ProxyAuthenticationRequired()})
    accepted = []
    async def enqueue(entries, **kwargs):
        accepted.extend(e['url'] for e in entries)
    runner.enqueue = enqueue
    with pytest.raises(ProxyAuthenticationRequired):
        await runner.feed()
    assert accepted == prior_urls
    resumed = fake_runner({ROOT: index(A, B), A: urlset(*prior_urls), B: urlset('https://supplier.example/new')}, copy.deepcopy(runner.rt))
    resumed.enqueue = enqueue
    await resumed.feed()
    assert accepted == prior_urls + ['https://supplier.example/new']
    assert resumed.doc['sitemap_discovery']['complete'] is True


async def test_many_initial_roots_keep_next_root_for_linked_retry():
    state = {}
    bodies = {ROOT: urlset(), A: urlset(), B: urlset()}
    assert [u async for u, _ in discovery.iter_sitemaps([ROOT, A, B], fetcher(bodies), state=state, max_files=2)] == []
    assert discovery.sitemap_report(state)['pending_sources'] == [{'url': B, 'reason': 'source_file_limit'}]
    retry = discovery.sitemap_retry_state(state)
    assert [u async for u, _ in discovery.iter_sitemaps([ROOT, A, B], fetcher(bodies), state=retry, max_files=2)] == []
    assert set(retry['sources']) == {B}
    assert discovery.sitemap_report(retry)['complete'] is True


@pytest.fixture
def service(tmp_path, monkeypatch):
    import json
    from app.core.db import dumps
    from app.core.schemas import CONTRACT_DIR
    from app.service import Service
    from tests.unit.test_logic import settings
    svc = Service(settings(data_dir=str(tmp_path), secrets_key='test-secret-key-0123456789'))
    monkeypatch.setattr('app.settings._settings', svc.settings)
    doc = json.loads((CONTRACT_DIR / 'examples/get_run.response.json').read_text())
    doc.update(site='supplier.example', sites=['supplier.example'], state='completed', outcome='partial')
    doc['config'].update(discovery_mode='sitemap', sitemap_url=ROOT, site_url='https://supplier.example/')
    doc['resolved']['discovery_mode'] = 'sitemap'
    svc.db.execute('INSERT INTO runs(run_id,idempotency_key,fingerprint,state,site,created_at,updated_at,doc,runtime) VALUES (?,?,?,?,?,?,?,?,?)',
                   (doc['run_id'], 'parent', 'f', 'completed', doc['site'], doc['created_at'], doc['updated_at'], dumps(doc), '{}'))
    yield svc, doc
    svc.db.close()


@pytest.mark.parametrize('source_body,expected', [(None, 'partial'), (b'<urlset/>', 'complete')])
async def test_zero_record_source_failure_changes_result_summary_and_list_outcome(service, source_body, expected):
    from app.core.schemas import validator
    svc, doc = service
    _, state = await collect({ROOT: source_body})
    await svc.jobs._complete_doc(doc['run_id'], doc, {'sitemap_traversal': state}, None)
    summary = svc.jobs.summary(doc['run_id'])
    assert summary['outcome'] == expected
    assert summary['result']['outcome'] == expected
    assert summary['sitemap_discovery']['complete'] is (expected == 'complete')
    assert not list(validator('run_summary.schema.json').iter_errors(summary))
    item = svc.jobs.list_item(svc.jobs._row(doc['run_id']))
    assert item['completeness']['sitemap_discovery'] == summary['sitemap_discovery']
    if expected == 'partial':
        assert 'none missing' not in summary['completeness']['sentence']
        assert 'incomplete' in summary['completeness']['sentence']
        assert 'recollect' in summary['explanation']['advice']


@pytest.mark.parametrize('with_failed_document', [False, True])
async def test_recollect_retries_only_unfinished_sources_and_failed_documents_in_linked_run(service, monkeypatch, with_failed_document):
    import json
    from app.core.db import dumps, loads
    from app.core.schemas import CONTRACT_DIR, validator
    svc, parent = service
    _, state = await collect({ROOT: index(A, B), A: urlset('https://supplier.example/complete')})
    runtime = {'sitemap_traversal': state, 'sitemaps': [ROOT]}
    svc.db.execute('UPDATE runs SET runtime=? WHERE run_id=?', (dumps(runtime), parent['run_id']))
    if with_failed_document:
        svc.db.execute('INSERT INTO unextracted(run_id,url_key,url,reason,doc) VALUES (?,?,?,?,?)',
                       (parent['run_id'], 'failed', 'https://supplier.example/failed', 'fetch_failed', '{}'))
    before = dict(svc.jobs._row(parent['run_id']))
    body = json.loads((CONTRACT_DIR / 'examples/post_run_recollect.request.json').read_text())
    async def rules(config, *args):
        return config, [], []
    monkeypatch.setattr('app.jobs.manager.apply_rules', rules)
    status, child = await svc.jobs.recollect(parent['run_id'], body, 'retry-source')
    assert status == 201
    assert child['recollect_of']['run_id'] == parent['run_id']
    assert child['recollect_of']['url_count'] == int(with_failed_document)
    assert child['recollect_of']['source_count'] == 1
    rt = loads(svc.jobs._row(child['run_id'])['runtime'])
    assert set(rt['sitemap_traversal']['sources']) == {B}
    assert rt['sitemap_traversal']['entries_seen'] == 0
    assert rt['sitemap_retry_roots'] == [ROOT]
    assert rt['sitemap_retry_urls'] == ([{'url': 'https://supplier.example/failed'}] if with_failed_document else [])
    assert not list(validator('run_summary.schema.json').iter_errors(child))
    assert dict(svc.jobs._row(parent['run_id'])) == before
    assert (await svc.jobs.recollect(parent['run_id'], body, 'retry-source'))[0] == 200


@pytest.mark.parametrize('body', [b'<urlset><url/></urlset>', b'<sitemapindex><sitemap><loc/></sitemap></sitemapindex>', b'<urlset><error>denied</error></urlset>', b'https://supplier.example/one\nupstream error'])
async def test_structurally_invalid_sources_never_claim_valid_empty(body):
    _, state = await collect({ROOT: body})
    assert discovery.sitemap_report(state)['failed_sources'] == [{'url': ROOT, 'reason': 'invalid_sitemap'}]


@pytest.mark.parametrize('signal', [True, False])
async def test_feeder_wait_does_not_seal_discovery(signal):
    from app.fetching.proxy_auth import ProxyAuthenticationRequired
    from app.jobs.runner import JobRunner
    runner = fake_runner({})
    runner.state = 'running'
    runner.config['single_page'] = False
    runner.doc['resolved'] = {'discovery_mode': 'sitemap'}
    async def pause():
        runner.state = 'waiting_for_credentials'
        if signal:
            raise ProxyAuthenticationRequired()
    runner._feed_sitemaps = pause
    await JobRunner._feed(runner)
    assert not runner.rt.get('feeder_done')


async def test_linked_retry_capped_sources_continue_after_saved_positions():
    bodies = {ROOT: urlset(*[f'https://supplier.example/{i}' for i in range(10)])}
    _, state = await collect(bodies, max_entries=5)
    retry = discovery.sitemap_retry_state(state)
    urls, retry = await collect(bodies, retry, max_entries=5)
    assert urls == [f'https://supplier.example/{i}' for i in range(5, 10)]
    assert discovery.sitemap_report(retry)['complete'] is True


async def test_source_retry_skips_completed_ancestor_documents_even_if_source_repeats_them(service):
    svc, parent = service
    done, new = 'https://supplier.example/complete', 'https://supplier.example/new'
    svc.db.execute('INSERT INTO urls(run_id,unique_key,url,source,status) VALUES (?,?,?,?,?)',
                   (parent['run_id'], done, done, 'sitemap', 'done'))
    runner = fake_runner({ROOT: urlset(done, new)})
    runner.svc = svc
    runner.doc['recollect_of'] = {'run_id': parent['run_id']}
    accepted = []
    async def enqueue(entries, **kwargs):
        accepted.extend(e['url'] for e in entries)
    runner.enqueue = enqueue
    await runner.feed()
    assert accepted == [new]


async def test_dns_failure_is_incomplete_without_weakening_private_guard(monkeypatch):
    async def guard(url):
        return True
    async def unresolved(host):
        return None
    monkeypatch.setattr(discovery, 'url_is_private', guard)
    monkeypatch.setattr(discovery, 'host_is_private', unresolved)
    _, state = await collect({ROOT: RuntimeError('must never fetch unresolved source')})
    report = discovery.sitemap_report(state)
    assert report['complete'] is False
    assert report['failed_sources'] == [{'url': ROOT, 'reason': 'fetch_failed'}]
    assert report['refused_sources'] == []


async def test_linked_retry_shared_source_and_cycle_skip_completed_grandparent_document(service):
    from app.core.db import dumps
    svc, grandparent = service
    done, new = 'https://supplier.example/complete', 'https://supplier.example/new'
    _, original = await collect({ROOT: index(A, B), A: urlset(done)})
    parent_id = 'run_aaaaaaaaaaaaaaaaaaaa'
    parent_doc = dict(grandparent, run_id=parent_id)
    svc.db.execute('INSERT INTO runs(run_id,idempotency_key,fingerprint,state,site,created_at,updated_at,doc,runtime,parent_run_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (parent_id, 'middle', 'f', 'completed', grandparent['site'], grandparent['created_at'], grandparent['updated_at'], dumps(parent_doc), '{}', grandparent['run_id']))
    svc.db.execute('INSERT INTO urls(run_id,unique_key,url,source,status) VALUES (?,?,?,?,?)',
                   (grandparent['run_id'], done, done, 'sitemap', 'done'))
    c, d = 'https://supplier.example/c.xml', 'https://supplier.example/d.xml'
    runner = fake_runner({B: index(A, c), A: urlset(done), c: index(B, d), d: urlset(new)},
                         {'sitemaps': [ROOT], 'sitemap_traversal': discovery.sitemap_retry_state(original)})
    runner.svc = svc
    runner.doc['recollect_of'] = {'run_id': parent_id}
    accepted = []
    async def enqueue(entries, **kwargs):
        accepted.extend(e['url'] for e in entries)
    runner.enqueue = enqueue
    await runner.feed()
    assert accepted == [new]
    assert runner.doc['sitemap_discovery']['complete'] is True
    assert set(runner.rt['sitemap_traversal']['sources']) == {A, B, c, d}


@pytest.mark.parametrize('changed', [
    ['https://supplier.example/new', 'https://supplier.example/a', 'https://supplier.example/b'],
    ['https://supplier.example/b', 'https://supplier.example/a'],
])
async def test_changed_incomplete_source_replays_new_front_and_reordered_entries(changed):
    state = {}
    iterator = discovery.iter_sitemaps([ROOT], fetcher({ROOT: urlset('https://supplier.example/a', 'https://supplier.example/b')}), state=state)
    assert (await anext(iterator))[0] == 'https://supplier.example/a'
    await iterator.aclose()
    old_fingerprint = state['sources'][ROOT].get('body_sha256')
    urls, state = await collect({ROOT: urlset(*changed)}, state)
    assert urls == changed
    assert state['entries_seen'] == 1 + len(changed)
    assert state['sources'][ROOT]['body_sha256'] != old_fingerprint
    assert discovery.sitemap_report(state)['complete'] is True


async def test_legacy_incomplete_cursor_without_fingerprint_replays_instead_of_skipping():
    state = {'sources': {ROOT: {'status': 'pending', 'page_offset': 2, 'nested_offset': 0}}, 'root_offset': 1, 'entries_seen': 2}
    urls, state = await collect({ROOT: urlset('https://supplier.example/new', 'https://supplier.example/a')}, state)
    assert urls == ['https://supplier.example/new', 'https://supplier.example/a']
    assert state['entries_seen'] == 4
    assert len(state['sources'][ROOT]['body_sha256']) == 64


async def test_changed_source_replay_spends_existing_budget_and_remains_partial():
    a, b, new = 'https://supplier.example/a', 'https://supplier.example/b', 'https://supplier.example/new'
    state = {}
    iterator = discovery.iter_sitemaps([ROOT], fetcher({ROOT: urlset(a, b)}), state=state, max_entries=3)
    await anext(iterator)
    await anext(iterator)
    await iterator.aclose()
    urls, state = await collect({ROOT: urlset(new, a, b)}, state, max_entries=3)
    assert urls == [new]
    assert state['entries_seen'] == 3
    assert discovery.sitemap_report(state)['complete'] is False
    assert discovery.sitemap_report(state)['cap_reason'] == 'entry_limit'
    retry = discovery.sitemap_retry_state(state)
    urls, retry = await collect({ROOT: urlset(new, a, b)}, retry, max_entries=3)
    assert urls == [a, b]
    assert discovery.sitemap_report(retry)['complete'] is True


async def test_changed_pending_index_replays_new_front_child():
    new_child = 'https://supplier.example/new.xml'
    _, original = await collect({ROOT: index(A, B), A: urlset()}, max_files=2)
    retry = discovery.sitemap_retry_state(original)
    urls, retry = await collect({ROOT: index(new_child, A, B), new_child: urlset('https://supplier.example/new'), A: urlset(), B: urlset()}, retry, max_files=4)
    assert urls == ['https://supplier.example/new']
    assert discovery.sitemap_report(retry)['complete'] is True


async def test_completed_sources_are_not_repolled_for_body_changes():
    _, state = await collect({ROOT: urlset('https://supplier.example/a')})
    urls, state = await collect({ROOT: RuntimeError('completed source must not be fetched again')}, state)
    assert urls == []
    assert discovery.sitemap_report(state)['complete'] is True


@pytest.mark.parametrize('map_body,partial', [(None, True), (Fetched(ROOT, ROOT, 503, b'<html>unavailable</html>'), True), (b'<urlset/>', False)])
async def test_auto_crawl_fallback_keeps_explicit_source_probe_failure(service, map_body, partial):
    svc, doc = service
    doc['config'].update(discovery_mode='auto', url_include=[], url_exclude=[])
    runtime = {'sitemaps': [ROOT]}
    runner = SimpleNamespace(doc=doc, config=doc['config'], rt=runtime, mappings_for=lambda site: [])
    mode, _ = await discovery.decide_discovery(runner, [ROOT], fetcher({ROOT: map_body}))
    assert mode == 'crawl'
    doc['resolved']['discovery_mode'] = mode
    await svc.jobs._complete_doc(doc['run_id'], doc, runtime, None)
    summary = svc.jobs.summary(doc['run_id'])
    assert summary['outcome'] == ('partial' if partial else 'complete')
    if partial:
        assert summary['sitemap_discovery']['failed_sources'] == [{'url': ROOT, 'reason': 'fetch_failed'}]
        assert 'none missing' not in summary['completeness']['sentence']
        assert summary['sitemap_probe']['complete'] is False
        assert runtime['sitemap_traversal']['sources'][ROOT]['page_offset'] == 0


async def test_auto_probe_successful_prefix_is_not_a_committed_feeder_cursor(service):
    _, doc = service
    product = 'https://supplier.example/product/a'
    doc['config'].update(discovery_mode='auto', url_include=[], url_exclude=[])
    bodies = {ROOT: index(A, B), A: urlset(product), doc['config']['site_url']: b'<main>Healthy start page</main>'}
    runtime = {'sitemaps': [ROOT]}
    runner = SimpleNamespace(doc=doc, config=doc['config'], rt=runtime, mappings_for=lambda site: [])
    mode, _ = await discovery.decide_discovery(runner, [ROOT], fetcher(bodies))
    assert mode == 'sitemap'
    assert runtime['sitemap_traversal']['sources'][A]['page_offset'] == 0
    feeder = fake_runner(bodies, runtime)
    accepted = []
    async def enqueue(entries, **kwargs):
        accepted.extend(e['url'] for e in entries)
    feeder.enqueue = enqueue
    await feeder.feed()
    assert accepted == [product]
    assert feeder.rt['sitemap_partial'] is True


async def test_auto_initial_probe_cap_remains_partial_if_next_body_changes(service, monkeypatch):
    svc, doc = service
    monkeypatch.setattr(discovery, 'SITEMAP_CAP', 5)
    doc['config'].update(discovery_mode='auto', url_include=['/product/'], url_exclude=[])
    runtime = {'sitemaps': [ROOT]}
    runner = SimpleNamespace(doc=doc, config=doc['config'], rt=runtime, mappings_for=lambda site: [])
    body = urlset(*[f'https://supplier.example/{"listing" if i < 5 else "product"}/{i}' for i in range(10)])
    mode, _ = await discovery.decide_discovery(runner, [ROOT], fetcher({ROOT: body}))
    assert mode == 'crawl'
    assert runtime['sitemap_probe']['capped'] is True
    feeder = fake_runner({ROOT: urlset()}, runtime)
    feeder.doc = doc
    async def enqueue(*args, **kwargs):
        pass
    feeder.enqueue = enqueue
    await feeder.feed()  # A later healthy, shorter body must not erase the initial cap.
    await svc.jobs._complete_doc(doc['run_id'], doc, runtime, None)
    summary = svc.jobs.summary(doc['run_id'])
    assert summary['outcome'] == 'partial'
    assert summary['sitemap_probe']['cap_reason'] == 'entry_limit'
    assert 'none missing' not in summary['completeness']['sentence']


@pytest.mark.parametrize('status,partial', [(503, True), (404, False), (410, False)])
async def test_auto_default_probe_distinguishes_unavailable_from_absent(service, monkeypatch, status, partial):
    import asyncio
    from app.jobs.runner import JobRunner
    svc, doc = service
    doc['config'].update(discovery_mode='auto', sitemap_url=None, single_page=False, url_include=[], url_exclude=[])
    runtime = {}
    calls = []
    async def fetch(url):
        calls.append(url)
        if url == ROOT:
            return Fetched(url, url, status, b'<html>unavailable</html>')
        return Fetched(url, url, 200, b'<main>Healthy start page</main>')
    async def public(url):
        return False
    async def robots(url):
        return None
    async def allowed(url):
        return True
    monkeypatch.setattr('app.jobs.runner.url_is_private', public)
    runner = SimpleNamespace(doc=doc, config=doc['config'], rt=runtime, mappings_for=lambda site: [],
                             aux_fetch=fetch, _robots_for=robots, robots_allowed=allowed,
                             timeline=lambda *args: None, lock=asyncio.Lock(), _persist=lambda: None)
    assert await JobRunner._resolve_discovery(runner)
    assert doc['resolved']['discovery_mode'] == 'crawl'
    assert calls.count(ROOT) == 1
    await svc.jobs._complete_doc(doc['run_id'], doc, runtime, None)
    summary = svc.jobs.summary(doc['run_id'])
    assert summary['outcome'] == ('partial' if partial else 'complete')
    if partial:
        assert summary['sitemap_discovery']['failed_sources'] == [{'url': ROOT, 'reason': 'fetch_failed'}]


async def test_auto_failed_source_recollect_preserves_healthy_crawl_documents(service, monkeypatch):
    import json
    from app.core.db import loads
    from app.core.schemas import CONTRACT_DIR, validator
    svc, doc = service
    done, new = 'https://supplier.example/complete', 'https://supplier.example/new'
    doc['config'].update(discovery_mode='auto', url_include=[], url_exclude=[])
    runtime = {'sitemaps': [ROOT]}
    runner = SimpleNamespace(doc=doc, config=doc['config'], rt=runtime, mappings_for=lambda site: [])
    assert (await discovery.decide_discovery(runner, [ROOT], fetcher({ROOT: None})))[0] == 'crawl'
    svc.db.execute('INSERT INTO urls(run_id,unique_key,url,source,status) VALUES (?,?,?,?,?)',
                   (doc['run_id'], done, done, 'link', 'done'))
    await svc.jobs._complete_doc(doc['run_id'], doc, runtime, None)
    before = dict(svc.jobs._row(doc['run_id']))
    body = json.loads((CONTRACT_DIR / 'examples/post_run_recollect.request.json').read_text())
    async def rules(config, *args):
        return config, [], []
    monkeypatch.setattr('app.jobs.manager.apply_rules', rules)
    status, child = await svc.jobs.recollect(doc['run_id'], body, 'auto-source-retry')
    assert status == 201
    assert child['recollect_of']['source_count'] == 1
    assert child['recollect_of']['url_count'] == 0
    child_runtime = loads(svc.jobs._row(child['run_id'])['runtime'])
    feeder = fake_runner({ROOT: urlset(done, new)}, child_runtime)
    feeder.svc = svc
    feeder.doc = child
    accepted = []
    async def enqueue(entries, **kwargs):
        accepted.extend(e['url'] for e in entries)
    feeder.enqueue = enqueue
    await feeder.feed()
    assert accepted == [new]
    assert not list(validator('run_summary.schema.json').iter_errors(svc.jobs.summary(doc['run_id'])))
    assert dict(svc.jobs._row(doc['run_id'])) == before
