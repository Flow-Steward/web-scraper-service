"""Browser observations supplement DOM links without changing their ranking."""

from app.extraction.page import Page
from app.extraction.sampling import link_groups


def test_observed_dom_links_keep_menu_flags_and_original_counts():
    origin = "https://catalogue.example.test"
    menu = "".join(f'<a href="/group-c{i}">Group</a>' for i in range(200))
    products = "".join(f'<a href="/item-p{i}">Item</a>' for i in range(12))
    page = Page(
        f'<nav>{menu}</nav><main>{products}</main>'
        '<a href="https://outside.example.test/">Other</a>'
        '<a href="/list?page=2">Next</a>',
        origin + "/list",
        extra_links=[
            *(f"{origin}/group-c{i}" for i in range(200)),
            *(f"{origin}/item-p{i}" for i in range(12)),
            "https://outside.example.test/",
            origin + "/list?page=2",
            origin + "/new-p12",
            origin + "/new-p12#details",
        ],
    )

    groups, stats = link_groups(page, "catalogue.example.test")

    assert stats == {
        "hrefs": 215,
        "in_site": 213,
        "other_hosts": 1,
        "variants_skipped": 1,
    }
    by_shape = {group["shape"]: group for group in groups}
    assert by_shape["/*-c<n>"]["in_menus"] == 1.0
    assert by_shape["/*-p<n>"]["in_menus"] == 0.0
    assert groups[0]["shape"] == "/*-p<n>"
    assert by_shape["/*-p<n>"]["links"] == 13


def test_existing_content_occurrence_still_outweighs_menu_occurrence():
    origin = "https://catalogue.example.test"
    page = Page(
        '<nav><a href="/item-p1">Menu</a></nav>'
        '<main><a href="/item-p1">Content</a></main>',
        origin + "/list",
        extra_links=[origin + "/item-p1"],
    )

    groups, stats = link_groups(page, "catalogue.example.test")

    assert stats["hrefs"] == 2
    assert groups[0]["links"] == 1
    assert groups[0]["in_menus"] == 0.0
