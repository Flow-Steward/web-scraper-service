# Productive listing navigation implementation plan

> **For agentic workers:** Use superpowers:executing-plans for inline implementation. The user has already asked for autonomous fixes and fixture/demo verification without stopping for design questions.

**Goal:** Finish productive same-URL catalogue pagination without spending one page's timeout on the entire catalogue.

**Architecture:** Retain the existing destination guards and finite action limits. Runtime pagination renews a per-transition timeout only when new links appear; deliberate host pacing runs outside that transition timeout. The overall job duration and page budget remain authoritative. Setup operations retain their existing bounded lifetime. A real progress hook keeps the stall watchdog accurate, and the outer crawler handler must allow the configured job lifetime rather than imposing another shorter catalogue deadline.

**Tech stack:** Existing Python/asyncio, Playwright and Crawlee. No new dependencies or site-specific logic.

**Spec/evidence:** Liberty Wines `/our-producers` has 24 same-URL JS pages. The prior direct smoke run performed 18 actions before its 60-second wall-clock navigation timeout, including its configured 2–5-second politeness waits. Required outcome: a small extension run with preserved vintages, CSV/JSONL, no timeout warning and four old paused runs unchanged.

## Constraints

- No paid proxy/solver calls; no old run Resume. Keep the existing approved Direct profile and unchanged extension release.
- Policy callbacks and browser/DOM stalls remain bounded. No suppression of real incomplete coverage.
- Preserve robots, same-site/private-destination checks, cancellation and no-progress/loop detection.
- Global max_pages and max_duration remain hard bounds; no hidden 100-action runtime truncation.

## Review focus

Paced finite pagination longer than one page timeout; a hung policy after real progress; a permanently live next control without new links; safe cancellation during pacing; the outer handler/watchdog ending productive navigation prematurely.

## Tasks

- [x] Add a finite paced listing regression and run it against the old implementation (RED). Cover the hard timeout of a hung callback after progress, global action bounds and cancellation during pacing.
- [x] Add progress timeout / pacing / progress hooks to `app/fetching/navigation.py`, explicitly retaining setup's total-budget behavior. Real-browser navigation regressions passed.
- [x] Wire runtime hooks and remaining page allowance in `app/jobs/runner.py`; keep handler lifetime consistent with the job duration and refresh watchdog only for real link progress. Add unit/integration evidence and update README timeout semantics.
- [ ] Run unit plus affected browser/runtime fixture suites in an owned isolated Docker environment. Review the frozen change and correct any material finding with regression coverage.
- [ ] Commit and publish the fix for review; deploy the verified immutable service source to the demo environment with the same data volume and paused states. Do not reinstall the unchanged extension or invalidate its approval.
- [ ] Run one new bounded Liberty Wines workflow through the approved no-proxy extension profile. Verify callback, CSV/JSONL bytes, no navigation timeout warning, global health and unchanged old paused runs. Save receipts and clean up only owned disposable check resources.

## Verification ledger

- Baseline: 870 unit tests passed in an isolated network-none Docker container.
- RED: old wall-clock collector retained only four of nine links in the paced finite browser fixture.
- Browser/runtime checks: 918 tests passed, including the new full paced-catalogue runtime fixture; a handler-initialization regression remained and was corrected.
- Independent review caught configured-auto plus persisted resolved-sitemap handling. A separate RED reproduced the short handler (215 instead of at least 600 seconds); the corrected guard explicitly preserves future link crawling for auto mode.
- Corrected whole-patch review: no remaining findings. Fresh unit gate: 875 passed. Full integration gate and deployment/live acceptance remain pending at this checkpoint.
