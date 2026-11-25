"""One opt-in native form allowance, never an authorization for general POSTs.

Headers and an opaque form shape are heuristics, not proof of vendor identity.
The native Firefox addon validates the body and the final wire headers; no form
values cross its authenticated policy bridge.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

MAX_FORM_BYTES = 65_536
FORM_TTL_SECONDS = 45


class ChallengeFormAllowance:
    def __init__(self, url: str, enabled: bool = False):
        self.url = url
        self.enabled = enabled
        self.request_id = None
        self.observed = False
        self.used = False
        self.revoked = False
        self.deadline = 0.0
        self.reservation = None
        self.reserved_bytes = None
        self.released = False

    def revoke(self):
        self.revoked = True
        self.reservation = None

    def get_started(self, request_id):
        if self.request_id is None:
            self.request_id = request_id
        elif self.request_id != request_id:
            self.revoke()

    def observe(self, url, request_id, status, headers):
        if status == 407:  # Native proxy authentication can precede the origin response.
            return False
        if self.observed or self.used or self.revoked or request_id != self.request_id:
            return False
        self.observed = True
        if (not self.enabled or url != self.url or urlsplit(url).scheme != "https"
                or status != 403 or headers.get("server") != "cloudflare"
                or headers.get("cf-mitigated") != "challenge" or not headers.get("cf-ray")):
            return False
        self.deadline = time.monotonic() + FORM_TTL_SECONDS
        return True

    def reserve(self, url, request_id, form_bytes):
        if (type(form_bytes) is not int or not 0 < form_bytes <= MAX_FORM_BYTES
                or not isinstance(request_id, str) or not request_id):
            return False
        # Firefox's native interception may report the same channel again after
        # Playwright continues it. The addon also compares its retained encoded
        # body and permits final wire headers only once for this request ID.
        if self.used:
            return (url == self.url and form_bytes == self.reserved_bytes
                    and self.can_release(request_id))
        if (not self.enabled or self.revoked or url != self.url
                or time.monotonic() >= self.deadline):
            return False
        # Burn before any asynchronous destination check. Refusal or cancellation
        # cannot turn a failed reservation into a second POST opportunity.
        self.used = True
        self.reservation = request_id
        self.reserved_bytes = form_bytes
        return True

    def can_release(self, request_id):
        return (self.used and not self.released and not self.revoked and self.reservation == request_id
                and time.monotonic() < self.deadline)

    def release(self, url, request_id, form_bytes):
        if (url != self.url or type(form_bytes) is not int or form_bytes != self.reserved_bytes
                or not self.can_release(request_id)):
            return False
        self.released = True
        return True
