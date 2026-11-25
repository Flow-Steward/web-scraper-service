# Single solver token completion path

Implement inline; review independently before publication. Do not enable a live paid solver or restart the demo during its active full crawl.

The complete fixture suite on c8ecb907 finished1214 passed/1 failed: test_solver_passes_and_is_counted lost its first record. Standalone passed. Code inspection found token injection calls the callback and submits its surrounding form in the same evaluation. A deterministic real-browser fixture reproduced a verification POST immediately raced by a default GET form navigation, ending403 instead of a collected page.

- [x] Reproduce on a local gate without proxy or provider API: callback POST and competing form GET, first page remains blocked.
- [x] Prefer a configured callback and retain form fallback only when no callback exists; do not change route budgets or protection permissions.
- [x] Reproduce Camoufox's isolated-world callback absence and use its explicit main-world evaluation only for token delivery. The four real-browser callback/fallback cases now pass; the existing protection sequence is still running.
- [x] Verify callback and no-callback cases in both engines and existing protection sequence:13 passed. All934 unit tests passed; actual stopped-container exit code verified.
- [x] Independent review found no important findings.
- [ ] Full verification and draft PR. Active demo remains dev13 until its collection can safely finish.

Camoufox's documented main_world_eval option permits an explicit mw: expression; it does not move ordinary evaluate calls out of their isolated world: https://camoufox.com/python/main-world-eval/ .
