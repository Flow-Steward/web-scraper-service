# Changelog

All notable changes to web-scraper-service. Versions follow `SERVICE_VERSION` in
`app/settings.py` and the release tags `vX.Y.Z`. Changes to the HTTP API are listed in
`contract/CHANGES.md`.

## 0.2.2-dev.13 (unreleased)

- Browser setup and runtime discovery retain native main-document GET bytes after
  protection clearance instead of rereading them through a separately gated HTTP
  client. Response ownership follows binding, request and document commit; reads
  have a finite timeout and discard bytes if the document changes while reading.
- Protection detection uses the current committed document's HTTP status after an
  automatic clearance reload, avoiding a stale initial 403 on a successful page.

## 0.2.2-dev.12

- Product/Offer jobs inspect clearly product/item-named sitemap documents before
  unrelated sources, in analysis, samples and full discovery. This changes order
  only: other documents and completeness limits remain in force. Versioned index
  ordering safely replays admission when older saved cursors used another order.

## 0.2.2-dev.11

- Sitemap-only sampling now stays within the published map, skips listing
  controls, and does not fall back to crawling or sibling-target guessing when
  a map is empty. Crawl and combined sitemap-and-crawl modes keep link discovery.

## 0.2.2-dev.10

- Fetch samples now respects `single_page`: it samples only `site_url`, requests
  one sample, and skips sitemap, sibling-target guessing and listing controls.
  Explicit sample URLs retain their existing behaviour; target filters still apply.

## 0.2.2-dev.9

- Camoufox can complete one bounded native opaque challenge form when both the
  service and run permit protection handling. Native response, body, final header,
  expiry, generation and destination checks preserve the denial of ordinary forms,
  replays and POST-preserving redirects. Setup and full crawls use the same rule;
  Chromium keeps its existing document POST denial.
- Added native HTTPS fixtures with real POST handlers and regression checks for
  final-release revocation and repeated interception of a single Firefox request.

## 0.2.1

First published release. Release notes: `release-notes/v0.2.1.md`.

- Distribution: no prebuilt image is published. `docker-compose.yml` builds the image on
  the user's machine from the release tag (`target: runtime`) and runs it on the Flow
  Steward Compact Docker network (`FS_DOCKER_NETWORK`, default `flow-steward_default`);
  `.env.example` holds the settings. Both files are attached to the GitHub Release
  (`env.example`).
- `GET /health` reports `service.version` and the commit as `service.build` (`WSS_BUILD`);
  OCI labels carry the source, version, commit and licence.
- The Camoufox browser download is checked against its pinned release digest on both
  architectures. An optional build secret `github_token` authenticates the Camoufox
  release lookup on shared CI runners.
- Run behaviour: a second start of a busy site is skipped by default (`when_site_busy`:
  skip or queue, `409 site_busy`). Politeness presets `gentle`, `normal` and `fast`.
  `page_timeout_seconds` and an automatic per-host slow-down. Unusable 200 answers (soft
  errors) are retried.
- Prices: split amounts, decimal marks by page locale, sale and regular price,
  machine-readable price checks in the mapping test, and `price_status`.
- CI runs the test suite on every push and pull request. A tag runs the tests, builds and
  starts the image natively on linux/amd64 and linux/arm64, and creates the GitHub Release.
