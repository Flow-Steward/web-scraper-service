"""Resolve native redirects only where Python and browser URL syntax agree.

Browsers use WHATWG authority parsing, including treating backslashes as HTTP
slashes. Refuse ambiguous raw spellings before RFC-style urljoin/urlsplit can
hide their actual browser destination. Keep properly encoded URL identity.
"""
from urllib.parse import urljoin, urlsplit


def native_redirect_target(base, location):
    if (not isinstance(location, str) or not location or len(location) > 8192
            or '\\' in location or any(ord(c) <= 32 or ord(c) == 127 or c.isspace() for c in location)
            or location.startswith('///')):
        return None
    try:
        raw = urlsplit(location)
        if raw.username is not None or raw.password is not None:
            return None
        if raw.scheme:
            # Reject http:host, http:/host and http:////host. Native browsers
            # can reinterpret these using their base URL and authority rules.
            tail = location[len(raw.scheme) + 1:]
            if raw.scheme.lower() not in ('http', 'https') or not tail.startswith('//') or tail.startswith('///'):
                return None
        joined = urljoin(base, location)
        target = urlsplit(joined)
        if (target.scheme.lower() not in ('http', 'https') or not target.hostname
                or target.username is not None or target.password is not None):
            return None
        target.port  # Validate the complete authority, not just its hostname.
        return joined
    except (ValueError, UnicodeError):
        return None
