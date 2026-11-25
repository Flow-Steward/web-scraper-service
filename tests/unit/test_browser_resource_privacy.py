"""Launch-lifetime privacy policy owns redirects outside page target channels."""
import asyncio
from types import SimpleNamespace
import pytest


class Session:
    def __init__(self): self.calls=[];self.listeners={}
    async def send(self, method, params=None):
        self.calls.append((method,params))
        return {'targetInfos':[]} if method=='Target.getTargets' else {}
    def on(self,event,callback): self.listeners[event]=callback
    def remove_listener(self,*args): pass


async def test_browser_privacy_singleton_survives_zero_page_owners(monkeypatch):
    from app.fetching.chromium_resources import BrowserResourcePrivacy
    session=Session()
    async def create(): return session
    browser=SimpleNamespace(new_browser_cdp_session=create,on=lambda *args:None)
    first,second=await asyncio.gather(BrowserResourcePrivacy.for_browser(browser),BrowserResourcePrivacy.for_browser(browser))
    assert first is second
    assert len([m for m,p in session.calls if m=='Fetch.enable'])==1
    assert all('handleAuthRequests' not in p for m,p in session.calls if m=='Fetch.enable')
    guard=SimpleNamespace(main_frame_id='main',generation=1)
    first.register(guard);first.unregister(guard)
    assert not first.closed


@pytest.mark.parametrize('locations',[['http://private.test/'],['http://private.test/','https://public.test/'],['https://public.test/','http://private.test/']])
async def test_browser_private_redirect_is_denied_without_unrelated_owner_poison(monkeypatch,locations):
    from app.fetching.chromium_resources import BrowserResourcePrivacy
    async def private(url): return 'private.test' in url
    monkeypatch.setattr('app.fetching.chromium_resources.url_is_private',private)
    session=Session();policy=BrowserResourcePrivacy(None,session)
    await policy._response({'requestId':'id','request':{'url':'https://public.test/start'},'responseStatusCode':302,'responseHeaders':[{'name':'LoCaTiOn','value':url} for url in locations]})
    assert session.calls[-1][0]=='Fetch.failRequest'


async def test_browser_table_mode_rejects_preexisting_shared_worker():
    from app.fetching.chromium_resources import BrowserResourcePrivacy
    session=Session()
    async def targets(method,params=None): return {'targetInfos':[{'type':'shared_worker'}]}
    session.send=targets
    policy=BrowserResourcePrivacy(None,session)
    guard=SimpleNamespace()
    with pytest.raises(RuntimeError,match='worker'):
        await policy.enable_table(guard)


@pytest.mark.parametrize('target',['http://2130706433/private','http://127.1/private','http://0x7f000001/private','http://[::1]/private','http://[::ffff:127.0.0.1]/private'])
async def test_browser_private_redirect_uses_real_numeric_address_refusal(monkeypatch,target):
    from app.fetching.chromium_resources import BrowserResourcePrivacy
    from tests.unit.test_logic import settings
    monkeypatch.setattr('app.settings._settings',settings(allow_private_targets=False))
    session=Session();policy=BrowserResourcePrivacy(None,session)
    await policy._response({'requestId':'id','request':{'url':'https://public.test/start'},'responseStatusCode':302,'responseHeaders':[{'name':'Location','value':target}]})
    assert session.calls[-1][0]=='Fetch.failRequest'


async def test_table_unarm_waits_for_native_worker_close_ack():
    from app.fetching.chromium_resources import BrowserResourcePrivacy
    session=Session();policy=BrowserResourcePrivacy(None,session)
    guard=SimpleNamespace()
    policy.table_guards[id(guard)]=guard
    released=asyncio.Event()
    worker_close=asyncio.create_task(released.wait())
    policy.tasks.add(worker_close)
    end=asyncio.create_task(policy.end_table(guard))
    await asyncio.sleep(.01)
    assert not end.done()
    assert id(guard) in policy.table_guards
    assert not [p for m,p in session.calls if m=='Target.setAutoAttach' and not p['autoAttach']]
    released.set()
    await end
    assert id(guard) not in policy.table_guards
    assert session.calls[-1][1]['autoAttach'] is False


@pytest.mark.parametrize('cancel',[False,True])
async def test_uncertain_table_unarm_closes_browser_without_releasing_worker(cancel):
    from app.fetching.chromium_resources import BrowserResourcePrivacy
    closed=asyncio.Event()
    async def close(): closed.set()
    session=Session();policy=BrowserResourcePrivacy(SimpleNamespace(close=close),session)
    policy.TIMEOUT_S=.02
    guard=SimpleNamespace();policy.table_guards[id(guard)]=guard
    worker=asyncio.create_task(asyncio.Event().wait());policy.tasks.add(worker)
    end=asyncio.create_task(policy.end_table(guard))
    if cancel:
        await asyncio.sleep(0)
        end.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await end
    assert closed.is_set()
    assert id(guard) in policy.table_guards
    assert not [p for m,p in session.calls if m=='Target.setAutoAttach' and not p['autoAttach']]
