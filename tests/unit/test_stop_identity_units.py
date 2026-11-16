"""Units of the 2026-11-13 round: identity per exit, the failed-route message,
product signals, the target-page selector gate, the contract additions, and a
guard against undefined module globals (the solver regression: INJECT_TOKEN_JS was used but
no longer defined, and the NameError was swallowed)."""

from __future__ import annotations

import builtins
import symtable
from pathlib import Path

import pytest

from app.core.schemas import validator

ROOT = Path(__file__).resolve().parents[2]
TIER = {"tier": 1, "provider": "oxylabs", "protocol": "http", "host": "gw.example", "port": 8000, "auth_mode": "user_password",
        "username": "acct", "password": "secret", "session_mode": "sticky", "sticky_minutes": 10}


def _geo(ip):
    from app.fetching.geo import ExitGeo

    return ExitGeo(ip=ip, timezone="Europe/Riga", locale="lv-LV", latitude=56.9, longitude=24.1, country="LV", source="echo")


# ---------------------------------------------------------------- identity per exit
def test_identity_keys_follow_account_tier_and_exit():
    from app.fetching.identity import key_for
    from app.fetching.proxies import tier_url

    u1, u2 = tier_url(TIER, "s1"), tier_url(TIER, "s2")
    other = tier_url(dict(TIER, tier=2), "s1")
    assert u1 != u2
    k = key_for("camoufox", "windows", u1, _geo("5.6.7.8"))
    assert k == key_for("camoufox", "windows", u2, _geo("5.6.7.8"))  # another sticky session, same exit IP
    assert k != key_for("camoufox", "windows", u1, _geo("9.9.9.9"))  # another exit IP
    assert k != key_for("camoufox", "windows", other, _geo("5.6.7.8"))  # another tier
    assert key_for("camoufox", "windows", u1, None) != key_for("camoufox", "windows", u2, None)  # exit unknown: per session
    assert key_for("camoufox", "windows", None, _geo("5.6.7.8")).split("|")[2:] == ["direct", "ip:5.6.7.8"]  # no proxy: host IP
    assert "secret" not in k and "acct" not in k


async def test_identity_is_reused_per_exit_and_survives_a_restart(tmp_path):
    from app.fetching import identity as idm
    from app.fetching.proxies import tier_url

    path = tmp_path / "identities.json"
    idm.reset_for_tests(path)
    try:
        a = await idm.identity_for("camoufox", "windows", tier_url(TIER, "s1"), _geo("5.6.7.8"))
        b = await idm.identity_for("camoufox", "windows", tier_url(TIER, "s2"), _geo("5.6.7.8"))
        c = await idm.identity_for("camoufox", "windows", tier_url(TIER, "s3"), _geo("9.9.9.9"))
        ch = await idm.identity_for("chromium", "windows", tier_url(TIER, "s1"), _geo("5.6.7.8"))
        assert a.preset_id == b.preset_id and a.preset_id != c.preset_id and ch.preset_id not in (a.preset_id, c.preset_id)
        assert a.preset and a.salt is not None and ch.fingerprint
        assert path.exists() and "secret" not in path.read_text()
        idm.reset_for_tests(path)  # a restart: read back from the volume
        again = await idm.identity_for("camoufox", "windows", tier_url(TIER, "s9"), _geo("5.6.7.8"))
        assert again.preset_id == a.preset_id and again.preset == a.preset and again.salt == a.salt
        assert idm.store().find_by_exit("camoufox", "windows", "5.6.7.8").preset_id == a.preset_id
    finally:
        idm.reset_for_tests(None)


def test_camoufox_identity_pins_fonts_voices_gpu_and_screen():
    from app.fetching.identity import camoufox_fingerprint, new_camoufox_identity

    ident = new_camoufox_identity("windows", "158", "5.6.7.8")
    one, two = camoufox_fingerprint(ident, "158"), camoufox_fingerprint(ident, "158")
    for key in ("fonts", "voices", "webGl:vendor", "webGl:renderer", "screen.width", "navigator.hardwareConcurrency", "navigator.userAgent"):
        assert one["config"].get(key) == two["config"].get(key), key
    assert one["config"].get("fonts"), "a Windows identity reports fonts"


def test_chromium_identity_speaks_the_exit_locale():
    from app.fetching.identity import chromium_fingerprint, new_chromium_identity

    ident = new_chromium_identity("5.6.7.8")
    fp = chromium_fingerprint(ident, "lv-LV")
    assert fp.navigator.language == "lv-LV" and fp.navigator.languages[0] == "lv-LV" and fp.headers["Accept-Language"].startswith("lv-LV")
    # Chromium claims the machine's own OS (Linux in the image), never Windows on Linux
    assert fp.screen.width >= 1280 and fp.screen.height >= 720 and "Linux" in fp.navigator.userAgent and "Windows" not in fp.navigator.userAgent
    assert fp.navigator.platform.startswith("Linux") and ident.os == "linux"
    assert chromium_fingerprint(ident, "lv-LV").videoCard == fp.videoCard  # the same GPU every time


# ---------------------------------------------------------------- message
def test_failed_route_message():
    from app.fetching.protection import describe_failure

    trace = {"challenge": "interstitial", "proxy_tier": 1, "steps": [
        {"step": "wait", "outcome": "timed_out", "seconds": 15.0},
        {"step": "click", "outcome": "still_challenged", "seconds": 15.5},
        {"step": "solver", "outcome": "skipped", "reason": "not_allowed_by_service"}]}
    msg = describe_failure({"host": "www.example.com", "trace": trace, "vendor": "Cloudflare"}, "No further pages of this host were fetched.")
    assert msg == ("www.example.com shows a Cloudflare interstitial challenge that was not passed (waited 15 s, clicked the checkbox once, "
                   "no solver allowed); proxy tier 1. No further pages of this host were fetched.")
    plain = describe_failure({"host": "a.test", "trace": {"challenge": None, "proxy_tier": None, "steps": []}, "vendor": None}, "x")
    assert plain.startswith("a.test shows a challenge that was not passed (no step could be tried); no proxy.")


def test_host_label():
    from app.fetching.oneoff import host_label

    assert host_label("https://www.Example.com/a") == "www.example.com" and host_label("http://guard.test:8603/p/1") == "guard.test:8603"


# ---------------------------------------------------------------- product signals, selector gate
def test_product_signals():
    from app.extraction.page import Page
    from app.extraction.target_guess import describe_signals, product_signals

    shop = Page("<html><head><meta property='og:type' content='product'></head><body><h1>Lamp</h1><span>129,90 €</span>"
                "<button>Add to cart</button><div class='gallery'><img src='a.jpg'></div></body></html>", "https://s.example/p/1")
    about = Page("<html><body><h1>About us</h1><p>Since 1990 we have grown to 40 people.</p><img src='team.jpg'></body></html>", "https://s.example/about")
    assert product_signals(shop) == {"price", "cart", "markup", "gallery"} and product_signals(about) == set()
    assert describe_signals(["price", "cart"]) == "price-like text, an add-to-cart or quantity control"
    assert product_signals(Page("<body><p>£12.50</p><input name='quantity'></body>", "https://s.example/x")) == {"price", "cart"}


def test_target_page_selector_gate():
    from app.extraction.mapping import passes_target_selector
    from app.extraction.page import Page

    page = Page("<body><div class='product-detail'>x</div></body>", "https://s.example/p")
    assert passes_target_selector(page, {}) and passes_target_selector(page, {"target_page_selector": "div.product-detail"})
    assert not passes_target_selector(page, {"target_page_selector": ".part-sheet"})
    assert passes_target_selector(page, {"target_page_selector": "div[[["})  # does not parse: gates nothing


# ---------------------------------------------------------------- contract additions
def test_contract_accepts_the_new_fields_and_refuses_bad_ones():
    stopped = {"site": "a.test", "requested_count": 2, "samples": [], "warnings": [
        {"field": "urls[0]", "code": "protection_not_passed", "message": "a.test shows a challenge that was not passed"}],
        "stopped": {"reason": "protection_not_passed", "host": "a.test", "url": "https://a.test/p", "skipped_urls": 1}}
    assert not list(validator("samples.schema.json").iter_errors(stopped))
    bad = dict(stopped, stopped={"reason": "timeout", "host": "a.test", "url": "https://a.test/p", "skipped_urls": -1})
    assert len(list(validator("samples.schema.json").iter_errors(bad))) >= 2
    trace_env = {"url": "https://a.test/", "browser": "camoufox", "proxy_tier": None, "steps": [], "passed_at_step": None, "cookies": [],
                 "final_title": "", "environment": {"exit": None, "browser_view": {}, "preset_id": "fp_0123456789abcdef"}}
    assert not list(validator("analysis.schema.json#/$defs/protection_trace").iter_errors(trace_env))
    trace_env["environment"]["preset_id"] = "NVIDIA"
    assert list(validator("analysis.schema.json#/$defs/protection_trace").iter_errors(trace_env))
    test_answer = {"passed": True, "required_fill_threshold": 0.9, "unmatched_sample_ids": [], "samples": [],
                   "templates": [{"template_id": "page", "matched_samples": 4, "passed": True, "fields": [], "empty_fields": ["price"]}],
                   "warnings": [{"field": "mappings[0].fields", "code": "fields_never_filled", "message": "template page: price filled on 0 of 4"}]}
    assert not list(validator("mapping_test.schema.json#/$defs/response").iter_errors(test_answer))


# ---------------------------------------------------------------- undefined globals
def _undefined_globals(path: Path) -> list[str]:
    """Names a function reads as module globals that the module never binds (pyflakes' F821
    for globals, without pyflakes)."""
    source = path.read_text()
    table = symtable.symtable(source, str(path), "exec")
    module_names = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}

    def declared(t):
        for child in t.get_children():
            module_names.update(s.get_name() for s in child.get_symbols() if s.is_declared_global() and s.is_assigned())
            declared(child)

    declared(table)
    missing: list[str] = []

    def walk(t):
        for child in t.get_children():
            for sym in child.get_symbols():
                if sym.is_referenced() and sym.is_global():
                    name = sym.get_name()
                    if name not in module_names and not hasattr(builtins, name) and name != "__file__":
                        missing.append(f"{path.relative_to(ROOT)}:{child.get_name()}:{name}")
            walk(child)

    walk(table)
    return missing


@pytest.mark.parametrize("path", sorted((ROOT / "app").rglob("*.py")) + [ROOT / "scripts" / "protection_report.py"], ids=lambda p: str(p.relative_to(ROOT)))
def test_no_undefined_module_globals(path):
    assert _undefined_globals(path) == []


def test_exit_locale_is_the_country_main_language_every_time():
    """Camoufox draws a language by share of speakers (en-DE, ru-LV now and then): one exit
    must not change its language between calls (the intermittent en-DE of the env test)."""
    from app.fetching.geo import _locale_for

    for _ in range(20):
        assert _locale_for("DE") == "de-DE" and _locale_for("LV") == "lv-LV" and _locale_for("lt") == "lt-LT"
    assert _locale_for("BE") == "nl-BE" and _locale_for(None) is None and _locale_for("QQ") is None


def test_counts_in_messages_are_singular_or_plural():
    from app.core.words import count

    assert count(1, "page") == "1 page" and count(4, "page") == "4 pages" and count(0, "page") == "0 pages"
    assert count(1, "matched sample") == "1 matched sample" and count(1, "target page") == "1 target page"


# ---------------------------------------------------------------- launch variants
def test_variant_from_config_and_identity_key():
    from app.fetching.browsers import Variant

    assert Variant.from_config({}) == Variant("host", "auto") and Variant.from_config({"browser_os": "bsd", "canvas_noise": "x"}) == Variant()
    assert Variant().os_name() == "linux"  # the image's own OS
    v = Variant.from_config({"browser_os": "host", "canvas_noise": "off"})
    assert v.os_name() == "linux" and v.noise_arg() is False and v.key() == "linux~canvas-off"
    assert Variant("windows", "auto").noise_arg() is None and Variant("windows", "on").noise_arg() is True
    keys = {Variant(o, n).key() for o in ("windows", "macos", "linux") for n in ("auto", "off", "on")}
    assert len(keys) == 9
    assert Variant("linux", "auto").key() == Variant("host", "auto").key()  # the same machine, the same identity


def test_job_settings_accept_the_variant_fields():
    ok = {"browser_os": "linux", "canvas_noise": "off"}
    assert not list(validator("job_config.schema.json#/$defs/job_settings").iter_errors(ok))
    assert list(validator("job_config.schema.json#/$defs/job_settings").iter_errors({"browser_os": "bsd"}))
    assert list(validator("job_config.schema.json#/$defs/job_settings").iter_errors({"canvas_noise": True}))
    env = {"exit": None, "browser_view": {}, "preset_id": None, "browser_os": "windows", "canvas_noise": "auto",
           "canvas_readback": "noise", "webgl_readback": "unavailable"}
    trace = {"url": "https://a.test/", "browser": "camoufox", "proxy_tier": 1, "steps": [], "passed_at_step": None, "cookies": [],
             "final_title": "", "environment": env, "passed_earlier": {"url": "https://a.test/", "step": "click"}}
    assert not list(validator("analysis.schema.json#/$defs/protection_trace").iter_errors(trace))
    env["canvas_readback"] = "random"
    assert list(validator("analysis.schema.json#/$defs/protection_trace").iter_errors(trace))


def test_failed_route_message_after_an_earlier_pass():
    from app.fetching.protection import describe_failure

    trace = {"challenge": "interstitial", "proxy_tier": 1, "passed_earlier": {"url": "https://x.test/", "step": "click"},
             "steps": [{"step": "wait", "outcome": "timed_out", "seconds": 15.3}, {"step": "click", "outcome": "still_challenged"},
                       {"step": "solver", "outcome": "skipped", "reason": "not_requested"}]}
    msg = describe_failure({"host": "x.test", "trace": trace, "vendor": "Cloudflare"}, "No further pages of this host were fetched.")
    assert msg == ("x.test shows a Cloudflare interstitial challenge that was not passed (waited 15 s, clicked the checkbox once, no solver "
                   "requested); the host had passed a challenge earlier in this call (click) and was challenged again; proxy tier 1. "
                   "No further pages of this host were fetched.")


def test_report_matrix_collapses_chromium_rows():
    import argparse
    import importlib.util

    spec = importlib.util.spec_from_file_location("protection_report", ROOT / "scripts" / "protection_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    args = argparse.Namespace(browser="camoufox,chromium", os="windows,linux", canvas_noise="auto,off", variants="click", humanize="on",
                              positions="28", geoip="on", webgl="default", proxy_only=False)
    combos = mod.build_combos(args, None)
    assert len(combos) == 5 and sum(c["browser_name"] == "chromium" for c in combos) == 1
    with_proxy = mod.build_combos(argparse.Namespace(**{**vars(args), "proxy_only": True}), "http://u:p@proxy.example:1")
    assert len(with_proxy) == 5 and all(c["proxy"] for c in with_proxy) and not any("u:p" in c["label"] for c in with_proxy)


def test_wait_step_defaults_to_30_seconds():
    """The host-OS identity's managed challenge cleared itself after 29-30 s."""
    from app.settings import load_settings

    import os

    saved = os.environ.pop("WSS_CHALLENGE_WAIT_SECONDS", None)
    try:
        os.environ.setdefault("WSS_API_TOKEN", "x" * 24)
        assert load_settings().challenge_wait_seconds == 30.0
    finally:
        if saved is not None:
            os.environ["WSS_CHALLENGE_WAIT_SECONDS"] = saved


# ---------------------------------------------------------------- sampling and explanation
def test_links_are_grouped_by_shape_and_probed_one_per_shape_first():
    from app.extraction.page import Page
    from app.extraction.sampling import link_groups, ranked_links

    html = ("<header><nav><a href='/menu-c1'>m1</a><a href='/menu-c1/sub-c2'>m2</a><a href='/menu-c3'>m3</a></nav></header>"
            "<main><a href='/text-link'>t</a><div class='product-card'><a href='/lamp-a-p1'>Lamp 1</a></div>"
            "<a href='/lamp-b-p2'><img src='x.jpg'></a><a href='/cat-c9?page=2'>2</a><a href='/cat-c9/sort/price'>s</a></main>"
            "<footer><a href='/kontakt'>k</a></footer><a href='https://other.example/x'>x</a><a href='/cat-c9'>self</a>")
    page = Page(f"<html><body>{html}</body></html>", "https://s.example/cat-c9")
    groups, stats = link_groups(page, "s.example")
    assert stats == {"hrefs": 11, "in_site": 7, "other_hosts": 1, "variants_skipped": 2}
    assert [(g["shape"], g["links"], g["in_menus"]) for g in groups] == [
        ("/*", 1, 0.0), ("/*-p<n>", 2, 0.0), ("/kontakt", 1, 1.0), ("/*-c<n>", 3, 1.0)]
    assert ranked_links(page, "s.example") == [
        "https://s.example/text-link", "https://s.example/lamp-a-p1", "https://s.example/kontakt", "https://s.example/menu-c1",
        "https://s.example/lamp-b-p2", "https://s.example/menu-c1/sub-c2", "https://s.example/menu-c3"]


def test_probe_plan_drops_dry_shapes_and_follows_a_productive_one():
    from app.extraction.sampling import ProbePlan

    groups = [{"shape": "/a/*", "urls": [f"a{i}" for i in range(5)]}, {"shape": "/*-p<n>", "urls": [f"p{i}" for i in range(5)]},
              {"shape": "/*-c<n>", "urls": [f"c{i}" for i in range(50)]}]
    plan = ProbePlan(groups, limit=20, misses=2)
    seen = []
    while (u := plan.next()) is not None:
        seen.append(u)
        plan.feedback(u, u.startswith("p"))
        if len(seen) == 6:
            break
    assert seen == ["a0", "p0", "p1", "p2", "p3", "p4"]  # one per shape until a shape yields, then that shape
    dry = ProbePlan(groups, limit=20, misses=2)
    out = []
    while (u := dry.next()) is not None:
        out.append(u)
        dry.feedback(u, False)
    assert out == ["a0", "p0", "c0", "a1", "p1", "c1"]  # every shape dropped after 2 misses


def test_url_shapes_and_patterns():
    import re

    from app.extraction.sampling import shape_to_pattern, spread_by_shape, url_shape

    assert url_shape("https://s.lt/automobiliu-zibintai-ju-dalys-c1331") == "/*-c<n>"
    assert url_shape("https://s.lt/kebulo-dalys-c12/bamperiai-c34/leaf-c56") == "/*-c<n>"
    assert url_shape("https://s.lt/priekinis-zibintas-dep55111azlmlde-p1023042") == "/*-p<n>"
    assert url_shape("https://s.lt/lt/x-c1") == "/lt/*-c<n>" and url_shape("https://s.lt/p/12") == "/p/<n>"
    assert url_shape("https://s.lt/produkt/leuchte-01") == "/produkt/*-<n>" and url_shape("https://s.lt/seite/about-us") == "/seite/*"
    assert url_shape("https://s.lt/item-12.html") == "/*-<n>.html" and url_shape("https://s.lt/") == "/"
    pat = re.compile(shape_to_pattern("/*-p<n>"))
    assert pat.search("https://s.lt/priekinis-zibintas-dep55111azlmlde-p1023042") and not pat.search("https://s.lt/x-c1331")
    assert shape_to_pattern("/lt/*") == "/lt/[^/?#]+$" and shape_to_pattern("/p/<n>") == r"/p/\d+$"
    urls = [f"https://s.example/info/page-{i}" for i in range(50)] + [f"https://s.example/p/lamp-{i}" for i in range(10)]
    urls += ["https://s.example/p/lamp-1?color=red"]
    picked = spread_by_shape(urls, 6)
    assert len(picked) == 6 and sum("/p/" in u for u in picked) == 3 and not any("?" in u for u in picked)


def test_explanation_counts_summary_hints_and_log_line():
    from app.extraction.sampling import Explain

    ex = Explain()
    ex.discovery.update(source="crawl", start_is_deep=True, shapes=2, start_page={
        "url": "https://s.example/cat-c1", "status": 200, "loaded": "browser", "hrefs": 2186, "in_site": 1040, "other_hosts": 14,
        "variants_skipped": 3, "shapes": [{"shape": "/*-c<n>", "links": 1028, "in_menus": 1.0}, {"shape": "/*-p<n>", "links": 12, "in_menus": 0.0}]})
    ex.page("https://s.example/cat-c1", "browser", "start", status=200, links=1040, selector_found=False)
    ex.page("https://s.example/a-p1", "browser", "not_target", "target_page_selector absent", ["price", "gallery"], status=200, links=40,
            selector_found=False)
    ex.page("https://s.example/b-c2", "browser", "not_target", "target_page_selector absent", [], status=200, links=900, selector_found=False)
    ex.page("https://s.example/x-c3", "http", "challenged", "protection: challenge", None, status=403)
    out = ex.public({"target_page_selector": ".pp"}, 2)
    assert out["counts"] == {"tried": 3, "http": 1, "browser": 2, "reused": 0, "not_loaded": 0, "samples": 0, "not_target": 2,
                             "challenged": 1, "failed": 0, "skipped": 0, "probed": 0, "selector_found": 0}
    assert [h["kind"] for h in out["hints"]] == ["target_page_selector", "url_include", "protection"]
    assert out["hints"][1]["value"] == r"/[^/?#]*-p\d+$"
    assert out["summary"].startswith("start page (browser, HTTP 200): 2186 links, 1040 on this site in 2 URL shapes "
                                     "(/*-c<n> ×1028, /*-p<n> ×12), 3 pagination/filter variants skipped; "
                                     "probed: /*-p<n> 1 (selector on 0) (product signals on 1); /*-c<n> 2 (selector on 0); "
                                     "3 pages tried (1 over HTTP, 2 in the browser, 0 already loaded); 0 samples, 2 not target pages, 1 challenged"), out["summary"]
    assert "Next: none of the 3 pages loaded had the product page selector .pp" in out["summary"]
    line = ex.log_line("s.example")
    assert line.startswith("samples site=s.example source=crawl start_links=2186/1040 shapes=2 sitemap=- tried=3 http=1 browser=2")
    assert "pages: https://s.example/cat-c1 browser 200 sel=no links=1040 start | https://s.example/a-p1 browser 200 sel=no links=40 not_target" in line
    assert not list(validator("samples.schema.json").iter_errors(
        {"site": "s.example", "requested_count": 2, "samples": [], "warnings": [], "explanation": out}))

# ---------------------------------------------------------------- a still-loading page is not judged
class _FakePage:
    """A page that shows Firefox's "Loading <url>" placeholder for ``loading`` checks, then the
    document; frames appear only once it has loaded."""

    def __init__(self, loading: int, url: str = "https://s.example/a-p1") -> None:
        self.loading, self.url, self.calls = loading, url, 0
        self.main_frame = object()

    async def wait_for_load_state(self, *_a, **_k):
        return None

    async def title(self):
        self.calls += 1
        return f"Loading {self.url}" if self.calls <= self.loading else "Lamp A"

    async def content(self):
        return "<html><body>x</body></html>"

    @property
    def frames(self):
        return [self.main_frame]


async def test_settle_waits_for_a_loading_page_and_gives_up_after_the_bound():
    from app.fetching.challenge import settle

    page = _FakePage(loading=3)
    assert await settle(page, 5.0) is True and page.calls == 4
    never = _FakePage(loading=10 ** 6)
    assert await settle(never, 1.0) is False
    blank = _FakePage(loading=0, url="about:blank")
    assert await settle(blank, 0.6) is False


async def test_click_looks_for_the_widget_again_after_the_page_loaded(monkeypatch):
    from app.fetching import protection

    page = _FakePage(loading=0)
    calls = {"n": 0}
    box = {"x": 10, "y": 10, "width": 300, "height": 65}

    async def find_box(_page, frames_only=False):
        calls["n"] += 1
        return (box, "frames") if calls["n"] >= 2 else (None, "none")

    async def fake_click(*_a, **_k):
        return None

    async def passed(_page, _status=None):
        from app.fetching.challenge import Verdict

        return Verdict("none")

    monkeypatch.setattr(protection, "_find_box", find_box)
    monkeypatch.setattr(protection, "human_click", fake_click)
    monkeypatch.setattr(protection, "detect_page", passed)
    out = await protection.click_checkbox_traced(page, wait_seconds=2.0, poll=0.1)
    assert calls["n"] == 2 and out["result"] == "passed" and out["frame_found_via"] == "frames"
