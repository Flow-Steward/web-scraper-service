"""Economy mode must suppress widgets without breaking external product data."""
import pytest


@pytest.mark.parametrize("url,kind,main,want", [
    ("https://embed.tawk.to/widget", "script", False, True),
    ("https://embed.tawk.to/frame", "document", False, True),
    ("https://assets.omnisnippet1.com/launcher.js", "script", False, True),
    ("https://wt.omnisendlink.com/event", "xhr", False, True),
    ("https://forms.soundestlink.com/form", "document", False, True),
    ("https://cdn-cookieyes.com/banner.js", "script", False, True),
    ("https://log.cookieyes.com/event", "fetch", False, True),
    ("https://connect.facebook.net/pixel.js", "script", False, True),
    ("https://www.clarity.ms/collect", "fetch", False, True),
    ("https://analytics.tiktok.com/pixel", "script", False, True),
    ("https://EMBED.TAWK.TO.:443/widget", "script", False, True),
    ("https://embed.tawk.to.evil.test/widget", "script", False, False),
    ("https://not-tawk.to/widget", "script", False, False),
    ("https://cdn.catalogue.test/?host=embed.tawk.to", "script", False, False),
    ("https://embed.tawk.to@cdn.catalogue.test/widget", "script", False, False),
    ("https://cdn.catalogue.test@embed.tawk.to/widget", "script", False, True),
    ("https://embed.tawk.to/product", "document", True, False),
    ("https://challenges.cloudflare.com/turnstile/v0/api.js", "script", False, False),
    ("https://challenges.cloudflare.com/frame", "document", False, False),
    ("https://cdnjs.cloudflare.com/library.js", "script", False, False),
    ("https://cdn.datatables.net/library.js", "script", False, False),
    ("https://api.other.test/product.json", "xhr", False, False),
    ("https://cdn.other.test/product.js", "script", False, False),
    ("https://catalogue.test/photo.jpg", "image", False, True),
    ("https://catalogue.test/song.mp3", "media", False, True),
    ("https://catalogue.test/font.woff", "font", False, True),
    ("https://catalogue.test/style.css", "stylesheet", False, False),
])
def test_economy_resource_admission(url, kind, main, want):
    from app.fetching.resources import should_block_resource

    assert should_block_resource(url, "https://catalogue.test/product", kind, main_document=main) is want


@pytest.mark.parametrize("page_url", ["https://tawk.to/product", "https://support.tawk.to/product", "https://TAWK.TO./product"])
def test_tracker_family_can_be_the_target_site(page_url):
    from app.fetching.resources import should_block_resource

    assert not should_block_resource("https://embed.tawk.to/product-data.js", page_url, "script")


@pytest.mark.parametrize("enabled,want", [(True, "blocked"), (False, "issued")])
async def test_route_filter_opt_out_reaches_the_terminal_consumer(enabled, want):
    from app.fetching.resources import resource_route_guard

    class Request:
        url = "https://embed.tawk.to/widget"
        resource_type = "script"
        def is_navigation_request(self):
            return False

    class Route:
        request = Request()
        disposition = None
        async def abort(self, *args):
            self.disposition = "blocked"

    async def terminal(route):
        route.disposition = "issued"

    route = Route()
    await resource_route_guard(lambda: "https://catalogue.test/product", enabled=enabled, then=terminal)(route)
    assert route.disposition == want
