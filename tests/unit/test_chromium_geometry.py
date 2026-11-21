"""A stored Chromium identity must contain a coherent whole fingerprint."""
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from app.fetching import identity


@pytest.fixture
def fingerprints():
    sample = identity.chromium_generator("linux").generate()
    good = deepcopy(sample)
    good.screen.width = good.screen.availWidth = 1366
    good.screen.height = good.screen.availHeight = 768
    good.screen.outerWidth, good.screen.outerHeight = 1280, 720
    good.screen.innerWidth = good.screen.innerHeight = 0
    bad = deepcopy(good)
    bad.screen.outerWidth, bad.screen.outerHeight = 1374, 772
    return bad, good


@pytest.mark.parametrize("axis", ["width", "height"])
def test_chromium_draw_rejects_window_larger_than_screen(monkeypatch, fingerprints, axis):
    bad, good = fingerprints
    if axis == "width":
        bad.screen.outerHeight = 720
    else:
        bad.screen.outerWidth = 1280
    draws = iter([bad, good])
    monkeypatch.setattr(identity, "chromium_generator", lambda os: SimpleNamespace(generate=lambda: next(draws)))

    ident = identity.new_chromium_identity("203.0.113.10", "linux")

    assert ident.fingerprint["screen"]["outerWidth"] == 1280
    assert ident.fingerprint["screen"]["outerHeight"] == 720
    assert ident.fingerprint == asdict(good)


def test_chromium_draw_retains_unknown_window_dimensions(monkeypatch, fingerprints):
    _, good = fingerprints
    good.screen.outerWidth = good.screen.outerHeight = 0
    monkeypatch.setattr(identity, "chromium_generator", lambda os: SimpleNamespace(generate=lambda: good))

    ident = identity.new_chromium_identity("203.0.113.10", "linux")

    assert ident.fingerprint["screen"]["outerWidth"] == 0
    assert ident.fingerprint["screen"]["outerHeight"] == 0


def test_chromium_draw_fails_clearly_when_no_coherent_fingerprint(monkeypatch, fingerprints):
    bad, _ = fingerprints
    # A runaway rejection loop fails quickly rather than hanging this test.
    draws = iter([bad] * 100)
    monkeypatch.setattr(identity, "chromium_generator", lambda os: SimpleNamespace(generate=lambda: next(draws)))

    with pytest.raises(ValueError, match="coherent.*fingerprint"):
        identity.new_chromium_identity("203.0.113.10", "linux")


async def test_persisted_invalid_geometry_is_replaced_once_before_context_creation(tmp_path, monkeypatch, fingerprints):
    bad, good = fingerprints
    path = tmp_path / "identities.json"
    geo = SimpleNamespace(ip="203.0.113.10")
    key = identity.key_for("chromium", "linux", None, geo)
    stored = identity.Identity(preset_id="invalid-synthetic", browser="chromium", os="linux", fingerprint=asdict(bad), exit_ip=geo.ip)
    path.write_text(json.dumps({"identities": {key: asdict(stored)}}))
    monkeypatch.setattr(identity, "chromium_generator", lambda os: SimpleNamespace(generate=lambda: good))
    identity.reset_for_tests(path)
    try:
        first = await identity.identity_for("chromium", "linux", None, geo)
        second = await identity.identity_for("chromium", "linux", None, geo)
        identity.reset_for_tests(path)
        after_restart = await identity.identity_for("chromium", "linux", None, geo)

        assert first.preset_id != "invalid-synthetic"
        assert first.preset_id == second.preset_id == after_restart.preset_id
        assert first.exit_ip == second.exit_ip == after_restart.exit_ip == geo.ip
        assert first.fingerprint == after_restart.fingerprint == asdict(good)
    finally:
        identity.reset_for_tests(None)


def test_invalid_direct_identity_cannot_start_a_context(fingerprints):
    bad, _ = fingerprints
    ident = identity.Identity(preset_id="invalid-synthetic", browser="chromium", os="linux", fingerprint=asdict(bad))

    with pytest.raises(ValueError, match="coherent.*fingerprint"):
        identity.chromium_fingerprint(ident, "de-DE")
