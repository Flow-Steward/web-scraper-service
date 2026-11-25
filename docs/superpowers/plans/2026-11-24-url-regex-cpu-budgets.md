# Charge URL regex budgets for matching CPU

Implement inline with systematic debugging, TDD and independent review.

Evidence: the full fixture snapshot at 35edef05 produced 1 failure and 1218 passes. `test_samples_mapping_test_and_tests` received pattern_timeout for an ordinary product URL mapping while discovering samples. The isolated API test passed. The wrapper charges monotonic wall time around a regex search, including process descheduling. The regex engine uses C clock() for its internal timeout (upstream src/_regex.c check_timed_out).

Goal: preserve the 20 ms search, 50 ms URL and 2 second bulk allowances, but charge the wrapper for the current thread's matching CPU rather than OS scheduling pauses. Keep engine timeouts and explicit errors. No paid tests, demo restart, old-run changes, or dependency changes.

- [x] Reproduce scheduling pauses around a real successful regex search as RED tests; both pre/post pauses failed with pattern_timeout before the fix. Preserve real pathological-pattern and shared-budget tests.
- [x] Change only wrapper clock accounting and clarify documentation. Unit suite plus the sample API contract passed945 checks. Independent review found no actionable issues.
- [ ] Run unit/consumer tests, independent review, then the complete suite from the final tree.
- [ ] Publish a separate draft PR stacked on PR #29. Leave the active demo run untouched.
