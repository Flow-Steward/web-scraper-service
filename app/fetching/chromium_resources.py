"""Browser-lifetime private redirect checks, including shared-worker channels.

This session owns no credentials and imposes no crawl or method policy. It must
outlive page guards: shared workers can outlive every page that created them.
"""
from __future__ import annotations

import asyncio

from app.core.netguard import url_is_private
from app.fetching.native_redirects import native_redirect_target
from app.fetching.destinations import DestinationDenied, REDIRECT_STATUSES


class BrowserResourcePrivacy:
    MAX_PENDING = 256
    TIMEOUT_S = 3

    def __init__(self, browser, session):
        self.browser, self.session = browser, session
        self.closed = False
        self.guards, self.table_guards = {}, {}
        self.tasks, self.paused = set(), set()
        self.shared_targets = {}

    @classmethod
    async def for_browser(cls, browser):
        task = getattr(browser, '_wss_resource_privacy_task', None)
        if task is None:
            async def start():
                policy = cls(browser, await browser.new_browser_cdp_session())
                await policy.start()
                return policy
            task = asyncio.create_task(start())
            browser._wss_resource_privacy_task = task
        # A cancelled caller must not detach a browser's live privacy boundary.
        return await asyncio.shield(task)

    async def start(self):
        self.session.on('Fetch.requestPaused', self._on_paused)
        self.session.on('Target.attachedToTarget', self._on_attached)
        self.session.on('Target.detachedFromTarget', self._on_detached)
        self.browser.on('disconnected', self._disconnected)
        try:
            async with asyncio.timeout(self.TIMEOUT_S):
                # No authRequired enrollment/credential handling in this session.
                await self.session.send('Fetch.enable', {'patterns':[{'requestStage':'Response'}]})

        except BaseException:
            await self.browser.close()
            raise

    def register(self, guard):
        self.guards[id(guard)] = guard

    def unregister(self, guard):
        self.guards.pop(id(guard), None)
        self.table_guards.pop(id(guard), None)
        # Deliberately retain the session and worker interception until disconnect.

    async def enable_table(self, guard):
        async with asyncio.timeout(self.TIMEOUT_S):
            targets = await self.session.send('Target.getTargets')
            if any(t['type']=='shared_worker' for t in targets['targetInfos']):
                raise RuntimeError('existing shared worker prevents native table ownership')
            self.table_guards[id(guard)] = guard
            # Browser-level auto-attachment requires flatten=True. No target is
            # ever resumed through this channel: table workers are unsupported.
            await self.session.send('Target.setAutoAttach', {
                'autoAttach':True,'waitForDebuggerOnStart':True,'flatten':True,
                'filter':[{'type':'shared_worker','exclude':False},{'exclude':True}],
            })
            targets = await self.session.send('Target.getTargets')
        if self.closed or self.shared_targets or any(t['type']=='shared_worker' for t in targets['targetInfos']):
            raise RuntimeError('shared worker prevents native table ownership')

    async def end_table(self, guard):
        if id(guard) not in self.table_guards:
            return
        if self.closed:
            self.table_guards.pop(id(guard), None)
            return
        try:
            async with asyncio.timeout(self.TIMEOUT_S):
                # A worker denial schedules page.close before its closeTarget
                # ACK arrives. Keep debugger throttles latched until that ACK.
                current = asyncio.current_task()
                while pending := [t for t in self.tasks if t is not current and not t.done()]:
                    await asyncio.gather(*pending)
                self.table_guards.pop(id(guard), None)
                if not self.table_guards:
                    await self.session.send('Target.setAutoAttach', {
                        'autoAttach':False,'waitForDebuggerOnStart':False,'flatten':True,
                    })
        except BaseException:
            # Never release a paused worker after uncertain/cancelled cleanup.
            await self.browser.close()
            raise

    def _disconnected(self, *_):
        self.closed = True
        for task in list(self.tasks):
            task.cancel()
        self.guards.clear(); self.table_guards.clear(); self.shared_targets.clear()

    def _spawn(self, coro):
        if self.closed:
            coro.close()
            return
        if len(self.tasks) >= self.MAX_PENDING:
            coro.close()
            # Releasing an unexamined redirect or debugger throttle is unsafe.
            self.closed = True
            asyncio.create_task(self.browser.close())
            return
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _on_paused(self, event):
        self.paused.add(event['requestId'])
        self._spawn(self._response(event))

    async def _response(self, event):
        request_id = event['requestId']
        url = event['request']['url']
        owner = next((g for g in self.guards.values() if not g.closed and g.main_frame_id == event.get('frameId')), None)
        generation = owner.generation if owner is not None else None
        try:
            async with asyncio.timeout(self.TIMEOUT_S):
                if event.get('responseStatusCode') in REDIRECT_STATUSES:
                    locations = [h['value'] for h in event.get('responseHeaders',[]) if h['name'].lower()=='location']
                    if len(locations)>1:
                        raise DestinationDenied(url,'ambiguous_redirect')
                    if locations:
                        target = native_redirect_target(url,locations[0])
                        if target is None or await url_is_private(target):
                            raise DestinationDenied(target or url,'private_resource_redirect')
                await self.session.send('Fetch.continueRequest',{'requestId':request_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if owner is not None:
                owner._remember(exc,generation)
            try:
                await self.session.send('Fetch.failRequest',{'requestId':request_id,'errorReason':'BlockedByClient'})
            except Exception:
                pass
        finally:
            self.paused.discard(request_id)

    def _on_attached(self, event):
        if event['targetInfo']['type'] != 'shared_worker':
            return
        self.shared_targets[event['sessionId']] = event['targetInfo']['targetId']
        self._spawn(self._worker(event))

    async def _worker(self, event):
        try:
            async with asyncio.timeout(self.TIMEOUT_S):
                if not self.table_guards:
                    await self.session.send('Target.detachFromTarget', {'sessionId':event['sessionId']})
                    return  # The old table document was destroyed before unarming.
                for guard in list(self.table_guards.values()):
                    guard._child_failure('table_worker_unsupported')
                await self.session.send('Target.closeTarget',{'targetId':event['targetInfo']['targetId']})
                # Never resume a table-session worker. Off-mode uses only Fetch
                # response checks and never attaches or throttles worker targets.

        except Exception:
            # A failed paused-worker channel must not be silently resumed.
            await self.browser.close()

    def _on_detached(self, event):
        self.shared_targets.pop(event['sessionId'],None)
