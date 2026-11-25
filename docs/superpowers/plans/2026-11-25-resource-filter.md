# Browser resource economy implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Suppress optional third-party widget traffic using the existing economy flag.
**Architecture:** A shared parsed-host resource policy wraps native route consumers; private-address and document authorization remain intact. Setup receives the same existing flag as jobs.
**Tech Stack:** Python, Playwright, Camoufox, Chromium, pytest, JSON Schema.
**Spec:** `docs/superpowers/specs/2026-11-25-resource-filter-design.md`

## Global Constraints

- No supplier-specific runtime rules, new dependencies, new API fields or new UI controls.
- Preserve native challenge transport and all existing security/destination guards.
- No paid proxy, supplier requests, demo deployment or old-run resumption.
- Public examples use fictional supplier names only.

## Review Focus

- Main documents and tracker families used as first-party targets remain usable.
- DNS-label boundaries, case, terminal dots and userinfo cannot misclassify hosts.
- Iframe documents cannot bypass optional-widget filtering.
- External product data and Cloudflare dependencies continue to load.
- The existing opt-out reaches both setup and job browser consumers.

### Task 1: Shared filtering and both browser consumers

**Files:**
- Create `app/fetching/resources.py` and `tests/unit/test_resource_policy.py`.
- Modify `app/fetching/oneoff.py`, `app/setup_ops.py`, `app/jobs/runner.py`.
- Create `tests/integration/test_resource_policy.py`.
- Document the existing flag in `README.md` and `contract/schemas/job_config.schema.json`.

**Interfaces:**
- Consumes existing `block_heavy_resources: bool` (default true).
- Produces `should_block_resource(url, page_url, resource_type, *, main_document=False) -> bool`
  and `resource_route_guard(page_url, *, enabled, then)` returning an async route handler.

- [ ] Write literal unit cases for all Review Focus boundaries and watch them fail before implementation.
- [ ] Implement the pure predicate and async guard, then wire setup and job consumers.
- [ ] Add local HTTP proxy/browser tests for both engines and job collection; assert zero upstream widget hits, preserved external product data and opt-out.
- [ ] Run focused unit tests and the project CI's complete unit/integration commands; report every failure and fix applicable regressions.
- [ ] Update documentation, review the whole branch with a fresh reviewer, and commit the tested change.

Expected focused command: `python -m pytest -p no:cacheprovider tests/unit/test_resource_policy.py tests/unit/test_browser_request_guards.py tests/unit/test_setup_lifetime.py tests/unit/test_logic.py -q`.
Expected canonical CI: `scripts/test.sh tests/unit -q` and `scripts/test.sh tests/integration -q -rf`, both green in the Linux dev image.
