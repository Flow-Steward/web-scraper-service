"""Unusable 200 answers and the automatic slow-down: the pure parts."""

from __future__ import annotations

from app.core.config import merge_layers, politeness_text
from app.jobs import softerror
from app.jobs.throttle import HostThrottle

PAGE = "<!doctype html><html><head><title>Lamp 12</title></head><body><main><h1>Lamp 12</h1>" + "<p>Opal glass, brass, E27.</p>" * 200 + "</main></body></html>"


def _stats(n: int = 6) -> softerror.ShapeStats:
    st = softerror.ShapeStats()
    for _ in range(n):
        st.add(len(PAGE), True, True)
    return st


def test_judge_unusable_answers():
    st = _stats()
    assert softerror.judge(PAGE, st) is None
    assert softerror.judge("", st)[0] == "empty" and softerror.judge("<html><body> </body></html>", None)[0] == "empty"
    assert softerror.judge("<!doctype html><html><head><title>Lamps</title></head><body><h1>Lamps</h1><a href='/p5'>Item 5</a></body></html>", None) is None
    assert softerror.judge(PAGE[: len(PAGE) // 2], st)[0] == "truncated"
    assert softerror.judge(PAGE[: len(PAGE) // 2] + "</p>", None) is None  # nothing known about the shape: an open end alone is no proof
    assert softerror.judge(PAGE[: len(PAGE) // 2] + "<p>Opal gl", None)[0] == "truncated"  # … unless it stops in the middle of the text
    for title, h1 in (("Wartungsarbeiten", "Wir sind gleich zurück"), ("Service Unavailable", "Please try again later"),
                      ("Error establishing a database connection", ""), ("Технические работы", "Попробуйте позже"),
                      ("Techniniai darbai", "Bandykite vėliau")):
        page = f"<!doctype html><html><head><title>{title}</title></head><body><h1>{h1}</h1><p>Sorry.</p></body></html>"
        assert softerror.judge(page, st)[0] == "error_page", title
    # a real product called "maintenance spray" is a full page, not an error page
    product = PAGE.replace("Lamp 12", "Maintenance spray — please try again later offers")
    assert softerror.judge(product, st) is None
    assert softerror.judge(PAGE[:300] + "</main></body></html>", st)[0] == "too_small"  # PAGE is over 5000 characters
    assert softerror.lost_template(st, "not_target")[0] == "lost_template" and softerror.lost_template(st, "extracted") is None
    mixed = _stats()
    mixed.add(len(PAGE), True, False)
    assert softerror.lost_template(mixed, "no_template") is None


def test_throttle_slows_down_and_relaxes():
    th = HostThrottle(1.0, 2.0)
    msgs = [th.observe("h", i % 3 != 0, 0.2) for i in range(12)]
    got = [m for m in msgs if m]
    assert got and got[0][0] == "slowed_down" and "errors in the last" in got[0][1] and th.factor("h") >= 2
    assert "delay now 2.0–4.0 s (one page at a time)" in got[0][1]
    for _ in range(80):
        th.observe("h", True, 0.2)
    assert th.factor("h") == 1.0  # relaxed back to the configured delays, never below them
    th2 = HostThrottle(0.0, 0.0)
    for _ in range(8):
        th2.observe("s", True, 1.0)
    msgs = [th2.observe("s", True, 7.0) for _ in range(20)]
    assert any(m and m[0] == "slowed_down" and "median response" in m[1] for m in msgs)


def test_politeness_preset_merge():
    cfg, src = merge_layers({"site_url": "https://a.example/", "politeness": "gentle"}, {}, {})
    assert (cfg["delay_min_seconds"], cfg["delay_max_seconds"], cfg["max_concurrency"], cfg["max_requests_per_minute"], cfg["page_timeout_seconds"]) == (5, 10, 1, 10, 120)
    assert politeness_text(cfg) == "gentle: 5–10 s, 1 at a time, 10/min, page timeout 120 s"
    cfg, _ = merge_layers({"site_url": "https://a.example/", "politeness": "fast"}, {"delay_max_seconds": 0.8}, {})
    assert (cfg["delay_min_seconds"], cfg["delay_max_seconds"]) == (0.5, 0.8) and cfg["when_site_busy"] == "skip"
    cfg, _ = merge_layers({"site_url": "https://a.example/"}, {}, {})
    assert politeness_text(cfg) == "normal: 2–5 s, 2 at a time, 30/min, page timeout 60 s"
