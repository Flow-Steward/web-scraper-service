# Keep explicitly excluded navigation separate from target response history

Implement inline with TDD and independent review. Do not interrupt the active demo run.

Live evidence: two selected-category listing URLs were retried with lost_template after25 successful product pages. They remain queued while the product queue advances. The URL shape intentionally collapses slug segments, so categories and products may share a shape. Classification already determines that a URL is outside the explicit extraction selection, but response-history heuristics still compare it with product history, discard its discovered links and retry it. Accepting a listing into that same history would also disable lost-template detection for later real products.

Goal: separate response-size, closing-tag and target-mark history by explicit URL selection. Keep malformed response checks, selected-product missing-selector retries, URL-list and single-page semantics. This does not suppress navigation_partial warnings or claim full listing coverage.

- [x] Reproduce real classification after warming product history: three regressions failed before the fix; genuine empty response and selected missing-selector baseline cases passed.
- [x] Separate private in-memory history keys by selection cohort; preserve genuine malformed-response and missing-selector behavior. Independent review found no important issues.
- [ ] Run unit and robustness/navigation integration fixtures, independent review and full suite. Publish a separate draft PR, no main merge.
- [ ] Deploy only after the current fresh full run ends; old held runs remain paused.
