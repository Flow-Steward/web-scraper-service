"""Pass-rate report for ONE URL of a site you own (Camoufox OS and canvas).

Tries a small matrix of browser variants against a managed challenge and prints a table:
browser, OS, canvas_noise, the canvas/WebGL self-check, passed/failed and at which step,
final title, cookies, preset id; a screenshot of every failed attempt goes to --shots.
Polite by construction: sequential, a pause between attempts, a hard cap on page loads,
and it refuses to run without --i-own-this-site.

Inside the service container (the script is in the image at /srv/scripts):

    docker exec -it -e WSS_REPORT_PROXY_URL='<proxy URL>' web-scraper \\
        python /srv/scripts/protection_report.py https://shop.example/ --i-own-this-site \\
        --browser camoufox,chromium --os host,windows --canvas-noise auto,off \\
        [--attempts 1] [--pause 30] [--max-loads 20] [--shots /data/protection-shots] \\
        [--variants click] [--humanize on] [--positions 28] [--geoip on] [--webgl default] \\
        [--proxy-only]

--browser: camoufox and/or chromium (Chromium ignores --os and --canvas-noise; one row each).
--os: the Camoufox identity's OS: host (default, the machine's own OS: linux here), windows,
macos, linux. Chromium always claims the host OS.
--canvas-noise: auto = Camoufox's own rule (a Windows identity on this Linux host answers
every canvas readback with random data), off = the host's real pixels, on = always random.
--variants: wait = only wait for the challenge to pass by itself; click = wait, then one
click on the checkbox. --geoip on: timezone, locale, geolocation and WebRTC IP of the exit
(app/fetching/geo.py); off: the container's defaults. --webgl spoofed: launch Camoufox with
webgl_config = the preset's own vendor/renderer (when Camoufox has data for it).

A proxy run is added when WSS_REPORT_PROXY_URL is set (scheme://user:pass@host:port; never
printed); --proxy-only skips the run without a proxy. Launch and contexts are the
service's own (app/fetching/browsers.py launch_camoufox, identity.py): every attempt uses
the identity the service keeps for that exit and variant (`identities.json` in
WSS_DATA_DIR), so the report tests the machine the service presents there.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("WSS_API_TOKEN", "protection-report")  # settings are loaded by the helpers

from app.core.redaction import redact  # noqa: E402
from app.fetching.challenge import detect_page  # noqa: E402
from app.fetching.oneoff import _proxy_cfg  # noqa: E402
from app.fetching.protection import _frame_box, _selector_box, human_click, page_environment, wait_for_pass  # noqa: E402


class LoadCap(Exception):
    pass


def _webgl_pair(os_name: str, preset: dict | None):
    from camoufox.webgl import firefox_gpus

    pairs = firefox_gpus(os_name)
    own = ((preset or {}).get("webgl") or {})
    pair = (own.get("unmaskedVendor"), own.get("unmaskedRenderer"))
    if pair in pairs:
        return pair
    return sorted(pairs)[0] if pairs else None


async def report_identity(browser_name: str, variant, proxy: str | None, geo, ff_version: str | None = None):
    """The identity the service gave this exit and variant (identities.json on the volume);
    a new one under the service's own key otherwise."""
    from app.fetching.identity import identity_for, store

    ip = getattr(geo, "ip", None) if geo is not None else None
    from app.fetching.browsers import host_os_name

    os_key = variant.key() if browser_name == "camoufox" else host_os_name()
    if ip:
        found = store().find_by_exit(browser_name, os_key, ip)
        if found is not None:
            return found
    if browser_name == "camoufox":
        return await identity_for("camoufox", variant.os_name(), proxy, geo, ff_version=ff_version, variant_key=os_key)
    return await identity_for("chromium", os_key, proxy, geo)


async def _context(pw, browser_name: str, variant, *, humanize: bool, webgl: str, proxy: str | None, geo, out: dict):
    """(browser, context) as the service makes them for this variant."""
    from app.fetching.browsers import WINDOW, launch_camoufox
    from app.fetching.identity import camoufox_context, chromium_context

    if browser_name == "camoufox":
        headless = False if os.environ.get("DISPLAY") else "virtual"
        overrides: dict = {} if humanize else {"humanize": False}
        if webgl == "spoofed":
            ident0 = await report_identity("camoufox", variant, proxy, geo)
            pair = _webgl_pair(variant.os_name(), ident0.preset)
            if pair:
                overrides["webgl_config"] = pair
        browser = await launch_camoufox(pw, headless=headless, variant=variant, **overrides)
        identity = await report_identity("camoufox", variant, proxy, geo, browser.version.split(".", 1)[0])
        kw = geo.context_kwargs(camoufox=True) if geo else {}
        try:
            ctx = await camoufox_context(browser, identity, proxy=_proxy_cfg(proxy), variant=variant, **kw)
        except ValueError as exc:  # a host-rendered launch refuses another identity
            out["note"] += f"identity refused ({str(exc)[:80]}); browser's own identity. "
            ctx = await camoufox_context(browser, None, proxy=_proxy_cfg(proxy), variant=variant, **kw)
        return browser, ctx
    browser = await pw.chromium.launch(headless=not os.environ.get("DISPLAY"), proxy=_proxy_cfg(proxy), chromium_sandbox=False,
                                       args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-blink-features=AutomationControlled"])
    identity = await report_identity("chromium", variant, proxy, geo)
    ctx = await chromium_context(browser, identity, viewport={"width": WINDOW[0], "height": WINDOW[1]},
                                 **(geo.context_kwargs(camoufox=False) if geo else {}))
    return browser, ctx


async def attempt(url: str, *, browser_name: str, os_name: str, canvas_noise: str, humanize: bool, step: str, position: float,
                  proxy: str | None, geoip: bool, webgl: str, wait_s: float, click_wait_s: float, shots: Path, label: str,
                  loads: list[int], max_loads: int) -> dict:
    from playwright.async_api import async_playwright

    from app.fetching.browsers import Variant
    from app.fetching.geo import exit_geo
    from app.fetching.identity import env_of, preset_id_of

    if loads[0] >= max_loads:
        raise LoadCap()
    variant = Variant(os_name, canvas_noise)
    out = {"variant": label, "browser": browser_name, "os": os_name if browser_name == "camoufox" else "-",
           "canvas_noise": canvas_noise if browser_name == "camoufox" else "-", "passed": False, "step": "-", "seconds": None,
           "cookies": "", "title": "", "note": "", "view": {}, "exit": {}, "env": {}}
    started = time.monotonic()
    geo = await exit_geo(proxy) if geoip else None
    out["exit"] = {k: v for k, v in (geo.public() if geo else {}).items() if k in ("ip", "timezone", "locale", "country", "source")}
    async with async_playwright() as pw:
        browser = None
        try:
            browser, ctx = await _context(pw, browser_name, variant, humanize=humanize, webgl=webgl, proxy=proxy, geo=geo, out=out)
            page = await ctx.new_page()
            out["env"] = env_of(page)
            out["preset_id"] = preset_id_of(page)
            loads[0] += 1
            resp = await page.goto(url, wait_until="load", timeout=60000)
            verdict = await detect_page(page, resp.status if resp else None)
            if verdict.kind == "none":
                out["note"] += "no challenge shown"
                out["passed"], out["step"] = True, "loaded"
            elif verdict.kind == "hard_block":
                out["note"] += "hard block"
            else:
                passed = await wait_for_pass(page, wait_s)
                if passed:
                    out["step"] = "wait"
                elif step == "click":
                    box = await _frame_box(page) or await _selector_box(page)
                    if box is None:
                        out["note"] += "frame not found"
                    else:
                        x = box["x"] + (position if position >= 1 else box["width"] * position)
                        await human_click(page, x, box["y"] + box["height"] / 2, humanized=humanize and browser_name == "camoufox")
                        out["note"] += f"clicked at +{x - box['x']:.0f}px of {box['width']:.0f}"
                        passed = await wait_for_pass(page, click_wait_s)
                        if passed:
                            out["step"] = "click"
                out["passed"] = passed
            if out["passed"]:
                out["seconds"] = round(time.monotonic() - started, 1)
            try:
                out["title"] = (await page.title())[:60]
            except Exception:
                out["title"] = "?"
            out["cookies"] = ",".join(sorted({c["name"] for c in await ctx.cookies()}))
            out["view"] = await page_environment(page)
            if not out["passed"]:
                shots.mkdir(parents=True, exist_ok=True)
                path = shots / f"{label.replace(' ', '_').replace('/', '-').replace('=', '-')}-{int(time.time())}.png"
                await page.screenshot(path=str(path), full_page=True)
                out["note"] += f" shot={path.name}"
        except LoadCap:
            raise
        except Exception as exc:
            out["note"] += redact(f"{type(exc).__name__}: {str(exc).splitlines()[0][:120] if str(exc) else ''}", [proxy or ""])
        finally:
            if browser is not None:
                await browser.close()
    return out


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_combos(args, proxy: str | None) -> list[dict]:
    proxies = ([proxy] if args.proxy_only else [None, proxy]) if proxy else [None]
    combos: list[dict] = []
    seen: set[str] = set()
    for browser_name, os_name, noise, step, hum, prx, gip, wgl in itertools.product(
            _list(args.browser), _list(args.os), _list(args.canvas_noise), _list(args.variants), _list(args.humanize), proxies,
            _list(args.geoip), _list(args.webgl)):
        if browser_name == "chromium":
            os_name, noise, wgl = "host", "auto", "default"  # Chromium ignores them (it claims the host OS): one row
        positions = [float(p) for p in _list(args.positions)] if step == "click" else [0.0]
        for pos in positions:
            label = (f"{browser_name}" + (f" os={os_name} canvas={noise}" if browser_name == "camoufox" else "")
                     + f" {step}" + (f" pos={pos:g}" if step == "click" else "") + ("" if hum == "on" else " humanize=off")
                     + ("" if gip == "on" else " geoip=off") + ("" if wgl == "default" else f" webgl={wgl}") + (" proxy" if prx else ""))
            if label in seen:
                continue
            seen.add(label)
            combos.append(dict(browser_name=browser_name, os_name=os_name, canvas_noise=noise, step=step, humanize=hum == "on", proxy=prx,
                               position=pos, label=label, geoip=gip == "on", webgl=wgl))
    return combos


def table(rows: list[dict]) -> list[str]:
    lines = ["| variant | browser | os | canvas_noise | canvas_readback | webgl_readback | result | step | seconds | final title | cookies | "
             "exit (ip tz locale) | platform | webgl renderer | preset | note |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        v, e, env = r.get("view") or {}, r.get("exit") or {}, r.get("env") or {}
        exit_s = f"{e.get('ip') or '-'} {e.get('timezone') or '-'} {e.get('locale') or '-'}" if e else "off"
        lines.append(f"| {r['variant']} | {r['browser']} | {r['os']} | {r['canvas_noise']} | {env.get('canvas_readback', '')} | "
                     f"{env.get('webgl_readback', '')} | {'passed' if r['passed'] else 'failed'} | {r['step']} | {r['seconds'] or ''} | "
                     f"{r['title']} | {r['cookies']} | {exit_s} | {v.get('platform', '')} | {v.get('webgl_renderer', '')} | "
                     f"{r.get('preset_id') or ''} | {r['note'].strip()} |")
    return lines


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--i-own-this-site", action="store_true", help="required: the URL belongs to you")
    ap.add_argument("--attempts", type=int, default=1)
    ap.add_argument("--pause", type=float, default=30.0, help="seconds between attempts (min 10)")
    ap.add_argument("--max-loads", type=int, default=20, help="hard cap on page loads for the whole report")
    ap.add_argument("--wait", type=float, default=30.0, help="seconds the challenge may take to clear by itself (stops as soon as it does)")
    ap.add_argument("--click-wait", type=float, default=15.0)
    ap.add_argument("--browser", default="camoufox,chromium")
    ap.add_argument("--os", default="host", help="Camoufox identity OS: host (default), windows, macos, linux; e.g. host,windows")
    ap.add_argument("--canvas-noise", default="auto,off", help="Camoufox canvas readback: auto, off, on")
    ap.add_argument("--variants", default="click", help="wait, click (click = wait, then one click)")
    ap.add_argument("--humanize", default="on")
    ap.add_argument("--positions", default="28", help="click x: pixels from the frame's left edge (>=1) or a share of its width (<1)")
    ap.add_argument("--geoip", default="on")
    ap.add_argument("--webgl", default="default")
    ap.add_argument("--proxy-only", action="store_true", help="with WSS_REPORT_PROXY_URL: skip the attempts without the proxy")
    ap.add_argument("--shots", default="protection-shots")
    args = ap.parse_args()
    if not args.i_own_this_site:
        print("refusing: pass --i-own-this-site (only for a site you own or may test)", file=sys.stderr)
        return 2
    from app.fetching.browsers import BROWSER_OS, CANVAS_NOISE

    bad = ([b for b in _list(args.browser) if b not in ("camoufox", "chromium")] + [o for o in _list(args.os) if o not in BROWSER_OS]
           + [c for c in _list(args.canvas_noise) if c not in CANVAS_NOISE])
    if bad:
        print(f"unknown value(s): {', '.join(bad)}", file=sys.stderr)
        return 2
    pause = max(10.0, args.pause)
    combos = build_combos(args, os.environ.get("WSS_REPORT_PROXY_URL") or None)
    print(f"{len(combos)} variant(s) x {args.attempts} attempt(s), cap {args.max_loads} page loads", file=sys.stderr)
    rows: list[dict] = []
    loads = [0]
    first = True
    try:
        for combo in combos:
            for _ in range(args.attempts):
                if not first:
                    await asyncio.sleep(pause)
                first = False
                rows.append(await attempt(args.url, wait_s=args.wait, click_wait_s=args.click_wait, shots=Path(args.shots),
                                          loads=loads, max_loads=args.max_loads, **combo))
                r = rows[-1]
                print(f"  {r['variant']}: {'passed' if r['passed'] else 'failed'} canvas={(r.get('env') or {}).get('canvas_readback')} "
                      f"{r['note'].strip()}", file=sys.stderr, flush=True)
    except LoadCap:
        print(f"stopped at the cap of {args.max_loads} page loads", file=sys.stderr)
    print("\n".join(table(rows)))
    by: dict[str, list[bool]] = {}
    for r in rows:
        by.setdefault(r["variant"], []).append(r["passed"])
    print()
    for variant, results in by.items():
        print(f"{variant}: {sum(results)}/{len(results)} passed")
    print(f"page loads: {loads[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
