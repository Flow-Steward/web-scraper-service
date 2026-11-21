"""Production browser contexts must keep request routing enforceable."""
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.fetching import identity


@pytest.mark.parametrize("preset", [None, {"id": "synthetic"}])
async def test_camoufox_context_blocks_service_workers(monkeypatch, preset):
    module = ModuleType("camoufox.async_api")

    async def create(browser, **options):
        return SimpleNamespace(options=options)

    module.AsyncNewContext = create
    # Without private helpers the supported compatibility path is used for a
    # preset, while the no-preset branch goes directly to AsyncNewContext.
    monkeypatch.setitem(sys.modules, "camoufox", ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.async_api", module)
    ident = SimpleNamespace(preset=preset, preset_id="synthetic") if preset else None

    context = await identity.camoufox_context_raw(
        object(), ident, proxy=None, service_workers="allow"
    )

    assert context.options["service_workers"] == "block"


async def test_chromium_context_blocks_service_workers(monkeypatch):
    module = ModuleType("browserforge.injectors.playwright")

    async def create(**options):
        return SimpleNamespace(options=options)

    async def readback(context, key):
        return {}

    module.AsyncNewContext = create
    monkeypatch.setitem(sys.modules, "browserforge.injectors.playwright", module)
    monkeypatch.setattr(identity, "chromium_fingerprint", lambda ident, locale: None)
    monkeypatch.setattr(identity, "readback", readback)

    context = await identity.chromium_context(
        object(), SimpleNamespace(os="synthetic", preset_id="synthetic"),
        service_workers="allow",
    )

    assert context.options["service_workers"] == "block"
