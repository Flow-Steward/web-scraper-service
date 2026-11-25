"""Native HTTP resource policy proofs. All servers use ephemeral loopback ports."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import socket
from urllib.parse import urlsplit, parse_qs
from threading import Event, Thread

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from app.fetching.browsers import launch_camoufox
from app.fetching.chromium_destinations import ChromiumDocumentGuard
from app.fetching.firefox_policy import FirefoxDocumentGuard
from app.fetching.oneoff import private_route_guard


@pytest.fixture
def native_resources():
    class Hits(list):
        slow_started=Event()
        slow_release=Event()
    hits = Hits()
    private_hits = []
    class Private(BaseHTTPRequestHandler):
        def do_GET(self):
            private_hits.append(self.path)
            self.send_response(200); self.send_header('Access-Control-Allow-Origin', '*'); self.end_headers(); self.wfile.write(b'private')
        def log_message(self, *args):
            pass
    private = ThreadingHTTPServer(('127.0.0.1', 0), Private)
    target = f'http://127.0.0.1:{private.server_port}/private'
    class IPv6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6
    private6 = IPv6Server(('::1', 0), Private)
    class Public(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(('GET', self.path))
            if self.path.startswith('/redirect'):
                case=parse_qs(urlsplit(self.path).query).get('case',['plain'])[0]
                locations=[target]
                if case=='backslash_authority': locations=[f'http://127.0.0.1:{private.server_port}\\@127.0.0.1:{self.server.server_port}/probe']
                elif case=='triple_slash': locations=[f'///127.0.0.1:{private.server_port}/private']
                elif case=='scheme_many_slash': locations=[f'http:////127.0.0.1:{private.server_port}/private']
                elif case=='scheme_relative': locations=[target.removeprefix('http:')]
                elif case=='encoded_host': locations=[target.replace('127.0.0.1','2130706433')]
                elif case=='ipv6': locations=[f'http://[::1]:{private6.server_port}/private']
                elif case=='duplicate_private_first': locations=[target,f'http://127.0.0.1:{self.server.server_port}/safe']
                elif case=='duplicate_private_last': locations=[f'http://127.0.0.1:{self.server.server_port}/safe',target]
                elif case=='public': locations=[f'http://127.0.0.1:{self.server.server_port}/safe']
                self.send_response(302)
                for location in locations: self.send_header('lOcAtIoN',location)
                self.end_headers(); return
            if self.path == '/slow':
                hits.slow_started.set()
                hits.slow_release.wait(timeout=5)
            body = b'<!doctype html><html><body>Native table fixture</body></html>' if self.path == '/page' else b'ok'
            if self.path == '/first-script':
                body = b"""<!doctype html><script>
                  window.worker_results={dedicated:'pending',shared:'pending'};
                  window.worker_violations=[];
                  document.addEventListener('securitypolicyviolation',e=>window.worker_violations.push(e.effectiveDirective));
                  for(const kind of ['dedicated','shared']) {
                    try {
                      const code=kind==='shared'?"onconnect=e=>e.ports[0].postMessage('ran')":"postMessage('ran')";
                      const uri=URL.createObjectURL(new Blob([code],{type:'application/javascript'}));
                      const worker=kind==='shared'?new SharedWorker(uri):new Worker(uri);
                      worker.onerror=()=>window.worker_results[kind]='blocked';
                      const port=kind==='shared'?worker.port:worker;
                      port.onmessage=e=>window.worker_results[kind]=e.data;
                      if(port.start)port.start();
                    } catch(_) {window.worker_results[kind]='blocked';}
                  }
                </script>"""
            self.send_response(500 if self.path == '/http500' else 200); self.send_header('Content-Type', 'text/html'); self.send_header('Content-Length', str(len(body))); self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def do_POST(self):
            hits.append(('POST', self.path)); self.send_response(200); self.end_headers()
        def log_message(self, *args):
            pass
    public = ThreadingHTTPServer(('127.0.0.1', 0), Public)
    threads = [Thread(target=s.serve_forever, daemon=True) for s in (public, private, private6)]
    for t in threads: t.start()
    try:
        yield f'http://127.0.0.1:{public.server_port}', target, hits, private_hits
    finally:
        hits.slow_release.set()
        for s in (public, private, private6): s.shutdown(); s.server_close()
        for t in threads: t.join()


@pytest.fixture
def table_worker_isolation():
    return True


@pytest_asyncio.fixture(params=['chromium', 'camoufox'])
async def resource_page(request, monkeypatch, native_resources, table_worker_isolation):
    from tests.unit.test_logic import settings
    monkeypatch.setattr('app.settings._settings', settings())
    origin, target, _, _ = native_resources
    async def private(url):
        try:
            candidate=urlsplit(url)
            public=urlsplit(origin)
            return (candidate.hostname,candidate.port)!=(public.hostname,public.port)
        except ValueError:
            return True
    for module in ('app.fetching.oneoff', 'app.fetching.chromium_destinations', 'app.fetching.chromium_resources', 'app.fetching.firefox_policy'):
        monkeypatch.setattr(module + '.url_is_private', private)
    async with async_playwright() as pw:
        if request.param == 'camoufox':
            binary = os.environ.get('CAMOUFOX_EXECUTABLE_PATH')
            if not binary:
                pytest.skip('Pinned local Camoufox binary required')
            monkeypatch.setattr('camoufox.utils.warn_if_package_outdated', lambda: None)
            browser = await launch_camoufox(pw, headless=True, executable_path=binary,
                table_dedicated=table_worker_isolation, fingerprint_preset={
                'navigator':{'platform':'MacIntel','userAgent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 Firefox/158.0','hardwareConcurrency':4},
                'screen':{'width':1440,'height':900,'availWidth':1440,'availHeight':860,'colorDepth':24}})
        else:
            browser = await pw.chromium.launch(headless=True, executable_path=os.environ.get('WSS_TEST_CHROMIUM_PATH'))
        guard = None
        try:
            context = await browser.new_context(service_workers='block', ignore_https_errors=True)
            page = await context.new_page()
            cls = FirefoxDocumentGuard if request.param == 'camoufox' else ChromiumDocumentGuard
            guard = await cls(page, origin + '/page', timeout_s=5).start()
            await page.route('**/*', private_route_guard(then=guard.request))
            await page.goto(origin + '/page')
            yield page, guard, request.param
        finally:
            if guard is not None:
                await guard.close()
            await browser.close()


async def test_native_table_get_ajax_and_completion(resource_page, native_resources):
    page, guard, engine = resource_page
    failures=[]
    if hasattr(guard, 'session'):
        guard.session.on('Network.loadingFailed',lambda event: failures.append(event))
    await guard.enable_table_readonly()
    await page.evaluate(('mw:' if engine == 'camoufox' else '') + "() => {window.ready=false;fetch('/slow').then(()=>window.ready=true)}")
    assert await asyncio.to_thread(native_resources[2].slow_started.wait, 3)
    assert not await guard.table_quiescent(.08, quiet_s=.02), (getattr(guard,'_table_pending',None), guard.errors, native_resources[2])
    native_resources[2].slow_release.set()
    assert await guard.table_quiescent(2, quiet_s=.05), (getattr(guard,'_table_pending',None),guard.errors,native_resources[2])
    assert not guard.errors, (failures, getattr(guard,'_network_pending',None), native_resources[2])
    assert await page.evaluate(('mw:' if engine == 'camoufox' else '') + 'window.ready')


async def test_native_table_redirect_never_contacts_private_target(resource_page, native_resources):
    page, guard, engine = resource_page
    _, _, _, private_hits = native_resources
    await guard.enable_table_readonly()
    await page.evaluate(("mw:" if engine == "camoufox" else "") + "() => fetch('/redirect').catch(()=>null)")
    await guard.table_quiescent(2, quiet_s=.05)
    assert private_hits == []
    assert guard.errors


async def test_native_table_delayed_post_remains_blocked(resource_page, native_resources):
    page, guard, engine = resource_page
    _, _, hits, _ = native_resources
    await guard.enable_table_readonly()
    await page.evaluate(("mw:" if engine == "camoufox" else "") + "() => {window.post_attempted=false;setTimeout(()=>{window.post_attempted=true;fetch('/write',{method:'POST'}).catch(()=>null)},200)}")
    assert await guard.table_quiescent(.15, quiet_s=.03), (getattr(guard,'_table_pending',None),guard.errors,native_resources[2])
    for _ in range(100):
        if guard.errors or any(h[0]=='POST' for h in hits):
            break
        await asyncio.sleep(.03)
    assert await page.evaluate(('mw:' if engine=='camoufox' else '')+'window.post_attempted')
    assert not [h for h in hits if h[0] == 'POST']
    assert guard.errors


async def test_ordinary_resource_redirect_also_never_contacts_private_target(resource_page, native_resources):
    # This was observed reaching the private server before the shared native fix.
    page, guard, engine = resource_page
    _, _, _, private_hits = native_resources
    await page.evaluate(("mw:" if engine == "camoufox" else "") + "() => fetch('/redirect').catch(()=>null)")
    assert private_hits == []
    assert guard.errors


@pytest.mark.parametrize('case', ['scheme_relative','encoded_host','ipv6','duplicate_private_first','duplicate_private_last','backslash_authority','triple_slash','scheme_many_slash'])
async def test_ordinary_resource_redirect_private_aliases_are_denied(resource_page, native_resources, case):
    page, guard, engine = resource_page
    await page.evaluate(('mw:' if engine == 'camoufox' else '') + "p=>fetch(p).catch(()=>null)", '/redirect?case='+case)
    assert native_resources[3] == []


async def test_ordinary_public_ajax_redirect_and_post_remain_native(resource_page, native_resources):
    page, guard, engine = resource_page
    assert await page.evaluate(('mw:' if engine == 'camoufox' else '') + "async()=>[await (await fetch('/redirect?case=public')).text(),(await fetch('/write',{method:'POST'})).status]") == ['ok',200]
    assert ('GET','/safe') in native_resources[2] and ('POST','/write') in native_resources[2]
    assert not guard.errors


async def test_worker_cannot_issue_a_post_in_native_table_mode(resource_page, native_resources):
    from playwright.async_api import Error
    page, guard, engine = resource_page
    await guard.enable_table_readonly()
    try:
        await page.evaluate(('mw:' if engine == 'camoufox' else '') + r'''url=>new Worker(URL.createObjectURL(new Blob(["fetch("+JSON.stringify(url)+",{method:'POST'}).catch(()=>null)"],{type:"application/javascript"})))''', native_resources[0]+'/write')
    except Error:
        pass  # Chromium closes before releasing a paused worker target.
    await asyncio.sleep(.3)
    assert not [hit for hit in native_resources[2] if hit[0]=='POST']
    if engine == 'chromium':
        assert guard.errors
    else:
        assert guard.bridge._worker_isolation_ready  # CSP prevents worker execution before network.


@pytest.mark.parametrize('table_worker_isolation', [False])
@pytest.mark.parametrize('owner', ['worker','shared_worker','image'])
@pytest.mark.parametrize('private_target', [False,True])
async def test_ordinary_resource_owners_obey_redirect_privacy(resource_page, native_resources, owner, private_target):
    page, guard, engine = resource_page
    path = '/redirect' if private_target else '/redirect?case=public'
    async def evaluate():
        return await page.evaluate(('mw:' if engine == 'camoufox' else '') + r'''async ({owner,path})=>{
      if(owner==='image') return await new Promise(resolve=>{let image=new Image();image.onload=image.onerror=()=>resolve('done');image.src=path;});
      const script="fetch("+JSON.stringify(new URL(path,location.href).href)+").then(r=>r.text()).then(()=>postMessage('ok')).catch(()=>postMessage('denied'))";
      const source=owner==='shared_worker'?"onconnect=e=>{const postMessage=x=>e.ports[0].postMessage(x);"+script+"}":script;
      return await new Promise(resolve=>{
        const url=URL.createObjectURL(new Blob([source],{type:'application/javascript'}));
        const worker=owner==='shared_worker'?new SharedWorker(url):new Worker(url);
        const port=owner==='shared_worker'?worker.port:worker;
        port.onmessage=e=>{resolve(e.data);if(owner==='worker')worker.terminate();else port.close();URL.revokeObjectURL(url)};
        if(port.start)port.start();
      });
    }''', {'owner':owner,'path':path})
    try:
        result = await asyncio.wait_for(evaluate(), 8)
    except Exception as exc:
        raise AssertionError([str(error) for error in guard.errors]) from exc
    assert native_resources[3] == []
    assert ('GET','/redirect'+('' if private_target else '?case=public')) in native_resources[2]
    if not private_target:
        assert ('GET','/safe') in native_resources[2]
        assert result in ('done','ok')


async def test_chromium_new_shared_worker_is_stopped_before_table_side_effect(resource_page, native_resources):
    from playwright.async_api import Error
    page, guard, engine = resource_page
    if engine != 'chromium':
        pytest.skip('Firefox worker isolation is enforced by its dedicated launch preference and common lease CSP')
    await guard.enable_table_readonly()
    try:
        await page.evaluate(r'''url=>new SharedWorker(URL.createObjectURL(new Blob(["fetch("+JSON.stringify(url)+",{method:'POST'})"],{type:'application/javascript'})))''', native_resources[0]+'/write')
    except Error:
        pass
    await asyncio.sleep(.3)
    assert not [hit for hit in native_resources[2] if hit[0]=='POST']
    assert guard.errors


async def test_chromium_privacy_guard_outlives_its_last_bound_page(resource_page, native_resources):
    page,guard,engine=resource_page
    if engine != 'chromium':
        pytest.skip('Launch lifetime Chromium CDP singleton proof')
    monitor=await page.context.new_page()
    try:
        await monitor.goto(native_resources[0]+'/page')
        await guard.close()
        assert guard.resource_privacy.guards == {}
        result=await asyncio.wait_for(monitor.evaluate(r'''async url=>await new Promise(resolve=>{
          const code="onconnect=e=>{fetch("+JSON.stringify(url)+").then(()=>e.ports[0].postMessage('allowed')).catch(()=>e.ports[0].postMessage('denied'))}";
          const worker=new SharedWorker(URL.createObjectURL(new Blob([code],{type:'application/javascript'})));
          worker.port.onmessage=e=>resolve(e.data);worker.port.start();
        })''',native_resources[0]+'/redirect'),8)
        assert result=='denied'
        assert native_resources[3]==[]
        assert ('GET','/redirect') in native_resources[2]
    finally:
        await monitor.close()


async def test_native_table_http500_is_incomplete_without_security_denial(resource_page):
    page,guard,engine=resource_page
    await guard.enable_table_readonly()
    status=await page.evaluate(('mw:' if engine=='camoufox' else '')+"async()=> (await fetch('/http500')).status")
    assert status==500
    assert await guard.table_quiescent(2,quiet_s=.05)
    assert guard.table_load_failure is not None
    assert guard.errors==[]


@pytest.mark.parametrize('table_worker_isolation',[True,False])
async def test_firefox_first_document_enforces_launch_worker_isolation(resource_page, native_resources, table_worker_isolation):
    page,guard,engine=resource_page
    if engine!='camoufox':
        pytest.skip('Firefox immutable launch CSP proof')
    response=await page.goto(native_resources[0]+'/first-script')
    for _ in range(60):
        results=await page.evaluate('mw:window.worker_results')
        if all(value != 'pending' for value in results.values()):
            break
        await asyncio.sleep(.05)
    assert results==dict.fromkeys(['dedicated','shared'],'blocked' if table_worker_isolation else 'ran')
    if table_worker_isolation:
        assert 'worker-src' in await page.evaluate('mw:window.worker_violations')
        assert guard.bridge._worker_isolation_ready
    assert guard.errors==[]


async def test_chromium_existing_shared_worker_prevents_table_arming(resource_page):
    page,guard,engine=resource_page
    if engine!='chromium':
        pytest.skip('Firefox dedicated launch prevents shared-worker creation before navigation')
    await page.evaluate(r'''async()=>await new Promise(resolve=>{
      const worker=new SharedWorker(URL.createObjectURL(new Blob(["onconnect=e=>e.ports[0].postMessage('ready')"],{type:'application/javascript'})));
      worker.port.onmessage=()=>resolve();worker.port.start();
    })''')
    with pytest.raises(RuntimeError,match='shared worker'):
        await guard.enable_table_readonly()


@pytest.mark.parametrize('table_worker_isolation',[False])
@pytest.mark.parametrize('case',['backslash_authority','triple_slash','scheme_many_slash'])
async def test_shared_worker_raw_redirect_authorities_never_reach_private(resource_page,native_resources,case):
    page,guard,engine=resource_page
    result=await asyncio.wait_for(page.evaluate(('mw:' if engine=='camoufox' else '')+r'''async url=>await new Promise(resolve=>{
      const code="onconnect=e=>{fetch("+JSON.stringify(url)+").then(()=>e.ports[0].postMessage('allowed')).catch(()=>e.ports[0].postMessage('denied'))}";
      const worker=new SharedWorker(URL.createObjectURL(new Blob([code],{type:'application/javascript'})));
      worker.port.onmessage=e=>resolve(e.data);worker.port.start();
    })''',native_resources[0]+'/redirect?case='+case),8)
    assert result=='denied'
    assert native_resources[3]==[]
    assert ('GET','/redirect?case='+case) in native_resources[2]
