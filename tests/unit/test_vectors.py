"""Test vectors of the contract: canonical URL, record_id, webhook signature, site key."""

import pytest

from app.core.urls import canonical_url, record_id, site_key
from app.events.journal import sign

VECTORS = [
    ("https://WWW.Lumen-Shop.example:443/produkt/pendelleuchte-aurora-60?utm_source=sitemap#reviews", "product", None,
     "https://www.lumen-shop.example/produkt/pendelleuchte-aurora-60", "rec_13819b01fd3b92df84ab95fcc8715cbf"),
    ("https://www.lumen-shop.example/ersatzteile/glasschirm-opal-150", "spare_part", "A-1042",
     "https://www.lumen-shop.example/ersatzteile/glasschirm-opal-150", "rec_30246844f630ff27ce6d51d1e11ab79e"),
    ("http://www.lumen-shop.example/ersatzteile/./g9-fassung/%7e%61?b=2&a=&utm_medium=x", "spare_part", None,
     "http://www.lumen-shop.example/ersatzteile/g9-fassung/~a?a=&b=2", "rec_ddad72307321bcfc3dee3bed43d832f6"),
]


@pytest.mark.parametrize("url,template,key,canonical,rid", VECTORS)
def test_record_id_vectors(url, template, key, canonical, rid):
    assert canonical_url(url) == canonical
    assert record_id(canonical, template, 0, key) == rid


@pytest.mark.parametrize("url,expected", [
    ("https://WWW.Lumen-Shop.example:443/x", "lumen-shop.example"),
    ("http://shop.test:8601/a", "shop.test:8601"),
    ("https://bücher.example/", "xn--bcher-kva.example"),
    ("https://www.example.com./", "example.com"),
])
def test_site_key(url, expected):
    assert site_key(url) == expected


@pytest.mark.parametrize("url,expected", [
    ("HTTP://Example.COM", "http://example.com/"),
    ("https://example.com/a/../b/./c", "https://example.com/b/c"),
    ("https://example.com/a b", "https://example.com/a%20b"),
    ("https://example.com/%c3%a4", "https://example.com/%C3%A4"),
    ("https://example.com/ä", "https://example.com/%C3%A4"),
    ("https://user:pw@example.com:8443/x?gclid=1&b=1&a=2&&fbclid=z#f", "https://example.com:8443/x?a=2&b=1"),
    ("https://example.com/?q=a+b&flag", "https://example.com/?flag=&q=a+b"),
    ("https://example.com/Path/", "https://example.com/Path/"),
    ("https://example.com/?UTM_Campaign=1&_ga=2&srsltid=3", "https://example.com/"),
])
def test_canonical_url(url, expected):
    assert canonical_url(url) == expected


def test_signature_vector():
    body = ('{"event_id":"evt_01JCXF5T6V7W8X9Y0Z1A2B3C4D","event_type":"run_completed","run_id":"run_01JCXA7Q3M5VZ8K2N4P6R8T0W1",'
            '"resource_id":"run_01JCXA7Q3M5VZ8K2N4P6R8T0W1","occurred_at":"2026-11-12T10:31:10Z","delivery_generation":1,'
            '"outcome":"complete","stop_reason":"finished"}').encode()
    assert sign("fs_whsec_3b1f0e6c9a2d47f8b5c1d0e9", "1794915070", body) == "d0789b1469a56d54c6f52a16c3783f9092b2f33e81e69d84912268bc7279a1a7"
