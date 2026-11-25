# Browser Table Acquisition Implementation Plan

> **For agentic workers:** Execute inline. Root approved this scoped plan; no new subagents.

**Goal:** Acquire complete settled paginated applicability tables through an opt-in generic source.

**Architecture:** A bounded collector observes the existing guarded native tab and captures ordered rows across in-page pagination. Original product HTML and variant behavior remain unchanged. Fingerprinted captures persist for offline mapping tests and stored re-extraction.

**Tech Stack:** Python, Playwright, existing JSON Schema contracts and pytest synthetic fixtures.

**Spec:** Root-approved design in the issue #33 task handoff.

## Global Constraints

No supplier network calls, paid proxy runs, historical run resumes, Core changes, deploys, pushes or merges. Synthetic example.test hosts only. Preserve native guards and existing variant behavior. Full document pagination is unsupported and explicitly incomplete.

## Review Focus

Loading placeholders must not prove emptiness; repeated rows must survive adjacent pages; missing captures must not fall back to specification years; serialization must respect UTF-8 limits before IPC; source configuration changes invalidate captures.

### Task 1: Collector and contract

- [x] Write synthetic source and browser tests and verify RED.
- [x] Add bounded native DOM collector and capture helpers; add opt-in HTML source contract.
- [x] Verify focused tests GREEN.

### Task 2: Runtime and offline persistence

- [x] Integrate matched runtime mappings, samples capture opt-in, offline mapping tests and stored re-extraction.
- [x] Verify focused runtime and persistence regressions.
- [x] Document synthetic mapping and limitations.
- [x] Review diff and provide commit, test and resource receipts to root for full suite.

Verification: 335 focused local unit/contract regressions and 20 local Chromium tests passed. Root coordinates the full suite; no push, deployment, merge, supplier calls, paid proxy use or historical resumes occurred.
