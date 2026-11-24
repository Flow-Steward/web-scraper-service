# Recollect unfinished durable queues after failure

Implement inline with TDD and independent review. Preserve the failed parent; never resume old held runs.

Evidence: the new Partecha run failed on the old wall-clock regex guard at594 records. Its durable URL table retains2709 queued products and3 queued navigation pages, while unextracted is empty and sitemap cursors already mark162 source documents complete. Existing Recollect reads only unextracted plus unfinished sitemap sources, so it would omit already-admitted work. It also switches to sitemap-only mode, losing the parent's category-link traversal.

Requirements:
- Default stopped-page recovery includes unfinished durable URL/backlog work of a failed parent. Explicit reason filters excluding stopped do not include it.
- Preserve source keys and navigation-vs-explicit-list admission, skip completed requests/backlog duplicates, keep pending refetches.
- A crawl/combined parent continues following links. Persist the recovery queue before scheduling and commit offsets only with enqueue.
- The parent and its records remain immutable. Current approved profile, active mappings, normal URL guards and idempotency apply.
- No supplied product inventory, paid fixture calls, Core edits or old-run Resume.

Steps:
- [x] Reproduce missing queued/backlog work and navigation loss. Three original regression cases were RED. Review found refetch, source-key prefilter and idempotency gaps; five further failures were reproduced before fixing them.
- [x] Add private durable recovery seed and normal feeder replay; preserve source continuation, pending refetches, ancestor identity keys and prior idempotency replay. Independent closure review found no important issues. Final broad verification follows.
- [ ] Targeted plus full verification, independent review, separate draft PR.
- [ ] Controlled demo update after verification, then use the approved extension continuation workflow for this fresh failed run only.

Final unit suite plus HTTP CSV/Recollect verification:964 passed. A prior frozen broader snapshot reported8 test-double AttributeErrors after ancestor filtering was extracted into a helper; the sitemap fixture now delegates to that real helper without weakening its cursor/ancestor assertions. The complete final-tree suite is running separately.
