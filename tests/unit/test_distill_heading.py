"""Missing semantic layout must not let navigation consume product evidence."""

import pytest

from app.extraction.distill import distill


def menu():
    return (
        '<div class="catalogue-menu">'
        + "".join(
            f'<a href="/category/{n}">Catalogue category number {n}</a>'
            for n in range(1000)
        )
        + "</div>"
    )


@pytest.mark.parametrize("wrapper", ["", '<div id="application">'])
def test_heading_region_prioritizes_plain_div_product_and_its_tabs(wrapper):
    html = (
        wrapper
        + menu()
        + """<div class="detail-region"><div class="container">
      <div><h1>Example product</h1><table><tr><th>Mounting side</th><td>Left</td></tr></table></div>
      <div id="identifiers"><h2>Original codes</h2><span>001234567</span></div>
      <div id="fitment"><h2>Fitment</h2><span>2010–2016</span></div>
    </div></div>"""
        + ("</div>" if wrapper else "")
    )
    text, truncated = distill(html, 1800)
    assert truncated is True
    assert "Mounting side" in text and "001234567" in text and "2010–2016" in text
    assert text.index("Example product") < text.index("Catalogue category")
    assert "div.detail-region" in text


@pytest.mark.parametrize("max_bytes", [1200, 200000])
def test_direct_body_heading_keeps_following_attribute_blocks_and_tail_text(max_bytes):
    html = (
        menu()
        + "<h1>Example product</h1>Raw format 6 × 75<table><tr><td>OEM 0000123</td></tr></table><div>Fitment 2011–2017</div>"
    )
    text, _ = distill(html, max_bytes)
    assert "OEM 0000123" in text and "Fitment 2011–2017" in text
    assert text.count("Raw format 6 × 75") == 1
    assert text.index("Example product") < text.index("Catalogue category")


@pytest.mark.parametrize(
    "prefix",
    [
        "<header><h1>Store navigation</h1></header>",
        '<div role="navigation"><h1>Store navigation</h1></div>',
        '<div role="dialog"><h1>Store navigation</h1></div>',
        "<div hidden><h1>Store navigation</h1></div>",
        '<div aria-hidden="true"><h1>Store navigation</h1></div>',
        '<div style="display: none"><h1>Store navigation</h1></div>',
        '<div style="visibility: hidden"><h1>Store navigation</h1></div>',
    ],
)
def test_navigation_and_hidden_headings_do_not_displace_content(prefix):
    html = (
        prefix
        + menu()
        + "<section><h1>Real product</h1><table><tr><td>OEM 000042</td></tr></table></section>"
    )
    text, _ = distill(html, 1200)
    assert "OEM 000042" in text
    assert text.index("Real product") < text.index("Store navigation")


def test_role_main_survives_attribute_cleaning_and_still_has_priority():
    html = menu() + '<div role="main"><h1>Real product</h1><p>OEM 000042</p></div>'
    text, _ = distill(html, 1200)
    assert "wss:section main-content" in text
    assert 'role="main"' in text
    assert text.index("OEM 000042") < text.index("Catalogue category")


def test_link_heavy_following_sibling_stays_behind_later_product_attributes():
    html = (
        menu()
        + "<h1>Real product</h1>"
        + menu()
        + "<table><tr><td>OEM 000042</td></tr></table>"
    )
    text, _ = distill(html, 1200)
    assert "OEM 000042" in text
    assert text.index("OEM 000042") < text.index("Catalogue category")


def test_link_heavy_fitment_table_is_product_evidence_not_a_menu():
    fitment = (
        "<table><tr><th>Original code</th><td>OEM 000042</td></tr>"
        + "".join(
            f'<tr><td><a href="/car/{n}">Car {n}</a></td><td>2011–2017</td></tr>'
            for n in range(80)
        )
        + "</table>"
    )
    text, _ = distill(menu() + "<h1>Real product</h1>" + fitment, 1200)
    assert "OEM 000042" in text and "2011–2017" in text
