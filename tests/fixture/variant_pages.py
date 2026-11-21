"""Synthetic shop pages with product variants, modelled on real shapes:

* a classic WooCommerce variable product — ``form.variations_form[data-product_variations]``
  with HTML-escaped JSON, option slugs (``attribute_pa_lens-cover-markings: "eu"``) whose
  labels live in the form's ``<label>`` and ``<select>``, the SAME parent SKU on every
  variation, no JSON-LD (e.g. lens covers: no / EU / USA sticker);
* a WooCommerce block-theme page without the form: the Store API lists the variations;
* a Shopify product page (theme JSON in ``<script type="application/json">``) and its
  ``/products/<handle>.js`` (prices in minor units, ``weight`` in grams, ``barcode``);
* a music release with JSON-LD ``offers`` for CD and vinyl;
* a ``ProductGroup`` with ``hasVariant`` and ``variesBy``;
* a microdata product.
"""

from __future__ import annotations

import html
import json

WOO_VARIATIONS = [
    {"attributes": {"attribute_pa_lens-cover-markings": slug}, "availability_html": "<p class=\"stock in-stock\">In stock</p>",
     "display_price": price, "display_regular_price": price, "image": {"full_src": f"/wp-content/uploads/lens-{slug}.jpg", "src": f"/wp-content/uploads/lens-{slug}-600x600.jpg"},
     "is_in_stock": True, "sku": "BMW-F10-KP-2009-2017", "variation_description": f"<p>Lens cover, {label} marking.</p>",
     "variation_id": vid, "weight": "3", "weight_html": "3 kg"}
    for slug, label, price, vid in (("no", "no", 24, 47833), ("eu", "EU", 54, 47834), ("usa", "USA", 54, 47835))
]


def woo_classic(slug: str) -> str:
    data = html.escape(json.dumps(WOO_VARIATIONS), quote=True)
    options = "".join(f"<option value=\"{v}\" class=\"attached enabled\">{t}</option>" for v, t in (("no", "No"), ("eu", "EU sticker"), ("usa", "USA sticker")))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>BMW lens cover {slug}</title>
<link rel="stylesheet" href="/wp-content/plugins/woocommerce/assets/css/woocommerce.css"></head>
<body class="product-template-default single single-product postid-6689 woocommerce">
<div class="product"><h1 class="product_title entry-title">BMW lens cover {slug}</h1>
<p class="price"><span class="woocommerce-Price-amount amount">24,00&nbsp;€</span> – 54,00&nbsp;€</p>
<span class="sku">BMW-F10-KP-2009-2017</span>
<form class="variations_form cart" action="/product/{slug}/" method="post" data-product_id="6689" data-product_variations="{data}">
<table class="variations" cellspacing="0"><tbody><tr><td class="label">
<label for="pa_lens-cover-markings">Lens cover markings: <span class="mf-attr-value">No</span></label></td>
<td class="value"><select id="pa_lens-cover-markings" name="attribute_pa_lens-cover-markings" data-attribute_name="attribute_pa_lens-cover-markings">
<option value="">Choose an option</option>{options}</select></td></tr></tbody></table>
<button type="submit" class="single_add_to_cart_button button">Add to cart</button></form></div></body></html>"""


def woo_block(slug: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Headlight glass {slug}</title>
<link rel="shortlink" href="/?p=7001"><script src="/wp-content/plugins/woocommerce/assets/client/blocks/product.js"></script></head>
<body class="wp-site-blocks postid-7001"><main><h1 class="wp-block-post-title">Headlight glass {slug}</h1>
<div class="wp-block-woocommerce-product-price">from 39,00 €</div>
<div class="wp-block-add-to-cart-form" data-product_id="7001"></div></main></body></html>"""


STORE_PRODUCT = {"id": 7001, "name": "Headlight glass", "type": "variable", "sku": "HG-7001",
                 "prices": {"price": "3900", "currency_code": "EUR", "currency_minor_unit": 2},
                 "variations": [{"id": 7101, "attributes": [{"name": "Sticker", "value": "US sticker"}]},
                                {"id": 7102, "attributes": [{"name": "Sticker", "value": "EU sticker"}]},
                                {"id": 7103, "attributes": [{"name": "Sticker", "value": "no sticker"}]}]}
STORE_VARIATIONS = [
    {"id": vid, "name": "Headlight glass", "sku": f"HG-7001-{tag}", "is_in_stock": vid != 7103,
     "prices": {"price": str(price), "regular_price": str(price), "currency_code": "EUR", "currency_minor_unit": 2},
     "description": f"<p>{tag} sticker</p>", "images": [{"src": f"/wp-content/uploads/hg-{tag}.jpg"}]}
    for vid, tag, price in ((7101, "US", 4900), (7102, "EU", 4500), (7103, "NONE", 3900))
]


SHOPIFY_PRODUCT = {
    "id": 9001, "title": "Big Bad & Back EP", "handle": "big-bad-back-ep", "vendor": "Little Angels",
    "options": [{"name": "Format", "position": 1, "values": ["CD [BB1CD]", "12\" [BB1LP]"]}],
    "variants": [
        {"id": 9101, "title": "CD [BB1CD]", "option1": "CD [BB1CD]", "option2": None, "option3": None, "price": 1099, "compare_at_price": None,
         "sku": "BB1CD", "barcode": "5037300088600", "weight": 52, "available": True, "featured_image": None},
        {"id": 9102, "title": "12\" [BB1LP]", "option1": "12\" [BB1LP]", "option2": None, "option3": None, "price": 1799, "compare_at_price": None,
         "sku": "BB1LP", "barcode": "5037300088617", "weight": 208, "available": False, "featured_image": {"src": "//cdn.shopify.example/lp.jpg"}},
    ],
}


def shopify_page(handle: str) -> str:
    theme = json.dumps(SHOPIFY_PRODUCT)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{SHOPIFY_PRODUCT['title']}</title>
<meta property="og:price:currency" content="GBP"><link rel="preconnect" href="https://cdn.shopify.com"></head>
<body><h1 class="product__title">{SHOPIFY_PRODUCT['title']}</h1><div class="price">£10.99</div>
<script type="application/json" id="ProductJson-main">{theme}</script>
<script>window.Shopify = {{"shop": "fixture.myshopify.com"}};</script></body></html>"""


def keyed_editions(slug: str) -> str:
    choices = [{"id": "a", "sku": "0001-A", "enabled": True}, {"id": "b", "sku": "0001-B", "enabled": False}, {"id": "c", "sku": "0001-C", "enabled": True}]
    return ('<!doctype html><html><body><main><h1>Fixture item</h1><div class="parent" data-allowed="true">Parent text must not fill an empty edition.</div>'
            f'<script type="application/json" id="choices">{json.dumps({"state": {"choices": choices}})}</script>'
            '<section data-edition="a"><div class="description"><p>First edition notes</p></div></section>'
            '<section data-edition="b"><div class="description"><p>Other edition notes</p></div></section>'
            '<section data-edition="c"><div class="description"></div></section>'
            '<p>' + 'Fixture context for a complete product sheet. ' * 80 + '</p></main></body></html>')


def dynamic_listing(slug: str) -> str:
    return (
        '<!doctype html><html><body><main id="listing"><a href="/v/plain-item/first">First</a></main>'
        '<nav aria-label="Pagination"><button type="button" aria-label="Next page" onclick="advance(this)">Next</button></nav>'
        "<script>function advance(button) { const a=document.createElement('a'); a.href='/v/plain-item/second'; "
        "a.textContent='Second'; document.querySelector('#listing').replaceChildren(a); button.disabled=true; }</script>"
        "<p>" + "Fixture catalogue context. " * 50 + "</p></body></html>"
    )


def shell_listing(slug: str) -> str:
    """A populated HTML shell whose useful listing exists only after hydration."""
    return (
        '<!doctype html><html><body><h1>Catalogue</h1><main id="listing"></main>'
        f'<nav id="catalogue"><a href="/v/shell-listing/{slug}">Catalogue</a></nav>'
        "<p>" + "Server-rendered catalogue context. " * 50 + "</p>"
        "<script>queueMicrotask(() => {"
        "const first=document.createElement('a'); first.href='/v/plain-item/first'; first.textContent='First';"
        "document.querySelector('#listing').append(first);"
        "const button=document.createElement('button'); button.type='button';"
        "button.setAttribute('aria-label','Next page'); button.textContent='Next';"
        "button.onclick=function() { const second=document.createElement('a');"
        "second.href='/v/plain-item/second'; second.textContent='Second';"
        "document.querySelector('#listing').replaceChildren(second); this.disabled=true; };"
        "document.querySelector('#catalogue').append(button);"
        "});</script></body></html>"
    )


def productive_shell_listing(slug: str) -> str:
    """Useful SSR navigation is not proof that a scripted listing is complete."""
    context = "<p>" + "Server-rendered catalogue navigation. " * 40 + "</p>"
    if slug != "category":
        item = {
            "navigation": "initial",
            "first-maker": "first",
            "second-maker": "second",
        }.get(slug)
        link = f'<a href="/v/plain-item/{item}">Item</a>' if item else ""
        return (
            "<!doctype html><html><body><main><h1>Catalogue navigation</h1>"
            + link
            + context
            + "</main></body></html>"
        )
    return (
        '<!doctype html><html><body><h1>Catalogue</h1><main id="maker-list"></main>'
        '<nav><a href="/v/productive-shell/navigation">Browse catalogue</a></nav>'
        '<nav id="pagination" aria-label="Pagination"></nav>'
        + context
        + "<script>queueMicrotask(() => {"
        "const first=document.createElement('a'); first.href='/v/productive-shell/first-maker'; first.textContent='First maker';"
        "document.querySelector('#maker-list').append(first);"
        "const next=document.createElement('button'); next.type='button'; next.setAttribute('aria-label','Next page'); next.textContent='Next';"
        "next.onclick=function() { const second=document.createElement('a');"
        "second.href='/v/productive-shell/second-maker'; second.textContent='Second maker';"
        "document.querySelector('#maker-list').replaceChildren(second); this.disabled=true; };"
        "document.querySelector('#pagination').append(next);"
        "});</script></body></html>"
    )


def paced_listing(slug: str) -> str:
    """Five same-URL listing states, long enough to exceed one paced page budget."""
    return """<!doctype html><html><body><main>
      <h1>Catalogue</h1><a id="item" href="/v/plain-item/paced-0">Item</a></main>
      <nav aria-label="Pagination"><button type="button" aria-label="Next page">Next</button></nav>
      <script>let index=0; document.querySelector('button').onclick=function() {
        index++; document.querySelector('#item').href='/v/plain-item/paced-'+index;
        if(index===4) this.disabled=true;
      };</script></body></html>"""


def plain_item(slug: str) -> str:
    return (
        '<html><body><main class="sheet"><h1>Item '
        + slug
        + "</h1><p>"
        + "Complete item description. " * 50
        + "</p></main></body></html>"
    )


def release_page(slug: str) -> str:
    ld = {
        "@context": "https://schema.org",
        "@type": ["Product", "MusicAlbum"],
        "name": f"Release {slug}",
        "byArtist": {"@type": "MusicGroup", "name": "Fixture Band"},
        "offers": [
            {
                "@type": "Offer",
                "price": "12.99",
                "priceCurrency": "EUR",
                "sku": f"{slug}-CD",
                "availability": "https://schema.org/InStock",
                "itemOffered": {
                    "@type": "MusicRelease",
                    "name": "CD",
                    "musicReleaseFormat": "https://schema.org/CDFormat",
                },
            },
            {
                "@type": "Offer",
                "price": "24.99",
                "priceCurrency": "EUR",
                "sku": f"{slug}-LP",
                "availability": "https://schema.org/OutOfStock",
                "itemOffered": {
                    "@type": "MusicRelease",
                    "name": "Vinyl",
                    "musicReleaseFormat": "https://schema.org/VinylFormat",
                },
            },
        ],
    }
    return (f"<!doctype html><html><head><title>Release {slug}</title><script type='application/ld+json'>{json.dumps(ld)}</script></head>"
            f"<body><h1>Release {slug}</h1><p>CD 12,99 € · Vinyl 24,99 €</p></body></html>")


def group_page(slug: str) -> str:
    ld = {"@context": "https://schema.org", "@type": "ProductGroup", "name": f"Shirt {slug}", "productGroupID": slug,
          "variesBy": ["https://schema.org/size", "https://schema.org/color"],
          "hasVariant": [{"@type": "Product", "sku": f"{slug}-{s}-{c}", "name": f"Shirt {slug} {s} {c}", "size": s, "color": c,
                          "image": f"/img/{slug}-{c}.jpg", "offers": {"@type": "Offer", "price": p, "priceCurrency": "EUR",
                                                                      "availability": "https://schema.org/InStock"}}
                         for s, c, p in (("S", "red", "19.00"), ("M", "red", "19.00"), ("M", "blue", "21.00"))]}
    return (f"<!doctype html><html><head><title>Shirt {slug}</title><script type='application/ld+json'>{json.dumps(ld)}</script></head>"
            f"<body><h1>Shirt {slug}</h1></body></html>")


def micro_page(slug: str) -> str:
    return f"""<!doctype html><html><head><title>Lamp {slug}</title></head><body>
<div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">Lamp {slug}</h1>
<span itemprop="sku">LMP-{slug}</span><img itemprop="image" src="/img/{slug}.jpg">
<div itemprop="offers" itemscope itemtype="https://schema.org/Offer"><meta itemprop="priceCurrency" content="EUR">
<span itemprop="price" content="89.50">89,50 €</span><link itemprop="availability" href="https://schema.org/InStock"></div></div></body></html>"""


# ---------------------------------------------------------------- price status pages
# Modelled on a typical WooCommerce product page:
# a Martfury WooCommerce theme whose header and mega menu are about 80 KB of the distilled page before the product summary.

def _heavy_header() -> str:
    cats = ["Headlight lens covers", "Tail lights", "LED bulbs", "Xenon bulbs", "Angel eyes", "Projectors", "Ballasts", "Fog lights"]
    makes = ["Audi", "BMW", "Mercedes-Benz", "Volkswagen", "Volvo", "Skoda", "Porsche", "Opel", "Ford", "Toyota", "Lexus", "Mazda"]
    items = []
    for c in cats:
        for mk in makes:
            for series in range(1, 9):
                slug = f"{c}-{mk}-{series}".lower().replace(" ", "-")
                items.append(f'<li class="menu-item menu-item-type-taxonomy menu-item-object-product_cat" data-menu-item="{slug}">'
                             f'<a href="/product-category/{slug}/" class="mf-menu-link" data-title="{mk} {c} series {series}">{mk} series {series}</a></li>')
    mega = "".join(f'<li class="menu-item-has-children mega-menu-col"><a href="/c/{i}/">{c}</a><ul class="sub-menu">{"".join(items[i * 96:(i + 1) * 96])}</ul></li>'
                   for i, c in enumerate(cats))
    return (f'<div id="page" class="hfeed site"><header id="site-header" class="site-header header-layout-3">'
            f'<div class="header-main"><div class="mini-cart"><span class="price"><span class="woocommerce-Price-amount amount">0,00&nbsp;€</span></span></div>'
            f'<nav id="primary-menu" class="main-navigation"><ul class="menu mega-menu">{mega}</ul></nav></div></header>')


def _footer() -> str:
    links = "".join(f'<li><a href="/info/{i}/">Information page {i}</a></li>' for i in range(60))
    return (f'<footer id="colophon" class="site-footer"><div class="footer-widgets"><ul>{links}</ul>'
            '<p>Delivery on request to all EU countries.</p></div></footer></div>')


def _yoast(name: str, desc: str) -> str:
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "@id": "#webpage", "name": name, "description": desc},
        {"@type": "WebSite", "@id": "#website", "name": "Fixture lights"}]}
    return f'<script type="application/ld+json" class="yoast-schema-graph">{json.dumps(graph)}</script>'


HEAVY_VARIATIONS = [
    dict(v, availability_html="<p class=\"stock in-stock\"><label>Status:</label>In stock</p>\n" + "<!-- theme padding -->" * 20,
         price_html=f"<span class=\"price\"><ins><span class=\"woocommerce-Price-amount amount\"><bdi>{v['display_price']},00&nbsp;€</bdi></span></ins></span>",
         gallery_images_html="<div class=\"woocommerce-product-gallery__image\"><img src=\"/wp-content/uploads/lens.jpg\"></div>" * 8)
    for v in WOO_VARIATIONS
]


def woo_heavy(slug: str) -> str:
    """A variable product (3 variants 24/54/54, one parent SKU) behind a heavy header and mega menu; the visible
    price is a range, the variant prices are in ``data-product_variations``; JSON-LD has only a WebPage description."""
    data = html.escape(json.dumps(HEAVY_VARIATIONS), quote=True)
    options = "".join(f"<option value=\"{v}\" class=\"attached enabled\">{t}</option>" for v, t in (("no", "No"), ("eu", "EU"), ("usa", "USA")))
    title = "BMW 5 F10 F11 LCI 2010–2016 Headlight Lens Cover Left Side"
    return f"""<!doctype html><html lang="en-US"><head><meta charset="utf-8"><title>{title}</title>
<meta name="description" content="Buy the BMW 5 F10 headlight lens cover at the best price. Fast delivery.">
<link rel="stylesheet" href="/wp-content/plugins/woocommerce/assets/css/woocommerce.css">{_yoast(title, "Buy the BMW 5 F10 headlight lens cover at the best price.")}</head>
<body class="product-template-default single single-product postid-6689 woocommerce">{_heavy_header()}
<div id="content" class="site-content"><div class="container"><div class="row"><div id="primary" class="content-area col-md-12">
<div id="product-6689" class="mf-single-product product type-product post-6689 instock product-type-variable">
<div class="mf-product-detail"><div class="mf-entry-product-header"><h1 class="product_title entry-title">{title}</h1>
<ul class="entry-meta"><li class="meta-sku">SKU: <span class="meta-value">BMW-F10-KP-2009-2017</span></li></ul></div>
<div class="woocommerce-product-gallery images"><a href="/wp-content/uploads/lens-left.jpg"><img src="/wp-content/uploads/lens-left-600x600.jpg" alt="lens"></a></div>
<div class="summary entry-summary"><div class="martfury-product-price"><p class="price" style="display: none;"><span class="woocommerce-Price-amount amount" aria-hidden="true"><bdi>24,00&nbsp;<span class="woocommerce-Price-currencySymbol">€</span></bdi></span> <span aria-hidden="true">–</span> <span class="woocommerce-Price-amount amount" aria-hidden="true"><bdi>54,00&nbsp;<span class="woocommerce-Price-currencySymbol">€</span></bdi></span><span class="screen-reader-text">Price range: 24,00&nbsp;€ through 54,00&nbsp;€</span> <span class="sale">(-31%)</span></p></div>
<div class="mf-summary-meta"><p class="stock in-stock"><label>Status:</label>In stock</p></div>
<div class="woocommerce-product-details__short-description"><p>Condition: New</p></div>
<form class="variations_form cart" action="/v/woo-heavy/{slug}" method="post" data-product_id="6689" data-product_variations="{data}">
<table class="variations"><tbody><tr><td class="label"><label for="pa_lens-cover-markings">Type: <span class="mf-attr-value">No</span></label></td>
<td class="value"><select id="pa_lens-cover-markings" name="attribute_pa_lens-cover-markings" data-attribute_name="attribute_pa_lens-cover-markings">
<option value="">Choose an option</option>{options}</select></td></tr></tbody></table>
<button type="submit" class="single_add_to_cart_button button">Add to cart</button></form></div></div>
<div class="woocommerce-tabs wc-tabs-wrapper"><ul class="tabs wc-tabs"><li class="description_tab"><a href="#tab-description">Description</a></li></ul>
<div class="woocommerce-Tabs-panel woocommerce-Tabs-panel--description panel entry-content wc-tab" id="tab-description">
<h2>{title}</h2><p>The BMW 5 F10 F11 LCI headlight lens cover left side restores your headlight clarity. Made to OEM standards from clear polycarbonate.</p></div></div>
</div></div></div></div></div>{_footer()}</body></html>"""


def woo_on_request(slug: str) -> str:
    """A product without a price: an empty price block and "contact us" wording; the header's mini cart shows 0,00 €."""
    title = f"BMW X5 E70 Headlight {slug}"
    return f"""<!doctype html><html lang="en-US"><head><meta charset="utf-8"><title>{title}</title>{_yoast(title, "Headlights for BMW X5.")}</head>
<body class="single-product postid-7100 woocommerce">{_heavy_header()}
<div id="content" class="site-content"><div id="product-7100" class="product type-product product-type-simple">
<div class="mf-product-detail"><h1 class="product_title entry-title">{title}</h1>
<div class="summary entry-summary"><div class="martfury-product-price"><p class="price"></p></div>
<div class="woocommerce-product-details__short-description"><p>To find out the price and condition of a product, please contact us by e-mail or phone.</p></div>
<span class="sku">X5-E70-HL</span></div></div>
<div id="tab-description" class="woocommerce-Tabs-panel"><p>Original BMW X5 E70 headlight, used.</p></div></div></div>{_footer()}</body></html>"""


def range_page(slug: str) -> str:
    """A simple product whose only price is a range, with no variant data (a price by length, chosen on the phone)."""
    title = f"LED strip {slug}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title></head>
<body><main><div class="product"><h1 class="product_title">{title}</h1>
<div class="summary entry-summary"><p class="price"><span class="woocommerce-Price-amount amount">10,00&nbsp;€</span> – <span class="woocommerce-Price-amount amount">20,00&nbsp;€</span></p>
<span class="sku">LED-{slug}</span><p>Price depends on the length; we cut it to order.</p></div></div></main></body></html>"""


# ---------------------------------------------------------------- a catalogue that turns variable later
# /gc/ lists simple products first, then a price-on-request product, a sale product (its price in <ins>, not in
# .regular-price) and three variable products (WooCommerce variation forms). A mapping built from the simple
# products reads .regular-price and declares no variants.
GAP_CATALOGUE = ["p1", "p2", "p3", "p4", "r1", "s1", "v1", "v2", "v3"]


def gap_listing() -> str:
    links = "".join(f'<li class="product"><a href="/gc/{slug}">Product {slug}</a></li>' for slug in GAP_CATALOGUE)
    return f'<!doctype html><html><head><title>Catalogue</title></head><body><main><h1>Catalogue</h1><ul class="products">{links}</ul></main></body></html>'


def gap_product(slug: str) -> str | None:
    if slug not in GAP_CATALOGUE:
        return None
    n = GAP_CATALOGUE.index(slug) + 1
    title = f"Lamp {slug.upper()}"
    form = ""
    if slug.startswith("p"):
        price = f'<p class="price"><span class="regular-price woocommerce-Price-amount amount">{10 + n},00&nbsp;€</span></p>'
    elif slug == "r1":
        price = '<p class="price"></p><p>Price on request: contact us for a price.</p>'
    elif slug == "s1":
        price = '<p class="price"><del><span class="woocommerce-Price-amount amount">15,00&nbsp;€</span></del> <ins><span class="woocommerce-Price-amount amount">11,00&nbsp;€</span></ins></p>'
    else:
        price = ('<p class="price"><span class="woocommerce-Price-amount amount">24,00&nbsp;€</span> – '
                 '<span class="woocommerce-Price-amount amount">54,00&nbsp;€</span></p>')
        data = html.escape(json.dumps([dict(v, sku=f"GC-{slug}") for v in WOO_VARIATIONS]), quote=True)
        options = "".join(f"<option value=\"{v}\">{t}</option>" for v, t in (("no", "No"), ("eu", "EU"), ("usa", "USA")))
        form = (f'<form class="variations_form cart" data-product_id="{8000 + n}" data-product_variations="{data}">'
                f'<label for="pa_lens-cover-markings">Type:</label><select id="pa_lens-cover-markings" name="attribute_pa_lens-cover-markings">'
                f'<option value="">Choose an option</option>{options}</select></form>')
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head><body class="woocommerce">'
            f'<div id="product-{8000 + n}" class="product"><div class="summary entry-summary"><h1 class="product_title">{title}</h1>'
            f'{price}<span class="sku">GC-{slug}</span>{form}</div></div></body></html>')


def nested_editions(slug: str) -> str:
    editions = [
        {
            "year": "2023",
            "notes": "Earlier edition",
            "packages": [
                {
                    "sku": "001",
                    "format": "1 x 150 cl",
                    "packaging": "Wood presentation box",
                    "available": False,
                },
                {"sku": "002", "format": "6 x 75 cl", "available": True},
            ],
        },
        {
            "year": "2024",
            "notes": "Later edition",
            "packages": [
                {"sku": "003", "format": "1 x 75 cl", "packaging": "Individual carton"},
            ],
        },
    ]
    return (
        '<html><body><h1>Example editions</h1><script type="application/json" id="state">'
        + json.dumps({"editions": editions})
        + "</script></body></html>"
    )


# ---------------------------------------------------------------- prices split into tags, separators, machine prices
# case -> (html lang, the price block inside .summary, expected price). /pc/<case> on variants.test.
PRICE_CASES = {
    "int-sup": ("en", '<p class="price"><span class="int">24</span><sup>99</sup> €</p>', 24.99),
    "whole-fraction": ("en", '<p class="price"><span class="price-whole">24</span><span class="price-fraction">99</span> €</p>', 24.99),
    "amazon": ("en", '<p class="price">$<span class="a-price-whole">24<span class="a-price-decimal">.</span></span><span class="a-price-fraction">99</span></p>', 24.99),
    "small-comma": ("de", '<p class="price">24<small>,99</small> €</p>', 24.99),
    "euro-split": ("de", '<p class="price">€<span>1.234</span><span>,56</span></p>', 1234.56),
    "sup-comma": ("de", '<p class="price">24<sup>,99</sup> €</p>', 24.99),
    "sale": ("de", '<p class="price"><ins>24 €</ins>  <del>35 €</del></p>', 24.0),
    "spaced-thousands": ("fr", '<p class="price">1 299,00 €</p>', 1299.0),
    "en-thousands": ("en", '<p class="price">€1,234.56</p>', 1234.56),
    "de-both": ("de", '<p class="price">1.234,56 €</p>', 1234.56),
    "ambiguous-de": ("de", '<p class="price">Price: 1.234</p>', 1234.0),
    "ambiguous-en": ("en", '<p class="price">Price: 1.234</p>', 1.234),
    "comma-de": ("de", '<p class="price">Preis: 1,234</p>', 1.234),
    "comma-en": ("en", '<p class="price">Price: 1,234</p>', 1234.0),
    "comma-only": ("lt", '<p class="price">24,99 €</p>', 24.99),
    "dot-only": ("lt", '<p class="price">24.99 €</p>', 24.99),
    # the visible price and the machine-readable one disagree: a mapping that reads only the text gets price_mismatch
    "mismatch": ("en", '<p class="price"><span itemprop="price" content="19.99">24.99 €</span></p>', 24.99),
    # sales: the current price, never the struck-through old one (which becomes regular_price)
    "woo-sale": ("en", '<p class="price"><del aria-hidden="true"><span class="woocommerce-Price-amount amount"><bdi>35,00&nbsp;<span '
                 'class="woocommerce-Price-currencySymbol">€</span></bdi></span></del> <span class="screen-reader-text">Original price was: '
                 '35,00&nbsp;€.</span><ins aria-hidden="true"><span class="woocommerce-Price-amount amount"><bdi>24,00&nbsp;<span '
                 'class="woocommerce-Price-currencySymbol">€</span></bdi></span></ins><span class="screen-reader-text">Current price is: '
                 '24,00&nbsp;€.</span></p>', 24.0),
    "magento-sale": ("de", '<div class="price-box price-final_price"><span class="old-price"><span class="price-label">Regulärer Preis</span>'
                     '<span class="price">49,90 €</span></span><span class="special-price"><span class="price-label">Sonderpreis</span>'
                     '<span class="price">39,90 €</span></span></div>', 39.9),
    "shopify-sale": ("en", '<div class="price price--on-sale"><div class="price__sale"><s class="price-item price-item--regular">£35.00</s> '
                     '<span class="price-item price-item--sale price-item--last">£24.00</span></div></div>', 24.0),
    "was-now": ("en", '<p class="price"><span class="was">Was $59.99</span> <span class="now">Now $44.99</span></p>', 44.99),
    "line-through": ("en", '<p class="price"><span style="text-decoration: line-through">$20.00</span> $15.00</p>', 15.0),
    "jsonld-sale": ("en", '<p class="price">€24.00</p>', 24.0),
}
SALE_REGULAR = {"woo-sale": 35.0, "magento-sale": 49.9, "shopify-sale": 35.0, "was-now": 59.99, "line-through": 20.0, "jsonld-sale": 35.0}


def price_case(case: str) -> str | None:
    if case not in PRICE_CASES:
        return None
    lang, block, _ = PRICE_CASES[case]
    offer = '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">' if case == "mismatch" else "<div>"
    head = ""
    if case == "jsonld-sale":
        ld = {"@context": "https://schema.org", "@type": "Product", "name": f"Price {case}",
              "offers": {"@type": "Offer", "price": "24.00", "priceCurrency": "EUR", "priceSpecification": [
                  {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/StrikethroughPrice", "price": "35.00", "priceCurrency": "EUR"}]}}
        head = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>Price {case}</title>{head}</head><body>'
            f'<div class="product" itemscope itemtype="https://schema.org/Product"><div class="summary entry-summary">'
            f'<h1 class="product_title" itemprop="name">Price {case}</h1>{offer}{block}</div></div></div></body></html>')


def woo_sale_variable(slug: str) -> str:
    """A variable product on sale: every variation has a regular price above its price; the visible block is a struck
    range above the current range."""
    variations = [dict(v, display_regular_price=v["display_price"] + 6) for v in WOO_VARIATIONS]
    data = html.escape(json.dumps(variations), quote=True)
    options = "".join(f"<option value=\"{v}\">{t}</option>" for v, t in (("no", "No"), ("eu", "EU"), ("usa", "USA")))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Lens cover {slug} sale</title></head><body class="woocommerce">'
            f'<div id="product-6690" class="product"><div class="summary entry-summary"><h1 class="product_title">Lens cover {slug} sale</h1>'
            '<p class="price"><del><span class="woocommerce-Price-amount amount">30,00&nbsp;€</span> – <span class="woocommerce-Price-amount amount">'
            '60,00&nbsp;€</span></del> <ins><span class="woocommerce-Price-amount amount">24,00&nbsp;€</span> – '
            '<span class="woocommerce-Price-amount amount">54,00&nbsp;€</span></ins></p>'
            f'<form class="variations_form cart" data-product_id="6690" data-product_variations="{data}">'
            f'<label for="pa_lens-cover-markings">Type:</label><select id="pa_lens-cover-markings" name="attribute_pa_lens-cover-markings">'
            f'<option value="">Choose an option</option>{options}</select></form></div></div></body></html>')
