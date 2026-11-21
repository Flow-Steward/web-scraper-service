"""Browser termination must survive setup's discovery and sampling fallbacks."""

from types import SimpleNamespace

import httpx
import pytest
from playwright.async_api import Error

from app.core.config import merge_layers
from app.core.errors import ApiError
from app.extraction.sampling import Explain
from app.fetching.oneoff import BrowserSession, Fetched
from app.service import Service
from app.setup_ops import SetupFetch, _samples
from tests.conftest import profile
from tests.unit.test_logic import settings


@pytest.mark.parametrize("closed_part", ["page", "browser"])
async def test_closed_browser_after_auxiliary_fetch_is_not_a_selector_failure(
    tmp_path, monkeypatch, closed_part
):
    svc = Service(
        settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567")
    )
    monkeypatch.setattr("app.settings._settings", svc.settings)
    monkeypatch.setattr("app.fetching.browsers.resolve_browser", lambda _: "chromium")
    config, _ = merge_layers(
        {
            "site_url": "https://supplier.test/item",
            "discovery_mode": "crawl",
            "fetch_mode": "browser",
            "target_page_selector": ".sheet",
            "sample_count": 1,
            "delay_min_seconds": 0,
            "delay_max_seconds": 0,
        },
        {},
        {},
    )
    state = {"page_closed": False, "browser_connected": True, "http": []}

    async def public(_url):
        return False

    async def closed_goto(*args, **kwargs):
        # A browser transport exception can contain proxy credentials or other secrets.
        raise Error("Target closed via http://secret-user:secret-password@proxy.test")

    async def close():
        pass

    async def http(url, **kwargs):
        state["http"].append(url)
        return Fetched(url, url, 200, b'<main class="sheet"><h1>Valid item</h1></main>')

    session = BrowserSession("chromium")
    session.page = SimpleNamespace(
        is_closed=lambda: state["page_closed"], goto=closed_goto, title=closed_goto
    )
    session._browser = SimpleNamespace(
        is_connected=lambda: state["browser_connected"], close=close
    )

    async def raw(url):
        return Fetched(url, url, 200, b"User-agent: *\nAllow: /\n")

    async def start(self):
        return session

    monkeypatch.setattr(BrowserSession, "start", start)
    monkeypatch.setattr(session, "raw", raw)
    monkeypatch.setattr("app.setup_ops.http_get", http)
    monkeypatch.setattr("app.setup_ops.url_is_private", public)
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    sf = SetupFetch(svc, config, profile())
    try:
        assert (
            await (await sf.session()).raw("https://supplier.test/robots.txt")
        ).status == 200
        state["page_closed" if closed_part == "page" else "browser_connected"] = (
            closed_part == "page"
        )
        with pytest.raises(ApiError) as failure:
            await _samples(svc, {"config": config, "urls": []}, config, 1, sf)
        assert failure.value.status == 503
        assert failure.value.details["reason"] == "browser_unavailable"
        assert failure.value.details["exception_type"] == "Error"
        assert "browser" in failure.value.message.lower()
        assert "selector" not in failure.value.message.lower()
        assert "secret-" not in str(failure.value.body("test-request"))
        assert state["http"] == []  # no misleading HTTP substitute after closure
        assert await sf.session() is None  # dead context is never returned again
    finally:
        await sf.close()
        svc.db.close()


def test_failed_documents_do_not_justify_selector_advice():
    ex = Explain()
    ex.page(
        "https://supplier.test/item",
        "http",
        "challenged",
        "protection: challenge",
        status=403,
    )
    assert not any(
        h["kind"] == "target_page_selector"
        for h in ex.hints({"target_page_selector": ".sheet"}, 1)
    )


def test_inspected_non_target_document_still_gets_selector_advice():
    ex = Explain()
    ex.page(
        "https://supplier.test/item",
        "browser",
        "not_target",
        "target_page_selector absent",
        status=200,
        selector_found=False,
    )
    assert (
        ex.hints({"target_page_selector": ".sheet"}, 1)[0]["kind"]
        == "target_page_selector"
    )


async def test_failed_browser_start_is_a_sanitized_api_error_and_closes_partial_session(
    tmp_path, monkeypatch
):
    from app.main import create_app

    svc = Service(
        settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567")
    )
    monkeypatch.setattr("app.settings._settings", svc.settings)
    monkeypatch.setattr("app.service._service", svc)
    monkeypatch.setattr("app.fetching.browsers.resolve_browser", lambda _: "chromium")
    config, _ = merge_layers(
        {
            "site_url": "https://supplier.test/item",
            "discovery_mode": "crawl",
            "fetch_mode": "browser",
            "target_page_selector": ".sheet",
            "sample_count": 1,
        },
        {},
        {},
    )
    closed = []

    async def close_context():
        closed.append("context")

    async def fail_start(self):
        self.context = SimpleNamespace(close=close_context)
        raise RuntimeError("launch failed with secret-password")

    monkeypatch.setattr(BrowserSession, "start", fail_start)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()),
            base_url="http://service.test",
        ) as client:
            response = await client.post(
                "/v1/samples",
                json={"config": config, "profile": profile()},
                headers={"Authorization": "Bearer t"},
            )
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["details"] == {
            "reason": "browser_unavailable",
            "exception_type": "RuntimeError",
        }
        assert error["request_id"]
        assert "secret-password" not in response.text
        assert closed == ["context"]
    finally:
        svc.db.close()
