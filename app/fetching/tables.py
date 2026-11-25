"""Bounded opt-in acquisition of settled, in-page paginated tables.

Never replaces product HTML, calls a framework API, or fetches table data outside
its existing native browser session. The ordinary request guards remain chained.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re

from app.extraction.tables import unavailable
from app.fetching.navigation import NavigationAction

# This code only reads the DOM. Limits apply before data crosses the browser IPC.
_SNAPSHOT = r"""s => {
    const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden' && !el.closest('[hidden], [aria-hidden="true"]');
    const tables = document.querySelectorAll(s.selector);
    if (tables.length !== 1 || tables[0].tagName !== 'TABLE') return {error:'table_missing'};
    const table = tables[0];
    const loaders = s.loading_selector ? Array.from(document.querySelectorAll(s.loading_selector)) : [];
    if (loaders.some(visible) || table.getAttribute('aria-busy') === 'true') return {loading:true};
    const empties = s.empty_selector ? Array.from(table.querySelectorAll(s.empty_selector)).filter(visible) : [];
    const headers = s.header_selector ? Array.from(table.querySelectorAll(s.header_selector), el => el.textContent) : [];
    if (headers.length > 64) return {error:'column_limit'};
    const encode = value => new TextEncoder().encode(JSON.stringify(value)).length;
    let used = 80 + encode(headers) * 2 + headers.length * 9, rows = [], fragments = [], truncated = null;
    const allRows = table.querySelectorAll(s.row_selector);
    for (const row of allRows) {
        if (!visible(row) || empties.some(empty => empty === row || row.contains(empty))) continue;
        const cells = Array.from(row.querySelectorAll(s.cell_selector));
        if (!cells.length || cells.length > 64 || cells.some(cell => Number(cell.getAttribute('colspan') || '1') !== 1 || Number(cell.getAttribute('rowspan') || '1') !== 1)) return {error:'unsupported_row'};
        const values = cells.map(cell => cell.textContent);
        const fragment = '<tr>' + cells.map(cell => cell.outerHTML).join('') + '</tr>';
        const size = encode([values, fragment]);
        if (rows.length >= s.remaining_rows) {truncated='row_limit';break;}
        if (used + size > s.remaining_bytes) {truncated='byte_limit';break;}
        used += size; rows.push(values);fragments.push(fragment);
    }
    if (used > s.remaining_bytes) return {error:'byte_limit'};
    let index = 1, next = null;
    if (s.next_selector) {
        const indices = document.querySelectorAll(s.current_page_selector);
        if (indices.length !== 1 || !/^\d+$/.test(indices[0].textContent.trim())) return {error:'page_sequence'};
        index = Number(indices[0].textContent.trim());
        const controls = document.querySelectorAll(s.next_selector);
        // A disappearing pager does not prove its terminal page was reached.
        if (controls.length !== 1) return {error:'pager_missing'};
        const el = controls[0];
        if ((el.textContent || '').length > 200 || (el.getAttribute('aria-label') || '').length > 200 || (el.getAttribute('href') || '').length > 2048) return {error:'unsafe_control'};
        const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true' || !!el.closest('.disabled, [disabled], [aria-disabled="true"]');
        next = {disabled, visible:visible(el), label:(el.getAttribute('aria-label') || el.textContent || '').trim(), href:el.getAttribute('href')};
    }
    return {index, headers, rows, fragments, next, used, truncated, empty:empties.length > 0, ready:rows.length > 0 || empties.length > 0 || !!truncated};
}"""

_REVALIDATE = r"""(el, expected) => {
    const source=expected.source;
    return el.isConnected && location.href === expected.url && document.querySelectorAll(source.selector).length === 1 &&
        document.querySelectorAll(source.next_selector).length === 1 && document.querySelector(source.next_selector) === el &&
        !el.disabled && el.getAttribute('aria-disabled') !== 'true' && !el.closest('.disabled,[disabled],[aria-disabled="true"]') &&
        !el.closest('form,[inert],[aria-hidden="true"]') && !el.form && !el.hasAttribute('download') &&
        (!el.getAttribute('target') || el.getAttribute('target') === '_self') &&
        (el.getAttribute('aria-label') || el.textContent || '').trim() === expected.label &&
        el.getAttribute('href') === expected.href &&
        document.querySelector(source.current_page_selector)?.textContent.trim() === String(expected.index);
}"""


async def _resolved(value):
    return await value if inspect.isawaitable(value) else value


async def collect_table(page, source: dict, *, before_action=None, cancelled=None, pace_action=None, on_progress=None) -> dict:
    """Collect raw ordered cells and HTML; incomplete captures remain evidence only."""
    result = unavailable()
    max_pages = min(64, max(1, int(source.get("max_pages", 32))))
    max_rows = min(10000, max(1, int(source.get("max_rows", 2000))))
    max_bytes = min(4 * 1024 * 1024, max(1, int(source.get("max_bytes", 1024 * 1024))))
    max_seconds = min(120.0, max(.01, float(source.get("max_seconds", 30))))
    fragments = []
    original_url = page.url
    blocked = None
    routes = set()
    installed = False
    pinned_table = None

    def stop(reason, complete=False):
        result.update(complete=complete, reason=reason)
        from html import escape
        headers = ''.join('<th>' + escape(value) + '</th>' for value in result['headers'])
        html = '<table><thead><tr>' + headers + '</tr></thead><tbody>' + ''.join(fragments) + '</tbody></table>'
        fits = len(html.encode('utf-8')) <= max_bytes
        result['html'] = html if fits else ''
        if complete and not fits:
            result.update(complete=False, reason='byte_limit')
        return result

    async def is_cancelled():
        return bool(await _resolved(cancelled())) if cancelled else False

    async def guard(route):
        nonlocal blocked
        task = asyncio.current_task()
        routes.add(task)
        try:
            request = route.request
            if request.is_navigation_request() and request.frame == page.main_frame:
                blocked = 'document_navigation'
                await route.fulfill(status=204, body='')
            elif request.method != 'GET':
                blocked = 'unsupported_method'
                await route.abort('blockedbyclient')
            else:
                # Private-address, native destination, proxy auth and resource
                # policy still run. Never continue_ here: it skips earlier guards.
                await route.fallback()
        finally:
            routes.discard(task)

    async def table_is_current():
        return pinned_table is not None and await pinned_table.evaluate(
            "(el, selector) => el.isConnected && document.querySelectorAll(selector).length === 1 && document.querySelector(selector) === el",
            source['selector'],
        )

    async def settled(expected_index):
        prior = None
        stable_since = None
        loop = asyncio.get_running_loop()
        while True:
            if blocked:
                return {'error': blocked}
            if await is_cancelled():
                return {'error': 'cancelled'}
            if page.url != original_url:
                return {'error': 'document_changed'}
            if not await table_is_current():
                return {'error': 'table_changed'}
            snapshot = await page.evaluate(_SNAPSHOT, source | {'remaining_rows': max_rows - len(result['rows']), 'remaining_bytes': max_bytes - result['bytes']})
            if snapshot.get('error'):
                return snapshot
            if not snapshot.get('loading') and snapshot.get('ready'):
                if snapshot['index'] != expected_index:
                    # During a transition, the old page is allowed to remain
                    # visible until its loading indicator/page index changes.
                    if snapshot['index'] != expected_index - 1 or expected_index == 1:
                        return {'error': 'page_sequence'}
                else:
                    signature = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
                    if signature == prior:
                        if stable_since is not None and loop.time() - stable_since >= .1:
                            return snapshot
                    else:
                        prior, stable_since = signature, loop.time()
            else:
                prior, stable_since = None, None
            await asyncio.sleep(.05)

    try:
        async with asyncio.timeout(max_seconds):
            await page.route('**/*', guard)
            installed = True
            pinned_table = await page.locator(source['selector']).element_handle(timeout=max_seconds * 1000)
            index = 1
            while True:
                snapshot = await settled(index)
                if snapshot.get('error'):
                    return stop(snapshot['error'])
                if result['pages'] and snapshot['headers'] != result['headers']:
                    return stop('headers_changed')
                result['headers'] = snapshot['headers']
                result['rows'].extend(snapshot['rows'])
                fragments.extend(snapshot['fragments'])
                result['bytes'] += snapshot['used']
                result['pages'] += 1
                if on_progress:
                    await _resolved(on_progress())
                if snapshot.get('truncated'):
                    return stop(snapshot['truncated'])
                control = snapshot.get('next')
                if control is None or control['disabled']:
                    return stop('finished', True)
                if result['pages'] >= max_pages:
                    return stop('page_limit')
                if len(result['rows']) >= max_rows:
                    return stop('row_limit')
                if not control['visible']:
                    return stop('pager_hidden')
                if not re.fullmatch(r'(?:next(?:\s+page)?|[»›→>])', control['label'], re.I):
                    return stop('unsafe_control')
                href = control['href']
                if href and not href.startswith('#'):
                    return stop('document_navigation')
                action = NavigationAction('table', control['label'], original_url)
                element = await page.locator(source['next_selector']).element_handle(timeout=3000)
                try:
                    if before_action and not await _resolved(before_action(action)):
                        return stop('policy_denied')
                    if await is_cancelled():
                        return stop('cancelled')
                    if pace_action:
                        await _resolved(pace_action(action))
                    if blocked:
                        return stop(blocked)
                    if await is_cancelled():
                        return stop('cancelled')
                    if not await table_is_current():
                        return stop('table_changed')
                    if element is None or not await element.evaluate(_REVALIDATE, {'source': source, 'url': original_url, 'label': control['label'], 'href': href, 'index': index}):
                        return stop('stale_control')
                    await element.click(timeout=3000)
                finally:
                    if element is not None:
                        await element.dispose()
                index += 1
    except TimeoutError:
        return stop(blocked or 'time_limit')
    except Exception:
        return stop(blocked or 'action_error')
    finally:
        if pinned_table is not None:
            try:
                await pinned_table.dispose()
            except Exception:
                pass
        for task in list(routes):
            task.cancel()
        if routes:
            await asyncio.gather(*list(routes), return_exceptions=True)
        if installed:
            try:
                await page.unroute('**/*', guard)
            except Exception:
                pass  # A closed page has no active routes to release.
