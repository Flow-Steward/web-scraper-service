# Explicit target selection wins over scope escape hints

Implement inline, review independently. Old held runs stay paused.

Live evidence: the combined three-category run received the exact configured crawl_include and url_include, but visited steering, fuel and cabin categories. Its site mapping used a broad product URL pattern, which qualified these unrelated categories as targets allowed outside navigation scope. The fresh run was stopped with results retained to avoid wasted proxy traffic.

- [x] Reproduce with generic mapping and learned-shape hints:2 failing cases,3 passing baseline cases.
- [x] Make explicit compiled url_include authoritative in runtime and setup target hints. Preserve inside-scope navigation and selected targets outside scope; keep existing inference when no explicit selection exists. A separate setup regression was RED before fixing its shared pattern planner.
- [x] Final unit, scope/discovery integration and API sample-contract checks:953 passed. Independent review found no remaining important issues after the learned-shape fix.
- [ ] Draft PR and controlled demo deployment after the fresh run has stopped; restart only a new full run.

Initial verification passed950 checks. Review found one remaining learned-shape bypass in backlog/trap target accounting; both selected-new-shape and excluded-learned-shape cases were reproduced RED before applying the same explicit-selection priority there. Final verification follows.
