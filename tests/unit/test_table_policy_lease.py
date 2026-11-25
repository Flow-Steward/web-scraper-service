"""Exclusive native table leases outlive collection and retire old documents safely."""
import asyncio
from types import SimpleNamespace

import pytest

from app.fetching.document_policy import NativeDocumentPolicy


class Surface:
    def __init__(self, events):
        self.events = events
        self.handlers = {}
        self.routes = []
        self.closed = False

    def on(self, name, callback):
        self.handlers.setdefault(name, []).append(callback)

    def remove_listener(self, name, callback):
        self.handlers[name].remove(callback)

    async def route(self, pattern, callback):
        self.routes.append(callback)
        self.events.append('route')

    async def unroute(self, pattern, callback):
        self.events.append('unroute')
        self.routes.remove(callback)


class Page(Surface):
    def __init__(self, events):
        super().__init__(events)
        self.url = 'https://tables.example.test/product'
        self.workers = []
        self.main_frame = SimpleNamespace(url=self.url)
        self.context = Surface(events)
        self.context.pages = [self]
        self.context._wss_table_owned = True
        self.context.service_workers = []
        self.context.browser = Surface(events)
        self.context.browser.contexts = [self.context]
        self.context.close = self.close_context
        self.context.browser.close = self.close_context

    def is_closed(self):
        return self.closed

    async def evaluate(self, script):
        self.events.append("workers_csp")
        return True

    async def goto(self, url, **kwargs):
        assert self.routes and self.context.routes
        self.events.append('blank_commit')
        assert url.startswith('about:blank#wss-table-retire=')
        self.url = url
        self.main_frame.url = url

    async def close(self):
        self.events.append('page_closed_guarded' if self.context.routes else 'page_closed_unguarded')
        self.closed = True
        self.context.pages = []

    async def close_context(self):
        self.events.append('context_closed')
        self.context.closed = True
        self.closed = True
        self.context.pages = []


class Guard:
    def __init__(self, page, events):
        self.page = page
        self.events = events
        self.errors = []
        self.timeout_s = .1
        self.active = False
        self.quiet = True

    async def enable_table_readonly(self, resource_policy=None):
        assert self.page.context.routes and self.page.routes
        self.events.append('native_ack')
        self.active = True

    async def table_quiescent(self, timeout_s, quiet_s=.1):
        self.events.append('native_quiet')
        return self.quiet

    def raise_if_denied(self):
        if self.errors:
            raise self.errors[0]

    async def end_table_readonly(self):
        assert self.page.closed or self.page.url.startswith('about:blank#wss-table-retire=')
        self.events.append('native_end')
        self.active = False

    async def rebind(self, url, callback, **kwargs):
        assert not self.active
        self.events.append('native_rebind')

    async def close(self):
        self.events.append('native_close')
        if not self.page.closed:
            await self.page.close()


async def enabled():
    events = []
    page = Page(events)
    guard = Guard(page, events)
    policy = NativeDocumentPolicy(guard)
    lease = await policy.enable_table_readonly()
    return events, page, guard, policy, lease


async def test_native_ack_and_persistent_gates_precede_any_table_action():
    events, page, guard, policy, lease = await enabled()
    assert events.index('route') < events.index('workers_csp') < events.index('native_ack')
    assert await lease.quiescent(.1) is True
    assert guard.active and page.routes and page.context.routes
    assert await policy.enable_table_readonly() is lease
    await policy.close()
    assert events.index('page_closed_guarded') < events.index('native_end') < events.index('unroute')


@pytest.mark.parametrize('surface', ['context', 'page', 'worker', 'service_worker', 'unmarked'])
async def test_unproved_exclusive_ownership_refuses_activation(surface):
    events = []
    page = Page(events)
    if surface == 'context':
        page.context.browser.contexts.append(object())
    elif surface == 'page':
        page.context.pages.append(object())
    elif surface == 'worker':
        page.workers.append(object())
    elif surface == 'service_worker':
        page.context.service_workers.append(object())
    else:
        page.context._wss_table_owned = False
    policy = NativeDocumentPolicy(Guard(page, events))
    with pytest.raises(Exception, match='unsupported_context'):
        await policy.enable_table_readonly()
    assert 'native_ack' not in events


async def test_rebind_destroys_old_document_before_releasing_native_mode():
    events, page, guard, policy, lease = await enabled()
    await policy.rebind('https://tables.example.test/next')
    assert events.index('blank_commit') < events.index('native_end') < events.index('unroute') < events.index('native_rebind')
    with pytest.raises(Exception, match='lease_closed'):
        await lease.quiescent(.1)


async def test_unresolved_native_chains_close_exclusive_context_before_release():
    events, page, guard, policy, lease = await enabled()
    guard.quiet = False
    with pytest.raises(Exception):
        await policy.rebind('https://tables.example.test/next')
    assert events.index('context_closed') < events.index('native_end')
    assert 'native_rebind' not in events


async def test_cancelled_reset_closes_document_with_guards_still_active():
    events, page, guard, policy, lease = await enabled()
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError
    page.goto = cancelled
    with pytest.raises(asyncio.CancelledError):
        await policy.rebind('https://tables.example.test/next')
    assert 'page_closed_guarded' in events or 'context_closed' in events
    assert not guard.active


async def test_popup_first_request_is_blocked_by_context_gate_before_handlers():
    events, page, guard, policy, lease = await enabled()
    issued = []
    popup = SimpleNamespace()
    request = SimpleNamespace(method='POST', url='https://tables.example.test/write', frame=SimpleNamespace(page=popup), is_navigation_request=lambda:True)
    async def abort(*args, **kwargs):
        issued.append('blocked')
    async def escaped(*args, **kwargs):
        issued.append('escaped')
    route = SimpleNamespace(request=request, abort=abort, fallback=escaped, fulfill=escaped)
    await page.context.routes[0](route)
    assert issued == ['blocked']
    with pytest.raises(Exception):
        lease.raise_if_denied()
    await policy.close()


async def test_partial_activation_failure_destroys_exclusive_transport_before_release():
    events = []
    page = Page(events)
    guard = Guard(page, events)
    async def failed(resource_policy=None):
        guard.active = True
        raise RuntimeError('activation_failed')
    guard.enable_table_readonly = failed
    policy = NativeDocumentPolicy(guard)
    with pytest.raises(RuntimeError, match='activation_failed'):
        await policy.enable_table_readonly()
    assert 'context_closed' in events
    assert not guard.active


async def test_close_registers_context_barrier_before_new_page_admission():
    events, page, guard, policy, lease = await enabled()
    task = policy.begin_close()
    assert page.context._wss_table_cleanup_task is task
    await task
    assert lease.released


@pytest.mark.parametrize(('dedicated', 'pages', 'contexts'), [(False, 4, 8), (True, 1, 1)])
def test_browser_pool_dedication_preserves_default_clearance_lifetime(monkeypatch, dedicated, pages, contexts):
    import crawlee.browsers
    from app.fetching import browsers
    captured = {}
    class Plugin:
        def __init__(self, **kwargs):
            captured['plugin'] = kwargs
    def pool(**kwargs):
        captured['pool'] = kwargs
        return kwargs
    monkeypatch.setattr(browsers, '_make_plugins', lambda:(Plugin,Plugin))
    monkeypatch.setattr(crawlee.browsers, 'BrowserPool', pool)
    browsers.make_browser_pool('chromium', headless=True, max_pages=4, dedicated_internal=dedicated)
    assert captured['plugin']['max_pages'] == pages
    assert captured['plugin']['dedicated_internal'] is dedicated
    assert captured['pool']['retire_browser_after_page_count'] == 200


async def pool_controller(dedicated=True):
    from app.fetching.browsers import _make_plugins
    events = []
    class Context:
        def __init__(self):
            self.pages = []
            self.closed = False
            self.handlers = {}
        def on(self, name, callback):
            self.handlers[name] = callback
        async def new_page(self):
            events.append('new_page')
            context = self
            class Tab:
                closed = False
                def on(self, *, event, f):
                    self.callback = f
                async def close(self):
                    self.closed = True
                    context.pages.remove(self)
                    self.callback(self)
            page = Tab()
            self.pages.append(page)
            return page
        async def close(self):
            events.append('context_closed')
            self.closed = True
            if 'close' in self.handlers:
                self.handlers['close']()
    browser = SimpleNamespace(contexts=[], is_connected=lambda:True)
    async def close():
        events.append('browser_closed')
    browser.close = close
    async def launch(**kwargs):
        return browser
    plugin_class, _ = _make_plugins()
    plugin = plugin_class(headless=True, max_pages=1, dedicated_internal=dedicated)
    plugin._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    controller = await plugin.new_browser()
    created = []
    async def create(**kwargs):
        context = Context()
        created.append(context)
        return context
    controller._create_browser_context = create
    return controller, created, events


@pytest.mark.parametrize('rotation', [False, True])
async def test_dedicated_context_reuse_and_rotation_wait_for_old_lease(rotation):
    controller, contexts, events = await pool_controller()
    assert controller.MAX_CONTEXTS == 1
    first = await controller.new_page(proxy_info=SimpleNamespace(session_id='first'))
    original_context = contexts[0]
    original_context.cookie = 'clearance-retained'
    await first.close()
    barrier = asyncio.get_running_loop().create_future()
    original_context._wss_table_cleanup_task = barrier
    opening = asyncio.create_task(controller.new_page(proxy_info=SimpleNamespace(session_id='second' if rotation else 'first')))
    await asyncio.sleep(0)
    assert len(contexts) == 1 and events.count('new_page') == 1
    assert not original_context.closed
    assert controller.is_opening_pages and not controller.has_free_capacity
    barrier.set_result(None)
    second = await opening
    assert not second.closed
    if rotation:
        assert original_context.closed
        assert len(contexts) == 2
    else:
        assert not original_context.closed
        assert len(contexts) == 1
        assert contexts[0].cookie == 'clearance-retained'
    await second.close()


async def test_failed_cleanup_retires_transport_before_any_new_page():
    controller, contexts, events = await pool_controller()
    first = await controller.new_page(proxy_info=SimpleNamespace(session_id='first'))
    await first.close()
    barrier = asyncio.get_running_loop().create_future()
    barrier.set_exception(RuntimeError('fixture cleanup failure'))
    contexts[0]._wss_table_cleanup_task = barrier
    with pytest.raises(RuntimeError, match='previous table lease cleanup failed'):
        await controller.new_page(proxy_info=SimpleNamespace(session_id='first'))
    assert events.count('new_page') == 1
    assert 'browser_closed' in events


async def test_ordinary_pool_keeps_eight_context_default():
    controller, contexts, events = await pool_controller(dedicated=False)
    assert controller.MAX_CONTEXTS == 8
    first = await controller.new_page(proxy_info=SimpleNamespace(session_id='first'))
    await first.close()
    second = await controller.new_page(proxy_info=SimpleNamespace(session_id='second'))
    assert len(contexts) == 2 and not contexts[0].closed
    assert not getattr(contexts[0], '_wss_table_owned', False)
    await second.close()


@pytest.mark.parametrize('table_mapping', [False, True])
async def test_runtime_dedication_is_enabled_only_for_active_table_mapping(tmp_path, monkeypatch, table_mapping):
    from tests.unit.test_proxy_auth_pause import make_run
    from tests.unit.test_table_sources import mapping
    svc, runner = await make_run(tmp_path, monkeypatch, config_overrides={'fetch_mode':'browser'})
    runner.browser = 'chromium'
    spec = mapping()
    if not table_mapping:
        spec['fields'] = spec['fields'][:1]
    monkeypatch.setattr(svc.mappings, 'active_mappings', lambda site:[{'template_id':'product','version':1,'mapping':spec}])
    captured = {}
    def pool(*args, **kwargs):
        captured.update(kwargs)
        return object()
    class Built(Exception):
        pass
    def build(**kwargs):
        raise Built
    monkeypatch.setattr('app.fetching.browsers.make_browser_pool', pool)
    monkeypatch.setattr('crawlee.crawlers.AdaptivePlaywrightCrawler.with_parsel_static_parser', build)
    await runner._open_storages()
    try:
        with pytest.raises(Built):
            runner._build_crawler()
        assert captured['dedicated_internal'] is table_mapping
    finally:
        svc.db.close()


async def test_close_with_unresolved_chains_retires_context_without_shutdown_error():
    events, page, guard, policy, lease = await enabled()
    guard.quiet = False
    await policy.close()
    assert events.index('context_closed') < events.index('native_end')
    assert lease.released


@pytest.mark.parametrize('dedicated,ready', [(False, False), (True, True), (True, False)])
async def test_camoufox_launch_consumes_table_flag_and_waits_for_native_worker_policy(monkeypatch, dedicated, ready):
    from app.fetching.browsers import launch_camoufox
    captured = {}
    events = []
    class Bridge:
        def __init__(self, **kwargs):
            captured['bridge'] = kwargs
            self.table_worker_isolation = kwargs.get('table_worker_isolation', False)
            self._worker_isolation_ready = False
            self.addon_path = '/synthetic-addon'
        async def start(self):
            return self
        async def wait_ready(self):
            events.append('ready')
            self._worker_isolation_ready = ready
        def revoke_all(self):
            pass
        async def close(self):
            pass
        def begin_close(self):
            pass
    class Browser:
        async def close(self, **kwargs):
            pass
        def on(self, *args):
            pass
        async def new_context(self, **kwargs):
            async def noop():
                pass
            return SimpleNamespace(new_page=noop, close=noop)
    browser = Browser()
    async def create(playwright, **kwargs):
        captured['launch'] = kwargs
        return browser
    monkeypatch.setattr('app.fetching.firefox_policy.FirefoxPolicyBridge', Bridge)
    monkeypatch.setattr('camoufox.async_api.AsyncNewBrowser', create)
    if dedicated and not ready:
        with pytest.raises(RuntimeError, match='native table worker isolation unavailable'):
            await launch_camoufox(object(), headless=True, table_dedicated=True)
        assert not getattr(browser, '_wss_table_worker_isolation', False)
        return
    result = await launch_camoufox(object(), headless=True, table_dedicated=dedicated)
    assert 'table_dedicated' not in captured['launch']
    assert captured['bridge'] == ({'table_worker_isolation':True} if dedicated else {})
    assert events == ['ready']
    assert bool(getattr(result, '_wss_table_worker_isolation', False)) is dedicated
    await result.close()


async def test_rebind_never_clears_security_denial_arriving_during_retirement():
    events, page, guard, policy, lease = await enabled()
    original = page.goto
    async def retirement_navigation(*args, **kwargs):
        await original(*args, **kwargs)
        guard.errors.append(RuntimeError('synthetic forbidden request during retirement'))
    page.goto = retirement_navigation
    with pytest.raises(RuntimeError, match='forbidden request during retirement'):
        await policy.rebind('https://tables.example.test/next')
    assert 'native_rebind' not in events
    assert guard.errors and lease.released
    await policy.close()
