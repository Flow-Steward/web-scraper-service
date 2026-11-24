# Native challenge form completion

> Implement inline with superpowers:executing-plans; final review through requesting-code-review.

**Goal:** Let an explicitly permitted Camoufox protection check complete its native opaque form while ordinary document POSTs remain blocked.

**Evidence:** A live HTTPS main-frame GET returned 403 with `server: cloudflare`, `cf-mitigated: challenge`, and `cf-ray`. Its challenge document attempted a same-URL POST with three opaque hidden fields named by 64 lowercase hexadecimal characters. The native guard rejected it before network issuance. Proxy authentication and geography were independently checked. No solver is involved.

**Architecture:** A one-use, short-lived allowance belongs to one Firefox document-policy generation. The native blocking response listener arms it before releasing a matching challenge response. Only a bounded URL-encoded opaque form to the exact HTTPS initial URL can reserve it. Native request identity and generation also bind header validation; all existing destination checks still apply. Response headers and form shape are heuristics, not proof of vendor provenance or absence of origin-side effects. Chromium keeps its existing POST denial; extending it would require separate native request-stage and preserved-method redirect coverage.

- [x] Add a native HTTPS fixture with a real POST handler; observe the regression fail.
- [x] Implement and unit-test one-use allowance, expiry, non-rearming, cancellation, generation, exact URL and malformed metadata refusal.
- [x] Bind Firefox native response/request/header observations without exposing form values. Check actual encoded size and Content-Type before release. Reject replay and preserved-method redirects.
- [x] Propagate the existing server-and-run protection permission from setup and crawl paths; default off.
- [x] Run focused native fixtures and existing form/redirect/private-destination regressions. Assert actual positive and zero forbidden POST hits. Targeted set: 109 passed; additional final unit cases run separately.
- [ ] Obtain fresh review, preserve evidence, then bounded live tests before deployment/full collection. Old held runs remain unchanged; solver stays off.

**Cleanup:** Disposable test containers use the task resource journal and borrowed runtime image. Source enters the container through stdin into tmpfs, with no remote checkout or image build. Keep sanitized receipts and remove disposable resources before handoff.
