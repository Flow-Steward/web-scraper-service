# Changelog

All notable changes to web-scraper-service. Versions follow `SERVICE_VERSION` in
`app/settings.py` and the release tags `vX.Y.Z`. Changes to the HTTP API are listed in
`contract/CHANGES.md`.

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
