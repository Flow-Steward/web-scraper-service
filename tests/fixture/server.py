"""Local fixture world for the integration tests (never a real site).

One process, several ports (all bound to 127.0.0.1; the test container maps the
host names ``shop.test``, ``www.shop.test``, ``plain.test`` and ``guard.test`` to it):

* 8601 shop.test   — a small lamp shop: robots.txt, sitemap index + gzip sitemap, two page
  templates (``/produkt/…`` with JSON-LD, ``/ersatzteile/…`` without), category pages,
  JavaScript-rendered products, slow pages, 429 pages, 404 pages, a layout switch.
* 8602 plain.test  — the same shop without any sitemap (crawl only).
* 8603 guard.test  — a fake interstitial challenge with a checkbox frame that sets a
  clearance cookie bound to the browser's User-Agent; auto-pass, click and solver modes;
  clearance expiring after N pages; a hard block for proxy tier 1.
* 8604 webhook receiver — verifies X-Scraper-Signature, records deliveries, can be off.
* 8605 fake solver API (CapSolver protocol).
* 8606 / 8607 forward HTTP proxies "tier 1" / "tier 2" (Basic auth), marking requests.
* /__admin/* on every app: state, hit counters, switches.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import os
import re
import sys
import time
from collections import Counter

import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cloudflare_pages import block_1020_page, decorate_normal_page  # noqa: E402
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

HOST = os.environ.get("FIXTURE_HOST", "127.0.0.1")
SHOP_PORT, PLAIN_PORT, GUARD_PORT, HOOK_PORT, SOLVER_PORT, PROXY1_PORT, PROXY2_PORT = 8601, 8602, 8603, 8604, 8605, 8606, 8607
PROXY_USERS = {PROXY1_PORT: ("tier1user", "tier1pass"), PROXY2_PORT: ("tier2user", "tier2pass")}

STATE = {
    "layout": 1,  # 2 = changed layout (forces a repair)
    "layout_switch_after": None,  # switch to layout 2 after this many product hits
    "hits": Counter(),
    "product_hits": 0,
    "flaky": Counter(),
    "hook_on": True,
    "hook_secret": "fs_whsec_fixture_0000000000000001",
    "deliveries": [],
    "guard_mode": "click",  # click | auto | solver | none
    "guard_ttl": 1000,  # page loads per clearance
    "clearances": {},  # cookie -> remaining loads
    "solver_tasks": {},
    "solver_calls": Counter(),
    "guard_discovery": False,
    "clicks": [],  # clicks inside the shadow-root widget: x in the frame, busy = during a verification  # robots.txt and the sitemap behind the challenge too
    "err5": Counter(),
    "echo_ip_tier2": None,  # the IP echo's answer through the tier-2 proxy (a second exit IP)
    "guard_clear_limit": None,  # how many more clearances the guard grants (None = no limit)
    "guard_loads": [],  # (path, referer) of every guarded page load
    "guard_storage": [],  # localStorage visit counters reported by guarded product pages (one context = rising numbers)
    "week": 1,  # week.test: 1 or 2
    "wk_fail": [],  # week.test product numbers that answer 500
    "lk_wrong": False,  # links.test: every product names its category as canonical (guard)
    # /jk/ (junk) catalogue: path -> kinds served on its first hits (maintenance | truncated | empty | partial | 500),
    # then the real page; "jk_fail_every": every n-th request of /jk/ answers 500
    "jk_junk": {},
    "jk_seen": Counter(),
    "jk_fail_every": 0,
    "jk_calls": 0,
}

CONTENT_PAGES = ["about-us", "our-portfolio", "our-producers", "blog-posts", "careers", "contact", "press", "faq", "terms",
                 "privacy", "events", "team", "history", "sustainability"]

PRODUCTS = [f"leuchte-{i:02d}" for i in range(1, 25)]
PARTS = [f"teil-{i:02d}" for i in range(1, 9)]
JS_PRODUCTS = [f"js-leuchte-{i:02d}" for i in range(1, 4)]


def _count(request: Request) -> None:
    STATE["hits"][f"{request.url.port}{request.url.path}"] += 1


def page(title: str, body: str, head: str = "") -> str:
    return f"<!doctype html><html lang='de'><head><meta charset='utf-8'><title>{title}</title>{head}</head><body>{body}</body></html>"


def product_html(slug: str, base: str, layout: int) -> str:
    n = int(slug.split("-")[-1])
    name = f"Pendelleuchte {slug.replace('-', ' ').title()}"
    price = f"{100 + n},90"
    jsonld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "sku": f"AUR-{n:03d}",
        "image": [f"{base}/media/{slug}-1.jpg", f"/media/{slug}-2.jpg"],
        "offers": {"@type": "Offer", "price": f"{100 + n}.90", "priceCurrency": "EUR", "availability": "https://schema.org/InStock"},
    }
    description = (
        "<p>Mundgeblasener <b>Opalglasschirm</b>.</p><ul><li>E27</li><li>max. 60 W</li></ul>"
        "<table><tr><th>Fassung</th><td>E27</td></tr></table>"
        "<script>alert('x')</script><style>.x{color:red}</style>"
        "<p onclick='track()' data-gtm-id='9'>Mehr <a href='/kategorie/pendel?utm_source=x&amp;a=1'>Pendel</a></p>"
        "<img src='/media/detail.jpg' alt='Detail'>"
    )
    if layout == 1:
        body = (
            f"<div class='product-detail'><h1 class='product-title'>{name}</h1>"
            f"<span class='sku'>Art.-Nr. AUR-{n:03d}</span><span class='price-current'>{price} €</span>"
            f"<div class='product-description'>{description}</div>"
            f"<div class='gallery'><img data-src='/media/{slug}-1.jpg'></div>"
            f"<a href='/ersatzteile/{PARTS[n % len(PARTS)]}'>Ersatzteil</a> <a href='/kategorie/pendel'>Zurück</a></div>"
        )
        head = f"<script type='application/ld+json'>{json.dumps(jsonld)}</script><meta property='og:title' content='{name}'>"
    else:
        body = (
            f"<main class='product-detail'><h1 class='pdp-name'>{name}</h1>"
            f"<div class='pdp-price'>{price} €</div><section class='pdp-text'>{description}</section></main>"
        )
        head = ""
    return page(name, body, head)


def part_html(slug: str) -> str:
    n = int(slug.split("-")[-1])
    body = (
        f"<div class='part-sheet'><h2 class='part-name'>Ersatzteil {slug}</h2>"
        f"<div class='part-price'>EUR {20 + n}.50</div><div class='part-no'>T-{n:04d}</div>"
        f"<a href='/produkt/{PRODUCTS[n]}'>Passend für</a></div>"
    )
    return page(f"Ersatzteil {slug}", body)


def category_html(name: str) -> str:
    links = "".join(f"<li><a href='/produkt/{p}'>{p}</a></li>" for p in PRODUCTS[:12])
    links += "".join(f"<li><a href='/ersatzteile/{p}'>{p}</a></li>" for p in PARTS[:4])
    links += "<li><a href='/private/secret'>intern</a></li><li><a href='https://elsewhere.test/x'>extern</a></li>"
    return page(f"Kategorie {name}", f"<h1>Kategorie {name}</h1><ul class='listing'>{links}</ul><p>{'Leuchten ' * 80}</p>")


def listings_html(name: str) -> str:
    """A site of listings only: every page links to the same twelve listings."""
    links = "".join(f"<li><a href='/kategorie/k{i:02d}'>Liste {i}</a></li>" for i in range(1, 13))
    return page(f"Liste {name}", f"<h1>Liste {name}</h1><ul>{links}</ul><p>{'Übersicht ' * 60}</p>")


def js_product_html(slug: str) -> str:
    n = int(slug.split("-")[-1])
    script = f"""
    <script>
    window.addEventListener('load', function () {{
      setTimeout(function () {{
        var root = document.getElementById('app');
        root.innerHTML = "<div class='product-detail'><h1 class='product-title'>JS Leuchte {n}</h1>" +
          "<span class='price-current'>{300 + n},00 €</span><div class='product-description'><p>Gerendert.</p></div></div>";
      }}, 300);
    }});
    </script>"""
    return page(f"JS {slug}", f"<div id='app'>Lädt…</div><noscript>JavaScript nötig</noscript>{script}")


def sitemap_xml(base: str, paths: list[str]) -> bytes:
    urls = "".join(f"<url><loc>{base}{p}</loc><lastmod>2026-11-0{1 + i % 9}</lastmod></url>" for i, p in enumerate(paths))
    return f"<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{urls}</urlset>".encode()


def _variant_pages():
    """tests/fixture/variant_pages.py — importable both as a package module and from this script's directory."""
    try:
        from tests.fixture import variant_pages as vp
    except ImportError:
        import variant_pages as vp  # type: ignore[no-redef]
    return vp


def make_shop(with_sitemap: bool) -> Starlette:
    async def robots(request: Request):
        _count(request)
        base = f"http://{request.url.netloc}"
        lines = ["User-agent: *", "Disallow: /private/"]
        if with_sitemap or request.url.hostname == "scope.test":
            lines.append(f"Sitemap: {base}/sitemap_index.xml")
        return PlainTextResponse("\n".join(lines) + "\n")

    async def sitemap_index(request: Request):
        _count(request)
        if not with_sitemap and request.url.hostname != "scope.test":
            return PlainTextResponse("no", status_code=404)
        base = f"http://{request.url.netloc}"
        xml = (
            "<?xml version='1.0' encoding='UTF-8'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            f"<sitemap><loc>{base}/sitemap-products.xml.gz</loc></sitemap><sitemap><loc>{base}/sitemap-parts.xml</loc></sitemap>"
            "</sitemapindex>"
        )
        return Response(xml, media_type="application/xml")

    async def sitemap_products(request: Request):
        _count(request)
        base = f"http://{request.url.netloc}"
        paths = [f"/produkt/{p}?utm_source=sitemap" if i == 0 else f"/produkt/{p}" for i, p in enumerate(PRODUCTS)]
        paths += [f"/js/{p}" for p in JS_PRODUCTS] + ["/gone/alt-leuchte", "/private/hidden"]
        return Response(gzip.compress(sitemap_xml(base, paths)), media_type="application/gzip")

    async def sitemap_parts(request: Request):
        _count(request)
        base = f"http://{request.url.netloc}"
        return Response(sitemap_xml(base, [f"/ersatzteile/{p}" for p in PARTS]), media_type="application/xml")

    async def sitemap_plain(request: Request):
        _count(request)
        if request.url.hostname == "oldmap.test":
            # a sitemap whose URLs mostly answer 404
            paths = [f"/gone/old-{i}" for i in range(8)] + [f"/produkt/{p}" for p in PRODUCTS[:2]]
            return Response(sitemap_xml(f"http://{request.url.netloc}", paths), media_type="application/xml")
        if request.url.hostname == "bigmap.test":
            # a site-wide sitemap whose first entries are not products
            paths = [f"/seite/{c}-{i}" for i in range(5) for c in CONTENT_PAGES] + [f"/produkt/{p}" for p in PRODUCTS]
            return Response(sitemap_xml(f"http://{request.url.netloc}", paths), media_type="application/xml")
        if not with_sitemap:
            return PlainTextResponse("not found", status_code=404)
        return PlainTextResponse("not found", status_code=404)

    async def home(request: Request):
        _count(request)
        if request.url.hostname == "listings.test":
            return HTMLResponse(listings_html("start"))
        if request.url.hostname == "mixed.test":
            # a shop without structured data whose largest group of leaf pages is content
            links = "".join(f"<li><a href='/seite/{c}'>{c}</a></li>" for c in CONTENT_PAGES)
            return HTMLResponse(page("Mixed shop", f"<h1>Mixed</h1><ul>{links}</ul><a href='/kategorie/pendel'>Pendelleuchten</a>" + "<p>Willkommen. </p>" * 50))
        return HTMLResponse(page("Lumen Fixture", "<h1>Lumen</h1><a href='/kategorie/pendel'>Pendelleuchten</a> <a href='/kategorie/teile'>Teile</a>" + "<p>Willkommen. </p>" * 50))

    async def category(request: Request):
        _count(request)
        if request.url.hostname == "listings.test":
            return HTMLResponse(listings_html(request.path_params["name"]))
        return HTMLResponse(category_html(request.path_params["name"]))

    async def product(request: Request):
        _count(request)
        slug = request.path_params["slug"]
        if slug not in PRODUCTS:
            return HTMLResponse(page("404", "<h1>Nicht gefunden</h1>"), status_code=404)
        STATE["product_hits"] += 1
        layout = STATE["layout"]
        after = STATE["layout_switch_after"]
        if after is not None and STATE["product_hits"] > after:
            layout = 2
        if request.url.hostname in ("nold.test", "mixed.test"):
            layout = 2  # a shop without any schema.org data
        return HTMLResponse(product_html(slug, f"http://{request.url.netloc}", layout))

    async def part(request: Request):
        _count(request)
        slug = request.path_params["slug"]
        if slug not in PARTS:
            return HTMLResponse(page("404", "<h1>Nicht gefunden</h1>"), status_code=404)
        return HTMLResponse(part_html(slug))

    async def js_product(request: Request):
        _count(request)
        return HTMLResponse(js_product_html(request.path_params["slug"]))

    async def slow(request: Request):
        _count(request)
        await asyncio.sleep(float(request.query_params.get("s", "2")))
        return HTMLResponse(product_html(PRODUCTS[0], f"http://{request.url.netloc}", 1))

    async def flaky(request: Request):
        _count(request)
        key = request.url.path
        STATE["flaky"][key] += 1
        if STATE["flaky"][key] <= 2:
            return PlainTextResponse("slow down", status_code=429, headers={"Retry-After": "1"})
        return HTMLResponse(product_html(PRODUCTS[1], f"http://{request.url.netloc}", 1))

    async def gone(request: Request):
        _count(request)
        return HTMLResponse(page("404", "<h1>Nicht gefunden</h1>"), status_code=404)

    async def private(request: Request):
        _count(request)
        return HTMLResponse(page("intern", "<h1>intern</h1>"))

    async def tiered(request: Request):
        _count(request)
        tier = request.headers.get("x-fixture-tier")
        if tier == "1":
            return HTMLResponse(page("Access denied", "<h1>Access denied</h1><p>Error 1020</p>"), status_code=403)
        return HTMLResponse(product_html(PRODUCTS[int(request.path_params["n"]) % len(PRODUCTS)], f"http://{request.url.netloc}", 1))

    async def cf_product(request: Request):
        """A normal product page carrying Cloudflare's JS-detection script, a contact-form
        Turnstile, a reCAPTCHA form and "Access denied" in its text."""
        _count(request)
        slug = request.path_params["slug"]
        if slug not in PRODUCTS:
            return HTMLResponse(page("404", "<h1>Nicht gefunden</h1>"), status_code=404)
        return HTMLResponse(decorate_normal_page(product_html(slug, f"http://{request.url.netloc}", 1)))

    async def blocked(request: Request):
        _count(request)
        return HTMLResponse(block_1020_page(request.url.hostname or "shop.test"), status_code=403)

    async def err5(request: Request):
        _count(request)
        key = request.url.path
        STATE["err5"][key] += 1
        if STATE["err5"][key] <= 2:
            return HTMLResponse(page("503", "<h1>Service Unavailable</h1>"), status_code=503)
        return HTMLResponse(product_html(PRODUCTS[2], f"http://{request.url.netloc}", 1))

    async def jsredirect(request: Request):
        """A public-looking page whose JavaScript sends the browser to an internal address (review M7a)."""
        _count(request)
        to = request.query_params.get("to", "http://internal.test:8601/private/secret")
        body = (f"<p>Weiterleitung…</p><script>location.href = {json.dumps(to)};</script>"
                f"<img src='{to.rsplit('/', 1)[0]}/pixel.png'>")
        return HTMLResponse(page("redirect", body))

    async def sitemap_evil(request: Request):
        """A sitemap index whose nested sitemap points at an internal host."""
        _count(request)
        base = f"http://{request.url.netloc}"
        xml = ("<?xml version='1.0' encoding='UTF-8'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
               f"<sitemap><loc>{base}/sitemap-parts.xml</loc></sitemap><sitemap><loc>http://internal.test:8601/sitemap-internal.xml</loc></sitemap>"
               "</sitemapindex>")
        return Response(xml, media_type="application/xml")

    async def sitemap_internal(request: Request):
        _count(request)
        return Response(sitemap_xml(f"http://{request.url.netloc}", ["/private/secret"]), media_type="application/xml")

    async def media(request: Request):
        _count(request)
        return Response(b"\x89PNG\r\n", media_type="image/png")

    async def down(request: Request):
        """A part of the site that is down (always 503)."""
        _count(request)
        return HTMLResponse(page("503", "<h1>Service Unavailable</h1>"), status_code=503)

    async def scope_page(request: Request):
        """On scope.test: the category /lt/lamps-c100 with a subcategory and a page 2
        under its path, products outside it (/lamp-*-p<n>), and a menu to other categories."""
        if request.url.hostname != "scope.test":
            return PlainTextResponse("not found", status_code=404)
        _count(request)
        path = request.url.path
        menu = "<nav><a href='/lt/tools-c200'>Tools</a><a href='/lt/garden-c300'>Garden</a><a href='/about-us'>About</a></nav>"
        listings = {
            "/lt/lamps-c100": (["/lamp-a-p1", "/lamp-b-p2"], "<a href='/lt/lamps-c100/pendant-c101'>Pendant</a><a href='/lt/lamps-c100?page=2'>2</a>"),
            "/lt/lamps-c100/pendant-c101": (["/lamp-c-p3"], ""),
            "/lt/tools-c200": (["/tool-x-p9"], ""),
            "/lt/garden-c300": (["/rake-y-p8"], ""),
        }
        if path in listings:
            prods, extra = listings[path]
            if path == "/lt/lamps-c100" and request.query_params.get("page") == "2":
                prods, extra = ["/lamp-d-p4"], ""
            cards = "".join(f"<div class='card'><a href='{u}'><img src='/media/x.jpg'>{u}</a></div>" for u in prods)
            return HTMLResponse(page(path, f"{menu}<h1>{path}</h1>{extra}{cards}"))
        if re.search(r"-p\d+$", path):
            name = path.strip("/")
            ld = {"@context": "https://schema.org", "@type": "Product", "name": name,
                  "offers": {"@type": "Offer", "price": "10.00", "priceCurrency": "EUR"}}
            return HTMLResponse(page(name, f"{menu}<h1>{name}</h1><a href='/lt/tools-c200'>Related tools</a>",
                                     f"<script type='application/ld+json'>{json.dumps(ld)}</script>"))
        return HTMLResponse(page("About", "<h1>About</h1>"))

    async def scope_sitemap(request: Request):
        """Declared source scope with irrelevant raw locations before products."""
        if request.url.hostname != "scope.test":
            return PlainTextResponse("not found", status_code=404)
        _count(request)
        base = f"http://{request.url.netloc}"
        name = request.path_params["name"]
        if name == "index":
            sources = ["products", "tools"]
            xml = "<sitemapindex>" + "".join(f"<sitemap><loc>{base}/scope-map-{part}.xml</loc></sitemap>" for part in sources) + "</sitemapindex>"
            return Response(xml, media_type="application/xml")
        paths = [f"/irrelevant/{i}" for i in range(6)] + ["/lamp-a-p1", "/lamp-b-p2"] if name == "products" else ["/tool-x-p9"]
        return Response(sitemap_xml(base, paths), media_type="application/xml")

    async def week_page(request: Request):
        """Re-collect and CSV on week.test: a category /wk/ and products /wk/p<n> whose set, prices and
        stock change between "week" 1 and 2; products in ``wk_fail`` answer 500."""
        if request.url.hostname != "week.test":
            return PlainTextResponse("not found", status_code=404)
        _count(request)
        week = STATE["week"]
        # week 1: p1..p5. week 2: p2 deleted (404), p3 repriced, p4 out of stock, p6 new
        listed = [1, 2, 3, 4, 5] if week == 1 else [1, 2, 3, 4, 5, 6]
        rest = request.path_params["rest"]
        if rest in ("", "index"):
            cards = "".join(f"<div class='card'><a href='/wk/p{n}'>Lamp {n}</a></div>" for n in listed)
            return HTMLResponse(page("Week shop", f"<h1>Lamps</h1>{cards}"))
        m = re.fullmatch(r"p(\d+)", rest)
        if not m:
            return HTMLResponse(page("404", "<h1>Not found</h1>"), status_code=404)
        n = int(m.group(1))
        if n in STATE["wk_fail"]:
            return HTMLResponse(page("500", "<h1>Error</h1>"), status_code=500)
        exists = n in (1, 2, 3, 4, 5) if week == 1 else n in (1, 3, 4, 5, 6)
        if not exists:
            return HTMLResponse(page("404", "<h1>Not found</h1>"), status_code=404)
        price = 99.0 if (week == 2 and n == 3) else 10.0 + n
        stock = "OutOfStock" if (week == 2 and n == 4) else "InStock"
        ld = {"@context": "https://schema.org", "@type": "Product", "name": f"Lamp {n}", "sku": f"WK-{n:03d}",
              "image": [f"/media/wk{n}-a.jpg", f"/media/wk{n}-b.jpg"],
              "offers": {"@type": "Offer", "price": f"{price:.2f}", "priceCurrency": "EUR", "availability": f"https://schema.org/{stock}"}}
        return HTMLResponse(page(f"Lamp {n}", f"<div class='product'><h1>Lamp {n}</h1><p>=SUM(A1)</p></div>",
                                 f"<script type='application/ld+json'>{json.dumps(ld)}</script>"))

    async def links_page(request: Request):
        """Link rules, canonicals and the stall watchdog on links.test: a home with every kind of link to skip, a category whose
        products are linked under alias addresses (canonical by <link> and by the HTTP Link
        header), a page 2 naming page 1 as canonical, an endless calendar, a page that hangs."""
        if request.url.hostname != "links.test":
            return PlainTextResponse("not found", status_code=404)
        _count(request)
        rest = request.path_params["rest"]
        base = f"http://{request.url.netloc}"

        def product(n: int, canonical: str | None, header: bool = False):
            ld = {"@context": "https://schema.org", "@type": "Product", "name": f"Item {n}", "sku": f"LK-{n}",
                  "offers": {"@type": "Offer", "price": f"{n}.00", "priceCurrency": "EUR"}}
            head = f"<script type='application/ld+json'>{json.dumps(ld)}</script>"
            if STATE["lk_wrong"]:
                canonical = f"{base}/lk/cat/a/"
            if canonical and not header:
                head += f"<link rel='canonical' href='{canonical}'>"
            body = (f"<div class='product'><h1>Item {n}</h1><a href='/lk/cat/a/'>Back</a>"
                    f"<a href='/lk/wishlist/index/add/product/{n}/uenc/aHR0cDovL2xpbmtzLnRlc3Q6ODYwMi9say9wMS5odG1s/form_key/{int(time.time() * 1000)}/'>Wish</a></div>")
            resp = HTMLResponse(page(f"Item {n}", body, head))
            if canonical and (header or STATE["lk_wrong"]):
                resp.headers["Link"] = f'<{canonical}>; rel="canonical"'
            return resp

        if rest in ("", "index"):
            sid = hashlib.md5(str(time.time()).encode()).hexdigest()
            links = (
                "<a href='/lk/cat/a/'>Lamps</a><a href='/lk/authentic-lamps/'>Authentic</a><a href='/lk/p1.html'>One</a>"
                f"<a href='/lk/p1.html?SID={sid}'>One again</a><a href='/lk/item?id=2'>Two</a>"
                "<a href='mailto:shop@links.test'>Mail</a><a href='tel:+3711234'>Call</a><a href='whatsapp://send?text=hi'>WA</a>"
                "<a href='viber://chat?number=1'>Viber</a><a href='https://wa.me/3711234'>wa.me</a>"
                "<a href='https://www.facebook.com/sharer/sharer.php?u=x'>Share</a>"
                "<a href='/lk/customer/account/login/'>Login</a><a href='/lk/checkout/cart/'>Cart</a>"
                "<a href='/lk/manual.pdf'>Manual</a><a href='/lk/cat/a/?color=red'>Red</a>"
                "<a href='/lk/nofollow-only/' rel='nofollow'>Hidden</a><a href='/lk/events?month=2026-01'>Events</a>"
            )
            return HTMLResponse(page("Links shop", f"<h1>Links</h1>{links}"))
        if rest == "cat/a/":
            page_no = request.query_params.get("page", "1")
            if page_no == "2":
                cards = "<a href='/lk/cat/a/p5.html'>Item 5</a>"
                head = f"<link rel='canonical' href='{base}/lk/cat/a/'>"
            else:
                cards = "".join(f"<a href='/lk/cat/a/p{n}.html'>Item {n}</a>" for n in (1, 2, 3, 4)) + "<a href='/lk/cat/a/?page=2'>2</a>"
                head = ""
            return HTMLResponse(page("Lamps", f"<h1>Lamps</h1>{cards}", head))
        if rest == "authentic-lamps/":
            return HTMLResponse(page("Authentic", "<h1>Authentic</h1><a href='/lk/p9.html'>Item 9</a>"))
        if rest == "events":
            month = request.query_params.get("month", "2026-01")
            y, m = (int(x) for x in month.split("-"))
            nxt = f"{y + (m // 12)}-{m % 12 + 1:02d}"
            return HTMLResponse(page("Events", f"<h1>Events {month}</h1><a href='/lk/events?month={nxt}'>Next month</a>"))
        if rest.startswith("hang/"):
            await asyncio.sleep(120)
            return HTMLResponse(page("Late", "<h1>Late</h1>"))
        m = re.fullmatch(r"(cat/a/)?p(\d+)\.html", rest)
        if m:
            n = int(m.group(2))
            return product(n, f"{base}/lk/p{n}.html")
        if rest == "item" and request.query_params.get("id"):
            n = int(request.query_params["id"])
            return product(n, f"{base}/lk/p{n}.html", header=True)
        return HTMLResponse(page("404", "<h1>Not found</h1>"), status_code=404)

    async def variant_page(request: Request):
        """Fixtures (tests/fixture/variant_pages.py) on variants.test."""
        vp = _variant_pages()

        _count(request)
        kind, slug = request.path_params["kind"], request.path_params["slug"]
        maker = {
            "woo": vp.woo_classic,
            "woo-block": vp.woo_block,
            "products": vp.shopify_page,
            "release": vp.release_page,
            "group": vp.group_page,
            "micro": vp.micro_page,
            "woo-heavy": vp.woo_heavy,
            "on-request": vp.woo_on_request,
            "range": vp.range_page,
            "woo-sale-var": vp.woo_sale_variable,
            "editions": vp.keyed_editions,
            "listing": vp.dynamic_listing,
            "shell-listing": vp.shell_listing,
            "productive-shell": vp.productive_shell_listing,
            "paced-listing": vp.paced_listing,
            "nested-editions": vp.nested_editions,
            "plain-item": vp.plain_item,
        }.get(kind)
        if maker is None:
            return PlainTextResponse("not found", status_code=404)
        return HTMLResponse(maker(slug))

    async def junk_catalogue(request: Request):
        """/jk/ lists 6 products; /__admin/state "jk_junk" makes chosen paths answer junk with status 200 first."""
        _count(request)
        path = request.url.path
        STATE["jk_calls"] += 1
        every = int(STATE.get("jk_fail_every") or 0)
        if every and STATE["jk_calls"] % every == 0:
            return PlainTextResponse("Injected error", status_code=500)
        n = STATE["jk_seen"][path]
        STATE["jk_seen"][path] += 1
        plan = list((STATE.get("jk_junk") or {}).get(path) or [])
        kind = plan[n] if n < len(plan) else None
        if kind == "500":
            return PlainTextResponse("Injected error", status_code=500)
        if kind == "empty":
            return HTMLResponse("")
        if kind == "maintenance":
            return HTMLResponse("<!doctype html><html lang='de'><head><title>Wartungsarbeiten</title></head><body><h1>Wir sind gleich zurück</h1>"
                                "<p>Bitte versuchen Sie es später erneut.</p></body></html>")
        slug = request.path_params.get("slug")
        filler = "".join(f"<p>Detail {i}: mundgeblasenes Glas, Messing, E27, handgefertigt in Litauen.</p>" for i in range(40))
        if not slug:
            links = "".join(f'<li><a href="/jk/p{i}">Leuchte {i}</a></li>' for i in range(1, 7))
            body = f"<!doctype html><html lang='de'><head><title>Leuchten</title></head><body><main><h1>Leuchten</h1><ul>{links}</ul>{filler}</main></body></html>"
        else:
            price = "" if kind == "partial" else f"<span class='price'>{100 + int(slug[1:])},00 €</span>"
            body = (f"<!doctype html><html lang='de'><head><title>Leuchte {slug}</title></head><body><main class='product'>"
                    f"<h1 class='name'>Leuchte {slug}</h1>{price}{filler}</main></body></html>")
        if kind == "truncated":
            body = body[: len(body) * 2 // 5]
        return HTMLResponse(body)

    async def gap_catalogue(request: Request):
        """/gc/ (a catalogue that turns variable later) on variants.test."""
        vp = _variant_pages()

        _count(request)
        slug = request.path_params.get("slug")
        if not slug:
            return HTMLResponse(vp.gap_listing())
        body = vp.gap_product(slug)
        return HTMLResponse(body) if body else PlainTextResponse("not found", status_code=404)

    async def price_case_page(request: Request):
        """/pc/<case> (prices split into tags, separators, machine prices) on variants.test."""
        vp = _variant_pages()

        _count(request)
        body = vp.price_case(request.path_params["case"])
        return HTMLResponse(body) if body else PlainTextResponse("not found", status_code=404)

    async def shopify_js(request: Request):
        vp = _variant_pages()

        _count(request)
        return JSONResponse(vp.SHOPIFY_PRODUCT)

    async def store_api(request: Request):
        vp = _variant_pages()

        _count(request)
        pid = request.path_params.get("pid")
        if pid is not None:
            return JSONResponse(vp.STORE_PRODUCT) if pid == "7001" else JSONResponse({"code": "woocommerce_rest_product_invalid_id"}, status_code=404)
        include = {int(x) for x in request.query_params.get("include", "").split(",") if x.isdigit()}
        return JSONResponse([v for v in vp.STORE_VARIATIONS if v["id"] in include] if request.query_params.get("type") == "variation" else [])

    async def content(request: Request):
        _count(request)
        name = request.path_params["name"]
        text = "<p>Unsere Geschichte, unsere Werte und unsere Menschen. </p>" * 30
        return HTMLResponse(page(name.replace("-", " ").title(), f"<h1>{name.replace('-', ' ').title()}</h1>{text}<img src='/media/team.jpg'>"))

    return Starlette(routes=[
        Route("/", home), Route("/robots.txt", robots), Route("/sitemap_index.xml", sitemap_index),
        Route("/products/{handle}.js", shopify_js), Route("/wp-json/wc/store/v1/products/{pid}", store_api),
        Route("/wp-json/wc/store/v1/products", store_api), Route("/v/{kind}/{slug}", variant_page), Route("/gc/", gap_catalogue), Route("/gc/{slug}", gap_catalogue), Route("/pc/{case}", price_case_page), Route("/jk/", junk_catalogue), Route("/jk/{slug}", junk_catalogue),
        Route("/lt/{rest:path}", scope_page), Route("/wk/{rest:path}", week_page), Route("/lk/{rest:path}", links_page), Route("/down/{slug}", down),
        Route("/scope-map-{name}.xml", scope_sitemap),
        Route("/sitemap-products.xml.gz", sitemap_products), Route("/sitemap-parts.xml", sitemap_parts),
        Route("/sitemap.xml", sitemap_plain), Route("/kategorie/{name}", category), Route("/produkt/{slug}", product),
        Route("/ersatzteile/{slug}", part), Route("/js/{slug}", js_product), Route("/slow/{slug}", slow),
        Route("/flaky/{slug}", flaky), Route("/gone/{slug}", gone), Route("/private/{slug}", private),
        Route("/tiered/{n}", tiered), Route("/media/{name}", media), Route("/seite/{name}", content),
        Route("/cf/{slug}", cf_product), Route("/blocked/{n}", blocked), Route("/err5/{slug}", err5), Route("/jsredirect", jsredirect),
        Route("/sitemap_evil.xml", sitemap_evil), Route("/sitemap-internal.xml", sitemap_internal),
        *admin_routes(),
        Route("/{slug}", scope_page),
    ])


# ---------------------------------------------------------------------------- guard (challenge)
def _clearance_value(ua: str) -> str:
    return hashlib.sha256(("clear:" + ua).encode()).hexdigest()[:24]


CHALLENGE_HTML = """<!doctype html><html><head><title>Just a moment...</title></head><body>
<div id="cf-wrapper"><h1>Checking your browser before accessing guard.test</h1>
<p>Verify you are human by completing the action below.</p>
{widget}
<form id="challenge-form"><input type="hidden" name="cf-turnstile-response" value=""></form>
<script>window.__cf_chl_opt = {{cType: 'managed'}};</script>
{auto}
</div></body></html>"""

FRAME_HTML = """<!doctype html><html><head><style>
body{margin:0;font:14px sans-serif} label{display:block;width:120px;height:65px;line-height:65px;cursor:pointer;background:#eee}
</style></head><body><label id="cb"><input type="checkbox" id="box"> I am human</label>
<script>
document.getElementById('cb').addEventListener('mousedown', function () {
  fetch('/__clear', {method: 'POST', credentials: 'include'}).then(function () { setTimeout(function(){ top.location.reload(); }, 200); });
});
</script></body></html>"""


SHADOW_WIDGET = """<div id="cf-host" style="width:300px;height:65px"></div>
<script>(function () {
  var host = document.getElementById('cf-host');
  var root = host.attachShadow({mode: 'closed'});
  root.innerHTML = "<iframe src='/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2/av0/rcv0/0/fx/0x4AAAAAAAFixtureShadow/light/normal'" +
    " title='Widget containing a Cloudflare security challenge' width='300' height='65' style='border:0'></iframe>";
})();</script>"""

# The checkbox sits 16-40 px from the frame's left edge. A click there starts a verification
# that takes 3.5 s; a second click while it runs restarts it (like Turnstile); a click
# elsewhere in the frame does nothing.
SHADOW_FRAME_HTML = """<!doctype html><html><head><style>
body{margin:0;font:14px sans-serif;width:300px;height:65px;background:#fafafa}
#cb{position:absolute;left:16px;top:20px;width:24px;height:24px;border:2px solid #555}
#msg{position:absolute;left:52px;top:22px}
</style></head><body><div id="cb"></div><div id="msg">Verify you are human</div>
<script>
var timer = null;
document.addEventListener('mousedown', function (e) {
  fetch('/__clicklog?x=' + Math.round(e.clientX) + '&busy=' + (timer ? 1 : 0), {method: 'POST'});
  if (timer) { clearTimeout(timer); timer = null; document.getElementById('msg').textContent = 'Verify you are human'; return; }
  if (e.clientX < 8 || e.clientX > 48) { return; }
  document.getElementById('msg').textContent = 'Verifying...';
  timer = setTimeout(function () {
    fetch('/__clear', {method: 'POST', credentials: 'include'}).then(function () { top.location.reload(); });
  }, 3500);
});
</script></body></html>"""


def make_guard() -> Starlette:
    def challenge_or_none(request: Request):
        ua = request.headers.get("user-agent", "")
        cookie = request.cookies.get("fx_clearance")
        mode = STATE["guard_mode"]
        if mode != "none":
            ok = cookie is not None and cookie == _clearance_value(ua) and STATE["clearances"].get(cookie, 0) > 0
            if not ok:
                if mode == "noframe":
                    widget = "<p>Please wait while we verify your browser.</p>"
                elif mode == "shadow":
                    widget = SHADOW_WIDGET
                elif mode == "solver":
                    widget = ("<div class='cf-turnstile' data-sitekey='fx-sitekey'></div><script>window.turnstileCallback = function (t) {"
                              "fetch('/__verify?token=' + encodeURIComponent(t), {method: 'POST', credentials: 'include'}).then(function(){ location.reload(); }); };</script>")
                else:
                    widget = "<iframe src='/cdn-cgi/challenge-platform/turnstile/frame' title='Widget containing a Cloudflare security challenge' width='300' height='65' style='border:0'></iframe>"
                auto = ""
                if mode == "auto":
                    auto = "<script>setTimeout(function(){ fetch('/__clear', {method:'POST', credentials:'include'}).then(function(){ location.reload(); }); }, 500);</script>"
                return HTMLResponse(CHALLENGE_HTML.format(widget=widget, auto=auto), status_code=403)
            STATE["clearances"][cookie] -= 1
        return None

    async def category(request: Request):
        """A guarded category page — the header menu's 30 links first, then product
        cards, then a pagination link."""
        _count(request)
        STATE["guard_loads"].append({"path": request.url.path, "referer": request.headers.get("referer"), "at": time.time()})
        refused = challenge_or_none(request)
        if refused is not None:
            return refused
        menu = "".join(f"<li><a href='/info/menu-{i}'>Menu {i}</a></li>" for i in range(30))
        cards = "".join(f"<div class='product-card'><a href='/p/{n}'><img src='/media/p{n}.jpg' alt=''>Lamp {n}</a></div>" for n in range(11, 17))
        body = (f"<header><nav class='main-menu'><ul>{menu}</ul></nav></header><main><h1>Lampen</h1>{cards}"
                f"<a class='next' href='/kategorie/lampen?page=2'>2</a></main><footer><a href='/info/kontakt'>Kontakt</a></footer>")
        return HTMLResponse(page("Lampen", body))

    async def info(request: Request):
        _count(request)
        STATE["guard_loads"].append({"path": request.url.path, "referer": request.headers.get("referer"), "at": time.time()})
        refused = challenge_or_none(request)
        if refused is not None:
            return refused
        return HTMLResponse(page("Info", "<h1>Info</h1>" + "<p>Text.</p>" * 40))

    async def shop_path(request: Request):
        """A protected category page in miniature (a shop with a site-wide mega menu): ``/zibintai-c1331`` lists 200
        menu category links (a category tree, ``…-c<n>``) first, then 5 other-language and 2
        pagination links, and only then 12 product links (``…-p<n>``) — plain links, no cards, no
        images, no nav markup. Product pages carry ``.product-page__content``."""
        path = request.url.path
        last = path.rstrip("/").rsplit("/", 1)[-1]
        if not re.search(r"-[cp]\d+$", last):
            return PlainTextResponse("not found", status_code=404)  # favicon.ico etc.: no clearance spent
        _count(request)
        STATE["guard_loads"].append({"path": path, "referer": request.headers.get("referer"), "at": time.time()})
        refused = challenge_or_none(request)
        if refused is not None:
            return refused
        if last.endswith("-p" + last.rsplit("-p", 1)[-1]) and last.rsplit("-p", 1)[-1].isdigit():
            body = (f"<div class='product-page'><div class='product-page__content'><h1>Lempa {last}</h1>"
                    f"<span class='price'>49,90 €</span><button>Į krepšelį</button></div></div>")
            return HTMLResponse(page(f"Lempa {last}", body))
        if path == "/zibintai-c1331":
            menu = "".join(f"<a href='/grupe-{i}-c{i}'>G{i}</a>" + "".join(f"<a href='/grupe-{i}-c{i}/sub-{j}-c{100 + i * 4 + j}'>S{j}</a>" for j in range(4))
                           for i in range(40))
            other = "".join(f"<a href='/ru/grupe-{i}-c{i}'>RU{i}</a>" for i in range(5)) + "<a href='https://other-lang.example/x-c1'>LV</a>"
            pages_ = "<a href='/zibintai-c1331?page=2'>2</a><a href='/zibintai-c1331?sort=price'>sort</a>"
            products = "".join(f"<a href='/lempa-{k}-dep55{k}az-p{1000 + k}'>Lempa {k}</a>" for k in range(12))
            return HTMLResponse(page("Žibintai", f"<div class='mega'>{menu}</div>{other}<div class='list'>{pages_}{products}</div>"))
        return HTMLResponse(page("Kategorija", "<h1>Kategorija</h1>" + "<p>Tekstas.</p>" * 30))

    async def protected(request: Request):
        _count(request)
        STATE["guard_loads"].append({"path": request.url.path, "referer": request.headers.get("referer"), "at": time.time()})
        refused = challenge_or_none(request)
        if refused is not None:
            return refused
        n = int(request.path_params["n"])
        storage = ("<script>try { var k = 'fx_n'; localStorage[k] = String((+localStorage[k] || 0) + 1);"
                   " fetch('/__storage?n=' + localStorage[k], {method: 'POST'}); } catch (e) {}</script>")
        return HTMLResponse(product_html(PRODUCTS[n % len(PRODUCTS)], f"http://{request.url.netloc}", 1).replace(
            "</body>", "".join(f"<a href='/p/{k}'>p{k}</a>" for k in range(n + 1, n + 3)) + storage + "</body>"))

    async def frame(request: Request):
        _count(request)
        return HTMLResponse(FRAME_HTML)

    async def shadow_frame(request: Request):
        _count(request)
        return HTMLResponse(SHADOW_FRAME_HTML)

    async def storage(request: Request):
        STATE["guard_storage"].append(int(request.query_params.get("n", "0")))
        return PlainTextResponse("ok")

    async def clicklog(request: Request):
        STATE["clicks"].append({"x": int(request.query_params.get("x", "-1")), "busy": request.query_params.get("busy") == "1"})
        return PlainTextResponse("ok")

    async def clear(request: Request):
        _count(request)
        if STATE["guard_clear_limit"] is not None:
            if STATE["guard_clear_limit"] <= 0:
                return PlainTextResponse("verification failed")  # the widget resets, no clearance
            STATE["guard_clear_limit"] -= 1
        value = _clearance_value(request.headers.get("user-agent", ""))
        STATE["clearances"][value] = STATE["guard_ttl"]
        resp = PlainTextResponse("ok")
        resp.set_cookie("fx_clearance", value, path="/")
        return resp

    async def verify(request: Request):
        _count(request)
        if request.query_params.get("token") != "fx-token-ok":
            return PlainTextResponse("bad token", status_code=400)
        return await clear(request)

    async def robots(request: Request):
        _count(request)
        if STATE["guard_discovery"]:
            # True: robots.txt and the sitemap behind the challenge; "sitemap": only the sitemap
            refused = challenge_or_none(request) if STATE["guard_discovery"] is True else None
            if refused is not None:
                return refused
            base = f"http://{request.url.netloc}"
            return PlainTextResponse(f"User-agent: *\nAllow: /\nSitemap: {base}/guarded-sitemap.xml\n")
        return PlainTextResponse("User-agent: *\nAllow: /\n")

    async def guarded_sitemap(request: Request):
        _count(request)
        refused = challenge_or_none(request)
        if refused is not None:
            return refused
        return Response(sitemap_xml(f"http://{request.url.netloc}", ["/p/1", "/p/2", "/p/3"]), media_type="application/xml")

    return Starlette(routes=[
        Route("/p/{n}", protected), Route("/guarded-sitemap.xml", guarded_sitemap),
        Route("/kategorie/{name}", category), Route("/info/{name}", info),
        Route("/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2/av0/rcv0/0/fx/0x4AAAAAAAFixtureShadow/light/normal", shadow_frame),
        Route("/__clicklog", clicklog, methods=["POST"]), Route("/__storage", storage, methods=["POST"]), Route("/cdn-cgi/challenge-platform/turnstile/frame", frame),
        Route("/__clear", clear, methods=["POST"]), Route("/__verify", verify, methods=["POST"]),
        Route("/robots.txt", robots), *admin_routes(),
        Route("/{a}", shop_path), Route("/{a}/{b}", shop_path), Route("/{a}/{b}/{c}", shop_path),
    ])


# ---------------------------------------------------------------------------- webhook receiver
def make_hook() -> Starlette:
    async def receive(request: Request):
        body = await request.body()
        entry = {
            "path": request.url.path,
            "at": time.time(),
            "headers": {k: v for k, v in request.headers.items() if k.lower().startswith(("x-scraper", "idempotency", "user-agent"))},
            "body": body.decode("utf-8", "replace"),
            "accepted": False,
            "signature_ok": False,
            "timestamp_ok": False,
        }
        ts = request.headers.get("x-scraper-timestamp", "")
        sig = request.headers.get("x-scraper-signature", "")
        expected = hmac.new(STATE["hook_secret"].encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        entry["signature_ok"] = hmac.compare_digest(sig, expected)
        body_expected = "sha256=" + hmac.new(STATE["hook_secret"].encode(), body, hashlib.sha256).hexdigest()
        entry["body_signature_ok"] = hmac.compare_digest(request.headers.get("x-scraper-body-signature", ""), body_expected)
        try:
            entry["timestamp_ok"] = abs(time.time() - int(ts)) <= 300
        except ValueError:
            entry["timestamp_ok"] = False
        if not STATE["hook_on"]:
            STATE["deliveries"].append(entry)
            return PlainTextResponse("down", status_code=503)
        if not entry["signature_ok"] or not entry["timestamp_ok"]:
            STATE["deliveries"].append(entry)
            return PlainTextResponse("bad signature", status_code=401)
        entry["accepted"] = True
        STATE["deliveries"].append(entry)
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/hook/{name}", receive, methods=["POST"]), *admin_routes()])


# ---------------------------------------------------------------------------- fake solver
def make_solver() -> Starlette:
    async def create(request: Request):
        data = await request.json()
        STATE["solver_calls"]["createTask"] += 1
        if data.get("clientKey") != "fx-solver-key":
            return JSONResponse({"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST", "errorDescription": "bad key"})
        tid = f"task{len(STATE['solver_tasks']) + 1}"
        STATE["solver_tasks"][tid] = time.time()
        return JSONResponse({"errorId": 0, "taskId": tid})

    async def result(request: Request):
        data = await request.json()
        STATE["solver_calls"]["getTaskResult"] += 1
        if data.get("taskId") not in STATE["solver_tasks"]:
            return JSONResponse({"errorId": 1, "errorCode": "ERROR_TASK_NOT_FOUND"})
        return JSONResponse({"errorId": 0, "status": "ready", "solution": {"token": "fx-token-ok"}})

    async def balance(request: Request):
        data = await request.json()
        STATE["solver_calls"]["getBalance"] += 1
        if data.get("clientKey") != "fx-solver-key":
            return JSONResponse({"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST", "errorDescription": "bad key"})
        return JSONResponse({"errorId": 0, "balance": 12.5})

    async def ipecho(request: Request):
        if request.headers.get("x-fixture-tier") == "2" and STATE["echo_ip_tier2"]:
            return JSONResponse({"ip": STATE["echo_ip_tier2"], "country": "DE", "city": "Berlin", "timezone": "Europe/Berlin", "lat": 52.52, "lon": 13.405,
                                 "via": "2"})
        return JSONResponse({"ip": "203.0.113.7", "country": "DE", "city": "Berlin", "timezone": "Europe/Berlin", "lat": 52.52, "lon": 13.405,
                             "via": request.headers.get("x-fixture-tier")})

    return Starlette(routes=[
        Route("/createTask", create, methods=["POST"]), Route("/getTaskResult", result, methods=["POST"]),
        Route("/getBalance", balance, methods=["POST"]), Route("/ip", ipecho), *admin_routes(),
    ])


# ---------------------------------------------------------------------------- admin
def admin_routes() -> list[Route]:
    async def get_state(request: Request):
        return JSONResponse({
            "layout": STATE["layout"], "hits": dict(STATE["hits"]), "product_hits": STATE["product_hits"],
            "deliveries": STATE["deliveries"], "hook_on": STATE["hook_on"], "guard_mode": STATE["guard_mode"],
            "solver_calls": dict(STATE["solver_calls"]), "clicks": STATE["clicks"], "guard_loads": STATE["guard_loads"],
            "guard_storage": STATE["guard_storage"],
        })

    async def set_state(request: Request):
        data = await request.json()
        for key in ("layout", "layout_switch_after", "hook_on", "hook_secret", "guard_mode", "guard_ttl", "guard_discovery", "echo_ip_tier2", "guard_clear_limit",
                    "week", "wk_fail", "lk_wrong", "jk_junk", "jk_fail_every"):
            if key in data:
                STATE[key] = data[key]
        if data.get("reset_hits"):
            STATE["hits"].clear()
            STATE["product_hits"] = 0
            STATE["flaky"].clear()
            STATE["err5"].clear()
            STATE["jk_seen"].clear()
            STATE["jk_calls"] = 0
        if data.get("reset_deliveries"):
            STATE["deliveries"].clear()
        if data.get("reset_clearances"):
            STATE["clearances"].clear()
            STATE["clicks"].clear()
            STATE["guard_loads"].clear()
            STATE["guard_storage"].clear()
        return JSONResponse({"ok": True})

    return [Route("/__admin/state", get_state), Route("/__admin/state", set_state, methods=["POST"])]


# ---------------------------------------------------------------------------- forward proxy
async def proxy_server(port: int) -> None:
    import base64

    import httpx

    user, password = PROXY_USERS[port]
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    tier = "1" if port == PROXY1_PORT else "2"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()
            length = int(headers.get("Content-Length", headers.get("content-length", "0")) or 0)
            body = await reader.readexactly(length) if length else b""
            auth = headers.get("Proxy-Authorization") or headers.get("proxy-authorization")
            if auth != expected:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"fx\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            fwd = {k: v for k, v in headers.items() if k.lower() not in ("proxy-authorization", "proxy-connection", "connection", "host", "content-length")}
            fwd["X-Fixture-Tier"] = tier
            async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
                resp = await client.request(method, target, headers=fwd, content=body)
            out = [f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}"]
            for k, v in resp.headers.multi_items():
                if k.lower() in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                    continue
                out.append(f"{k}: {v}")
            out.append(f"Content-Length: {len(resp.content)}")
            out.append("Connection: close")
            writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + resp.content)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, HOST, port)
    async with server:
        await server.serve_forever()


async def main() -> None:
    apps = [
        (make_shop(True), SHOP_PORT),
        (make_shop(False), PLAIN_PORT),
        (make_guard(), GUARD_PORT),
        (make_hook(), HOOK_PORT),
        (make_solver(), SOLVER_PORT),
    ]
    servers = [uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning", lifespan="off")) for app, port in apps]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    tasks += [asyncio.create_task(proxy_server(PROXY1_PORT)), asyncio.create_task(proxy_server(PROXY2_PORT))]
    print("fixture ready", flush=True)
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
