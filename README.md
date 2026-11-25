# web-scraper-service

The scraper service of the Flow Steward **Web Scraper** extension: a separate
container (Python, [Crawlee for Python](https://github.com/apify/crawlee-python) 1.11.2)
that runs a whole crawl job by itself — discovery, fetching with HTTP or a browser,
proxy tiers, challenge handling, declarative extraction mappings — and returns one
JSON Lines result file. The extension `flowsteward.web-scraper` calls it over HTTP.

| Path | Content |
|---|---|
| `contract/` | The binding API contract (README, OpenAPI 3.1, JSON Schemas, examples) |
| `app/` | The service (FastAPI + Crawlee) |
| `tests/` | Unit tests, integration tests, the local fixture world (`tests/fixture/server.py`) |
| `Dockerfile` | The service image (plain Python base, Playwright Chromium, Camoufox, Xvfb); linux/amd64 and linux/arm64 |
| `Dockerfile.server` | Alternative linux/amd64 image from `apify/actor-python-playwright-camoufox` (pinned by digest) + Chromium |
| `docker-compose.yml`, `.env.example` | Build and run the service next to a Flow Steward installation (see below) |
| `.github/workflows/` | CI (tests on every push and pull request) and Release (build checks and GitHub Release on a tag) |
| `release-notes/`, `CHANGELOG.md` | Release notes per version and the change log |
| `NOTICE` | Third-party licences |

Version **0.2.1**. `GET /health` reports it as `service.version`.

## Run with Docker Compose (recommended)

You build the image on your own machine from this public repository; no prebuilt image is
published. Two files are all you need:

* `docker-compose.yml` — builds the service from the release tag and runs it;
* `.env.example` — the settings, copied to `.env`.

1. **Download the two files** into an empty folder, from `main` or from the
   [latest release](https://github.com/Flow-Steward/web-scraper-service/releases/latest)
   (assets `docker-compose.yml` and `env.example`):

   ```bash
   mkdir web-scraper && cd web-scraper
   curl -fsSLO https://raw.githubusercontent.com/Flow-Steward/web-scraper-service/main/docker-compose.yml
   curl -fsSL -o .env https://raw.githubusercontent.com/Flow-Steward/web-scraper-service/main/.env.example
   # or the release assets:
   # curl -fsSLO https://github.com/Flow-Steward/web-scraper-service/releases/download/v0.2.1/docker-compose.yml
   # curl -fsSL -o .env https://github.com/Flow-Steward/web-scraper-service/releases/download/v0.2.1/env.example
   ```

2. **Fill `.env`.** Set the two required values (one command each generates them) and keep
   a copy of the secrets key outside the server:

   ```bash
   openssl rand -hex 32   # WSS_API_TOKEN: you type it into the Flow Steward scraper profile
   openssl rand -hex 32   # WSS_SECRETS_KEY: encrypts job secrets at rest
   ```

   `FS_DOCKER_NETWORK` is the Docker network of your Flow Steward Compact installation,
   `<compose project>_default`. A standard install uses the project `flow-steward`, so the
   default `flow-steward_default` fits; another project name is in the installation's
   `.flow-steward/compose.env` (`FS_COMPOSE_PROJECT_NAME`), and `docker network ls` lists
   the networks.

3. **Build and start:**

   ```bash
   docker compose up -d
   ```

   The first start builds the image: about 10–15 minutes and about 6 GB of disk (two
   browsers). The build downloads the Camoufox browser from the Camoufox project's official
   release on GitHub and checks it against a pinned digest. Give the machine 3 GB of
   memory for the service (the file sets `mem_limit: 3g` and a 1 GB `/dev/shm`).

4. **Check it:**

   ```bash
   docker compose ps                     # "healthy" after about a minute
   docker compose exec web-scraper python -c "import os,urllib.request as u;print(u.urlopen(u.Request('http://127.0.0.1:8080/health',headers={'Authorization':'Bearer '+os.environ['WSS_API_TOKEN']})).read().decode())"
   ```

   `/health` reports `service.version` `0.2.1` and both browsers (`chromium`,
   `camoufox`) with `present: true`. Every route, `/health` included, needs
   `Authorization: Bearer <token>`; without it the answer is `401`.

5. **In Flow Steward:**
   1. Allow private addresses once, from the installation folder:
      `./flow-steward config set FS_ALLOW_PRIVATE_REMOTE_URLS=1`, then
      `./flow-steward config apply`. The service's address on the Docker network is a
      private address, which Flow Steward refuses by default.
   2. Install the **Web Scraper** extension and enable it.
   3. Create a **scraper profile**: service URL `http://web-scraper:8080`, the token from
      `.env`.
   4. **Approve use of secrets** on the profile, then **Test service**.
   5. Add the **Scraper events** webhook and put its internal URL and signing secret into
      the profile (the extension's guide, *Setup checklist*, has every field).

**Update:** download the `docker-compose.yml` of the new release (it names the new tag and
image), then `docker compose up -d`; the data volume is kept. `docker image prune` removes
the old image.

### Host port instead of the Flow Steward network

When the service cannot join Flow Steward's Docker network (Flow Steward on another
machine, or a Scalable installation), publish a port instead. Put this
`compose.override.yaml` next to `docker-compose.yml` (Compose reads it by itself):

```yaml
services:
  web-scraper:
    ports:
      - "8080:8080"
    networks: !override [default]
networks: !reset {}
```

(`!override` and `!reset` need Docker Compose 2.24 or later.)

Then use `http://<host address>:8080` as the service URL (from Flow Steward in Docker
Desktop on the same machine: `http://host.docker.internal:8080`). The platform needs
`FS_ALLOW_PRIVATE_REMOTE_URLS=1` here as well whenever that address is private. The port is
open on every interface of the host and protected only by the token: limit it with a
firewall, or bind it to one address (`"10.0.0.5:8080:8080"`). The webhook URL in the
profile must then be an address of Flow Steward that the service can reach.

### Without Compose

```bash
docker build --target runtime --build-arg WSS_VERSION=0.2.1 \
  -t web-scraper-service:0.2.1 https://github.com/Flow-Steward/web-scraper-service.git#v0.2.1

docker run -d --name web-scraper --restart unless-stopped \
  --shm-size 1g --memory 3g --stop-timeout 90 \
  --log-driver json-file --log-opt max-size=20m --log-opt max-file=5 \
  --env-file .env --network flow-steward_default \
  -v web-scraper-data:/data web-scraper-service:0.2.1
```

Keep the log options (see "Disk use and clean-up").

## Development

Build from a checkout (native for the machine's architecture):
`docker build --target runtime -t web-scraper-service:local .`

Tests run only in the container (never on the host):

```bash
scripts/test.sh tests/unit -q          # pure logic and the contract examples/vectors
scripts/test.sh tests -q               # everything, incl. integration against the fixture world
```

`scripts/test.sh` takes a Docker lock when `WSS_DOCKER_LOCK` is set (see
`scripts/dock.sh`), mounts the tree read-only into `web-scraper-service:dev-next` (override
with `WSS_TEST_IMAGE`; `WSS_TEST_CPUS` sets the container's CPUs, default 4) and maps the
fixture host names to 127.0.0.1. CI runs the same script on every push to `main` and every
pull request.

Images (one multi-stage `Dockerfile`, every `docker` call through `scripts/dock.sh`):

```bash
scripts/dock.sh docker build --target runtime -t web-scraper-service:rc .        # the service only
scripts/dock.sh docker build --target dev -t web-scraper-service:dev-next .      # + test dependencies
scripts/lock.sh                                                                  # requirements.in -> requirements.lock (hashes)
```

## Releases

Push a tag `vX.Y.Z` equal to `SERVICE_VERSION` in `app/settings.py`, with
`release-notes/vX.Y.Z.md` in the tagged commit and `docker-compose.yml` building that
tag. The Release workflow runs the tests, builds the runtime image natively on
`ubuntu-24.04` (linux/amd64) and `ubuntu-24.04-arm` (linux/arm64), starts each image once
(`GET /health`: version, build, both browsers) and creates the GitHub Release from the
notes file with `docker-compose.yml` and `env.example` (a copy of `.env.example`)
attached. No image is published: users build it from the tag. The image labels carry the
source, version, commit (`org.opencontainers.image.revision`) and licence; the commit is
also `WSS_BUILD`.

## Configuration (service operator)

Environment variables, all optional except the first:

| Variable | Default | Meaning |
|---|---|---|
| `WSS_API_TOKEN` | — (required) | Bearer token of the API |
| `WSS_SECRETS_KEY` | unset | Key that encrypts job secrets at rest (AES-256-GCM): 32 bytes URL-safe base64, or a passphrase ≥ 16 chars. Without it `POST /v1/runs` and resume answer `503 secrets_key_unavailable`. A changed key puts running jobs into `waiting_for_credentials` until Resume. Explicit upstream proxy authentication rejection also pauses for Resume after credentials or IP allow-list approval, preserving pending work and budgets |
| `WSS_DATA_DIR` | `/data` | The one volume: `service.sqlite3`, `jobs/<run_id>/crawlee` (Crawlee storage per job), `results/`, `samples/`, `identities.json` |
| `WSS_PORT`, `WSS_HOST` | `8080`, `0.0.0.0` | Listen address |
| `WSS_ALLOW_CHALLENGE_CLICK` | `0` | Allow the model-free checkbox click and the bounded native Camoufox challenge form described under Protection; the run must also request `challenge_click` |
| `WSS_ALLOW_SOLVERS` | `0` | Allow paid challenge solvers (route step `solver`) |
| `WSS_ALLOW_ROBOTS_OVERRIDE` | `0` | Allow `respect_robots: false` |
| `WSS_LIMIT_<NAME>` | contract defaults | Every limit of `GET /health` → `limits`, e.g. `WSS_LIMIT_MAX_PAGES_PER_JOB`, `WSS_LIMIT_MAX_CONCURRENT_JOBS`, `WSS_LIMIT_MAX_RESULT_BYTES` (default 103,809,024 = 99 MiB). Values above the contract ceilings are refused at start |
| `WSS_RETENTION_RESULT_DAYS` / `_JOB_RECORD_DAYS` / `_STORED_PAGES_DAYS` / `_SAMPLES_HOURS` | 14 / 30 / 7 / 24 | Retention (see "Disk use and clean-up") |
| `WSS_RETENTION_INTERVAL_SECONDS` | 60 | How often the automatic sweeper runs |
| `WSS_STALL_SECONDS` | 900 | Stall watchdog: no progress this long → `problem` `stalled`; twice as long → the run stops `stalled` (0 = off) |
| `WSS_CLAIM_TIMEOUT_SECONDS`, `WSS_MAPPING_REQUEST_TIMEOUT_SECONDS` | 21600, 3600 | Timeouts (minimum 60) |
| `WSS_REPAIR_WINDOW_PAGES`, `_MIN_PAGES`, `_NO_TEMPLATE_PAGES`, `_BLOCK_WINDOW_PAGES`, `_BLOCK_SHARE` | 50, 10, 5, 50, 0.8 | Repair and block-rule triggers |
| `WSS_WEBHOOK_SCHEDULE_SECONDS`, `WSS_WEBHOOK_REPEAT_SECONDS`, `WSS_WEBHOOK_JITTER`, `WSS_WEBHOOK_TIMEOUT_SECONDS` | `0,10,30,60,120,300,600,1200,1800,3600`, 3600, 0.1, 10 | Delivery back-off |
| `WSS_CHALLENGE_WAIT_SECONDS` | 30 | Route step `wait`: up to this long for the challenge to clear by itself (stops as soon as it does), then the click |
| `WSS_CHALLENGE_CLICK_WAIT_SECONDS` | 15 | After the one click on a widget: how long the page is polled (every second) before the click counts as failed |
| `WSS_NAVIGATION_TIMEOUT_SECONDS` | 60 | Page timeout of a run whose configuration has no `page_timeout_seconds` (the politeness preset normally sets it) |
| `WSS_SETUP_TIMEOUT_SECONDS` | 180 | Overall analysis/samples work budget, 5–1800 s; separate from the per-page timeout. Caller disconnect/cancellation stops work sooner and closes request resources |
| `WSS_BROWSER_HEADLESS` | `0` | Run the browsers headless. By default they run headed on the image's virtual display (Xvfb), which the protection route relies on; set `1` only where no display can run |
| `WSS_BREAKER_FAILURES` | 5 | Circuit breaker per host: this many server errors, timeouts or network errors (or unusable answers) in a row pause the host (1–1000) |
| `WSS_BREAKER_PAUSE_SECONDS` | 30 | Length of the first pause; it doubles with every further pause (0–3600) |
| `WSS_BREAKER_MAX_PAUSES` | 3 | Pauses without a success in between before the run stops with `stop_reason: site_unavailable` (0–100) |
| `WSS_SITEMAP_OUTDATED_MIN`, `WSS_SITEMAP_OUTDATED_SHARE` | 20, 0.3 | "Outdated sitemap": when at least this share of at least this many fetched sitemap URLs answer 404/410, a `problem` `sitemap_outdated` is sent once and `auto` discovery adds the link crawl |
| `WSS_COMPARE_DROP_SHARE` | 0.2 | Run comparison: warn when a run has more than this share fewer records than the previous full run of the site |
| `WSS_SUPERVISOR_INTERVAL_SECONDS` | 0.5 | How often each running job checks its stop, time limit and completion (0.05–10) |
| `WSS_BUILD` | the image's commit | Free text reported as `service.build` by `GET /health`; a build with `--build-arg WSS_REVISION=<commit>` (the release workflow) sets it to that commit |
| `WSS_IP_ECHO_URL`, `WSS_GEO_LOOKUP_URL` | ipify, ipinfo | Used by `POST /v1/proxies/test` |
| `WSS_UNSAFE_ALLOW_PRIVATE_TARGETS` | `0` | **Tests only**: lets jobs target loopback/private hosts (the local fixture site). Never set in production |
| `WSS_DENIED_TARGET_HOSTS` | empty | Comma-separated host names always refused as crawl/sample/analysis targets (also when private targets are allowed) |
| `WSS_WEBHOOK_ALLOW_PRIVATE` | `1` | Webhook deliveries to private, loopback and plain-http addresses (the platform on a Docker network). The webhook is not a crawl target; redirects are never followed. `0` refuses private webhook URLs (event parked with `webhook_private_address_refused`) |
| `WSS_LOG_LEVEL` | `INFO` | Log level. Logs pass a redaction filter (proxy credentials in URLs, bearer tokens, secret JSON fields) |

## Disk use and clean-up

Everything the service keeps is on the one volume (`WSS_DATA_DIR`, `/data`):

| What | Where | Kept for (automatic sweeper) |
|---|---|---|
| Run working files (Crawlee storage, stored pages) | `jobs/<run_id>/` | `WSS_RETENTION_STORED_PAGES_DAYS` (7) after the run finished |
| Result files | `results/*.jsonl` | `WSS_RETENTION_RESULT_DAYS` (14) after the run finished |
| Run records (runs, records, events …) | `service.sqlite3` | `WSS_RETENTION_JOB_RECORD_DAYS` (30) after the run finished |
| Samples and protection screenshots | `samples/`, `samples/shots/` | `WSS_RETENTION_SAMPLES_HOURS` (24) |
| Database and its write-ahead log | `service.sqlite3`, `-wal`, `-shm` | Always; shrinks only by compaction |
| Browser identity cache | `identities.json` | Up to 1,000 entries; least recently used entries are replaced |

The sweeper runs every `WSS_RETENTION_INTERVAL_SECONDS` (60) and never touches a run that
is not finished. Each pass is recorded (`last_cleanup` below).

Identity cache format 2 derives proxy identifiers from public routing fields,
including session-bearing usernames, excluding URL passwords, paths, query strings
and fragments. This also applies to `custom_list` tiers. On the first upgrade,
legacy session keys and ambiguous tier-scoped format-1 entries are removed and
recreated as needed. A site may require another protection challenge after that
one-time fingerprint change. Direct/endpoint-only entries and format-2 public
identities retain their cache entries.

Two routes (same bearer token; contract README 16):

* `GET /v1/admin/storage` — bytes, item counts and the oldest item per kind, runs by
  state, the retention settings, total and free bytes of the volume, the last automatic
  and manual clean-ups.
* `POST /v1/admin/cleanup` — removes finished runs' working files, result files or whole
  run records older than N days, samples and screenshots, and compacts the database
  (WAL checkpoint always; VACUUM only when no run is active). `dry_run` is the default.
  A result the platform has not downloaded yet is kept unless the body says
  `include_unfetched: true`.

```bash
curl -s -H "Authorization: Bearer $WSS_API_TOKEN" http://127.0.0.1:8080/v1/admin/storage
curl -s -H "Authorization: Bearer $WSS_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"run_working_files":{"older_than_days":1},"samples":{"older_than_hours":0},"compact_database":true}' \
  http://127.0.0.1:8080/v1/admin/cleanup                                     # dry run
```

**Logs.** The service logs to stdout; Docker keeps the container log. Docker Desktop's
default `local` driver rotates it, but a Linux server's default `json-file` driver does
not: the log grows until the disk is full. `docker-compose.yml` sets `json-file` with
`max-size: 20m` and `max-file: 5` (at most 100 MB). With `docker run` pass
`--log-opt max-size=20m --log-opt max-file=5`. Log options apply only when a container
is created, so re-create an existing container (`docker compose up -d`, or `docker rm` +
`docker run`) to rotate its log.

## Image composition

`Dockerfile` (built natively for linux/amd64 and linux/arm64):
`python:3.13-slim-trixie` pinned by digest, every Python package from `requirements.lock` (pinned, `--require-hashes`),
`crawlee[adaptive-crawler,parsel,playwright]==1.11.2`, `playwright==1.62.0` (Camoufox
requires < 1.63) with Chromium and its system libraries, Firefox system libraries,
`camoufox[geoip]==0.5.9` with the Camoufox browser fetched at build time (v158.0.1-beta.41:
`lin.x86_64` on amd64, `lin.arm64` on arm64), its fingerprint model and browserforge data pre-fetched,
`extruct`, FastAPI/Uvicorn, Xvfb (the browsers run headed on a virtual display from the
start) and `tini`. Runs as user `app` (uid 10001). Chromium without the headless shell
(`--no-shell`; the service runs headed). The Camoufox release is the one `camoufox==0.5.9`
pins; its asset digest is checked against the one pinned in the Dockerfile for each
architecture and a failed fetch or a different digest fails the build. The macOS-only Camoufox font set is dropped (the service
always presents Windows identities). `scikit-learn`/`scipy` stay: Crawlee imports
`sklearn` when its adaptive crawler module is imported. Target `runtime` has no test
dependencies and no tests; target `dev` adds the test dependencies (tests are mounted).

`Dockerfile.server` (amd64 only, an alternative): `apify/actor-python-playwright-camoufox:3.13-1.62.0`
pinned by digest, the same `requirements.lock`, Chromium at the same Playwright version
without the headless shell, `APIFY_DEFAULT_BROWSER_PATH` unset so Chromium does not inherit
the Camoufox binary, `tini` as PID 1, runs as the base's non-root user.

`GET /health` reports both browsers with `present` and version; a browser that is not
in the image is refused by `validate_config` (`browser_not_available`).

## How a job runs

One Crawlee crawler per job as an asyncio task, with its own storage directory,
configuration, event manager, request queue, session pool, browser pool and proxy
configuration (nothing shared; upstream issue 2301). A cap on running jobs
(`max_concurrent_jobs`, FIFO queue). SIGTERM: crawlers stop, state persists, jobs
resume on the next start; a job restarted 3 times within 10 minutes fails `crash_loop`.

* **Fetching:** `AdaptivePlaywrightCrawler` with our predictor (no double-fetch
  detection runs; a host goes to the browser after a challenge or after the browser
  repeatedly extracted pages HTTP could not). `retry_on_blocked=False`; challenge
  statuses are classified by us. Camoufox through our own plugin/controller
  (`AsyncNewContext` with a fingerprint preset per session and the session's proxy);
  Chromium through Crawlee's plugin with our controller (one fingerprint per session).
  `fetch_mode: http` uses a plain `ParselCrawler`.
* **Discovery:** robots.txt, sitemaps (index, gzip, text; our parser; every nested sitemap
  checked against the private rule) and the start page for the `auto` rule are fetched
  **through the job's crawler** as auxiliary requests, so they use the proxy tiers, the
  job headers, the sessions and the challenge route (a site that challenges plain HTTP is
  read through the browser session after clearance). Link crawl inside the site key with
  target-looking links (an active mapping's `url_patterns`, `url_include`) at the front
  of the queue; `url_include` decides what is extracted, not what is followed; `url_list`
  with keys; robots.txt honoured by our own check (counts `urls_skipped.robots`).
  Configured URL patterns retain Python regular-expression syntax, including lookaround
  and backreferences. Matching is limited to 20 ms per pattern, 50 ms per URL-policy
  evaluation and 2 seconds of matching work per synchronous bulk operation. Wrapper
  allowances use the matching thread's CPU time; process scheduling pauses, parsing
  and network time do not consume them. The engine's search timeout remains enabled.
  A limit raises `pattern_timeout`
  with the configuration field and pattern identifier; a running job fails explicitly
  instead of silently omitting URLs. These limits do not cover extraction transforms.
* **Job record and SQLite:** the follower applies page outcomes in one synchronous
  transaction per batch (no `await` inside a transaction, enforced by `Database.tx()`);
  links and refetches go through a backlog table and are enqueued after the commit.
* **Limits:** `max_records` is exact — admission control before a target page starts,
  surplus records of pages already in progress trimmed deterministically when the result
  is written (`counters.records_dropped_over_limit`). After a limit cut or an operator stop
  no crawler is started again, also after a restart.
* **Politeness:** a preset (`politeness`, see "How a run behaves"), random per-host delay,
  robots `Crawl-delay`, 429 back-off (`Retry-After`), `max_requests_per_minute`, a page
  timeout and an automatic slow-down. A 429, a 5xx and a move to the next tier give the
  request back to the queue (Crawlee's `RequestThrottledError` path): a 429 never uses up
  `max_retries`, a failed static run is never re-run in the browser.
* **Proxy tiers:** our own escalation — next selected tier only on a block, kept per host
  in the job record; provider usernames built per Crawlee session (sticky).
* **Extraction:** JSON-LD via `extruct`, the declarative mapping (all types, `multiple`,
  the closed transform list, the fixed HTML clean-up), page classes, stored pages,
  repair trigger, mapping requests, versions with `expected_version`.
* **Result:** records de-duplicated by `record_id` (highest `mapping_version`, then latest
  `extracted_at`), sorted, one `.jsonl` file with SHA-256, descriptor, ranged download.
  A site key with a port gives a file name with `-` instead of `:`. The same records are also
  written as CSV (UTF-8 with BOM, `;`; `…/result/download?format=csv`; contract README 7.5).
* **Links and duplicates:** skip lists before the queue (schemes, messenger links, account/cart/
  wishlist/compare/search paths, action and facet parameters, files, `rel=nofollow`), a trap
  budget per URL pattern, learned dynamic parameters, session parameters out of the canonical URL,
  duplicates by the page's canonical address (contract README 4.11, 4.12).
* **One run per site, watchdog:** a second start of a busy site is skipped by default
  (`when_site_busy`, see below); a run with no progress for `WSS_STALL_SECONDS` raises
  `stalled`, then stops (contract README 4.13, 4.14).
* **Re-collect:** `POST /v1/runs/{id}/recollect` starts a new linked run over a finished
  run's selected unextracted URLs and unfinished sitemap sources, preserving per-source
  progress with its own result (contract README 4.10). For a failed parent, the default
  `stopped` selection also recovers durable queued/backlog work and pages still awaiting
  extraction or refetch. It preserves source keys and category traversal, skips completed
  ancestor identities for newly discovered links, and leaves the parent unchanged.
  Recovery seeds obey `max_url_list_entries`; changed parent/reason intent with an existing
  idempotency key is refused rather than silently reusing a different recovery run.
* **Events:** journal in SQLite, HMAC-SHA256 signing, delivery with the contract's
  back-off across restarts, claim/stage/complete/resend.

## How a run behaves

**A site that is already being scraped.** One run per site at a time. A second start
while a run of the same site is `queued`, `running`, `stopping` or
`waiting_for_credentials` is **skipped** by default (`when_site_busy: skip`):
`POST /v1/runs` answers `409 site_busy` with the running run's id, state and start time
(`error.details`), and nothing is stored, so a daily schedule never piles up runs behind
a long one; the same `Idempotency-Key` may be sent again later. With
`when_site_busy: queue` the run is stored and starts when the running one ends; while it
waits on a running site or exhausted capacity, its summary has `queued_reason`
(`same_site_running` or `no_free_slot`). A queued run with free capacity is waiting
for the scheduler and has no blocker reason.

**Politeness presets.** `politeness` fills the politeness fields that the run, the
connection profile and the mapping do not set:

| `politeness` | Delay between pages | Pages at once (`max_concurrency`) | `max_requests_per_minute` | `page_timeout_seconds` |
|---|---|---|---|---|
| `gentle` | 5–10 s | 1 | 10 | 120 |
| `normal` (default) | 2–5 s | 2 | 30 | 60 |
| `fast` | 0.5–1.5 s | 4 | 120 | 30 |

A field set explicitly wins; robots.txt `Crawl-delay` is always the floor. The run summary
states the result in `politeness.text`, e.g. "normal: 2–5 s, 2 at a time, 30/min, page
timeout 60 s".

**Page timeout.** `page_timeout_seconds` bounds one page: the HTTP request and the browser
navigation of a run, and every fetch of `POST /v1/analyze` and `POST /v1/samples`. A page
that takes longer fails like a network error (retried up to `max_retries`, then
`fetch_failed`).

During a crawl, scripted listing pagination renews the selected timeout whenever it
finds new links. The timeout bounds a stalled transition, not the entire catalogue.
Deliberate politeness/back-off waits do not consume that transition budget; Stop and
the job's remaining `max_duration_minutes` still bound those waits. Navigation actions
consume the shared `max_pages` allowance, without a separate 100-action runtime cap.
Real link progress also refreshes the stall watchdog. Setup analysis/sampling retains
one absolute navigation window and its overall setup deadline. Genuine stalls,
no-progress loops and exhausted budgets still report partial coverage.

**Setup previews and caller lifetime.** Analysis preserves explicit `crawl` or `url_list`
intent and skips sitemap enumeration. Its `sitemap.inspection` is `skipped`, with
`found`, `url_count` and `matching_url_count` set to `null` (unknown, not zero).
`auto`, `sitemap` and `sitemap_and_crawl` inspect at most 1,000 observed page URLs,
20 sitemap files and 10 seconds including sitemap discovery. Missing, malformed or refused
nested sources also leave coverage incomplete. A limited preview reports
`inspection: limited` and `url_count_capped: true`; known counts are lower bounds, and
counts remain `null` when no count was established. `target_share` remains unknown
for a limited preview. Full sitemap inventory belongs to a run.

Analysis and samples work also ends at `WSS_SETUP_TIMEOUT_SECONDS` or when the caller
disconnects/cancels. The overall budget can end a request before its per-page timeout.
The service cancels and awaits request work, closing its active HTTP streams and browser
session (including partial browser startup). Budget exhaustion returns a sanitized
`503 service_unavailable` with `error.details.reason: setup_deadline_exceeded`.

**Automatic slow-down.** Per host the service watches the last 20 pages. When at least
20 % of them were server errors, timeouts, network errors or unusable answers, or the
median response time reaches 3 times the host's first median and at least 5 s, the host's
delay doubles (up to 8 times the configured delay) and no new page of the host starts
before its usual answer time has passed, which makes it one page at a time. After 10 clean
pages it eases by 1.5 until it is back at the configured values; it never goes faster than
them. Each change is a timeline entry (`slowed_down`, `relaxed`); a slowed host is listed
in `politeness.slowed_hosts`.

**Soft errors (answers with status 200 that are not the page).** Every 200 answer is
judged before extraction: `empty` (no text), `truncated` (the HTML stops early while pages
of the same URL shape end properly), `error_page` (error or maintenance wording in the
title or first heading, in about 12 languages, on a small page), `too_small` (under 10 % of
the usual size of its URL shape) and, after extraction, `lost_template` (a URL shape that
was always a target page lost its template's marks). Explicit URL filters keep
navigation-only pages in a separate response history from selected pages, even when
their collapsed URL shapes coincide. Such an answer is retried like a
server error (same route, growing back-off, `max_retries`), counts in the host's breaker
and slow-down, and is counted in `counters.soft_errors` / `soft_errors_by_reason` (first one
in the timeline as `soft_error`). After the retries the page is `fetch_failed` with
"unusable answer: …" and never counts towards a mapping repair. Before a repair opened by
the share of incomplete pages, up to 5 of the triggering pages are fetched once more
(`repair_check`); a page that is fine now no longer counts (`counters.repairs_avoided`).

## Prices

Fields of type `price` are read as one amount in major currency units (contract README 6.1
and 6.5):

* **Split amounts.** A price split into tags is one amount: a trailing 1–2 digit part in
  `sup`/`small` or in an element whose class or id names the minor units (`fraction`,
  `decimal`, `cents`, …) is joined with a decimal mark (`24<sup>99</sup>`,
  `price-whole`/`price-fraction` → 24.99); a part that starts with `,` or `.` is joined as
  it is (`€<span>1.234</span><span>,56</span>` → 1234.56). Other digits in separate tags
  stay separate amounts; only the first amount of a text counts.
* **Decimal marks.** `1.234,56` and `1,234.56` are read by the last mark; `1 299,00`
  (space, NBSP or `'` between groups) is one amount. An ambiguous `1.234` / `1,234` is
  decided by the page: `html[lang]`, else `og:locale`, else the page's currency; with no
  hint it is thousands.
* **Sale and regular price.** The price field holds the current price, never the
  struck-through one. When the page or a variant shows a sale the record also has
  `regular_price` and `on_sale: true` (from the variant's own regular price, schema.org
  `priceSpecification` StrikethroughPrice/ListPrice, or the struck-through amount of the
  product's price block).
* **Price status.** Every record of a template with a price field carries `price_status`:
  `shown`, `range` (lowest value in the field, highest in `price_max`), `on_request` (the
  product says the price is given on request) or `missing`. `currency_from_default: true`
  marks a currency that came from `default_currency` because the page named none.
* **Machine-readable price checks.** A mapping should read the price the page states for
  programs first (`[itemprop=price]` `content`, JSON-LD `offers.price`/`lowPrice`,
  `product:price:amount`, `data-price`), the visible text only as a fallback. The mapping
  test cross-checks against the stated price: `price_mismatch` when they differ by more
  than rounding, `price_is_old_price` when on a sale page the mapping read the
  struck-through amount, `price_not_extracted` when the page shows a price the mapping
  did not read. Samples carry `price_details` (`machine_price`, `machine_source`,
  `current`, `regular_price`, `on_sale`, `sale_source`).

## Protection

Route per page: `http` → the job's browser (`camoufox` or `chromium`) → `wait` →
`next_proxy_tier` → `click` (only if `WSS_ALLOW_CHALLENGE_CLICK` and requested) → `solver`
(only if `WSS_ALLOW_SOLVERS` and requested and the page has a Turnstile sitekey). A hard
block moves the host to the next tier at once and is not counted as a challenge. Sessions
live at most `sticky_minutes` (≤ 30 min, 50 pages), are retired on a block, and never carry
cookies to another tier. Detection after every load: strong interstitial markers
(`_cf_chl_opt`, `__cf_chl_`, the orchestrate script), interstitial titles and phrases only
on an error status or a small page, a Turnstile only outside forms on a small page; the
JS-detection script, a contact-form Turnstile, a reCAPTCHA form or "access denied" in a
product text are ordinary pages; no bare `cloudflare` marker. The click is
ported from the old scraper without a model or screenshot: the challenge frame is found
through the page's frame list (Cloudflare renders it in a closed shadow root, which no
selector reaches), clicked **once** on the checkbox (≈ 28 px from the frame's left edge,
vertical middle, ±3 px), then the page is polled for up to
`WSS_CHALLENGE_CLICK_WAIT_SECONDS`; with Camoufox one `mouse.move` lets its `humanize`
draw the path. `POST /v1/analyze` and `POST /v1/samples` run the same route (one browser
session per request, the config's flags and caps), so a protected site gets samples.
With that same explicit server-and-run permission, Camoufox can also submit one
native opaque verification form to the exact HTTPS page being loaded. The allowance
requires its native GET response to be HTTP 403 with unambiguous `server: cloudflare`,
`cf-mitigated: challenge` and `cf-ray` headers. It expires after 45 seconds and cannot
be re-armed in that page generation. Only 1–8 unique lowercase hexadecimal field names
of length 64, one bounded printable ASCII value each, are accepted. The native addon
checks the URL-encoded wire Content-Type and exact body length (at most 64 KiB) before
sending; values stay in the browser. Destination, private-address, scope and robots
checks still apply. Replays, other forms, changed URLs and POST-preserving redirects
are blocked. The headers and form shape are heuristics, not proof of vendor identity
or of an origin's behavior: enabling the permission authorizes this one POST as part
of protection handling. Chromium retains its existing document POST denial.
If that browser closes or fails to start, setup returns HTTP 503 with
`error.details.reason: browser_unavailable`. It closes and discards the failed session;
it does not relaunch automatically or substitute an HTTP challenge for the browser failure.
Retry the setup request; repeated failures require checking the service logs. The error
alone does not establish resource exhaustion. Selector advice is based only on usable
documents inspected during sampling.
`scripts/protection_report.py` measures pass rates on a site you own (server, amd64). Clicks and solves are capped
per job; a challenge-limit or block-rate `problem` is emitted; a site that blocks almost
everything ends with `stop_reason: blocked`. A run that asks for the click or a solver with
a rotating proxy tier is refused. **No vision model anywhere.**

Solver adapters: `capsolver`, `two_captcha` (protocol tested against a fake API only),
`anti_captcha`, `capmonster_cloud` (same protocol, unverified). Version 1: Turnstile only.

## Proxy presets — verification status

| Provider | Status |
|---|---|
| `generic` | Complete (`scheme://user:password@host:port`), tested |
| `custom_list` | Complete (one of `proxy_urls` per session), tested |
| `oxylabs` | `customer-…-cc-…-sessid-…-sesstime-…` at `pr.oxylabs.io:7777` verified against the docs; `city-` unverified |
| `bright_data` | `-country-`, `-city-`, `-session-` parameters verified individually; endpoint `brd.superproxy.io:33335` unverified |
| `decodo` | Endpoint `gate.decodo.com:7000` verified; username parameters unverified |

## Limits

All limits of contract README 12 apply (`GET /health` → `limits`); values above them are
refused with `limit_exceeded` and the field named. `max_pages` and `max_records` may be
exceeded by the pages in progress when the limit is reached; `max_duration_minutes`
counts time in `running`/`stopping` only; the result never exceeds `max_result_bytes`
(`stop_reason: max_result_size`).

## Licence

Apache-2.0 (see `LICENSE` and `NOTICE`). Users build the image themselves; the
third-party components in it are downloaded from their upstream sources during the build.

## Generic catalogue development candidate

This branch is an unreleased development candidate. It adds bounded discovery through
intermediate pages and JavaScript listings, typed record inclusion filters and
per-variant DOM fields. Discovery never relies on a maintained product URL list.
Mappings still describe each site's observed data and selectors. Required values
are checked on included variants; unknown inclusion inputs report incomplete
extraction. Optional missing values can remain null without inheriting a parent
value. Navigation limits and unsupported controls must be reviewed before claiming
complete catalogue coverage. Test and deployment evidence is separate from a release.

Listing observation permits in-page DOM/AJAX updates. A control requesting a new
main-frame document is checked and deferred to normal discovery rather than loaded
by the click collector; this prevents an unvalidated redirect chain from executing
inside an observation. The deferred URL remains eligible for sample discovery,
including validated pagination URLs. Such an observation reports bounded coverage.
Browser contexts block Service Workers so request guards remain effective;
layouts that require Service Workers may remain partially discovered.

Embedded JSON variants may declare `children_path` relative to each object selected
by `path`. A source with `path: "editions"` and `children_path: "packages"` emits
each package with its own `raw` data and its corresponding edition at `parent.raw`.
Map fields explicitly, for example `raw.sku` and `parent.raw.year`; parent values
are never merged into missing child values. An absent or empty child collection
keeps one context-only row so required child fields report incomplete extraction.
The bounded sample candidates expose this relational choice as optional evidence;
they do not automatically treat every nested array as saleable product variants.

When a sample has no recognized summary/main container, a bounded heading-based
fallback prioritizes the product's local region and following content before
large preceding menus. Section comments still name the original DOM paths; no
supplier-specific selectors are built into this fallback. Unsupported layouts
and truncated evidence still require review before declaring optional fields absent.

Sitemap source coverage is reported in `sitemap_discovery` and
`completeness.sitemap_discovery`. A missing, inaccessible, empty-body or malformed
sitemap is a failed source, while a valid empty `<urlset>` or `<sitemapindex>` (or
URLs all excluded by the configured filters) can be complete. Source failures and
the total 100,000-location or 500-source-file limits make the result partial even
when no failed product URL is known. The location budget counts all locations,
before product filters, and stays spent across a service restart. Source report
URLs omit user information, queries and fragments; failure reasons do not contain
raw upstream errors. Private-address refusals remain explicit and outcome-neutral.

Use `POST /v1/runs/{id}/recollect` with a fresh profile to retry unfinished sitemap
sources and selected failed document URLs. This creates a linked run with a fresh
discovery budget and retained per-source offsets, including an index's remaining
children. It does not rewrite the finished parent's result. Completed source
entries are skipped and previously completed ancestor document URLs are not fetched
again if a retried source repeats them. Cursors are committed only after their
queue batch commits, so a crash can safely replay a batch through queue deduplication.
If a linked recovery itself fails before admitting its full seed, the next linked
run also recovers the unadmitted suffix using the saved admission cursor. Completed
prefix requests stay skipped; all recovered work shares the normal URL-list limit.
Original URL lists and explicit source-retry URLs also retain their own admission
cursors. A recovery's private seed is the sole feeder for its mirrored URL list,
and follower links remain durable if the feeder fails before task teardown.
Each incomplete source cursor retains a private body fingerprint. If its body changes
or an older cursor has no fingerprint, that source replays from the start through
queue/ancestor deduplication; replay spends the current run's remaining location
budget and stays partial when exhausted. Completed sources are not re-polled; use
a fresh full run when a complete new inventory is required.

A category page may show only recent or featured products until a vehicle, model,
or other catalogue context is selected. Crawling that view does not establish
full category coverage. Use advertised sitemap sources as well as category links
when available, and continue incomplete sources through linked Re-collect runs
until their reported coverage is complete. A bounded sample or a run stopped at
`max_records` proves extraction, not a complete catalogue. Category page counts
are a useful cross-check, but repeated fitments and canonical aliases mean their
position totals need not equal unique extracted records.

Category membership also need not match a product URL's directory. A listing can
link to products whose canonical addresses live in another section. Explicit
`url_include` filters still apply to those addresses; a successful visit to every
listing page does not prove that every listed product passed the selection.
Compare actual listing links with the extracted source URLs. Keep `crawl_include`
focused on the relevant listing pages and include any verified alternate product
URL shapes in `url_include`, rather than maintaining a fixed product URL list or
removing selection constraints. Links on product pages outside the walk scope
are not followed.

Listing navigation warnings remain visible when a browser action fails or a
document navigation is deferred to normal fetching. Check the ordinary queue's
pagination URLs and the final source coverage before claiming completeness. A
warning does not itself discard the links already found or abort product work.

`auto` retains its initial source inspection in `sitemap_probe`, including failed
sources and caps, even when it falls back to crawling. These remain partial-run
provenance if a later source body changes. Probe offsets are reset before feeding;
inspecting a URL never counts as having queued it. A default `/sitemap.xml` that
returns 404/410 is absent; an inaccessible or invalid default is incomplete.

Mappings may explicitly acquire an asynchronously loaded, paginated table with a
`browser_table` source. This source produces cleaned HTML containing the headers
and every observed ordered row; repeated models, month values and blank bounds
remain unchanged. It does not interpret blank end dates as finite year ranges.
For example, an invented product page can declare this field:

```json
{
  "name": "fitment_raw",
  "type": "html",
  "sources": [{
    "kind": "browser_table",
    "selector": "#applications",
    "header_selector": "thead th",
    "row_selector": "tbody tr",
    "cell_selector": "td",
    "loading_selector": "#applications-processing",
    "empty_selector": "td.empty",
    "next_selector": "#applications-pager .next",
    "current_page_selector": "#applications-pager .current",
    "max_pages": 32,
    "max_rows": 2000,
    "max_bytes": 1048576,
    "max_seconds": 30
  }]
}
```

Selectors describe the observed layout. The loading indicator must be hidden
before rows or an explicit empty marker can settle. Paginated sources require a
current page containing an integer, beginning at 1 and advancing by 1, and an
explicit Next control that remains present and becomes disabled on the last page.
Supported disabled states are the native `disabled` property/attribute,
`aria-disabled="true"`, and a `disabled` class on the control or its ancestor.
Supported Next names are `Next`, `Next page`, and `»`, `›`, `→` or `>`.
A single-page source omits both pagination selectors. Each row must have ordinary
cells without merged row or column spans. Unsupported layouts report incomplete
coverage. The hard limits are 64 pages, 10,000 rows, 4 MiB and 120 seconds per
source. All sources on one page additionally share hard limits of 4 MiB of
serialized retained capture evidence, 10,000 rows and 64 observed pages, including
existing captures and proposed sample sources. Exhausted sources report an
explicit `aggregate_byte_limit`, `aggregate_row_limit` or `aggregate_page_limit`;
their original source fingerprints stay unchanged. The run's action and time
budgets still apply.

Acquisition uses the existing guarded browser tab and supports in-page pagination.
Document navigation and non-GET browser requests (including fetch/XHR and beacons)
are refused during acquisition. HTTP-only jobs
report `browser_required`; automatic mode uses the normal browser escalation.
Other product fields and variants continue reading the original product HTML.
Each successful JSON Lines record includes `table_coverage` with page/row/byte
counts. A missing, partial or differently configured capture cannot satisfy the
mapping, even when the table field is optional or another source could supply
specification years. Its partial rows and stop reason remain in stored evidence.

To test a proposed source before activating its mapping, pass the same source
object in `POST /v1/samples`' optional `table_sources` array (at most five), using
explicit synthetic URLs such as `https://parts.example.test/products/mock-part`.
Active mappings' table sources are captured automatically with their samples.
Samples expose bounded raw-cell evidence and retain their captures privately;
`POST /v1/mappings/test` replays those captures without new network requests.
Changing a source requires new samples. Stored page re-extraction also retains
its original capture and never treats an initial table page as the full result.
