"""Real-browser contracts for generic, bounded listing navigation.

These fixtures use invented catalogues and intercept every request: no external
site, category vocabulary, framework or particular pagination library is needed.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright


@pytest_asyncio.fixture
async def listing_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel="chromium", headless=True, args=["--no-sandbox"],
            executable_path=os.environ.get("WSS_TEST_CHROMIUM_PATH"),
        )
        page = await browser.new_page(viewport={"width": 900, "height": 600}, service_workers="block")
        yield page
        await browser.close()


async def open_fixture(page, html, url="https://catalogue.example.test/list"):
    await page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
    await page.goto(url)


def navigation_api():
    # Import at the behavior boundary so the initial RED identifies the missing
    # feature without preventing collection of the surrounding integration suite.
    from app.fetching.navigation import NavigationLimits, collect_listing_links

    return NavigationLimits, collect_listing_links


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "path", "first", "second", "control"),
    [
        (
            "https://parts.example.test",
            "/brakes",
            "/item/rotor?fit=left",
            "/item/rotor?fit=right",
            '<nav aria-label="Pagination"><button type="button">Next</button></nav>',
        ),
        (
            "https://music.example.test",
            "/releases",
            "/record/echo?edition=mono",
            "/record/echo?edition=stereo",
            '<section class="pagination"><span role="button" tabindex="0" aria-label="Next page">→</span></section>',
        ),
    ],
)
async def test_same_url_next_controls_collect_query_distinct_links(
    listing_page, origin, path, first, second, control
):
    limits, collect = navigation_api()
    html = f'''<main><a id="entry" href="{first}#details">First</a></main>{control}
    <script>document.querySelector('button,[role=button]').onclick = function() {{
      document.querySelector('#entry').setAttribute('href', '{second}');
      this.setAttribute('aria-disabled', 'true');
    }};</script>'''
    await open_fixture(listing_page, html, origin + path)

    result = await collect(listing_page, limits=limits(settle_ms=250))

    assert set(result.links) == {origin + first, origin + second}
    assert listing_page.url == origin + path
    assert result.steps == 1
    assert result.observations == 2
    assert result.stop_reason == "controls_exhausted"
    assert result.bounded is False


@pytest.mark.asyncio
async def test_load_more_collects_appended_links_without_clicking_product(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<main id="list"><a href="/album/a?format=lp">A</a></main>
      <button type="button" aria-label="Load more results">More</button>
      <script>let count = 0; document.querySelector('button').onclick = function() {
        count++; document.querySelector('#list').insertAdjacentHTML('beforeend',
          '<a href="/album/' + (count === 1 ? 'b' : 'c') + '?format=lp">Album</a>');
        if (count === 2) this.disabled = true;
      };</script>""",
    )

    result = await collect(listing_page, limits=limits(settle_ms=250))

    assert set(result.links) == {
        "https://catalogue.example.test/album/a?format=lp",
        "https://catalogue.example.test/album/b?format=lp",
        "https://catalogue.example.test/album/c?format=lp",
    }
    assert listing_page.url == "https://catalogue.example.test/list"
    assert result.steps == 2
    assert result.stop_reason == "controls_exhausted"


@pytest.mark.asyncio
async def test_no_progress_stops_a_live_control_and_reports_partial_coverage(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<a href="/part/a">A</a>
      <button type="button" aria-label="Next page" onclick="this.dataset.clicks = Number(this.dataset.clicks || 0) + 1">Next</button>""",
    )

    result = await collect(
        listing_page, limits=limits(max_steps=8, max_no_progress=2, settle_ms=100)
    )

    assert result.steps == 2
    assert await listing_page.locator("button").get_attribute("data-clicks") == "2"
    assert result.stop_reason == "no_progress"
    assert result.bounded is True
    assert result.coverage_notes


@pytest.mark.asyncio
async def test_endless_more_is_bounded_even_when_each_click_adds_links(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<main><a href="/item/0">First</a></main>
      <button type="button" onclick="const a=document.createElement('a'); a.href='/item/'+document.querySelectorAll('a').length; a.textContent='item'; document.querySelector('main').append(a)">Load more</button>""",
    )

    result = await collect(listing_page, limits=limits(max_steps=2, settle_ms=100))

    assert set(result.links) == {
        "https://catalogue.example.test/item/0",
        "https://catalogue.example.test/item/1",
        "https://catalogue.example.test/item/2",
    }
    assert result.steps == 2
    assert result.stop_reason == "step_limit"
    assert result.bounded is True
    assert result.coverage_notes


@pytest.mark.asyncio
async def test_ordinary_pages_do_not_click_forms_cart_media_or_external_next(listing_page):
    _, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<a href="/part/a?size=2">Part</a>
      <form><button type="submit" aria-label="Next page">Continue</button></form>
      <button type="button">Add to cart</button><button type="button">Sign in</button>
      <section aria-roledescription="carousel"><button aria-label="Next page">Next</button></section>
      <a href="https://outside.example.test/list" rel="next">Next page</a>
      <script>window.clicks=0; document.addEventListener('click', () => window.clicks++);</script>""",
    )

    result = await collect(listing_page)

    assert result.links == ["https://catalogue.example.test/part/a?size=2"]
    assert result.steps == 0
    assert await listing_page.evaluate("window.clicks") == 0
    assert result.stop_reason == "controls_exhausted"


@pytest.mark.asyncio
async def test_policy_runs_before_each_action_and_denial_preserves_observations(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<a id="part" href="/part/a">Part</a>
      <button type="button" aria-label="Next page" onclick="document.querySelector('#part').href='/part/b'">Next</button>""",
    )
    actions = []

    async def policy(action):
        actions.append((action.kind, action.label, action.url))
        return len(actions) == 1

    result = await collect(listing_page, limits=limits(settle_ms=100), before_action=policy)

    assert set(result.links) == {
        "https://catalogue.example.test/part/a",
        "https://catalogue.example.test/part/b",
    }
    assert [action[0] for action in actions] == ["click", "click"]
    assert all(action[2] == "https://catalogue.example.test/list" for action in actions)
    assert result.steps == 1
    assert result.stop_reason == "policy_denied"
    assert result.bounded is True


@pytest.mark.asyncio
async def test_cancellation_prevents_further_clicks(listing_page):
    _, collect = navigation_api()
    await open_fixture(
        listing_page, '<a href="/part/a">A</a><button aria-label="Next page">Next</button>'
    )
    stopped = asyncio.Event()
    stopped.set()

    result = await collect(listing_page, cancelled=stopped.is_set)

    assert result.links == ["https://catalogue.example.test/part/a"]
    assert result.steps == 0
    assert result.stop_reason == "cancelled"


@pytest.mark.asyncio
async def test_scroll_can_reveal_links_without_a_button(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<main style="min-height:1800px"><a href="/record/a">A</a></main>
      <script>window.addEventListener('scroll', function reveal() {
        document.querySelector('main').insertAdjacentHTML('beforeend', '<a href="/record/b?edition=live">B</a>');
        window.removeEventListener('scroll', reveal);
      });</script>""",
    )

    result = await collect(
        listing_page,
        limits=limits(allow_scroll=True, max_steps=3, max_no_progress=1, settle_ms=200),
    )

    assert "https://catalogue.example.test/record/b?edition=live" in result.links
    assert result.steps <= 3
    assert result.bounded is True


@pytest.mark.asyncio
async def test_rel_next_defers_same_site_document_and_keeps_prior_links(listing_page):
    limits, collect = navigation_api()

    async def listing(route):
        body = (
            '<a href="/track/a?release=1">A</a><a rel="next" href="/list?page=2">→</a>'
            if "page=2" not in route.request.url
            else '<a href="/track/b?release=2">B</a>'
        )
        await route.fulfill(content_type="text/html", body=body)

    await listing_page.route("**/*", listing)
    await listing_page.goto("https://music.example.test/list")

    result = await collect(listing_page, limits=limits(settle_ms=100))

    assert set(result.links) == {
        "https://music.example.test/track/a?release=1",
        "https://music.example.test/list?page=2",
    }
    assert listing_page.url == "https://music.example.test/list"
    assert result.deferred_links == ["https://music.example.test/list?page=2"]
    assert result.steps == 1
    assert result.stop_reason == "navigation_deferred"


@pytest.mark.asyncio
async def test_repeating_listing_states_stop_before_a_pagination_loop(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<a id="part" href="/part/a">Part</a>
      <button type="button" aria-label="Next page" onclick="const a=document.querySelector('#part'); a.href=a.href.endsWith('/a') ? '/part/b' : '/part/a'">Next</button>""",
    )

    result = await collect(listing_page, limits=limits(max_steps=8, settle_ms=100))

    assert set(result.links) == {
        "https://catalogue.example.test/part/a",
        "https://catalogue.example.test/part/b",
    }
    assert result.steps == 2
    assert result.stop_reason == "loop_detected"
    assert result.bounded is True


@pytest.mark.asyncio
async def test_time_budget_includes_a_blocked_policy_callback(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page, '<a href="/item/a">A</a><button aria-label="Next page">Next</button>'
    )

    async def policy(action):
        await asyncio.Event().wait()
        return True

    result = await asyncio.wait_for(
        collect(listing_page, limits=limits(max_seconds=0.5), before_action=policy),
        timeout=10,
    )

    assert result.links == ["https://catalogue.example.test/item/a"]
    assert result.steps == 0
    assert result.stop_reason == "time_limit"
    assert result.bounded is True


@pytest.mark.asyncio
async def test_ajax_loading_state_does_not_end_navigation_before_new_links(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<main id="list"><a href="/part/a?fit=first">A</a></main>
      <button type="button" aria-label="Next page">Next</button>
      <script>document.querySelector('button').onclick = function() {
        this.disabled = true;
        document.querySelector('#list').innerHTML = '<p role="status">Loading</p>';
        setTimeout(() => {
          document.querySelector('#list').innerHTML = '<a href="/part/b?fit=second">B</a>';
        }, 150);
      };</script>""",
    )

    result = await collect(listing_page, limits=limits(settle_ms=500))

    assert set(result.links) == {
        "https://catalogue.example.test/part/a?fit=first",
        "https://catalogue.example.test/part/b?fit=second",
    }
    assert result.steps == 1
    assert result.stop_reason == "controls_exhausted"


@pytest.mark.asyncio
async def test_dom_reorder_during_policy_never_retargets_click_to_cart(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<main><a id="part" href="/part/a">A</a></main>
      <button id="next" type="button" aria-label="Next page" onclick="document.querySelector('#part').href='/part/b'; this.disabled=true">Next</button>
      <script>window.cartClicks=0;</script>""",
    )
    inserted = False

    async def policy(action):
        nonlocal inserted
        if not inserted:
            await listing_page.locator("#next").evaluate("""el => el.insertAdjacentHTML('beforebegin',
              '<button type="button" onclick="window.cartClicks++">Add to cart</button>')""")
            inserted = True
        return True

    result = await collect(listing_page, limits=limits(settle_ms=100), before_action=policy)

    assert await listing_page.evaluate("window.cartClicks") == 0
    assert set(result.links) == {
        "https://catalogue.example.test/part/a",
        "https://catalogue.example.test/part/b",
    }
    assert result.steps == 1
    assert result.stop_reason == "controls_exhausted"


@pytest.mark.asyncio
async def test_session_navigation_uses_caller_policy_without_a_second_delay(
    listing_page, monkeypatch
):
    from app.fetching.oneoff import BrowserSession

    limits, _ = navigation_api()
    await open_fixture(
        listing_page,
        """<a id="part" href="/part/a?fit=left">A</a>
      <button type="button" aria-label="Next page" onclick="document.querySelector('#part').href='/part/b?fit=right'; this.disabled=true">Next</button>""",
    )

    async def public_address(url):
        return False

    # Isolate DNS/private-address I/O; DOM navigation and policy dispatch remain
    # real. The caller's pacing policy replaces the session's default delay.
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public_address)
    session = BrowserSession("chromium", delay=(60, 60))
    session.page = listing_page
    approved = []

    async def policy(action):
        approved.append(action.url)
        return True

    result = await session.discover_links(
        limits=limits(max_seconds=2, settle_ms=100), before_action=policy
    )

    assert set(result.links) == {
        "https://catalogue.example.test/part/a?fit=left",
        "https://catalogue.example.test/part/b?fit=right",
    }
    assert result.stop_reason == "controls_exhausted"
    assert approved == ["https://catalogue.example.test/list"]


@pytest.mark.asyncio
async def test_session_navigation_respects_a_previously_stopped_host(listing_page, monkeypatch):
    from app.fetching.oneoff import BrowserSession, HostStopped

    await open_fixture(
        listing_page,
        """<a href="/part/a">A</a>
      <button aria-label="Next page" onclick="window.clicks++">Next</button>
      <script>window.clicks=0;</script>""",
    )

    async def public_address(url):
        return False

    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public_address)
    session = BrowserSession("chromium")
    session.page = listing_page
    session.failures["catalogue.example.test"] = {
        "host": "catalogue.example.test",
        "url": listing_page.url,
    }

    with pytest.raises(HostStopped):
        await session.discover_links()

    assert await listing_page.evaluate("window.clicks") == 0


@pytest.mark.asyncio
async def test_later_listing_anchors_preserve_nofollow_and_previously_followed_links(listing_page):
    from types import SimpleNamespace

    from app.extraction.page import Page
    from app.jobs.runner import JobRunner

    limits, collect = navigation_api()
    initial = """<main><a href="/part/shared?fit=left">Shared</a></main>
      <button type="button" aria-label="Next page">Next</button>
      <script>document.querySelector('button').onclick = function() {
        document.querySelector('main').innerHTML =
          '<a href="/part/shared?fit=left" rel="nofollow">Shared</a>' +
          '<a href="/part/hidden?fit=right" rel="nofollow">Hidden</a>' +
          '<a href="/part/visible?fit=right">Visible</a>';
        this.disabled = true;
      };</script>"""
    await open_fixture(listing_page, initial)

    result = await collect(listing_page, limits=limits(settle_ms=100))
    page = Page(
        initial,
        "https://catalogue.example.test/list",
        extra_links=result.links,
        extra_anchors=result.anchors,
    )
    kept, skips = JobRunner._filter_links(
        SimpleNamespace(link_rules={"nofollow"}), page, page.links()
    )

    assert set(kept) == {
        "https://catalogue.example.test/part/shared?fit=left",
        "https://catalogue.example.test/part/visible?fit=right",
    }
    assert skips == {"nofollow": 1}
    assert (
        "/part/hidden?fit=right",
        "https://catalogue.example.test/part/hidden?fit=right",
        True,
    ) in page.anchors()


@pytest.mark.asyncio
async def test_selected_nofollow_control_can_be_denied_before_click(listing_page):
    limits, collect = navigation_api()
    await open_fixture(
        listing_page,
        """<a href="/part/a">A</a>
      <a href="/list?page=2" rel="next nofollow" onclick="window.clicks++; event.preventDefault()">Next</a>
      <script>window.clicks=0;</script>""",
    )

    async def policy(action):
        return not action.nofollow

    result = await collect(listing_page, limits=limits(settle_ms=100), before_action=policy)

    assert result.stop_reason == "policy_denied"
    assert result.steps == 0
    assert await listing_page.evaluate("window.clicks") == 0
    assert listing_page.url == "https://catalogue.example.test/list"


@pytest.mark.asyncio
@pytest.mark.parametrize("settle_requested", [False, True])
async def test_session_get_only_waits_for_network_idle_when_requested(
    listing_page, monkeypatch, settle_requested
):
    from app.fetching.oneoff import BrowserSession

    await open_fixture(listing_page, '<main><h1>Ready catalogue</h1><a href="/part/a">A</a></main>')

    async def public_address(url):
        return False

    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public_address)
    states = []
    original_wait = listing_page.wait_for_load_state

    async def tracked_wait(state=None, **kwargs):
        if state == "networkidle":
            states.append(state)
        return await original_wait(state, **kwargs)

    # Track the real browser wait rather than replacing readiness behavior.
    monkeypatch.setattr(listing_page, "wait_for_load_state", tracked_wait)
    session = BrowserSession("chromium")
    session.page = listing_page

    status, html, final, _ = await session.get(
        "https://catalogue.example.test/list", settle=settle_requested
    )

    assert status == 200
    assert "Ready catalogue" in html
    assert final == "https://catalogue.example.test/list"
    assert states == (["networkidle"] if settle_requested else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filter_group",
    [
        '<section class="refinement-list">{control}</section>',
        '<div role="group" aria-label="Filters">{control}</div>',
        "<aside>{control}</aside>",
        "<fieldset><legend>Categories</legend>{control}</fieldset>",
    ],
)
async def test_filter_show_more_is_not_listing_navigation(listing_page, filter_group):
    limits, collect = navigation_api()
    filters = filter_group.format(
        control='<button type="button" onclick="window.filterClicks++">Show more</button>'
    )
    await open_fixture(
        listing_page,
        '<div class="filter-layout">'
        + filters
        + """<main><a id="record" href="/record/a?format=lp">A</a></main>
        <nav aria-label="Pagination"><button type="button" aria-label="Next page"
          onclick="document.querySelector('#record').href='/record/b?format=lp'; this.disabled=true">Next</button></nav>
        </div><script>window.filterClicks=0;</script>""",
    )

    result = await collect(listing_page, limits=limits(settle_ms=100))

    assert set(result.links) == {
        "https://catalogue.example.test/record/a?format=lp",
        "https://catalogue.example.test/record/b?format=lp",
    }
    assert await listing_page.evaluate("window.filterClicks") == 0
    assert result.steps == 1
    assert result.stop_reason == "controls_exhausted"
    assert result.bounded is False


@pytest.fixture
def redirect_listing_server():
    """Local HTTP redirect chain: the collector must never start it."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/list?page=2":
                self.send_response(302)
                self.send_header("Location", "/cart?add-to-cart=42")
                self.end_headers()
                return
            body = (
                '<a rel="next" href="/list?page=2">Next page</a>'
                if self.path == "/list"
                else "<main>Action endpoint</main>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.asyncio
async def test_listing_defers_document_navigation_before_http_redirect_chain(
    listing_page, redirect_listing_server
):
    limits, collect = navigation_api()
    origin, requests = redirect_listing_server
    await listing_page.goto(origin + "/list")

    result = await collect(listing_page, limits=limits(settle_ms=100))

    assert "/list?page=2" not in requests
    assert "/cart?add-to-cart=42" not in requests
    assert listing_page.url == origin + "/list"
    assert result.deferred_links == [origin + "/list?page=2"]
    assert origin + "/list?page=2" in result.links
    assert result.stop_reason == "navigation_deferred"
    assert result.bounded is True


@pytest.mark.asyncio
@pytest.mark.parametrize("destination, method", [
    ("/cart?add-to-cart=42", "GET"),
    ("/list?action=delete", "GET"),
    ("https://outside.example.test/list", "GET"),
    ("/list?page=2", "POST"),
])
async def test_script_navigation_cannot_issue_unsafe_document_requests(
    listing_page, destination, method
):
    limits, collect = navigation_api()
    sent = []
    script = (
        f"location.href={destination!r}"
        if method == "GET"
        else "const form=document.createElement('form'); form.method='POST'; "
             "form.action='/list?page=2'; document.body.append(form); form.submit()"
    )
    initial = f'<button aria-label="Next page" onclick="{script}">Next</button>'

    async def fixture(route):
        sent.append((route.request.url, route.request.method))
        await route.fulfill(content_type="text/html", body=initial if len(sent) == 1 else "Done")

    await listing_page.route("**/*", fixture)
    await listing_page.goto("https://catalogue.example.test/list")
    result = await collect(listing_page, limits=limits(settle_ms=100))

    assert sent == [("https://catalogue.example.test/list", "GET")]
    assert listing_page.url == "https://catalogue.example.test/list"
    assert await listing_page.locator("button").count() == 1
    assert result.stop_reason == "policy_denied"
    assert result.bounded is True
    assert not result.deferred_links


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["scope", "robots", "cancelled", "error", "timeout"])
async def test_document_policy_and_cleanup_preserve_existing_routes(listing_page, denial):
    limits, collect = navigation_api()
    sent = []
    calls = []
    stopped = False
    body = """<button aria-label="Next page" onclick="location.href='/list?page=2'">Next</button>"""

    async def fixture(route):
        sent.append(route.request.url)
        await route.fulfill(content_type="text/html", body=body)

    async def policy(action):
        nonlocal stopped
        calls.append((action.kind, action.url))
        if action.kind == "navigation":
            if denial == "error":
                raise RuntimeError("policy unavailable")
            if denial == "timeout":
                await asyncio.Event().wait()
            if denial == "cancelled":
                stopped = True
                return True
            return False
        return True

    async def cancelled():
        return stopped

    await listing_page.route("**/*", fixture)
    await listing_page.goto("https://catalogue.example.test/list")
    result = await collect(
        listing_page, limits=limits(settle_ms=100, max_seconds=0.5), before_action=policy,
        cancelled=cancelled if denial == "cancelled" else None,
    )

    assert sent == ["https://catalogue.example.test/list"]
    assert result.stop_reason == {"error": "action_error", "timeout": "time_limit", "cancelled": "cancelled"}.get(denial, "policy_denied")
    assert not result.deferred_links
    assert ("navigation", "https://catalogue.example.test/list?page=2") in calls
    # The temporary guard is removed even on error; the previous route survives.
    await listing_page.goto("https://catalogue.example.test/after")
    assert sent[-1] == "https://catalogue.example.test/after"


@pytest.mark.asyncio
async def test_document_guard_preserves_xhr_listing_rendering(listing_page):
    limits, collect = navigation_api()
    sent = []
    html = """<main><a href="/part/a">A</a></main>
      <button aria-label="Next page" onclick="this.disabled=true; fetch('/api/list')
        .then(response => response.text()).then(html => document.querySelector('main').innerHTML=html)">Next</button>"""

    async def fixture(route):
        sent.append(route.request.url)
        await route.fulfill(content_type="text/html", body=(
            '<a href="/part/b">B</a>' if route.request.url.endswith("/api/list") else html
        ))

    await listing_page.route("**/*", fixture)
    await listing_page.goto("https://catalogue.example.test/list")
    result = await collect(listing_page, limits=limits(settle_ms=300))

    assert sent == ["https://catalogue.example.test/list", "https://catalogue.example.test/api/list"]
    assert "https://catalogue.example.test/part/b" in result.links
    assert result.stop_reason == "controls_exhausted"
    assert not result.deferred_links


@pytest.mark.asyncio
async def test_session_can_reuse_document_after_deferring_navigation(listing_page, monkeypatch):
    from app.fetching.oneoff import BrowserSession

    limits, _ = navigation_api()
    url = "https://catalogue.example.test/list"
    await open_fixture(listing_page, '<a rel="next" href="/list?page=2">Next page</a>', url)

    async def public(url):
        return False

    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    session = BrowserSession("chromium")
    session.page = listing_page
    result = await session.discover_links(limits=limits(settle_ms=100))

    assert result.stop_reason == "navigation_deferred"
    assert result.deferred_links == [url + "?page=2"]
    assert listing_page.url == url
    assert await listing_page.locator('a[rel="next"]').count() == 1
    status, html, final, _ = await session.get(url)
    assert status == 200
    assert "Next page" in html
    assert final == url


@pytest.mark.asyncio
async def test_deferred_navigation_waits_for_async_destination_policy(listing_page):
    limits, collect = navigation_api()
    await open_fixture(listing_page, """<a href="/part/a">A</a>
      <button aria-label="Next page" onclick="this.disabled=true;
        setTimeout(() => location.href='/list?page=2', 50)">Next</button>""")

    async def policy(action):
        if action.kind == "navigation":
            await asyncio.sleep(0.2)
        return True

    result = await collect(
        listing_page, limits=limits(settle_ms=500), before_action=policy
    )

    assert result.stop_reason == "navigation_deferred"
    assert result.deferred_links == ["https://catalogue.example.test/list?page=2"]
