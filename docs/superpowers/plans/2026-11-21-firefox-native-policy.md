# Firefox native document policy implementation plan

**Goal:** Block disallowed native redirect destinations before issuance while preserving Camoufox transport.

**Architecture:** Launch-owned loopback listener and unpacked MV2 extension. Async blocking document listener calls the supplied Python policy; one-use about:blank nonce binds each tab to a Python page/context/generation. Main frames receive caller policy and private checks; subframes receive private checks only.

**Constraints:** No live suppliers, paid proxies, deployment, or local Docker. Unknown tabs, stale generations, bridge failures and cancellation deny. Existing static resource routing remains. Root approved bounded prototype and inline execution.

- [ ] Write failing bridge tests: auth/schema, nonce one-use, context/tab/generation identity, cancellation, cleanup.
- [ ] Implement bounded FirefoxPolicyBridge, persistent unpacked addon, readiness and teardown.
- [ ] Implement FirefoxDocumentGuard start/request/rebind/errors/close with native browser transport.
- [ ] Hook launch_camoufox; preserve addon overrides; clean failed launches.
- [ ] Add fixture Camoufox redirect/private subframe/parallel context tests.
- [ ] Run fast unit and syntax checks; return fixture commands and commit for root Docker gate.

Review focus: private extension readiness, nonce mismatch, stale allow after rebind, close with paused callbacks, redirect limits independent of iframe count.
