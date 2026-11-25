"""Native shared-worker HTTP remains compatible with credentialed local proxies."""
import asyncio
import pytest

from app.fetching.oneoff import BrowserSession
from tests.integration.test_chromium_proxy_auth_native import local_proxy, USER, PASSWORD


@pytest.fixture(autouse=True)
def trust_local_worker_certificate(monkeypatch):
    # Chromium SharedWorkers do not inherit context.ignore_https_errors. The
    # proxy fixture refuses all destinations except its ephemeral local origin.
    from playwright.async_api import BrowserType
    launch=BrowserType.launch
    async def local_tls(self,*args,**kwargs):
        kwargs['args']=[*kwargs.get('args',[]),'--ignore-certificate-errors']
        return await launch(self,*args,**kwargs)
    monkeypatch.setattr(BrowserType,'launch',local_tls)


@pytest.mark.parametrize('transport',['http','https'])
@pytest.mark.parametrize('outcome',['good','origin401'])
async def test_shared_worker_public_fetch_preserves_native_proxy_auth(local_proxy,transport,outcome):
    fixture=local_proxy
    worker_path='/auth/worker-data' if outcome=='origin401' else '/worker-data'
    session=BrowserSession('chromium',proxy=f"http://{USER}:{PASSWORD}@127.0.0.1:{fixture['port']}",timeout_s=15)
    try:
        await session.start()
        assert (await session.get(fixture[transport]+'/product'))[0]==200
        result=await asyncio.wait_for(session.page.evaluate(r'''async url=>await new Promise(resolve=>{
          const code="onconnect=e=>{fetch("+JSON.stringify(url)+").then(r=>e.ports[0].postMessage(r.status)).catch(()=>e.ports[0].postMessage('failed'))}";
          const worker=new SharedWorker(URL.createObjectURL(new Blob([code],{type:'application/javascript'})));
          worker.port.onmessage=e=>resolve(e.data);worker.port.start();
        })''',fixture[transport]+worker_path),8)
        assert result==200 if outcome=='good' else result in (401,'failed')
        assert any(path==worker_path for path,_ in fixture['origin'])
        assert not session._native_document_policy.guard._proxy_auth_rejected
        assert any(item['authenticated'] for item in fixture['proxy'])
        assert all('authorization' not in headers and 'proxy-authorization' not in headers for _,headers in fixture['origin'])
        session._native_document_policy.raise_if_denied()
    finally:
        await session.close()
