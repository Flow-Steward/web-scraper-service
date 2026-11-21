"""Static browser-fallthrough hints use DOM semantics, not text volume or hosts."""

import pytest
from app.extraction.page import Page


@pytest.mark.parametrize(
    "control",
    [
        '<button type="button">Load more</button>',
        '<a rel="next" href="?page=2">→</a>',
        '<nav aria-label="Pagination"><button type="button">Next</button></nav>',
        '<section class="site-pagination"><span role="button" aria-label="Next page">→</span></section>',
        '<ul class="paging"><li><a href="?page=2">\u203a</a></li></ul>',
        '<span id="continue-label">Next results</span><button aria-labelledby="continue-label" type="button">→</button>',
    ],
)
def test_listing_control_requests_browser_even_with_substantial_server_rendered_text(control):
    from app.fetching.navigation import has_listing_controls

    page = Page(
        "<main><h1>Available items</h1><p>"
        + "Server-rendered description. " * 30
        + "</p></main>"
        + control,
        "https://catalogue.example.test/list",
    )

    assert page.visible_text_chars > 300
    assert has_listing_controls(page) is True


@pytest.mark.parametrize(
    "control",
    [
        '<button type="button">Next</button>',
        '<form><button type="submit">Load more</button></form>',
        '<button form="checkout" aria-label="Next page">Next</button>',
        '<button disabled aria-label="Next page">Next</button>',
        '<button aria-disabled="true" aria-label="Next page">Next</button>',
        '<div hidden><button aria-label="Next page">Next</button></div>',
        '<div inert><button aria-label="Next page">Next</button></div>',
        '<div aria-hidden="true"><button aria-label="Next page">Next</button></div>',
        '<button type="button">Add to cart</button>',
        '<button type="button">Sign in</button>',
        '<section aria-roledescription="carousel"><button aria-label="Next page">Next</button></section>',
        '<a rel="next" href="https://outside.example.test/list">Next page</a>',
        '<a rel="next" href="/checkout/cart?add-to-cart=1">Next page</a>',
        '<a rel="next" href="javascript:submitOrder()">Next page</a>',
        '<p>Next page loading instructions</p><a href="/part/a">Part</a>',
    ],
)
def test_unrelated_disabled_or_mutating_controls_do_not_request_browser(control):
    from app.fetching.navigation import has_listing_controls

    assert has_listing_controls(Page(control, "https://catalogue.example.test/list")) is False


@pytest.mark.parametrize(
    ("html", "executable"),
    [
        ('<script src="/assets/catalogue.js"></script>', True),
        ("<script>queueMicrotask(() => hydrate());</script>", True),
        ('<script type="module">hydrate();</script>', True),
        ('<script type="text/javascript">hydrate();</script>', True),
        ('<script type="application/json">{"items": []}</script>', False),
        ('<script type="application/ld+json">{"@type": "Product"}</script>', False),
        ("<main><p>Script instructions are ordinary text.</p></main>", False),
    ],
)
def test_navigation_script_hint_excludes_non_executable_data_blocks(html, executable):
    from app.fetching.navigation import has_navigation_scripts

    assert has_navigation_scripts(Page(html, "https://catalogue.example.test/list")) is executable


@pytest.mark.parametrize(
    "markup",
    [
        '<section class="refinement-list"><button type="button">Show more</button></section>',
        '<div role="group" aria-label="Filters"><button type="button">Show more</button></div>',
        '<aside><button type="button">Show more</button></aside>',
        '<fieldset><legend>Categories</legend><button type="button">Show more</button></fieldset>',
    ],
)
def test_filter_expansion_does_not_hint_at_listing_pagination(markup):
    from app.fetching.navigation import has_listing_controls

    assert has_listing_controls(Page(markup, "https://catalogue.example.test/list")) is False


def test_filter_layout_does_not_hide_explicit_next_page_pagination():
    from app.fetching.navigation import has_listing_controls

    markup = '<section class="filter-layout"><nav aria-label="Pagination"><button type="button">Next</button></nav></section>'
    assert has_listing_controls(Page(markup, "https://catalogue.example.test/list")) is True
