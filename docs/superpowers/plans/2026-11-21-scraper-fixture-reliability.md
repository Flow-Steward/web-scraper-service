# Scraper fixture reliability implementation plan

> **For agentic workers:** Execute isolated issue tasks, review each change, then verify the combined branch. Use systematic debugging and regression-first tests.

**Goal:** Merge PR #16 with green CI and resolve the remaining scraper issues using synthetic fixtures without a paid proxy.

**Architecture:** Preserve the existing setup, job, queue, mapping and event boundaries. Share destination policy at fetch boundaries, persist discovery checkpoints after queue admission, and retain job state during connection failures. Keep changes generic across suppliers.

**Tech stack:** Python, SQLite, Crawlee, Playwright, FastAPI, JSON Schema, extension YAML workflows and pytest in disposable Docker.

**Spec:** Service issues #10, #11, #13, #14, #15, #17 and extension issue #3 in the Flow-Steward repositories.

## Global constraints

- No paid proxy, paid AI call or live supplier request.
- Keep the demo scraper stopped and its persistent records and queues unchanged.
- Do not change Core release work owned by the release coordinator.
- Preserve private-address checks, robots policy, cancellation and existing protection budgets.
- Use isolated worktrees and commits for independent fixes; integrate overlapping paths deliberately.
- Verify exact final commits, report targeted versus full coverage, and remove only owned disposable resources.

## Review focus

- A browser may close while a lower layer returns normally: retain the real setup failure.
- Redirected document requests must be denied before the destination receives a request.
- Failed or capped sitemap traversal cannot prove a complete empty catalogue.
- An outage or crash between traversal and queue admission must never lose pending URLs.
- Optional variant absence remains valid, while published optional values trigger bounded mapping review.

## Tasks

- [x] **PR #16:** Retain its separate head; repair the stale selector-count assertion, run its CI, verify all required checks and merge without bypassing protection. Merged as `334418c67c2a8e2648090379fa81e8cd2734a80b`; CI: 606 unit and 195 integration tests passed.
- [x] **Issue #10:** Add reusable destination policy in fetching, wire setup/job callers and test denied HTTP/browser redirect handlers receive zero requests, with allowed-hop controls. Include committed OOPIF targets and native proxy authentication, without replacing document transport.
- [x] **Issue #11:** Skip unrelated sitemap enumeration for explicit discovery modes; bound metadata work and setup lifetime; test timeout/disconnect/cancellation cleanup and expose truthful unknown counts.
- [x] **Issues #13/#17:** Track bounded source traversal and failures in discovery/runner; checkpoint after enqueue; report partial coverage; test caps, root/nested failures, valid emptiness, interruption and linked retry.
- [x] **Issue #15:** Classify upstream proxy authentication failure, stop consuming pending work, emit one actionable problem and resume with preserved queue, records and budgets; use a refusing fixture proxy. Verify real HTTP and HTTPS/CONNECT credentials, origin 401 separation and the Camoufox/Requests 407 wrapper.
- [x] **Extension issue #3:** Exercise real workflow refinement against actual service extraction on generic CD/LP/DVD fixtures, with deterministic author output, warning repair, budget limits and last-passing fallback. PR #7 merged as `25a10d953df554633dfa5e6a85ac379f66a71a46`; CI: 626 extension tests passed (6 intentionally skipped), 40 repository workflow checks passed. Independent cross-repository acceptance and contract review: 26 passed.
- [ ] **Integration gate:** Component diffs reviewed, overlaps resolved and service/extension schemas synchronized. Publish the reviewed PR, finish related Docker regressions and full repository CI, then merge and close only verified issues.
- [ ] **Cleanup:** Retain final SHA/test receipts, remove owned fixture images/containers/context, archive merged worktrees through the native tool and leave demo data untouched.

This plan records the pre-publication checkpoint. Final CI, merge and resource-cleanup receipts belong to the task's verification artifacts and issue comments, rather than requiring another source change after testing.
