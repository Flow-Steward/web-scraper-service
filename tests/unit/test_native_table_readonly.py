"""Native table mode gates real request stages and owns completion, not route callbacks."""
import asyncio
from types import SimpleNamespace

import pytest

from app.fetching.chromium_destinations import ChromiumDocumentGuard
from app.fetching.firefox_policy import FirefoxDocumentGuard, FirefoxPolicyBridge

URL = 'https://catalogue.test/product'


class Channel:
    def __init__(self):
        self.calls = []
        self.closed = False
    async def send(self, method, params=None):
        self.calls.append((method, params))


def chrome():
    channel = Channel()
    async def close():
        pass
    page = SimpleNamespace(workers=[], url=URL, is_closed=lambda: False, close=close)
    guard = ChromiumDocumentGuard(page, URL)
    guard.session, guard.main_frame_id = channel, 'main'
    return guard, channel


def event(url=URL + '/data', method='GET', **extra):
    return {'requestId': 'fetch', 'networkId': 'network', 'frameId': 'main',
            'resourceType': 'XHR', 'request': {'url': url, 'method': method}, **extra}


async def test_chromium_table_mode_arms_native_all_resource_patterns():
    guard, channel = chrome()
    await guard.enable_table_readonly()
    options = [p for m, p in channel.calls if m == 'Fetch.enable'][-1]
    assert options['handleAuthRequests']
    assert {p['requestStage'] for p in options['patterns']} == {'Request', 'Response'}
    assert all('resourceType' not in p for p in options['patterns'])


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE', 'HEAD'])
async def test_chromium_table_mode_denies_non_get_at_native_request_stage(monkeypatch, method):
    async def public(url):
        return False
    monkeypatch.setattr('app.fetching.chromium_destinations.url_is_private', public)
    guard, channel = chrome()
    await guard.enable_table_readonly()
    await guard._response(event(method=method))
    assert channel.calls[-1][0] == 'Fetch.failRequest'
    assert guard.errors


@pytest.mark.parametrize('target', ['http://internal.test/private', 'https://catalogue.test/cart?add-to-cart=1', 'https://u:p@catalogue.test/data', 'file:///tmp/private'])
async def test_chromium_table_redirect_is_checked_before_follow(monkeypatch, target):
    async def private(url):
        return 'internal.test' in url
    monkeypatch.setattr('app.fetching.chromium_destinations.url_is_private', private)
    guard, channel = chrome()
    await guard.enable_table_readonly()
    await guard._response(event(responseStatusCode=302, responseHeaders=[{'name': 'Location', 'value': target}]))
    assert channel.calls[-1][0] == 'Fetch.failRequest'


async def test_chromium_native_completion_controls_quiescence(monkeypatch):
    async def public(url):
        return False
    monkeypatch.setattr('app.fetching.chromium_destinations.url_is_private', public)
    guard, channel = chrome()
    await guard.enable_table_readonly()
    guard._on_network_request({'requestId': 'n', 'request': {'url': URL + '/data', 'method': 'GET'}})
    await guard._response(event())
    assert not await guard.table_quiescent(.03, quiet_s=.01)
    guard._on_network_done({'requestId': 'n'})
    assert await guard.table_quiescent(.1, quiet_s=.01)


async def test_chromium_table_resource_policy_does_not_use_product_scope(monkeypatch):
    async def public(url):
        return False
    monkeypatch.setattr('app.fetching.chromium_destinations.url_is_private', public)
    guard, channel = chrome()
    guard.destination_policy = lambda url: False
    await guard.enable_table_readonly(resource_policy=lambda url: url.endswith('/data'))
    await guard._response(event())
    assert channel.calls[-1][0] == 'Fetch.continueRequest'
    assert not guard.errors


async def test_firefox_table_mode_requires_an_addon_ack():
    bridge = FirefoxPolicyBridge(table_worker_isolation=True)
    bridge._worker_isolation_ready = True
    page = SimpleNamespace(url=URL, workers=[], context=SimpleNamespace(browser=SimpleNamespace(_wss_firefox_policy_bridge=bridge)))
    guard = FirefoxDocumentGuard(page, URL, timeout_s=.03)
    guard.active = True
    try:
        with pytest.raises((TimeoutError, RuntimeError)):
            await guard.enable_table_readonly()
        assert not await guard.decide_table(URL + '/data', 'xmlhttprequest', 0, 'GET', request_id='n')
    finally:
        await bridge.close()


async def test_firefox_native_redirect_policy_rechecks_private_and_method(monkeypatch):
    async def private(url):
        return 'internal.test' in url
    monkeypatch.setattr('app.fetching.firefox_policy.url_is_private', private)
    guard = FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)), URL)
    guard.active = True
    guard._table_mode = guard.generation
    assert not await guard.decide_table('http://internal.test/private', 'xmlhttprequest', 1, 'GET', request_id='n')
    assert not await guard.decide_table(URL + '/data', 'xmlhttprequest', 0, 'POST', request_id='n')


@pytest.mark.parametrize('engine', ['chromium', 'firefox'])
async def test_quiet_interval_starts_at_each_quiescence_call(engine):
    if engine == 'chromium':
        guard, _ = chrome()
        guard._network_activity = 0
    else:
        guard = FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)), URL)
        guard.active = True
        guard._table_activity = 0
    assert not await guard.table_quiescent(.01, quiet_s=.05)


@pytest.mark.parametrize('locations', [
    ['http://internal.test/private','https://catalogue.test/safe'],
    ['https://catalogue.test/safe','http://internal.test/private'],
])
async def test_duplicate_redirect_headers_cannot_hide_a_private_target(monkeypatch, locations):
    async def private(url):
        return 'internal.test' in url
    monkeypatch.setattr('app.fetching.chromium_destinations.url_is_private', private)
    guard, channel = chrome()
    await guard._response(event(responseStatusCode=302,responseHeaders=[{'name':'LoCaTiOn','value':v} for v in locations]))
    assert channel.calls[-1][0] == 'Fetch.failRequest'


async def test_off_mode_xhr_redirect_checks_only_private_not_product_scope(monkeypatch):
    async def private(url):
        return 'internal.test' in url
    monkeypatch.setattr('app.fetching.chromium_destinations.url_is_private', private)
    guard, channel = chrome()
    guard.destination_policy = lambda url: False
    await guard._response(event(responseStatusCode=302,responseHeaders=[{'name':'Location','value':'https://cdn.test/asset'}]))
    assert channel.calls[-1][0] == 'Fetch.continueRequest'
    await guard._response(event(responseStatusCode=302,responseHeaders=[{'name':'Location','value':'http://internal.test/private'}]))
    assert channel.calls[-1][0] == 'Fetch.failRequest'


async def test_firefox_private_resource_redirect_is_a_binding_policy_error(monkeypatch):
    async def private(url):
        return True
    monkeypatch.setattr('app.fetching.firefox_policy.url_is_private', private)
    bridge = FirefoxPolicyBridge()
    guard = SimpleNamespace(active=True,generation='g',errors=[])
    bridge._tabs[1] = SimpleNamespace(guard=guard,generation='g',store_id='s',bound=True,enrolled=asyncio.get_running_loop().create_future())
    result = await bridge._dispatch('/private-redirect', {'url':'http://internal.test/private','tabId':1,'storeId':'s','generation':'g'})
    assert result['allow'] is False
    assert guard.errors
    await bridge.close()


async def test_chromium_table_resource_policy_is_time_bounded(monkeypatch):
    async def public(url):
        return False
    async def pending_policy(url):
        await asyncio.Event().wait()
    monkeypatch.setattr('app.fetching.chromium_destinations.url_is_private', public)
    guard, channel = chrome()
    guard.timeout_s=.01
    await guard.enable_table_readonly(pending_policy)
    try:
        await asyncio.wait_for(guard._response(event()), .1)
    except TimeoutError:
        pytest.fail('native table destination callback escaped guard timeout')
    assert channel.calls[-1][0] == 'Fetch.failRequest'
    assert guard.errors


async def test_firefox_unknown_and_stale_resource_owner_cannot_poison_other_binding(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def private(url):
        started.set()
        await release.wait()
        return True
    monkeypatch.setattr('app.fetching.firefox_policy.url_is_private', private)
    bridge = FirefoxPolicyBridge()
    guard = SimpleNamespace(active=True,generation='old',errors=[])
    old = SimpleNamespace(guard=guard,generation='old',store_id='s',bound=True,enrolled=asyncio.get_running_loop().create_future())
    bridge._tabs[1] = old
    pending=asyncio.create_task(bridge._dispatch('/private-redirect', {'url':'http://internal.test/private','tabId':1,'storeId':'s','generation':'old'}))
    await started.wait()
    guard.generation='new'
    release.set()
    assert await pending == {'allow':False}
    assert await bridge._dispatch('/private-redirect', {'url':'http://internal.test/private'}) == {'allow':False}
    assert guard.errors == []
    await bridge.close()


async def test_chromium_http_data_failure_is_not_a_security_denial(monkeypatch):
    async def public(url): return False
    monkeypatch.setattr('app.fetching.chromium_destinations.url_is_private',public)
    guard,channel=chrome()
    await guard.enable_table_readonly()
    await guard._response(event(responseStatusCode=500,responseHeaders=[]))
    assert not guard.errors
    assert guard.table_load_failure=='table_http_failed'
    assert channel.calls[-1][0]=='Fetch.continueRequest'
    assert await guard.table_quiescent(.1,quiet_s=.01)


async def test_native_transport_failures_are_nonsecurity_load_diagnostics():
    guard,channel=chrome()
    await guard.enable_table_readonly()
    guard._on_network_request({'requestId':'n','type':'XHR','request':{'url':URL+'/data'}})
    guard._on_network_failed({'requestId':'n','errorText':'net::ERR_CONNECTION_RESET'})
    assert not guard.errors
    assert guard.table_load_failure=='table_request_failed'
    page=SimpleNamespace(context=SimpleNamespace(browser=SimpleNamespace()),workers=[])
    ff=FirefoxDocumentGuard(page,URL)
    ff.active=True;ff._table_mode=ff.generation;ff._table_pending['n']=URL+'/data'
    ff.table_completed('n',True)
    assert not ff.errors
    assert ff.table_load_failure=='table_request_failed'


async def test_firefox_retained_closed_binding_accepts_completion_only():
    bridge=FirefoxPolicyBridge()
    completed=[]
    guard=SimpleNamespace(active=False,generation='g',_table_mode='g',page=SimpleNamespace(is_closed=lambda:True),table_completed=lambda request_id,failed:completed.append((request_id,failed)))
    binding=SimpleNamespace(guard=guard,generation='g',tab_id=1,store_id='s',bound=True)
    bridge._table_latches[1]=binding
    identity={'tabId':1,'storeId':'s','generation':'g'}
    result=await bridge._dispatch('/table-complete',{**identity,'requestId':'n','failed':False})
    assert result['allow'] is True
    assert completed==[('n',False)]
    assert await bridge._dispatch('/table-policy',{**identity,'requestId':'new','url':URL,'resourceKind':'xmlhttprequest','redirects':0,'method':'GET','stage':'request'})=={'allow':False}
    bridge._table_latches.clear()
    assert await bridge._dispatch('/table-complete',{**identity,'requestId':'n','failed':False})=={'allow':False}
    await bridge.close()


async def test_firefox_closed_page_can_prove_retained_table_requests_drained():
    page=SimpleNamespace(is_closed=lambda:True,context=SimpleNamespace(browser=None))
    guard=FirefoxDocumentGuard(page,URL)
    guard._table_mode=guard.generation
    guard._table_pending['n']=URL
    assert not await guard.table_quiescent(.02,quiet_s=.005)
    guard.table_completed('n')
    assert await guard.table_quiescent(.1,quiet_s=.005)


async def test_firefox_launch_readiness_attests_immutable_worker_policy():
    bridge=FirefoxPolicyBridge(table_worker_isolation=True)
    result=await bridge._dispatch('/ready',{'privateAccess':True,'tableWorkerIsolation':False})
    assert result['allow'] is False
    await bridge.close()
    bridge=FirefoxPolicyBridge(table_worker_isolation=True)
    result=await bridge._dispatch('/ready',{'privateAccess':True,'tableWorkerIsolation':True})
    assert result['allow'] is True
    assert bridge._worker_isolation_ready
    await bridge.close()
