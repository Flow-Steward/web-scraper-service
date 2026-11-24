"""One job = one Crawlee crawler run as an asyncio task.

Shape:

* per-job ``Configuration(storage_dir=<volume>/jobs/<run_id>/crawlee, purge_on_start=False)``,
  ``FileSystemStorageClient``, own event manager, own request queue, session pool, browser
  pool and proxy configuration; nothing is shared between jobs (upstream issue 2301);
* ``AdaptivePlaywrightCrawler`` with our ``RenderingTypePredictor`` (detection probability 0,
  so a page is never fetched twice for detection; "browser only" per host after a
  challenge), ``retry_on_blocked=False`` and challenge/5xx statuses ignored, so our own
  classification decides and Crawlee never retires the session that just passed;
* **deferral instead of exceptions that fall through**: a 429, a 5xx, a move to the next
  proxy tier and the ``max_records`` admission control raise ``Deferred`` (Crawlee's
  ``RequestThrottledError``); the crawler gives the request back to the queue without
  counting a retry and without running the browser for a failed static run. Our own counters bound 429/5xx deferrals;
* the request handler writes **only through the crawling context** (``push_data`` and the
  key-value store); a follower task applies the pushed page outcomes to the job record in
  SQLite, in order, in **one synchronous transaction per batch** with its offset (no
  ``await`` inside a transaction). Links and refetches are written to a backlog
  table in that transaction and enqueued after it commits;
* discovery (robots.txt, sitemaps, the start page for the ``auto`` rule) is fetched
  **through the crawler** as auxiliary requests, so it uses the job's proxy tiers, headers,
  sessions and challenge route;
* a supervisor loop enforces limits, mapping-request timeouts, re-extraction and the
  completion condition (README 4.3), then writes the result.
"""

from __future__ import annotations

import asyncio
import json
import copy
import hashlib
import logging
import shutil
import time
from functools import wraps
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from crawlee.errors import RequestThrottledError

from app.core.db import dumps, loads
from app.core.ids import new_id
from app.core.netguard import url_is_private
from app.core.redaction import redact, secret_values
from app.core.timeutil import iso, now, now_iso, parse, plus
from app.core.urls import canonical_url, host_of, record_id, result_file_name, site_key
from app.core.url_patterns import (
    UrlPattern, UrlPatternBatch, UrlPatternError, compile_patterns, matches_any,
    url_batch, url_pattern_batch, url_policy,
)
from app.extraction.pricing import annotate as annotate_price, plain as plain_prices, price_field, price_view
from app.jobs import softerror
from app.extraction.distill import sample_doc, suggest_template_id
from app.extraction.mapping import apply_mapping, looks_like_target, passes_target_selector, pick_template, required_set
from app.extraction.page import Page
from app.fetching.challenge import detect_html, detect_page, page_html
from app.fetching.proxies import TierPlan
from app.fetching.proxy_auth import ProxyAuthenticationRequired, is_proxy_auth_failure
from app.jobs.throttle import HostThrottle

if TYPE_CHECKING:
    from app.fetching.protection import RouteBudget
    from app.service import Service

log = logging.getLogger("wss.job")

# statuses our handler sees instead of Crawlee raising (challenges, blocks, 429, every 5xx)
IGNORED_STATUSES = sorted({401, 403, 404, 407, 410, 429, 451} | set(range(500, 600)))
STOP_LISTED_REASONS = {"stopped", "max_duration", "max_result_size", "blocked", "stalled"}
ADMISSION_WAIT_SECONDS = 20.0
REQUEST_CACHE_SIZE = 2000  # pending requests Crawlee keeps in memory per run
GAP_REPAIR_PAGES = 3  # stored pages with variant data / a shown price the mapping misses that open a repair
GAP_WORDS = {
    "variants_not_used": "the page carries variant data (two or more variants) but the mapping declares no variants",
    "price_not_extracted": "the page shows a price but the mapping's price is empty",
}


def gaps_text(gaps: list[str] | None) -> str | None:
    """The unextracted entry's plain words for mapping gaps (README 4.5)."""
    if not gaps:
        return None
    return "; ".join(GAP_WORDS.get(g, g) for g in gaps) + "; kept for a repair of the mapping"
TRACE_KEEP = 5  # route traces kept in a job's protection summary


RECORD_ORDER = ["record_id", "source_url", "requested_url", "source_key", "site", "template_id", "mapping_version", "item_index", "variant",
                "price_status", "price_max", "regular_price", "on_sale", "currency_from_default", "extracted_at", "data"]  # key order of record.schema.json


class Deferred(RequestThrottledError):
    """Give the request back to the queue: no retry counted, no browser fall-through."""


def page_key(canonical: str, source_key: str | None) -> str:
    return f"{canonical}\x1f{source_key or ''}"


def kvs_key(pkey: str) -> str:
    return "page-" + hashlib.sha256(pkey.encode("utf-8")).hexdigest()[:40]


def fail_on_url_pattern_error(handler):
    """Stop the run before Crawlee can retry a configuration policy failure."""
    @wraps(handler)
    async def bounded(self, ctx, *args, **kwargs):
        try:
            return await handler(self, ctx, *args, **kwargs)
        except UrlPatternError as exc:
            ctx.request.no_retry = True
            await self._url_pattern_failed(exc)
            raise
    return bounded


def raise_document_policy_error(ctx):
    """Surface native late navigation refusals before accepting page contents."""
    policy = getattr(ctx.page, "_wss_native_document_policy", None)
    if policy is not None and policy.errors:
        if not isinstance(policy.errors[0], ProxyAuthenticationRequired):
            ctx.request.no_retry = True
        policy.raise_if_denied()


def write_unextracted(db, run_id: str, canonical: str, requested: str | None, source_key: str | None, site: str, reason: str,
                      template_id: str | None, http_status: int | None, attempts: int, last_error: str | None = None,
                      missing: list[str] | None = None, neutral: bool = False, secrets: list[str] | None = None,
                      gaps: list[str] | None = None) -> None:
    """One entry of the unextracted list. ``neutral`` entries do not make the outcome partial
    (README 4.2: ``not_found``; sitemap URLs refused by robots.txt or the private rule)."""
    doc: dict[str, Any] = {
        "url": canonical,
        "site": site,
        "reason": reason,
        "template_id": template_id,
        "http_status": http_status,
        "attempts": attempts,
        "last_seen_at": now_iso(),
    }
    if requested and requested != canonical:
        doc["requested_url"] = requested
    if source_key:
        doc["source_key"] = source_key
    if missing:
        doc["missing_fields"] = missing
    if gaps:
        doc["mapping_gaps"] = list(gaps)  # README 4.5
    if last_error:
        doc["last_error"] = redact(last_error, secrets or ())[:1000]
    db.execute(
        "INSERT INTO unextracted(run_id, url_key, url, reason, template_id, doc, neutral) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id, url_key) DO UPDATE SET reason=excluded.reason, template_id=excluded.template_id, doc=excluded.doc, neutral=excluded.neutral",
        (run_id, page_key(canonical, source_key), canonical, reason, template_id, dumps(doc), 1 if (neutral or reason == "not_found") else 0),
    )


class JobRunner:
    def __init__(self, service: "Service", run_id: str) -> None:
        self.svc = service
        self.settings = service.settings
        self.run_id = run_id
        row = service.db.one("SELECT doc, runtime FROM runs WHERE run_id=?", (run_id,))
        self.doc: dict = loads(row["doc"])
        self.rt: dict = loads(row["runtime"])
        self.config: dict = self.doc["config"]
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.crawler = None
        self.crawler_task: asyncio.Task | None = None
        self.feeder_task: asyncio.Task | None = None
        self.follower_task: asyncio.Task | None = None
        self.rq = None
        self.dataset = None
        self.kvs = None
        self.cfg = None
        self.sc = None
        self._shutdown = False
        self._no_crawl = False
        self.job_dir = Path(self.settings.data_dir) / "jobs" / run_id
        rate_floor = 60 / float(self.config.get("max_requests_per_minute") or 60000)
        self.throttle = HostThrottle(max(rate_floor, self.config["delay_min_seconds"]), max(rate_floor, self.config["delay_max_seconds"]))
        self._shape_stats: dict[str, softerror.ShapeStats] = {}  # what normal pages of a URL shape look like
        self.tiers = TierPlan([], self.rt.get("host_level"))
        self.solver = None
        self.browser = self.doc["resolved"].get("browser")
        self.robots: dict[str, Any] = {}
        self.mappings: dict[str, dict[str, dict]] = {}  # site -> template_id -> {"version", "mapping"}
        self.include = compile_patterns(self.config.get("url_include") or [], "url_include")
        self.exclude = compile_patterns(self.config.get("url_exclude") or [], "url_exclude")
        self.pages_started = int(self.rt.get("pages_started", self.doc["counters"].get("pages_fetched", 0)))
        self.last_persist = 0.0
        self.secret_strings: list[str] = []
        # admission control (max_records) and per-attempt bookkeeping, in memory only
        self._attempt_token: dict[str, str] = {}
        self._admitted: set[str] = set()
        self._pending_records: dict[str, float] = {}  # unique_key -> when a record was pushed, not yet applied
        self._target_patterns: dict[str, list[UrlPattern]] = {}
        # URL shapes (sampling.url_shape) of pages that turned out to be target pages: their
        # sibling links go to the front of the queue (a category's 12 product links
        # behind 1028 menu links)
        self._target_shapes: set[str] = set()
        # auxiliary fetches (robots.txt, sitemaps, start page) through the crawler
        self._aux: dict[str, asyncio.Future] = {}
        self._proxy_tier_by_url: dict[str, int] = {}
        # link rules, canonicals, stall watchdog
        from app.core.linkrules import DynamicParams, enabled_rules
        from app.jobs.canon import CanonRegistry

        self.link_rules = enabled_rules(self.config)
        self.dynamic = DynamicParams(self.rt.get("dynamic_params"))
        self.canon = CanonRegistry(self.rt)
        self.last_progress = time.monotonic()
        self.paused_until = 0.0
        self._stall_cut_at: float | None = None

    # ================================================================== state helpers
    @property
    def state(self) -> str:
        return self.doc["state"]

    def counters(self) -> dict:
        return self.doc["counters"]

    def prot(self) -> dict:
        return self.doc["protection"]

    def clean(self, text: str | None) -> str:
        """Every error text that is stored or returned passes here."""
        return redact(text or "", self.secret_strings)

    def _persist(self) -> None:
        self.doc["updated_at"] = now_iso()
        self.rt["host_level"] = self.tiers.host_level
        self.svc.db.execute(
            "UPDATE runs SET state=?, updated_at=?, finished_at=?, doc=?, runtime=? WHERE run_id=?",
            (self.doc["state"], self.doc["updated_at"], self.doc.get("finished_at"), dumps(self.doc), dumps(self.rt), self.run_id),
        )
        self.last_persist = time.monotonic()

    def _snapshot(self) -> tuple:
        return copy.deepcopy(self.doc), copy.deepcopy(self.rt), dict(self.tiers.host_level)

    def _restore(self, snap: tuple) -> None:
        """A failed transaction leaves the in-memory job exactly as committed."""
        doc, rt, host_level = snap
        state = self.doc["state"]
        self.doc, self.rt = doc, rt
        self.doc["state"] = state  # the API may have changed it (stop) meanwhile
        self.config = self.doc["config"]
        self.tiers.host_level = host_level

    def add_error(self, code: str, message: str, *, url: str | None = None, template_id: str | None = None, severity: str = "error") -> None:
        if severity == "debug" and not self.config.get("debug"):
            return
        entry: dict[str, Any] = {"code": code, "message": self.clean(message)[:2000], "at": now_iso(), "severity": severity}
        if url:
            entry["url"] = self.clean(url)[:2048]
        if template_id:
            entry["template_id"] = template_id
        errors = self.doc.setdefault("errors", [])
        errors.append(entry)
        del errors[:-100]

    def template(self, tid: str) -> dict:
        templates = self.rt.setdefault("templates", {})
        if tid not in templates:
            mapping = self._mapping_for_any_site(tid)
            version = mapping["version"] if mapping else None
            templates[tid] = {
                "status": "ok",
                "version": version,
                "versions_used": [],
                "repairs_used": 0,
                "open_request": None,
                "window": [],
                "record_count": 0,
                "unextracted_count": 0,
                "matched": 0,
                "filled_pages": 0,
            }
        return templates[tid]

    def _mapping_for_any_site(self, tid: str) -> dict | None:
        for site_maps in self.mappings.values():
            if tid in site_maps:
                return site_maps[tid]
        return None

    def mappings_for(self, site: str) -> list[dict]:
        if site not in self.mappings:
            self.mappings[site] = {m["template_id"]: m for m in self.svc.mappings.active_mappings(site)}
        return [entry["mapping"] | {"__version": entry["version"]} for entry in self.mappings[site].values()]

    def _unextracted(self, canonical: str, requested: str | None, source_key: str | None, site: str, reason: str, template_id: str | None,
                     http_status: int | None, attempts: int, last_error: str | None = None, missing: list[str] | None = None,
                     neutral: bool = False, gaps: list[str] | None = None) -> None:
        write_unextracted(self.svc.db, self.run_id, canonical, requested, source_key, site, reason, template_id, http_status, attempts,
                          last_error, missing, neutral, self.secret_strings, gaps)

    def _drop_unextracted(self, canonical: str, source_key: str | None) -> None:
        self.svc.db.execute("DELETE FROM unextracted WHERE run_id=? AND url_key=?", (self.run_id, page_key(canonical, source_key)))

    # ================================================================== lifecycle
    def start(self) -> None:
        self.task = asyncio.create_task(self._main(), name=f"job-{self.run_id}")

    async def shutdown(self, budget: float = 45.0) -> None:
        """SIGTERM: stop fetching, persist, leave the state as it is (the job resumes after a restart).

        The whole shutdown stays inside the container's grace period."""
        self._shutdown = True
        for fut in list(self._aux.values()):
            if not fut.done():
                fut.set_result(None)
        if self.crawler is not None:
            try:
                self.crawler.stop("service shutdown")
            except Exception:
                pass
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=budget)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()
                try:
                    await asyncio.wait_for(self.task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

    async def _main(self) -> None:
        try:
            await self._prepare()
            # a job that moved to waiting_for_credentials (or ended) in _prepare stops here
            if self.state in ("failed", "completed", "waiting_for_credentials"):
                return
            await self._run_crawl()
        except asyncio.CancelledError:
            raise
        except ProxyAuthenticationRequired:
            pass  # the queue and source traversal remain resumable
        except UrlPatternError as exc:
            await self._url_pattern_failed(exc)
        except Exception as exc:  # unexpected: internal_error
            log.exception("job %s crashed", self.run_id)
            try:
                await self._fail("internal_error", f"internal error ({type(exc).__name__})")
            except Exception:
                log.exception("job %s: failing the job failed", self.run_id)
        finally:
            await self._teardown()
            self.svc.jobs.runner_done(self.run_id)

    def _load_secret(self) -> dict | None:
        """The job's proxy/solver secrets; None when they cannot be decrypted."""
        from app.core.secrets import SecretsUnavailable

        if self.rt.get("needs_secrets"):
            try:
                return self.svc.secrets.get(self.run_id, "proxy_solver")
            except SecretsUnavailable:
                return None
        return {"proxy_tiers": [t for t in self.doc["profile"].get("proxy_tiers") or [] if t.get("auth_mode") == "ip_allowlist" and t.get("provider") != "custom_list"], "solver": None}

    def apply_secret(self, secret: dict) -> None:
        """Tiers and solver from the secret part; also used when Resume rotates a running job's secrets."""
        from app.core.config import selected_tiers
        from app.fetching.solvers import from_profile

        self.secret_strings = secret_values(secret)
        try:
            webhook = self.svc.secrets.get(self.run_id, "webhook")
            self.secret_strings += secret_values({"signing_secret": webhook.get("signing_secret")})
        except Exception:
            pass
        self.tiers = TierPlan(selected_tiers(self.config, secret), self.tiers.host_level or self.rt.get("host_level"))
        self._proxy_tier_by_url.clear()
        self.solver = None
        if self.config.get("use_solver") and self.settings.solvers_allowed:
            try:
                self.solver = from_profile(secret.get("solver"))
            except Exception:
                self.solver = None

    async def _prepare(self) -> None:
        secret = self._load_secret()
        if secret is None:
            if self.state == "stopping":
                # an operator stop of a job without its secrets: finish without fetching
                secret = {"proxy_tiers": [], "solver": None}
                self._no_crawl = True
            else:
                await self.svc.jobs.to_waiting_for_credentials(self.run_id)
                self.doc = loads(self.svc.db.one("SELECT doc FROM runs WHERE run_id=?", (self.run_id,))["doc"])
                return
        self.apply_secret(secret)
        first_host = self._first_host()
        self.doc["counters"]["current_proxy_tier"] = self.tiers.tier_for(first_host)["tier"] if self.tiers.enabled and first_host else None
        self.job_dir.mkdir(parents=True, exist_ok=True)
        started = now()
        if not self.doc.get("started_at"):
            self.doc["started_at"] = iso(started)
            self.timeline("started", f"started on {self.doc['site']}")
        else:
            self.timeline("resumed", "resumed")
        self.rt["running_since"] = iso(started)
        if self.doc["progress"]["discovery"] == "pending":
            self.doc["progress"]["discovery"] = "running"
        await self._open_storages()
        # A resumed auxiliary request can finish before its source feeder starts.
        # Register its waiter before starting the crawler and reuse the queue key.
        for key in (self.rt.get("aux_pending") or {}).values():
            self._aux[key] = asyncio.get_running_loop().create_future()
        self._reconcile_requests()
        self._persist()

    def _first_host(self) -> str | None:
        try:
            if self.config.get("site_url") and self.config.get("discovery_mode") != "url_list":
                return host_of(self.config["site_url"])
            urls = self.config.get("urls") or []
            return host_of(urls[0]["url"]) if urls else None
        except ValueError:
            return None

    async def _open_storages(self) -> None:
        from crawlee.configuration import Configuration
        from crawlee.storage_clients import FileSystemStorageClient
        from crawlee.storages import Dataset, KeyValueStore, RequestQueue

        self.cfg = Configuration(
            storage_dir=str(self.job_dir / "crawlee"),
            purge_on_start=False,
            persist_state_interval=timedelta(seconds=15),
            headless=self.settings.headless,
            available_memory_ratio=0.6,
        )
        self.sc = FileSystemStorageClient()
        self.rq = await RequestQueue.open(configuration=self.cfg, storage_client=self.sc)
        # Crawlee keeps up to 100 000 pending requests in memory per queue; a crawl that has found
        # tens of thousands of products then grows by several KB per page. A few
        # thousand are enough to keep the workers fed; the rest are read from disk in order.
        client = getattr(self.rq, "_client", None)
        if client is not None and hasattr(client, "_MAX_REQUESTS_IN_CACHE"):
            client._MAX_REQUESTS_IN_CACHE = REQUEST_CACHE_SIZE
        self.dataset = await Dataset.open(configuration=self.cfg, storage_client=self.sc)
        self.kvs = await KeyValueStore.open(configuration=self.cfg, storage_client=self.sc)

    async def _teardown(self) -> None:
        for task in (self.feeder_task, self.follower_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        for fut in list(self._aux.values()):
            if not fut.done():
                fut.set_result(None)
        # persist first: a cancelled wait below must not lose the state
        self._accumulate_running_time()
        if self.state not in ("completed", "failed"):
            self._persist()
        if self.crawler is not None and self.crawler_task and not self.crawler_task.done():
            try:
                self.crawler.stop()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.crawler_task, timeout=25 if self._shutdown else 90)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self.crawler_task.cancel()
        if self.state not in ("completed", "failed"):
            self._persist()

    def _accumulate_running_time(self) -> None:
        since = parse(self.rt.get("running_since"))
        if since is not None:
            self.rt["running_seconds"] = float(self.rt.get("running_seconds", 0.0)) + (now() - since).total_seconds()
            self.rt["running_since"] = iso(now()) if self.state in ("running", "stopping") and not self._shutdown else None

    def running_seconds(self) -> float:
        total = float(self.rt.get("running_seconds", 0.0))
        since = parse(self.rt.get("running_since"))
        if since is not None:
            total += (now() - since).total_seconds()
        return total

    # ================================================================== auxiliary fetches
    async def aux_fetch(self, url: str, timeout: float = 150.0):
        """robots.txt, a sitemap or the start page through the job's crawler: proxy tiers,
        headers, the session pool and the challenge route (a site that challenges plain HTTP is
        read through the browser session after clearance). Returns ``Fetched`` or None."""
        from crawlee import Request

        from app.fetching.oneoff import http_get

        if self.state == "waiting_for_credentials":
            raise ProxyAuthenticationRequired()
        if self._shutdown or await url_is_private(url):
            return None
        if self.crawler is None or self.crawler_task is None or self.crawler_task.done() or self._cut_or_stopping():
            proxy = None
            try:
                proxy = self.tiers.url_for(host_of(url), "discovery") if self.tiers.enabled else None
            except ValueError:
                proxy = None
            try:
                fetched = await http_get(url, proxy=proxy, headers=self.config.get("headers") or None, timeout=30)
                if fetched is not None and is_proxy_auth_failure(fetched.error, status=fetched.status, proxy_used=bool(proxy)):
                    await self._pause_for_proxy_auth()
                    raise ProxyAuthenticationRequired()
                return fetched
            except ProxyAuthenticationRequired:
                raise
            except Exception:
                return None
        pending = self.rt.setdefault("aux_pending", {})
        key = pending.get(url)
        fut = self._aux.get(key) if key else None
        if key and (fut is None or not fut.done()):
            previous = await self.rq.get_request(key)
            if previous is not None and previous.handled_at is not None:
                # A crash after queue commit but before source consumption cannot
                # reconstruct an in-memory body; refetch only that source file.
                self._aux.pop(key, None)
                key, fut = None, None
        key = key or f"aux\x1f{url}\x1f{new_id('aux')}"
        fut = fut or asyncio.get_running_loop().create_future()
        self._aux[key] = fut
        pending[url] = key
        self._persist()
        headers = dict(self.config.get("headers") or {})
        consumed = False
        try:
            if not fut.done():
                req = Request.from_url(url, unique_key=key, user_data={"aux": True, "src": "aux"}, headers=headers or None)
                await self.rq.add_requests([req], forefront=True, wait_for_all_requests_to_be_added=True)
            fetched = await asyncio.wait_for(asyncio.shield(fut), timeout)
            if fetched is not None and is_proxy_auth_failure(fetched.error, status=fetched.status, proxy_used=self.tiers.enabled):
                await self._pause_for_proxy_auth()
                raise ProxyAuthenticationRequired()
            consumed = True
            return fetched
        except asyncio.TimeoutError:
            return None
        except ProxyAuthenticationRequired:
            raise
        except Exception:
            log.exception("job %s: auxiliary fetch failed", self.run_id)
            return None
        finally:
            if consumed:
                self._aux.pop(key, None)
                pending.pop(url, None)
                self._persist()

    async def _pause_for_proxy_auth(self) -> None:
        """Pause without consuming page/source queues or resetting protection budgets."""
        # This transition is synchronous until the manager call; auxiliary fetches
        # can also reach it while the supervisor already owns self.lock.
        self.doc = self._reload_state_fields()
        if self.state != "running":
            return
        self.doc["state"] = "waiting_for_credentials"
        self.rt["credentials_wait_reason"] = "proxy_auth_failed"
        self.rt["proxy_auth_pauses"] = int(self.rt.get("proxy_auth_pauses", 0)) + 1
        self._accumulate_running_time()
        self._persist()
        await self.svc.jobs.to_waiting_for_credentials(self.run_id, reason="proxy_auth_failed")
        if self.crawler is not None:
            self.crawler.stop("upstream proxy authentication requires Resume")
        for fut in list(self._aux.values()):
            if not fut.done():
                fut.set_exception(ProxyAuthenticationRequired())
                fut.exception()  # a pre-registered waiter may not have a consumer yet

    def _aux_done(self, request, fetched) -> None:
        self.last_progress = time.monotonic()
        fut = self._aux.get(request.unique_key)
        if fut is not None and not fut.done():
            fut.set_result(fetched)

    async def _robots_for(self, url: str):
        from app.jobs.discovery import _origin, robots_from

        try:
            host = host_of(url)
            origin = _origin(url)
        except ValueError:
            return None
        if origin in self.robots:
            return self.robots[origin]
        robots = await robots_from(origin, self.aux_fetch)
        self.robots[origin] = robots
        if robots is not None:
            self.throttle.set_crawl_delay(host, robots.get_crawl_delay())
        return robots

    async def robots_allowed(self, url: str) -> bool:
        if not self.config.get("respect_robots", True):
            return True
        robots = await self._robots_for(url)
        if robots is None:
            return True
        try:
            return robots.is_allowed(url)
        except Exception:
            return True

    # ================================================================== discovery decision
    async def _resolve_discovery(self) -> bool:
        from app.jobs.discovery import decide_discovery, find_sitemaps

        cfg = self.config
        resolved = self.doc["resolved"]
        mode = cfg["discovery_mode"]
        reasons: list[str] = list(resolved.get("reasons") or [])
        if mode != "url_list":
            site_url = cfg["site_url"]
            if await url_is_private(site_url):
                await self._fail("internal_error", "site_url resolves to a private address")
                return False
            if not await self.robots_allowed(site_url):
                await self._fail("robots_disallowed", "robots.txt disallows site_url")
                return False
        if cfg.get("single_page"):
            resolved["discovery_mode"] = None
            reasons.append("single_page: discovery skipped")
            self.rt["sitemaps"] = []
        elif mode == "url_list":
            resolved["discovery_mode"] = "url_list"
        elif mode == "crawl":
            resolved["discovery_mode"] = "crawl"
            self.rt["sitemaps"] = []  # crawl never reads a sitemap
        else:
            robots = await self._robots_for(cfg["site_url"])
            probe = {}
            sitemaps = await find_sitemaps(cfg["site_url"], cfg.get("sitemap_url"), robots, self.aux_fetch, probe=probe)
            if mode == "auto" and not sitemaps:
                # An absent optional default map is ordinary; an unavailable or
                # invalid one is source uncertainty even when crawl is viable.
                sitemaps = [u for u, fetched in probe.items() if fetched is None or fetched.status not in (404, 410)]
            self.rt["sitemaps"] = sitemaps
            if mode == "auto":
                async def probe_fetch(url):
                    if url in probe:
                        return probe.pop(url)
                    return await self.aux_fetch(url)

                decided, why = await decide_discovery(self, sitemaps, probe_fetch)
                resolved["discovery_mode"] = decided
                reasons += why
            else:
                resolved["discovery_mode"] = mode
        resolved["reasons"] = reasons
        self.rt["discovery_resolved"] = True
        self.timeline("discovery_decided", f"discovery: {resolved.get('discovery_mode')}" + (f" ({'; '.join(reasons)[:200]})" if reasons else ""))
        async with self.lock:
            self._persist()
        return True

    # ================================================================== crawler
    def _session_settings(self) -> dict:
        """Sessions die before the provider rotates a sticky exit IP: a clearance
        cookie is never presented from another address. Old system: 50 pages / 30 minutes."""
        sticky = [int(t.get("sticky_minutes") or 10) for t in self.tiers.tiers if t.get("session_mode") == "sticky"]
        minutes = min(sticky + [30])
        return {"max_age": timedelta(seconds=max(60, minutes * 60 - 30)), "max_usage_count": 50}

    def _build_crawler(self):
        from crawlee import ConcurrencySettings
        from crawlee.crawlers import AdaptivePlaywrightCrawler
        from crawlee.events import LocalEventManager
        from crawlee.proxy_configuration import ProxyConfiguration
        from crawlee.sessions import SessionPool

        from app.fetching.browsers import Variant, make_browser_pool
        from app.jobs.predictor import JobPredictor

        cfg = self.config
        browser = self.browser or "chromium"
        # the job's page timeout bounds the HTTP request and the browser navigation
        page_timeout = int(cfg.get("page_timeout_seconds") or self.settings.navigation_timeout_seconds)
        nav = page_timeout + int(cfg["delay_max_seconds"]) + 5
        handler_timeout = (nav + int(self.settings.challenge_wait_seconds) * 2 + 60 + (150 if self.solver else 0)
                           + cfg["wait_for_selector_ms"] // 1000 + cfg["wait_after_load_ms"] // 1000 + int(ADMISSION_WAIT_SECONDS))
        walks_links = cfg["discovery_mode"] == "auto" or (
            self.doc["resolved"].get("discovery_mode") or cfg["discovery_mode"]
        ) in ("crawl", "sitemap_and_crawl")
        if cfg["fetch_mode"] != "http" and browser and not cfg.get("single_page") and walks_links:
            # One browser handler can walk many same-URL listing states. Each
            # transition has its own deadline; the job's supervisor bounds the
            # whole traversal rather than another page-sized handler timeout.
            handler_timeout = max(handler_timeout, cfg["max_duration_minutes"] * 60 + nav)
        concurrency = ConcurrencySettings(
            min_concurrency=1,
            max_concurrency=cfg["max_concurrency"],
            desired_concurrency=1,
            max_tasks_per_minute=cfg["max_requests_per_minute"],
        )
        from app.fetching.crawlee_http import PolicyHttpClient

        http_client = PolicyHttpClient(destination_policy=self._http_redirect_destination_allowed)
        self._redirect_http_client = http_client
        kwargs: dict[str, Any] = dict(
            http_client=http_client,
            rendering_type_predictor=JobPredictor(self),
            result_checker=self._result_ok,
            playwright_crawler_specific_kwargs={
                "browser_pool": make_browser_pool(browser, headless=self.settings.headless, max_pages=max(1, cfg["max_concurrency"]),
                                                  variant=Variant.from_config(cfg)),
                "navigation_timeout": timedelta(seconds=nav),
            },
            configuration=self.cfg,
            storage_client=self.sc,
            event_manager=LocalEventManager(),
            request_manager=self.rq,
            session_pool=SessionPool(max_pool_size=max(1, cfg["max_concurrency"]), create_session_settings=self._session_settings()),
            max_request_retries=cfg["max_retries"],
            max_session_rotations=6,
            retry_on_blocked=False,
            ignore_http_error_status_codes=IGNORED_STATUSES,
            respect_robots_txt_file=False,
            concurrency_settings=concurrency,
            request_handler_timeout=timedelta(seconds=handler_timeout),
            keep_alive=True,
            configure_logging=False,
            status_message_logging_interval=timedelta(minutes=10),
        )
        if self.tiers.enabled:
            kwargs["proxy_configuration"] = ProxyConfiguration(new_url_function=self._proxy_url)
        self._adaptive = not (cfg["fetch_mode"] == "http" or not self.browser)
        if not self._adaptive:
            from crawlee.crawlers import ParselCrawler

            for key in ("rendering_type_predictor", "result_checker", "playwright_crawler_specific_kwargs"):
                kwargs.pop(key)
            crawler = ParselCrawler(navigation_timeout=timedelta(seconds=page_timeout), **kwargs)
        else:
            # the adaptive crawler builds its static sub-crawler itself and takes no navigation
            # timeout for it: give it the job's page timeout while it is built
            from crawlee.crawlers._abstract_http._abstract_http_crawler import AbstractHttpCrawler

            original = AbstractHttpCrawler.__init__

            def with_timeout(obj, *args, **kw):
                kw.setdefault("navigation_timeout", timedelta(seconds=page_timeout))
                original(obj, *args, **kw)

            AbstractHttpCrawler.__init__ = with_timeout
            try:
                crawler = AdaptivePlaywrightCrawler.with_parsel_static_parser(**kwargs)
            finally:
                AbstractHttpCrawler.__init__ = original
        # every Deferred goes back to the queue (Crawlee's own throttling manager is not used)
        crawler._is_held_back = lambda request: True
        crawler.router.default_handler(self._handle)
        crawler.failed_request_handler(self._failed)
        crawler.pre_navigation_hook(self._pre_nav)
        from app.jobs.proxy_auth import guard_proxy_auth

        guard_proxy_auth(crawler, self, adaptive=self._adaptive)
        return crawler

    def _proxy_url(self, session_id: str | None, request) -> str | None:
        if request is None:
            return None
        try:
            host = host_of(request.url)
        except ValueError:
            return None
        url = self.tiers.url_for(host, session_id)
        tier = self.tiers.tier_for(host)
        if tier is not None and url:
            self._proxy_tier_by_url[url] = tier["tier"]
            by = self.counters()["proxy_requests_by_tier"]
            by[str(tier["tier"])] = by.get(str(tier["tier"]), 0) + 1
            self.counters()["current_proxy_tier"] = tier["tier"]
        return url

    def _request_tier(self, ctx) -> int | None:
        info = getattr(ctx, "proxy_info", None)
        if info is None:
            return None
        return self._proxy_tier_by_url.get(getattr(info, "url", None) or "")

    def _result_ok(self, result) -> bool:
        for call in result.push_data_calls:
            data = call.get("data") if isinstance(call, dict) else getattr(call, "data", None)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("needs_browser"):
                    return False
        return True

    def _crawl_already_ended(self) -> bool:
        """After a limit cut or an operator stop nothing may be fetched again, also after a
        restart."""
        return bool(self.rt.get("stop_reason")) or self.doc["progress"]["discovery"] == "cut" or self.state == "stopping"

    def _start_crawler(self) -> None:
        self.crawler = self._build_crawler()
        self.crawler_task = asyncio.create_task(self.crawler.run([]), name=f"crawler-{self.run_id}")

    async def _run_crawl(self) -> None:
        self.follower_task = asyncio.create_task(self._follow(), name=f"follower-{self.run_id}")
        if self._no_crawl or self._crawl_already_ended():
            if self.state == "stopping" and not self.rt.get("stop_handled"):
                async with self.lock:
                    with self.svc.db.tx():
                        self._handle_operator_stop()
                        self._persist()
            await self._supervise()
            return
        self._start_crawler()
        if not self.rt.get("discovery_resolved"):
            ok = await self._resolve_discovery()
            if not ok:
                return
        if not self.rt.get("feeder_done") and self.doc["progress"]["discovery"] not in ("cut", "finished"):
            self.feeder_task = asyncio.create_task(self._feed(), name=f"feeder-{self.run_id}")
        await self._supervise()

    # ================================================================== feeding
    @url_policy
    def _passes_filters(self, canonical: str, source: str = "sitemap") -> bool:
        """``url_exclude`` keeps a URL out of the queue. ``url_include`` says what is *extracted*:
        it filters sitemap entries (a sitemap entry is a page to scrape) but not crawled links
        (a link is also a way to discover); a crawled page outside it is ``not_target``
        (README 4.4 as clarified)."""
        if source != "link" and self.include and not matches_any(self.include, canonical):
            return False
        if matches_any(self.exclude, canonical):
            return False
        return True

    def _outside_include(self, canonical: str, ud) -> bool:
        if not self.include or self.config.get("single_page"):
            return False
        return ud.get("src") in ("link", "start") and not matches_any(self.include, canonical)

    async def enqueue(self, entries: list[dict], *, forefront: bool = False, backlog_seqs: list[int] | None = None,
                      feed: tuple[str, int] | None = None) -> int:
        """entries: {url, source (start|sitemap|link|list|refetch), source_key?, unique_key? (refetch)}.

        Three phases: the checks that await (DNS, robots.txt) without touching the
        job record; the request queue; then one synchronous transaction that records the URLs,
        the counters, the consumed backlog rows and the feeder position."""
        from crawlee import Request

        if self.state == "waiting_for_credentials":
            raise ProxyAuthenticationRequired()
        job_site = self.doc["site"]
        decisions: list[tuple] = []
        new_requests = []
        seen_now: set[str] = set()
        batch = UrlPatternBatch()
        for entry in entries:
            with url_pattern_batch(batch):
                url = entry["url"]
                source = entry["source"]
                try:
                    canonical = canonical_url(url)
                    site = site_key(url)
                except ValueError:
                    continue
                listed = source == "list"
                if source in ("link", "sitemap"):
                    if source == "link" and site != job_site:
                        continue
                    if not self._passes_filters(canonical, source):
                        decisions.append(("filtered",))
                        continue
                if source == "link":
                    rule = self._link_state_rule(canonical)
                    if rule:
                        decisions.append(("rule", rule))
                        continue
                ukey = entry["unique_key"] if source == "refetch" else self._ukey(canonical, entry.get("source_key"))
                if source != "refetch":
                    if ukey in seen_now or self.svc.db.one("SELECT 1 FROM urls WHERE run_id=? AND unique_key=?", (self.run_id, ukey)):
                        decisions.append(("duplicate",))
                        continue
                seen_now.add(ukey)
            # DNS may return from cache without suspending: yield explicitly before
            # resetting the continuous synchronous allowance. No regex context spans it.
            await asyncio.sleep(0)
            batch = UrlPatternBatch()
            if await url_is_private(url):
                decisions.append(("private", ukey, url, canonical, site, source, entry.get("source_key"), listed or source == "sitemap"))
                continue
            if source != "refetch" and not await self.robots_allowed(url):
                decisions.append(("robots", ukey, url, canonical, site, source, entry.get("source_key"), listed or source == "sitemap"))
                continue
            user_data: dict[str, Any] = {"src": source, "requested_url": url}
            if entry.get("source_key"):
                user_data["source_key"] = entry["source_key"]
            if source == "refetch":
                user_data["refetch_of"] = ukey.rsplit("\x1frefetch", 1)[0]
            headers = dict(self.config.get("headers") or {})
            try:
                # refetch keys are unique by their suffix, so no always_enqueue (it refuses custom keys)
                new_requests.append(Request.from_url(url, unique_key=ukey, user_data=user_data, headers=headers or None))
            except ValueError:
                log.warning("job %s: a URL Crawlee refuses was skipped", self.run_id)
                continue
            decisions.append(("queued", ukey, url, source, entry.get("source_key")))
        if self.state == "waiting_for_credentials":
            raise ProxyAuthenticationRequired()
        if new_requests:
            await self.rq.add_requests(new_requests, forefront=forefront, wait_for_all_requests_to_be_added=True)
        with self.svc.db.tx():
            c = self.counters()
            skipped = c["urls_skipped"]
            for d in decisions:
                kind = d[0]
                if kind == "filtered":
                    skipped["filtered"] += 1
                elif kind == "rule":
                    by_rule = c.setdefault("links_skipped", {})
                    by_rule[d[1]] = int(by_rule.get(d[1], 0)) + 1
                elif kind == "duplicate":
                    skipped["duplicate"] += 1
                elif kind in ("private", "robots"):
                    _, ukey, url, canonical, site, source, skey, listable = d
                    skipped["private_address" if kind == "private" else "robots"] += 1
                    if listable:
                        # refusals of sitemap URLs are the site's/operator's decision (outcome-neutral);
                        # refusals of an explicit urls list are not
                        reason = "private_address_refused" if kind == "private" else "robots_disallowed"
                        why = "the host resolves to a private address" if kind == "private" else "robots.txt disallows this URL"
                        self._unextracted(canonical, url, skey, site, reason, None, None, 0, why, neutral=source == "sitemap")
                    if source != "refetch":
                        self._remember_url(ukey, url, source, skey, "skipped")
                elif kind == "queued":
                    _, ukey, url, source, skey = d
                    if source != "refetch":
                        self._remember_url(ukey, url, source, skey, "queued")
                        c["urls_discovered"] += 1
            if backlog_seqs:
                self.svc.db.executemany("DELETE FROM link_backlog WHERE seq=?", [(s,) for s in backlog_seqs])
            if feed is not None:
                self.rt[feed[0]] = int(self.rt.get(feed[0], 0)) + feed[1]
            self._persist()
        return len(new_requests)

    def _ukey(self, canonical: str, source_key: str | None) -> str:
        """Queue canonical path identity, ignoring only learned dynamic query parameters.

        URL shape heuristics group trap budgets; they never merge resource paths.
        """
        from app.core.linkrules import key_url

        return page_key(key_url(canonical, self.dynamic.learned), source_key)

    def _link_state_rule(self, canonical: str) -> str | None:
        """Stateful link rules: a trapped URL pattern; learns dynamic parameters."""
        from app.core.linkrules import trap_pattern

        if "trap_budget" in self.link_rules and trap_pattern(canonical) in (self.rt.get("trapped_patterns") or {}):
            return "trap_budget"
        if "dynamic_params" in self.link_rules:
            learned = self.dynamic.observe(canonical)
            if learned:
                self.rt["dynamic_params"] = sorted(self.dynamic.learned)
                msg = f"query parameter '{learned}' takes a new value on almost every link: ignored when telling pages apart"
                self.add_error("dynamic_parameter_ignored", msg, severity="warning")
                self.timeline("links", msg)
        return None

    @url_batch
    def _filter_links(self, page, links: list[str]) -> tuple[list[str], dict[str, int]]:
        """Skip lists and rel=nofollow, before the links enter the queue; counts per rule."""
        from app.core.linkrules import offsite_rule, raw_skip, url_skip

        rules = self.link_rules
        skips: dict[str, int] = {}
        follow: set[str] = set()
        nofollow: set[str] = set()
        for raw, resolved, nf in page.anchors():
            r = raw_skip(raw, rules)
            if r is None and resolved is not None:
                r = offsite_rule(resolved, rules)
            if r:
                skips[r] = skips.get(r, 0) + 1
                continue
            if resolved is None:
                continue
            try:
                (nofollow if nf else follow).add(canonical_url(resolved))
            except ValueError:
                pass
        kept = []
        for u in links:
            try:
                cu = canonical_url(u)
            except ValueError:
                continue
            r = "nofollow" if ("nofollow" in rules and cu in nofollow and cu not in follow) else url_skip(cu, rules)
            if r == "facet_params" and self._looks_like_target_url(cu):
                r = None  # an explicit target URL can use size/color/etc as identity
            if r:
                skips[r] = skips.get(r, 0) + 1
                continue
            kept.append(u)
        return kept, skips

    def _link_header(self, ctx) -> str | None:
        try:
            headers = getattr(ctx.http_response, "headers", None) or {}
            for k in ("link", "Link"):
                v = headers.get(k) if hasattr(headers, "get") else None
                if v:
                    return v if isinstance(v, str) else ", ".join(v)
        except Exception:
            return None
        return None

    def _canonical_step(self, ctx, page, canonical: str, site: str, ud, out: dict) -> dict | None:
        """A duplicate outcome for a page whose canonical was already collected, else None
        (the page's records are keyed by its declared canonical when it has one)."""
        from app.jobs.canon import identity, only_pagination_differs, rekey

        if "canonical" not in self.link_rules or self.canon.distrusted or ud.get("src") not in ("link", "sitemap", "start"):
            return None
        cls = out.get("page_class")
        if cls not in ("extracted", "incomplete", "no_template", "not_target"):
            return None
        self.canon.load(self.svc.db, self.run_id)
        declared = self.canon.declared(page, self._link_header(ctx), site)
        eff = declared or canonical
        target = cls != "not_target"
        records = out.get("records") or []
        # what registered a canonical: a product's identity, "target" without one, or "page" (a listing)
        ident = (identity(records[0]["data"]) if records else None) or "\x00target" if target else "\x00page"
        if eff in self.canon.seen:
            known = self.canon.seen.get(eff) or "\x00target"
            if target != (known != "\x00page") or (target and ident[0] != "\x00" and known[0] != "\x00" and ident != known):
                # a product and a listing, or two different products, under one canonical: the
                # canonical is wrong for this page, which is collected under its own address
                why = self.canon.note_mismatch(eff, ident.strip("\x00"), known.strip("\x00"))
                if why:
                    self._distrust_canonicals(why)
                return None
            if not target and only_pagination_differs(canonical, eff):
                return None
            dup = {k: out[k] for k in ("kind", "ukey", "requested_url", "source", "source_key", "attempts", "fetch", "url", "final_url",
                                       "site", "status", "fetched_at") if k in out}
            dup.update({"page_class": "duplicate", "duplicate_of": eff})
            return dup
        self.canon.seen[eff] = ident
        if declared and declared != canonical:
            why = self.canon.note_alias(eff, target)
            if why:
                self._distrust_canonicals(why)
                return None
            out["canonical"] = eff
            if records:
                req = ud.get("requested_url", ctx.request.url)
                keyed = [{k: r[k] for k in RECORD_ORDER if k in r} for r in (rekey(rec, eff, req) for rec in records)]
                out["records"] = keyed
                out["record"] = keyed[0]
                out["own_url"] = canonical
                self.canon.keyed(keyed, canonical)
        return None

    def _distrust_canonicals(self, why: str) -> None:
        if self.canon.distrust(why):
            msg = f"canonical addresses ignored for this run: {why}"
            self.add_error("canonical_ignored", msg, severity="warning")
            self.timeline("links", msg)

    def _canon_rewrite(self) -> None:
        """Records keyed by a canonical before the run stopped trusting canonicals: own address again."""
        from app.jobs.canon import rekey

        self.canon.rewrite_pending = False
        c = self.counters()
        for rid, own in self.canon.state.get("probation") or []:
            row = self.svc.db.one("SELECT line FROM records WHERE run_id=? AND record_id=?", (self.run_id, rid))
            if row is None:
                continue
            rec = loads(row["line"])
            self.svc.db.execute("DELETE FROM records WHERE run_id=? AND record_id=?", (self.run_id, rid))
            c["records_extracted"] = max(0, c["records_extracted"] - 1)
            c["result_bytes"] = max(0, c["result_bytes"] - (len(row["line"].encode("utf-8")) + 1))
            t = self.template(rec["template_id"])
            t["record_count"] = max(0, t.get("record_count", 0) - 1)
            fixed = rekey(rec, own, rec.get("requested_url"))
            self._write_record({k: fixed[k] for k in RECORD_ORDER if k in fixed}, None)
        self.canon.state["probation"] = []

    def _trap_budget(self, item: dict, cls: str) -> None:
        """A URL pattern whose last ``trap_pattern_budget`` pages (reached by links)
        were no target page and led to no new target page stops being followed."""
        from app.core.linkrules import key_url, trap_pattern

        budget = int(self.config.get("trap_pattern_budget", 100) or 0)
        if not budget or "trap_budget" not in self.link_rules or item.get("source") != "link":
            return
        site = item.get("site") or self.doc["site"]
        if not self._target_known(site) or not (self._target_shapes or self.counters()["records_extracted"]):
            return
        pattern = trap_pattern(item["url"])
        trapped = self.rt.setdefault("trapped_patterns", {})
        if pattern in trapped:
            return
        # a target link is new the first time any page shows it (also while it still waits in the
        # backlog): filter pages that list products other pages already showed are not useful
        seen = self.__dict__.setdefault("_target_links_seen", set())
        new_targets = 0
        for u in item.get("links") or []:
            if self._targetish(u):
                try:
                    k = key_url(canonical_url(u), self.dynamic.learned)
                except ValueError:
                    continue
                if k not in seen:
                    seen.add(k)
                    if not self.svc.db.one("SELECT 1 FROM urls WHERE run_id=? AND unique_key=?", (self.run_id, page_key(k, None))):
                        new_targets += 1
        st = self.rt.setdefault("trap_stats", {}).setdefault(pattern, [0, 0])  # pages, pages since the last useful one
        st[0] += 1
        st[1] = 0 if (new_targets or cls in ("extracted", "incomplete", "no_template", "duplicate")) else st[1] + 1
        if st[1] >= budget:
            trapped[pattern] = st[0]
            msg = f"suspected trap: {pattern} ({st[0]} pages, the last {budget} found no new target page); its links are no longer followed"
            self.add_error("trap_suspected", msg, severity="warning")
            self.timeline("trap", msg)
        if len(self.rt["trap_stats"]) > 5000:
            self.rt["trap_stats"].pop(next(iter(self.rt["trap_stats"])))

    def _remember_url(self, ukey: str, url: str, source: str, source_key: str | None, status: str) -> None:
        self.svc.db.execute(
            "INSERT OR IGNORE INTO urls(run_id, unique_key, url, source, source_key, status) VALUES (?,?,?,?,?,?)",
            (self.run_id, ukey, url, source, source_key, status),
        )

    async def _feed(self) -> None:
        from app.fetching.proxy_auth import ProxyAuthenticationRequired

        cfg = self.config
        mode = self.doc["resolved"].get("discovery_mode")
        try:
            if cfg.get("single_page"):
                await self.enqueue([{"url": cfg["site_url"], "source": "start"}])
            elif mode == "url_list":
                from app.core.config import dedupe_url_list

                entries = [{"url": e["url"], "source": "list", "source_key": e.get("key")} for e in dedupe_url_list(cfg["urls"])]
                done = int(self.rt.get("list_fed", 0))  # a restarted feeder continues
                for i in range(done, len(entries), 500):
                    if self._cut_or_stopping():
                        break
                    chunk = entries[i : i + 500]
                    await self.enqueue(chunk, feed=("list_fed", len(chunk)))
            elif mode in ("crawl", "sitemap_and_crawl"):
                if not self.rt.get("start_fed"):
                    await self.enqueue([{"url": cfg["site_url"], "source": "start"}], feed=("start_fed", 1))
            if mode in ("sitemap", "sitemap_and_crawl") and not cfg.get("single_page"):
                await self._feed_sitemaps()
            if self.state == "waiting_for_credentials":
                return
            with self.svc.db.tx():
                self.rt["feeder_done"] = True
                self._persist()
        except asyncio.CancelledError:
            raise
        except ProxyAuthenticationRequired:
            return  # waiting_for_credentials: retain the committed feeder cursor
        except UrlPatternError as exc:
            await self._url_pattern_failed(exc)
        except Exception as exc:
            log.exception("feeder of %s failed", self.run_id)
            with self.svc.db.tx():
                self.rt["discovery_partial"] = True
                self.add_error("internal_error", f"discovery failed ({type(exc).__name__})", severity="warning")
                self.rt["feeder_done"] = True
                self._persist()

    async def _feed_sitemaps(self) -> None:
        """Commit per-source cursors only after their queue batch commits.

        The iterator mutates a private copy: a supervisor checkpoint while enqueue
        awaits must never contain a cursor for URLs the queue has not accepted.
        Legacy sitemap_fed offsets are deliberately replayed through normal dedup.
        """
        from app.jobs.discovery import SITEMAP_CAP, iter_sitemaps, sitemap_report, _origin

        urls = list(self.rt.get("sitemap_retry_roots") or self.rt.get("sitemaps") or [])
        if not urls:
            urls = [self.config.get("sitemap_url") or _origin(self.config["site_url"]) + "/sitemap.xml"]
        state = copy.deepcopy(self.rt.get("sitemap_traversal") or {})

        def checkpoint():
            with self.svc.db.tx():
                self.rt["sitemap_traversal"] = copy.deepcopy(state)
                self.rt["sitemap_feeding_started"] = True
                report = sitemap_report(state)
                self.doc["sitemap_discovery"] = report
                self.rt["sitemap_partial"] = not report["complete"] or bool(self.rt.get("sitemap_probe_partial"))
                self._persist()

        # Recollect retries failed document URLs as well as failed source files.
        retry_urls = self.rt.get("sitemap_retry_urls") or []
        done = int(self.rt.get("sitemap_retry_fed", 0))
        for i in range(done, len(retry_urls), 100):
            if self._cut_or_stopping():
                return
            chunk = retry_urls[i:i + 100]
            await self.enqueue([dict(e, source="list", source_key=e.get("key")) for e in chunk],
                               feed=("sitemap_retry_fed", len(chunk)))

        async def enqueue_source_batch(entries):
            parent_id = (self.doc.get("recollect_of") or {}).get("run_id")
            if parent_id:
                marks = ",".join("?" for _ in entries)
                prior = self.svc.db.all(
                    "WITH RECURSIVE ancestors(run_id) AS (SELECT ? UNION SELECT runs.parent_run_id "
                    "FROM runs JOIN ancestors ON runs.run_id=ancestors.run_id WHERE runs.parent_run_id IS NOT NULL) "
                    "SELECT url FROM urls WHERE status='done' AND run_id IN (SELECT run_id FROM ancestors) "
                    f"AND url IN ({marks})", (parent_id, *(e["url"] for e in entries)))
                completed = {r["url"] for r in prior}
                entries = [e for e in entries if e["url"] not in completed]
            if entries:
                await self.enqueue(entries, feed=("sitemap_fed", len(entries)))

        batch: list[dict] = []
        async for loc, _lastmod in iter_sitemaps(urls, self.aux_fetch, state=state, max_entries=SITEMAP_CAP):
            if self._cut_or_stopping():
                return
            batch.append({"url": loc, "source": "sitemap"})
            if len(batch) >= 100:
                await enqueue_source_batch(batch)
                checkpoint()
                batch = []
        if self._cut_or_stopping():
            return
        if batch:
            await enqueue_source_batch(batch)
        checkpoint()

    def _cut_or_stopping(self) -> bool:
        return self.doc["progress"]["discovery"] == "cut" or self.state in ("stopping", "waiting_for_credentials") or self._shutdown

    async def _drain_backlog(self) -> None:
        """Enqueues links and refetches recorded by the follower (after its transaction committed)."""
        while not self._shutdown:
            # links that look like target pages first, wherever they are in the backlog: a crawl that
            # finds thousands of listing variants per page still reaches its products
            rows = self.svc.db.all("SELECT seq, url, source, source_key, unique_key, forefront FROM link_backlog WHERE run_id=? "
                                   "ORDER BY forefront DESC, seq LIMIT 500", (self.run_id,))
            if not rows:
                return
            if self.state == "waiting_for_credentials":
                return  # pause preserves the backlog for Resume
            if self._cut_or_stopping() or self.state != "running" or self.crawler_task is None or self.crawler_task.done():
                with self.svc.db.tx():
                    self.svc.db.execute("DELETE FROM link_backlog WHERE run_id=? AND source != 'refetch'", (self.run_id,))
                if any(r["source"] == "refetch" for r in rows) and (self.crawler_task is None or self.crawler_task.done()) \
                        and not self._cut_or_stopping() and self.state == "running":
                    self._start_crawler()  # refetches after the crawler already ended
                    continue
                return
            # links that look like targets (an active mapping's url_patterns, url_include) go to the
            # front of the queue, listings behind: a trial run reaches its records first, a full
            # crawl still fetches every listing
            with url_pattern_batch():
                front = [r for r in rows if r["forefront"] or (r["source"] == "link" and self._targetish(r["url"]))]
                back = [r for r in rows if r not in front]
            for forefront, part in ((1, front), (0, back)):
                if part:
                    await self.enqueue(
                        [{"url": r["url"], "source": r["source"], "source_key": r["source_key"], "unique_key": r["unique_key"]} for r in part],
                        forefront=bool(forefront), backlog_seqs=[r["seq"] for r in part],
                    )

    # ================================================================== admission control (max_records)
    def _maybe_target(self, request) -> bool:
        """Whether the page may produce a record, as far as that is knowable before the fetch."""
        ud = request.user_data
        if ud.get("src") in ("list", "refetch") or self.config.get("single_page"):
            return True
        try:
            canonical = canonical_url(request.url)
            site = site_key(request.url)
        except ValueError:
            return True
        pats = self._target_patterns.get(site)
        if pats is None:
            from app.jobs.discovery import target_patterns

            pats = target_patterns(self.mappings_for(site), self.config.get("url_include") or [])
            self._target_patterns[site] = pats
        if not pats:
            return True  # no pattern known: every page may be a target
        return matches_any(pats, canonical)

    def _variant_rows_static(self, tmpl: dict, data: dict, page) -> list:
        from app.extraction.variants import variant_rows

        return variant_rows(tmpl, data, [], None, page.base_url, page=page)

    def _variant_fetcher(self, ctx, browser_run: bool):
        """The shop's product JSON for a variant source: one light request through the
        page's own session — the cleared browser context when the page came through the browser
        (a Cloudflare host never sees a plain HTTP client for it), else Crawlee's HTTP client
        with the session's proxy — after robots.txt and the host's politeness delay. A host that
        needs the browser while this page came over HTTP gets no request: the in-page sources
        are tried instead and a debug entry says so."""

        async def fetch(u: str):
            if self.state == "waiting_for_credentials":
                raise ProxyAuthenticationRequired()
            try:
                host = host_of(u)
            except ValueError:
                return None
            if await url_is_private(u) or not await self.robots_allowed(u):
                return None
            if not browser_run and host in (self.rt.get("browser_hosts") or []):
                self.add_error("debug", f"variants: shop JSON not fetched over HTTP for a host that needs the browser ({u})", url=u, severity="debug")
                return None
            await self.throttle.wait(host)
            if self.state == "waiting_for_credentials":
                raise ProxyAuthenticationRequired()
            try:
                if browser_run and getattr(ctx, "page", None) is not None:
                    resp = await ctx.page.context.request.get(u, timeout=30000, headers={"Accept": "application/json"})
                    if is_proxy_auth_failure(status=resp.status, proxy_used=self.tiers.enabled):
                        raise ProxyAuthenticationRequired()
                    if resp.status >= 400:
                        return None
                    return json.loads(await resp.text())
                resp = await ctx.send_request(u, headers={"Accept": "application/json"})
                if is_proxy_auth_failure(status=getattr(resp, "status_code", None), proxy_used=self.tiers.enabled):
                    raise ProxyAuthenticationRequired()
                if getattr(resp, "status_code", 200) >= 400:
                    return None
                body = resp.read()
                if hasattr(body, "__await__"):
                    body = await body
                return json.loads(body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body)
            except Exception as exc:
                if is_proxy_auth_failure(exc, proxy_used=self.tiers.enabled):
                    await self._pause_for_proxy_auth()
                    raise ProxyAuthenticationRequired() from None
                self.add_error("debug", f"variants: shop JSON {u} failed ({type(exc).__name__})", url=u, severity="debug")
                return None

        return fetch

    def _crawl_scope(self) -> list:
        if not hasattr(self, "_scope_cache"):
            from app.core.scope import crawl_patterns

            self._scope_cache = crawl_patterns(self.config)
        return self._scope_cache

    def _target_known(self, site: str) -> bool:
        """Whether target pages can be told by URL (url_include, a mapping's url_patterns, shapes of
        pages already found to be targets): then links outside the walk scope are queued only when
        they look like targets; otherwise every link of a walked page is queued (fetched, not walked)."""
        pats = self._target_patterns.get(site)
        if pats is None:
            from app.jobs.discovery import target_patterns

            pats = target_patterns(self.mappings_for(site), self.config.get("url_include") or [])
            self._target_patterns[site] = pats
        return bool(pats) or bool(self._target_shapes)

    @url_batch
    def _ranked_links(self, page) -> list[str]:
        """The page's links in crawl order: links that look like targets
        (``url_include``, an active mapping's ``url_patterns``, URL shapes of pages that already
        turned out to be targets) first; then content links, one per URL shape first; then
        header/nav/footer/menu links; pagination/sort/filter variants last (a full crawl still
        needs them). Duplicates out. Before, the first 2000 hrefs in document order were taken:
        a category whose 2186 hrefs start with a site-wide menu lost its product links."""
        from app.extraction.sampling import ProbePlan, is_variant, link_groups

        try:
            site = site_key(page.url)
        except ValueError:
            return []
        groups, _ = link_groups(page, site)
        seen: set[str] = set()
        first: list[str] = []
        for g in groups:
            for u in g["urls"]:
                if self._looks_like_target_url(u):
                    first.append(u)
                    seen.add(u)
        content = [u for u in ProbePlan([g for g in groups if g["in_menus"] < 0.5], limit=10 ** 6, misses=10 ** 6).drain() if u not in seen]
        chrome = [u for u in ProbePlan([g for g in groups if g["in_menus"] >= 0.5], limit=10 ** 6, misses=10 ** 6).drain() if u not in seen]
        variants: list[str] = []
        for u in page.links():
            try:
                c = canonical_url(u)
            except ValueError:
                continue
            if is_variant(c) and c not in seen and c not in variants:
                variants.append(c)
        return (first + content + chrome + variants)[:5000]

    def _looks_like_target_url(self, url: str) -> bool:
        try:
            canonical = canonical_url(url)
            site = site_key(url)
        except ValueError:
            return False
        pats = self._target_patterns.get(site)
        if pats is None:
            from app.jobs.discovery import target_patterns

            pats = target_patterns(self.mappings_for(site), self.config.get("url_include") or [])
            self._target_patterns[site] = pats
        if bool(pats) and matches_any(pats, canonical):
            return True
        if self._target_shapes:
            from app.extraction.sampling import url_shape

            return url_shape(canonical) in self._target_shapes
        return False

    def _targetish(self, url: str) -> bool:
        """Whether a link likely leads to a target page, for the trap budget and the backlog order:
        by the URL shapes of pages that turned out to be targets once there are some (a mapping's
        url_patterns may be broad, e.g. a selector-only template), else by ``_looks_like_target_url``."""
        if self._target_shapes:
            from app.extraction.sampling import url_shape

            try:
                return url_shape(canonical_url(url)) in self._target_shapes
            except ValueError:
                return False
        return self._looks_like_target_url(url)

    def _in_progress_keys(self) -> set[str]:
        try:
            state = getattr(self.rq._client, "_state", None)
            current = getattr(state, "current_value", None) if state else None
            return set(getattr(current, "in_progress_requests", set()) or set())
        except Exception:
            return set()

    async def _admit(self, request) -> None:
        """Do not start a target page when the records written, pushed or possibly produced by
        pages in progress already reach ``max_records`` (README 4.8)."""
        limit = self.config.get("max_records")
        ukey = request.unique_key
        self._admitted.discard(ukey)
        self._pending_records.pop(ukey, None)
        if not limit or not self._maybe_target(request):
            return
        deadline = time.monotonic() + ADMISSION_WAIT_SECONDS
        while True:
            # a pushed record is applied by the follower within a second; a stale entry (its
            # handler result was never committed) stops counting after a minute
            stale = time.monotonic() - 60
            for k in [k for k, at in self._pending_records.items() if at < stale]:
                self._pending_records.pop(k, None)
            produced = self.counters()["records_extracted"] + len(self._pending_records)
            if produced >= limit or self._cut_or_stopping():
                await asyncio.sleep(0.5)  # the follower cuts the crawl; do not spin meanwhile
                self._defer(request, "max_records reached")
            in_progress = self._in_progress_keys()
            self._admitted &= in_progress
            if produced + len(self._admitted) < limit:
                self._admitted.add(ukey)
                return
            if time.monotonic() >= deadline:
                self._defer(request, "waiting for pages in progress (max_records)")
            await asyncio.sleep(0.2)

    def _defer(self, request, reason: str):
        ud = request.user_data
        ud["defers"] = int(ud.get("defers", 0)) + 1
        self._admitted.discard(request.unique_key)
        raise Deferred(reason)

    # ================================================================== crawling hooks
    @fail_on_url_pattern_error
    async def _pre_nav(self, ctx) -> None:
        from crawlee.crawlers._adaptive_playwright._adaptive_playwright_crawling_context import AdaptiveContextError

        request = ctx.request
        if self.state == "waiting_for_credentials":
            self._defer(request, "upstream proxy authentication requires Resume")
        ud = request.user_data
        url = request.url
        try:
            host = host_of(url)
        except ValueError:
            host = ""
        # one attempt = one token; the static run and the browser fall-through of the same attempt
        # share it, so the delay, the counters and the admission run once
        token = f"{request.retry_count}:{request.session_rotation_count or 0}:{ud.get('defers', 0)}"
        if self._attempt_token.get(request.unique_key) != token:
            self._attempt_token[request.unique_key] = token
            # a proxy tier older than the host's current one (another page escalated meanwhile)
            req_tier = self._request_tier(ctx)
            cur = self.tiers.tier_for(host) if self.tiers.enabled and host else None
            if req_tier is not None and cur is not None and req_tier < cur["tier"]:
                self._defer(request, "stale proxy tier")
            if not ud.get("aux"):
                if (
                    self.pages_started + int(self.rt.get("navigation_actions", 0))
                    >= self.config["max_pages"]
                ):
                    self._cut("max_pages")
                    self._defer(request, "page budget reached")
                await self._admit(request)
                self.pages_started += 1
                self.rt["pages_started"] = self.pages_started
            if not self.tiers.enabled:
                by = self.counters()["proxy_requests_by_tier"]
                by["none"] = by.get("none", 0) + 1
            await self.throttle.wait(host)
            ud["t0"] = time.monotonic()  # the host's response time
        if self.state == "waiting_for_credentials":
            self._defer(request, "upstream proxy authentication requires Resume")
        try:
            page = getattr(ctx, "page", None)
        except AdaptiveContextError:
            return
        if page is None:
            return
        headers = self.config.get("headers") or {}
        if headers:
            await page.set_extra_http_headers(headers)
        from app.fetching.oneoff import private_route_guard

        block_heavy = self.config.get("block_heavy_resources", True)

        async def _allowed(route):
            try:
                if block_heavy and route.request.resource_type in ("image", "media", "font"):
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass

        # every request of the page passes the private-address rule
        from app.fetching.document_policy import bind_document_policy

        async def destination_allowed(target):
            try:
                allowed = await self._document_destination_allowed(
                    url, target, auxiliary=bool(ud.get("aux")),
                    robots_allowed=lambda target: self._browser_redirect_robots_allowed(page, target),
                )
            except ProxyAuthenticationRequired:
                raise  # Resume retains ordinary retry eligibility.
            except Exception:
                request.no_retry = True
                raise
            if not allowed:
                request.no_retry = True
            return allowed

        proxy_info = getattr(ctx, "proxy_info", None)
        proxy_auth = None if proxy_info is None else {
            "server": f"{proxy_info.scheme}://{proxy_info.hostname}:{proxy_info.port}",
            "username": proxy_info.username, "password": proxy_info.password,
        }
        policy = await bind_document_policy(
            page, self.browser or "chromium", url, destination_policy=destination_allowed,
            proxy_auth=proxy_auth,
            challenge_post_allowed=bool(self.browser == "camoufox" and self.config.get("challenge_click")
                                        and self.settings.challenge_click_allowed and self.route_budget().click_usable()),
            on_proxy_auth_required=lambda: request.user_data.__setitem__("_wss_proxy_auth_failed", True),
            timeout_s=min(3.0, float(self.config.get("page_timeout_seconds") or 45)),
        )

        async def native_allowed(route):
            if route.request.is_navigation_request():
                await policy.request(route)
            else:
                await _allowed(route)

        await page.route("**/*", private_route_guard(None, native_allowed))

    def _is_browser(self, ctx) -> bool:
        try:
            return getattr(ctx, "page", None) is not None
        except Exception:  # AdaptiveContextError: a static run
            return False

    def _attempts(self, request) -> int:
        ud = request.user_data
        return request.retry_count + 1 + int(ud.get("http_retries", 0)) + int(ud.get("rate_limited", 0))

    def _retire(self, ctx) -> None:
        session = getattr(ctx, "session", None)
        if session is not None:
            try:
                session.retire()
            except Exception:
                pass
        self.prot()["sessions_retired"] += 1

    def _check_session_tier(self, ctx) -> None:
        """A session belongs to the exit address (tier) it was first used with; its cookies are
        never replayed through another tier."""
        session = getattr(ctx, "session", None)
        req_tier = self._request_tier(ctx)
        if session is None or req_tier is None:
            return
        try:
            host = host_of(ctx.request.url)
        except ValueError:
            return
        tiers = session.user_data.setdefault("tiers", {})  # cookies are per host, so is the tag
        tagged = tiers.get(host)
        if tagged is None:
            tiers[host] = req_tier
        elif tagged != req_tier:
            self._retire(ctx)
            self._defer(ctx.request, "session belongs to another proxy tier")

    @fail_on_url_pattern_error
    async def _handle(self, ctx) -> None:
        """Request handler. Writes only through the context (push_data, key-value store)."""
        request = ctx.request
        ud = request.user_data
        url = request.url
        host = host_of(url)
        browser_run = self._is_browser(ctx)
        status = ctx.http_response.status_code
        if is_proxy_auth_failure(status=status, proxy_used=self.tiers.enabled):
            await self._pause_for_proxy_auth()
            self._defer(request, "upstream proxy authentication requires Resume")
        fetch_mode = self.config["fetch_mode"]
        self._check_session_tier(ctx)
        if ud.get("aux"):
            await self._handle_aux(ctx, host, status, browser_run)
            return
        passed_step: str | None = None
        html: str | None = None
        if browser_run:
            from app.fetching.challenge import PageNotLoaded, settle

            if not await settle(ctx.page, 20.0):
                # still loading ("Loading https://…") is not a challenge; Crawlee retries the request
                raise PageNotLoaded("the page did not finish loading within 20 s")
            raise_document_policy_error(ctx)
            verdict = await detect_page(ctx.page, status)
        else:
            body = await ctx.http_response.read()
            html = body.decode("utf-8", errors="replace")
            verdict = detect_html(html, status)

        if status == 429 and not verdict.is_challenge:
            self._rate_limited(ctx, host)
            outcome = await self._push_failed_status(ctx, url, status, "HTTP 429 (rate limited)")
            return outcome

        if verdict.is_block:
            await self._hard_block(ctx, host, status, verdict, url)
            return
        if verdict.is_challenge:
            ud["challenged"] = True
            if not browser_run:
                if fetch_mode != "http" and self.browser:
                    self.mark_browser_host(host)
                    await ctx.push_data({"needs_browser": True})
                    return
                await self._push_blocked(ctx, status, verdict, url)
                return
            passed_step = await self._challenge_route(ctx, host, url)
            if passed_step is None:
                self._retire(ctx)
                await self._push_blocked(ctx, status, verdict, url)
                return
            session = ctx.session
            if session is not None:
                session.user_data["cleared_usage"] = session.usage_count
            try:
                await ctx.page.wait_for_load_state("load", timeout=15000)
            except Exception:
                pass
            status = 200
        elif ud.get("challenged"):
            # challenged earlier (over HTTP or in an earlier attempt), loads now
            if ud.get("challenge_escalated"):
                passed_step = "next_proxy_tier"
            elif browser_run:
                passed_step = self.browser or "chromium"
            else:
                passed_step = "http"

        if browser_run:
            await self._browser_waits(ctx.page)
            html = await page_html(ctx.page)
            raise_document_policy_error(ctx)
            final_url = ctx.page.url
            # the challenge may come back while the page settles
            if html is None or detect_html(html, None).is_challenge:
                ud["challenged"] = True
                self._retire(ctx)
                await self._push_blocked(ctx, status, detect_html(html or "", None), url)
                return
        else:
            final_url = request.loaded_url or url
        self.throttle.success(host)
        # the breaker's success (_host_ok) is decided in _classify: a server error or an unusable
        # answer is not a success

        outcome = await self._classify(ctx, html or "", final_url, status, browser_run, ud)
        if outcome is None:
            return
        if ud.get("challenged") and outcome.get("kind") == "page":
            outcome["challenge"] = {"passed_at_step": passed_step}
        if browser_run:
            raise_document_policy_error(ctx)
        await ctx.push_data(outcome)

    def _rate_limited(self, ctx, host: str) -> None:
        """429: back off (Retry-After honoured) and give the request back without using up
        ``max_retries`` and without the browser. Bounded by our own counter."""
        request = ctx.request
        ud = request.user_data
        retry_after = None
        try:
            ra = ctx.http_response.headers.get("retry-after")
            retry_after = float(ra) if ra else None
        except (TypeError, ValueError):
            retry_after = None
        self.throttle.backoff(host, retry_after)
        if retry_after:
            self.paused_until = max(self.paused_until, time.monotonic() + retry_after)
        n = int(ud.get("rate_limited", 0)) + 1
        if n <= max(10, 3 * int(self.config["max_retries"])):
            ud["rate_limited"] = n
            self._defer(request, "HTTP 429")

    async def _push_failed_status(self, ctx, url: str, status: int, error: str) -> None:
        request = ctx.request
        try:
            canonical = canonical_url(request.loaded_url or url)
            site = site_key(request.loaded_url or url)
        except ValueError:
            canonical, site = url, self.doc["site"]
        await ctx.push_data(self._item(ctx, {
            "url": canonical, "site": site, "status": status, "page_class": "fetch_failed", "error": error, "fetched_at": now_iso(),
        }))

    def _item(self, ctx, fields: dict) -> dict:
        request = ctx.request
        ud = request.user_data
        item = {
            "kind": "page",
            "ukey": request.unique_key,
            "requested_url": ud.get("requested_url", request.url),
            "source": ud.get("src"),
            "source_key": ud.get("source_key"),
            "attempts": self._attempts(request),
            "fetch": self._fetch_info(ctx),
        }
        if ud.get("refetch_of"):
            item["refetch_of"] = ud["refetch_of"]
        item.update(fields)
        return item

    async def _hard_block(self, ctx, host: str, status: int, verdict, url: str) -> None:
        """A refusal (not a challenge): next proxy tier when there is one, else ``blocked``.
        Hard blocks stay out of ``challenges_met``."""
        self.prot()["hard_blocks"] += 1
        self._retire(ctx)
        if self.tiers.can_escalate(host):
            self._escalate(host)
            self._defer(ctx.request, "hard block: next proxy tier")
        await self._push_blocked(ctx, status, verdict, url, challenge=False)

    def route_budget(self) -> "RouteBudget":
        from app.fetching.protection import RouteBudget

        prot = self.prot()
        cfg, st = self.config, self.settings
        click_skip = None if cfg.get("challenge_click") and st.challenge_click_allowed else (
            "not_allowed_by_service" if cfg.get("challenge_click") else "not_requested")
        solver_skip = None if (cfg.get("use_solver") and st.solvers_allowed and self.solver is not None) else (
            "not_requested" if not cfg.get("use_solver") else "not_allowed_by_service" if not st.solvers_allowed else "not_configured")
        return RouteBudget(
            click_skip=click_skip,
            solver_skip=solver_skip,
            counters=prot,
            click_allowed=bool(self.config.get("challenge_click")) and self.settings.challenge_click_allowed,
            clicks_limit=int(prot["clicks_limit"]),
            solver=self.solver if (self.config.get("use_solver") and self.settings.solvers_allowed) else None,
            solves_limit=int(prot["solves_limit"]),
            wait_seconds=self.settings.challenge_wait_seconds,
            click_wait_seconds=self.settings.challenge_click_wait_seconds,
            humanized=(self.browser or "chromium") == "camoufox",
        )

    async def _challenge_route(self, ctx, host: str, url: str) -> str | None:
        """Wait -> next proxy tier -> one click (if allowed and requested) -> solver (if allowed
        and requested) inside the page (the free wait comes first). The
        same route as analysis and samples (``app.fetching.protection.run_route``)."""
        from app.fetching.protection import run_route

        ud = ctx.request.user_data
        session = ctx.session
        if session is not None and session.user_data.get("cleared_usage") is not None \
                and session.usage_count - session.user_data["cleared_usage"] <= 1 and not ud.get("retired_once"):
            # challenged again right after clearance: retire the session, start a new one
            ud["retired_once"] = True
            self._retire(ctx)
            self._defer(ctx.request, "challenged again right after clearance")

        async def next_tier() -> None:
            if self.tiers.can_escalate(host):
                self._escalate(host)
                ud["challenge_escalated"] = True
                self._retire(ctx)
                self._defer(ctx.request, "challenge: next proxy tier")

        budget = self.route_budget()
        trace = {"url": url, "at": now_iso(), "browser": self.browser or "chromium", "proxy_tier": self._request_tier(ctx), "exit_ip": None, "steps": [],
                 "_proxy_url": getattr(getattr(ctx, "proxy_info", None), "url", None)}
        try:
            step = await run_route(ctx.page, budget, after_wait=next_tier, trace=trace["steps"])
        except Deferred:
            trace["steps"].append({"step": "next_proxy_tier", "outcome": "moved", "to_tier": (self.tiers.tier_for(host) or {}).get("tier")})
            await self._record_trace(ctx.page, trace, None)
            raise
        for severity, message in budget.errors:
            self.add_error("solver_error", message, url=url, severity=severity)
        await self._record_trace(ctx.page, trace, step)
        return step

    async def _record_trace(self, page, trace: dict, passed: str | None) -> None:
        """The route's trace: logged for every challenge, kept in ``protection.traces`` for the
        first ``TRACE_KEEP`` challenged pages of the job (so a run page can show why)."""
        from app.fetching.protection import page_facts, trace_line

        from app.fetching.geo import cached
        from app.fetching.protection import page_environment

        trace["passed_at_step"] = passed
        trace["cookies"], trace["final_title"] = await page_facts(page)
        geo = cached(trace.pop("_proxy_url", None))
        trace["exit_ip"] = geo.ip if geo else None
        from app.fetching.identity import env_of, preset_id_of

        trace["environment"] = {"exit": geo.public() if geo else None, "browser_view": await page_environment(page), "preset_id": preset_id_of(page),
                                **env_of(page)}
        log.info("job %s %s", self.run_id, self.clean(trace_line(trace)))
        traces = self.prot().setdefault("traces", [])
        if len(traces) < TRACE_KEEP:
            trace["url"] = self.clean(trace["url"])
            traces.append(trace)

    async def _handle_aux(self, ctx, host: str, status: int, browser_run: bool) -> None:
        """Robots.txt / sitemap / start page for discovery."""
        from app.fetching.oneoff import Fetched

        request = ctx.request
        url = request.url
        if not browser_run:
            body = await ctx.http_response.read()
            head = body[:300_000].decode("utf-8", errors="replace") if body[:2] != b"\x1f\x8b" else ""
            verdict = detect_html(head, status)
            if status == 429:
                self._rate_limited(ctx, host)
            if verdict.is_block:
                self.prot()["hard_blocks"] += 1
                self._retire(ctx)
                if self.tiers.can_escalate(host):
                    self._escalate(host)
                    self._defer(request, "hard block: next proxy tier")
                self._aux_done(request, Fetched(url, url, status, b"", {}, "blocked"))
                return
            if verdict.is_challenge and self.config["fetch_mode"] != "http" and self.browser:
                self.mark_browser_host(host)
                await ctx.push_data({"needs_browser": True})
                return
            final = request.loaded_url or url
            if final != url and await url_is_private(final):
                self._aux_done(request, None)
                return
            headers = {}
            try:
                headers = {k.lower(): v for k, v in ctx.http_response.headers.items()}
            except Exception:
                headers = {}
            self._aux_done(request, Fetched(url, final, status, body, headers, None))
            return
        from app.fetching.challenge import PageNotLoaded, settle

        if not await settle(ctx.page, 20.0):
            raise PageNotLoaded("the page did not finish loading within 20 s")
        raise_document_policy_error(ctx)
        verdict = await detect_page(ctx.page, status)
        if verdict.is_block:
            self.prot()["hard_blocks"] += 1
            self._retire(ctx)
            if self.tiers.can_escalate(host):
                self._escalate(host)
                self._defer(request, "hard block: next proxy tier")
            self._aux_done(request, Fetched(url, url, status, b"", {}, "blocked"))
            return
        if verdict.is_challenge:
            passed = await self._challenge_route(ctx, host, url)
            if passed is None:
                self._retire(ctx)
                self._aux_done(request, Fetched(url, url, status, b"", {}, "challenge"))
                return
            session = ctx.session
            if session is not None:
                session.user_data["cleared_usage"] = session.usage_count
        # the raw bytes through the page's context: its cookies (clearance) and its proxy
        if self.state == "waiting_for_credentials":
            self._defer(request, "upstream proxy authentication requires Resume")
        try:
            from app.fetching.destinations import document_destination_allowed
            from app.fetching.oneoff import context_get

            async def destination_allowed(target):
                return await document_destination_allowed(
                    target, site_url=url, rules={"sensitive_path", "action_params"},
                    cancelled=lambda: self._shutdown or self.state != "running" or self._cut_or_stopping(),
                )

            fetched = await context_get(
                ctx.page.context, url, headers=self.config.get("headers") or None,
                destination_policy=destination_allowed, proxy_used=self.tiers.enabled,
            )
            if fetched is not None and is_proxy_auth_failure(
                fetched.error, status=fetched.status, proxy_used=self.tiers.enabled,
            ):
                raise ProxyAuthenticationRequired()
            self._aux_done(request, fetched)
        except Exception as exc:
            if is_proxy_auth_failure(exc, proxy_used=self.tiers.enabled):
                await self._pause_for_proxy_auth()
                self._defer(request, "upstream proxy authentication requires Resume")
            self._aux_done(request, Fetched(url, url, None, b"", {}, type(exc).__name__))

    async def _browser_waits(self, page) -> None:
        selectors = []
        for site_maps in self.mappings.values():
            for entry in site_maps.values():
                sel = (entry["mapping"].get("match") or {}).get("selector")
                if sel:
                    selectors.append(sel)
        if self.config.get("target_page_selector"):
            selectors.append(self.config["target_page_selector"])
        ms = self.config.get("wait_for_selector_ms", 10000)
        if selectors and ms:
            try:
                await page.wait_for_selector(", ".join(selectors), timeout=ms, state="attached")
            except Exception:
                pass
        if self.config.get("wait_after_load_ms"):
            await asyncio.sleep(self.config["wait_after_load_ms"] / 1000)

    def mark_browser_host(self, host: str) -> None:
        hosts = self.rt.setdefault("browser_hosts", [])
        if host not in hosts:
            hosts.append(host)

    def _escalate(self, host: str) -> None:
        if self.tiers.escalate(host):
            tier = self.tiers.tier_for(host)
            self.timeline("tier_change", f"{host} moved to proxy tier {tier['tier'] if tier else '?'}")
            self.counters()["current_proxy_tier"] = tier["tier"] if tier else None
            self.rt["host_level"] = self.tiers.host_level
            self.prot()["tier_escalations"] = int(self.prot().get("tier_escalations", 0)) + 1

    # ---------------------------------------------------------------- stop on trouble
    def timeline(self, kind: str, message: str) -> None:
        """Notable moments for the run summary: start, discovery decided, first block, tier change,
        repair requested/done, pauses, problems, stop. At most 50 kept (the first 10, the last 40)."""
        tl = self.doc.setdefault("timeline", [])
        tl.append({"at": now_iso(), "kind": kind, "message": self.clean(message)[:300]})
        if len(tl) > 50:
            del tl[10:len(tl) - 40]

    def _observe(self, host: str, ok: bool, seconds: float | None) -> None:
        """One page of ``host`` ended; the throttle may slow the host down or relax it."""
        if not host:
            return
        got = self.throttle.observe(host, ok, seconds)
        if got is None:
            return
        kind, words = got
        self.timeline(kind, words)
        sd = self.rt.setdefault("slowdown", {})
        factor = self.throttle.factor(host)
        if factor > 1:
            sd[host] = {"factor": factor, "since": (sd.get(host) or {}).get("since") or now_iso(), "reason": words}
        else:
            sd.pop(host, None)

    def _soft_retry(self, ctx, ud, url: str, host: str, elapsed: float | None, base: dict, soft: tuple[str, str]) -> dict:
        """An unusable answer with status 200 — counted, retried like a server error (same
        route, growing back-off, at most ``max_retries``), then ``fetch_failed`` "unusable answer: …"."""
        code, words = soft
        c = self.counters()
        c["soft_errors"] = int(c.get("soft_errors", 0)) + 1
        by = c.setdefault("soft_errors_by_reason", {})
        by[code] = int(by.get(code, 0)) + 1
        self._observe(host, False, elapsed)
        if not self.rt.get("first_soft_error"):
            self.rt["first_soft_error"] = True
            self.timeline("soft_error", f"unusable answer (status 200) from {url}: {words}; fetched again")
        if host:
            self._host_trouble(host, f"unusable answer: {words}")
        n = int(ud.get("http_retries", 0)) + 1
        if n <= int(self.config["max_retries"]):
            ud["http_retries"] = n
            if host:
                self.throttle.backoff(host, min(2.0 ** n, 30.0))
            self._defer(ctx.request, f"unusable answer: {code}")
        return base | {"page_class": "fetch_failed", "error": f"unusable answer: {words}", "soft_error": code}

    def _host_ok(self, host: str) -> None:
        b = (self.rt.get("breaker") or {}).get(host)
        if b and (b.get("fails") or b.get("pauses")):
            b["fails"] = 0
            b["pauses"] = 0

    def _host_trouble(self, host: str, why: str) -> None:
        """A run of server errors, timeouts or network errors on a host: after
        ``breaker_failures`` in a row, pause the host with a growing wait; after
        ``breaker_max_pauses`` pauses without a success, stop the run ("site unavailable")."""
        b = self.rt.setdefault("breaker", {}).setdefault(host, {"fails": 0, "pauses": 0})
        b["fails"] += 1
        b["last"] = why[:200]
        if b["fails"] < self.settings.breaker_failures:
            return
        b["fails"] = 0
        b["pauses"] += 1
        if b["pauses"] > self.settings.breaker_max_pauses:
            message = (f"{host} looks unavailable: {self.settings.breaker_failures} failures in a row after "
                       f"{self.settings.breaker_max_pauses} pauses (last: {b['last']}); the run stopped")
            self.svc.events.create(self.run_id, "problem", self.run_id, {"problem_code": "site_unavailable", "message": message[:500]},
                                   problem_key=f"site_unavailable:{host}")
            self.timeline("stopped", message)
            self._cut("site_unavailable")
            return
        wait = self.settings.breaker_pause_seconds * (2 ** (b["pauses"] - 1))
        self.throttle.backoff(host, wait)
        self.paused_until = max(self.paused_until, time.monotonic() + wait)  # not a stall
        self.timeline("paused", f"{host}: {self.settings.breaker_failures} failures in a row ({b['last']}); paused {int(wait)} s")

    def _check_sitemap_health(self, item: dict) -> None:
        """A high share of 404 among the sitemap's URLs: problem "sitemap outdated"; in ``auto``
        discovery the job adds the link crawl by itself, otherwise the message advises it."""
        if item.get("source") != "sitemap":
            return
        sm = self.rt.setdefault("sitemap_health", {"fetched": 0, "not_found": 0, "reported": False})
        sm["fetched"] += 1
        if item.get("not_found"):
            sm["not_found"] += 1
        if sm["reported"] or sm["fetched"] < self.settings.sitemap_outdated_min:
            return
        share = sm["not_found"] / sm["fetched"]
        if share < self.settings.sitemap_outdated_share:
            return
        sm["reported"] = True
        resolved = self.doc["resolved"]
        auto = self.config.get("discovery_mode") == "auto" and resolved.get("discovery_mode") == "sitemap"
        if auto:
            resolved["discovery_mode"] = "sitemap_and_crawl"
            resolved.setdefault("reasons", []).append("sitemap outdated: the link crawl was added")
            start = self.config.get("site_url")
            if start:
                self.svc.db.execute(
                    "INSERT INTO link_backlog(run_id, url, source, source_key, unique_key, forefront) VALUES (?,?,?,?,?,1)",
                    (self.run_id, start, "link", None, None))
            advice = "the job added the link crawl from the start page"
        else:
            advice = "set Discovery = sitemap and crawl (or crawl) to find the current pages"
        message = f"{sm['not_found']} of {sm['fetched']} sitemap URLs answered 404: the sitemap looks outdated; {advice}"
        self.svc.events.create(self.run_id, "problem", self.run_id, {"problem_code": "sitemap_outdated", "message": message[:500]},
                               problem_key="sitemap_outdated:")
        self.timeline("problem", message)

    def click_usable(self) -> bool:
        return self.route_budget().click_usable()

    def solver_usable(self) -> bool:
        return self.route_budget().solver_usable()

    async def _push_blocked(self, ctx, status, verdict, url, challenge: bool = True) -> None:
        try:
            canonical = canonical_url(ctx.request.loaded_url or url)
        except ValueError:
            canonical = url
        fields: dict[str, Any] = {"url": canonical, "status": status, "page_class": "blocked", "fetched_at": now_iso()}
        if challenge:
            fields["challenge"] = {"passed_at_step": None, "kind": verdict.kind}
        await ctx.push_data(self._item(ctx, fields))

    def _fetch_info(self, ctx) -> dict:
        browser_run = self._is_browser(ctx)
        tier = self._request_tier(ctx)
        if tier is None and self.tiers.enabled:
            try:
                t = self.tiers.tier_for(host_of(ctx.request.url))
                tier = t["tier"] if t else None
            except ValueError:
                tier = None
        return {"fetch_mode": "browser" if browser_run else "http", "browser": (self.browser or "chromium") if browser_run else None, "proxy_tier": tier}

    async def _http_redirect_destination_allowed(self, request, target):
        """Static Crawlee redirects obey ordinary link admission before issuance."""
        auxiliary = bool(request.user_data.get("aux"))
        return await self._document_destination_allowed(
            request.url, target, auxiliary=auxiliary,
            robots_allowed=None if auxiliary else self._http_redirect_robots_allowed,
        )

    async def _browser_redirect_robots_allowed(self, page, target):
        if not self.config.get("respect_robots", True):
            return True
        from app.fetching.destinations import document_destination_allowed
        from app.fetching.oneoff import context_get
        from app.jobs.discovery import _origin, robots_from

        origin = _origin(target)
        if origin not in self.robots:
            async def allowed_aux(url):
                return await document_destination_allowed(
                    url, site_url=target, rules={"sensitive_path", "action_params"},
                    cancelled=lambda: self._shutdown or self.state != "running",
                )
            async def fetch_aux(url):
                return await context_get(
                    page.context, url, headers=self.config.get("headers") or None,
                    destination_policy=allowed_aux,
                )
            # Raw robots reads already use APIRequestContext in browser jobs.
            # This path never loads document bytes through it or queues a worker.
            self.robots[origin] = await robots_from(origin, fetch_aux)
            robots = self.robots[origin]
            if robots is not None:
                self.throttle.set_crawl_delay(host_of(target), robots.get_crawl_delay())
        robots = self.robots[origin]
        return robots is None or robots.is_allowed(target)

    async def _http_redirect_robots_allowed(self, target):
        if not self.config.get("respect_robots", True):
            return True
        from app.jobs.discovery import _origin, robots_from

        origin = _origin(target)
        if origin not in self.robots:
            # Never enqueue into the same crawler while its document is held.
            # The nested auxiliary request shares this document's proxy/cookies,
            # has a bounded deadline, and does not recurse into robots policy.
            async def fetch_aux(url):
                return await self._redirect_http_client.fetch_auxiliary(
                    url, headers=self.config.get("headers") or None,
                )
            robots = await robots_from(origin, fetch_aux)
            self.robots[origin] = robots
            if robots is not None:
                self.throttle.set_crawl_delay(host_of(target), robots.get_crawl_delay())
        robots = self.robots[origin]
        return robots is None or robots.is_allowed(target)

    async def _document_destination_allowed(self, initial_url, target, *, auxiliary=False, robots_allowed=None):
        from app.core.scope import in_scope
        from app.fetching.destinations import document_destination_allowed

        def cancelled():
            return (
                self._shutdown or bool(self.rt.get("stop_reason"))
                or self.state in ("stopping", "completed", "failed")
                or self.running_seconds() >= self.config["max_duration_minutes"] * 60
            )

        canonical = canonical_url(target)
        if not auxiliary and matches_any(self.exclude, canonical):
            return False
        target_page = not auxiliary and self._looks_like_target_url(target)
        scope = self._crawl_scope()
        if auxiliary or not in_scope(scope, initial_url) or target_page:
            scope = ()
        rules = {"sensitive_path", "action_params"} if auxiliary else self.link_rules
        if target_page:
            rules = rules - {"facet_params"}
        return await document_destination_allowed(
            target, site_url=initial_url, rules=rules, scope=scope,
            robots_allowed=None if auxiliary else (robots_allowed or self.robots_allowed),
            cancelled=cancelled,
        )

    async def _collect_navigation(self, ctx, page, canonical, browser_run, ud, out):
        """Discover links across listing states without changing extraction input."""
        if not browser_run:
            return
        from dataclasses import asdict
        from app.core.scope import in_scope
        from app.fetching.navigation import NavigationLimits, collect_listing_links

        scope = self._crawl_scope()
        if scope and ud.get("src") != "start" and not in_scope(scope, canonical):
            return

        def cancelled():
            return (
                bool(self.rt.get("stop_reason"))
                or getattr(self, "_shutdown", False)
                or self.state in ("stopping", "completed", "failed", "waiting_for_credentials")
                or self.running_seconds() >= self.config["max_duration_minutes"] * 60
            )

        async def allowed(action):
            from app.core.linkrules import url_skip

            document = action.kind == "navigation"
            if (
                not document
                and self.pages_started + int(self.rt.get("navigation_actions", 0))
                >= self.config["max_pages"]
            ):
                return False
            if cancelled() or await url_is_private(action.url):
                return False
            if getattr(action, "nofollow", False) and "nofollow" in self.link_rules:
                return False
            if site_key(action.url) != site_key(canonical) or url_skip(
                action.url, self.link_rules
            ):
                return False
            if scope and action.url != canonical and not in_scope(scope, action.url):
                return False
            if not await self.robots_allowed(action.url):
                return False
            if document:
                return not cancelled()
            if cancelled() or self.pages_started + int(self.rt.get("navigation_actions", 0)) >= self.config["max_pages"]:
                return False
            self.rt["navigation_actions"] = (
                int(self.rt.get("navigation_actions", 0)) + 1
            )
            return True

        async def pace(action):
            while time.monotonic() < self.paused_until:
                if cancelled():
                    return
                await asyncio.sleep(0.2)
            if not cancelled():
                await self.throttle.wait(host_of(action.url))

        def progress():
            self.last_progress = time.monotonic()

        remaining = self.config["max_duration_minutes"] * 60 - self.running_seconds()
        if remaining <= 0:
            self._cut("max_duration")
            return

        result = await collect_listing_links(
            ctx.page,
            site_url=self.config["site_url"],
            limits=NavigationLimits(
                max_steps=max(0, int(self.config["max_pages"]) - self.pages_started - int(self.rt.get("navigation_actions", 0))),
                max_seconds=int(self.config.get("page_timeout_seconds") or 45),
                max_total_seconds=remaining,
            ),
            before_action=allowed,
            cancelled=cancelled,
            pace_action=pace,
            on_progress=progress,
        )
        if result.stop_reason == "total_time_limit":
            self._cut("max_duration")
        if self.state == "waiting_for_credentials":
            self._defer(ctx.request, "upstream proxy authentication requires Resume")
        page.extra_links = result.links
        page.extra_anchors = result.anchors
        out["navigation"] = {
            k: v for k, v in asdict(result).items() if k not in ("links", "anchors", "deferred_links")
        }
        if result.bounded:
            self.rt["navigation_partial"] = True
            self.add_error(
                "navigation_partial",
                result.coverage_notes[-1],
                url=canonical,
                severity="warning",
            )

    @url_policy
    def _scope_allows_link(self, scope, url: str, known: bool) -> bool:
        from app.core.scope import in_scope

        return in_scope(scope, url) or (known and self._looks_like_target_url(url)) or not known

    async def _classify(
        self, ctx, html: str, final_url: str, status: int, browser_run: bool, ud
    ) -> dict | None:
        url = ctx.request.url
        try:
            canonical = canonical_url(final_url)
            site = site_key(final_url)
        except ValueError:
            canonical, site = canonical_url(url), site_key(url)
        if await url_is_private(final_url):
            await ctx.push_data(self._item(ctx, {"url": canonical, "status": status, "page_class": "private_address"}))
            return None
        base = self._item(ctx, {
            "url": canonical,
            "final_url": final_url,
            "site": site,
            "status": status,
            "fetched_at": now_iso(),
        })
        if status in (404, 410):
            return base | {"page_class": "fetch_failed", "not_found": True}
        try:
            host = host_of(url)
        except ValueError:
            host = ""
        elapsed = time.monotonic() - ud["t0"] if ud.get("t0") else None
        if status >= 500:
            self._observe(host, False, elapsed)
            # retried over the same route (never the browser for a failed static run),
            # with a back-off, at most max_retries times
            n = int(ud.get("http_retries", 0)) + 1
            try:
                self._host_trouble(host_of(url), f"HTTP {status}")
            except ValueError:
                pass
            if n <= int(self.config["max_retries"]):
                ud["http_retries"] = n
                try:
                    self.throttle.backoff(host_of(url), min(2.0 ** n, 30.0))
                except ValueError:
                    pass
                self._defer(ctx.request, f"HTTP {status}")
            return base | {"page_class": "fetch_failed", "error": f"HTTP {status}"}
        if status >= 400:
            self._observe(host, True, elapsed)
            self._host_ok(host)
            return base | {"page_class": "fetch_failed", "error": f"HTTP {status}"}
        # a 200 that is not the page (empty, cut off, an error or maintenance page) is retried like a 5xx
        from app.extraction.sampling import url_shape

        shape = url_shape(canonical)
        stats = self._shape_stats.get(shape)
        soft = softerror.judge(html, stats)
        if soft is not None and soft[0] in ("empty", "too_small") and (html or "").strip() and not browser_run \
                and self.config["fetch_mode"] == "auto" and self.browser:
            soft = None  # a script-built page answers a near-empty HTML: the browser fall-through below decides
        if soft is not None:
            return self._soft_retry(ctx, ud, url, host, elapsed, base, soft)
        page = Page(html, final_url, status)
        from_list = ud.get("src") == "list"
        mappings = self.mappings_for(site)
        outside = self._outside_include(canonical, ud)
        # a page without target_page_selector is not a target page even when a template
        # matches it by URL (pages the operator listed in `urls` are exempt)
        if not outside and not from_list and not passes_target_selector(page, self.config):
            outside = True
        tmpl = None if outside else pick_template(mappings, page, canonical)
        out = dict(base)
        follow_links = self.doc["resolved"].get("discovery_mode") in ("crawl", "sitemap_and_crawl") and not self.config.get("single_page")
        if follow_links:
            await self._collect_navigation(ctx, page, canonical, browser_run, ud, out)
            with url_pattern_batch():
                links, link_skips = self._filter_links(page, self._ranked_links(page))
                if link_skips:
                    out["link_skips"] = link_skips
                scope = self._crawl_scope()
                if scope:
                    from app.core.scope import in_scope

                    walked = ud.get("src") == "start" or in_scope(scope, canonical)
                    if not walked:
                        links = []  # a page outside the walk scope is fetched, its links are not followed
                    else:
                        known = self._target_known(site)
                        kept = [u for u in links if self._scope_allows_link(scope, u, known)]
                        out["scope_skipped"] = len(links) - len(kept)
                        links = kept
                out["links"] = links
        if outside:
            out["page_class"] = "not_target"  # crawled only to discover links (outside url_include / without target_page_selector)
        elif tmpl is not None:
            ext = apply_mapping(tmpl, page, self.config["extraction_mode"])
            rows = self._variant_rows_static(tmpl, ext.data, page)
            if tmpl.get("variants"):
                from app.extraction.variants import resolve, variant_rows

                variants, vinfo = await resolve(page, tmpl["variants"], self._variant_fetcher(ctx, browser_run))
                rows = variant_rows(
                    tmpl,
                    ext.data,
                    variants,
                    (vinfo or {}).get("kind"),
                    page.base_url,
                    page=page,
                )
                out["variants"] = {"count": len(variants), "source": (vinfo or {}).get("kind")}
            required = required_set(tmpl, self.config.get("required_fields") or [])
            from app.extraction.record_filter import record_filter_missing, record_matches

            selected = [
                (index, row)
                for index, row in enumerate(rows)
                if record_matches(tmpl, row[0])
            ]
            missing = sorted(
                {
                    name
                    for _index, (data, _meta, _filled) in selected
                    for name in required
                    if data.get(name) is None or data.get(name) == []
                }
            )
            missing = sorted(
                set(missing)
                | {
                    name
                    for data, _meta, _filled in rows
                    for name in record_filter_missing(tmpl, data)
                }
            )
            out["records_filtered"] = len(rows) - len(selected)
            out["template_id"] = tmpl["template_id"]
            out["mapping_version"] = tmpl["__version"]
            out["filled"] = [name for name, ok in ext.filled.items() if ok]
            view = price_view(page, tmpl)  # price status, ranges, the default currency
            gaps = (
                []
                if missing or not selected
                else self._mapping_gaps(
                    tmpl, page, [row for _index, row in selected], view
                )
            )
            if not selected and not missing:
                out["page_class"] = "not_target"
                out["records"] = []
            elif not missing and not gaps:
                out["page_class"] = "extracted"
                records = []
                for index, (data, meta, _filled) in selected:
                    price_keys = annotate_price(tmpl, data, view)
                    rec: dict[str, Any] = {
                        "record_id": record_id(
                            canonical, tmpl["template_id"], index, ud.get("source_key")
                        ),
                        "source_url": canonical,
                        "site": site,
                        "template_id": tmpl["template_id"],
                        "mapping_version": tmpl["__version"],
                        "item_index": index,
                        "extracted_at": now_iso(),
                        "data": data,
                    }
                    if meta:
                        rec["variant"] = meta
                    rec.update(price_keys)
                    req_url = ud.get("requested_url", url)
                    if req_url != canonical:
                        rec["requested_url"] = req_url
                    if ud.get("source_key"):
                        rec["source_key"] = ud["source_key"]
                    records.append({k: rec[k] for k in RECORD_ORDER if k in rec})
                out["record"] = records[0]
                out["records"] = records
            else:
                out["page_class"] = "incomplete"
                out["missing_fields"] = missing
                if gaps:
                    out["gaps"] = gaps  # variant data or a shown price the mapping does not read
        else:
            # a template matches by its own rules (url_patterns, match.selector); without one a page is a
            # target candidate by target_page_selector, JSON-LD type, url_list or a matching url_include
            target = looks_like_target(page, self.config, from_list) or bool(self.include and matches_any(self.include, canonical))
            out["page_class"] = "no_template" if target else "not_target"
        # a kind of page that was always a target page answered without its template's marks
        can_fall_through = not browser_run and self.config["fetch_mode"] == "auto" and self.browser
        lost = None if can_fall_through or from_list or out.get("records_filtered") else softerror.lost_template(stats, out["page_class"])
        if lost is not None:
            return self._soft_retry(ctx, ud, url, host, elapsed, base, lost)
        self._observe(host, True, elapsed)
        self._host_ok(host)
        target = True if out["page_class"] in ("extracted", "incomplete") else (False if out["page_class"] == "not_target" else None)
        self._shape_stats.setdefault(shape, softerror.ShapeStats()).add(len(html), softerror.is_closed(html), target)
        if out["page_class"] in ("extracted", "incomplete", "no_template"):
            self._target_shapes.add(url_shape(canonical))
        # static -> browser fall-through for pages that may need JavaScript
        if (
            not browser_run
            and self.config["fetch_mode"] == "auto"
            and self.browser
            and not ctx.request.user_data.get("static_rejected")
        ):
            from app.fetching.navigation import needs_browser_navigation

            interactive = follow_links and needs_browser_navigation(
                page, is_target=out["page_class"] != "not_target"
            )
            weak = (
                interactive
                or out["page_class"] in ("incomplete", "no_template")
                or (out["page_class"] == "not_target" and page.visible_text_chars < 300)
            )
            host = host_of(url)
            stats = self.rt.setdefault("render_stats", {}).setdefault(host, [0, 0])
            if out["page_class"] == "extracted":
                stats[0] += 1
            trusted = stats[0] >= 3 and stats[1] == 0
            if weak and (not trusted or interactive):
                ctx.request.user_data["static_rejected"] = True
                return {"needs_browser": True}
        if browser_run and ctx.request.user_data.get("static_rejected") and out["page_class"] == "extracted":
            stats = self.rt.setdefault("render_stats", {}).setdefault(host_of(url), [0, 0])
            stats[1] += 1
        dup = self._canonical_step(ctx, page, canonical, site, ud, out)
        if dup is not None:
            return dup
        if out["page_class"] in ("incomplete", "no_template"):
            key = kvs_key(page_key(canonical, ud.get("source_key")))
            store = await ctx.get_key_value_store()
            await store.set_value(key, html, content_type="text/html")
            out["kvs_key"] = key
        if out["page_class"] == "extracted":
            self._pending_records[ctx.request.unique_key] = time.monotonic()
        return out

    async def _failed(self, ctx, error: Exception) -> None:
        """Final failure after retries. Not a request-handler write: runs once per request."""
        if isinstance(error, UrlPatternError):
            await self._url_pattern_failed(error)
            return
        request = ctx.request
        ud = request.user_data
        self._admitted.discard(request.unique_key)
        if ud.get("aux"):
            self._aux_done(request, None)
            return
        status = getattr(error, "status_code", None)
        try:
            canonical = canonical_url(request.url)
        except ValueError:
            canonical = request.url
        first_line = str(error).splitlines()[0][:300] if str(error) else ""
        item = {
            "kind": "page",
            "ukey": request.unique_key,
            "url": canonical,
            "requested_url": ud.get("requested_url", request.url),
            "source": ud.get("src"),
            "source_key": ud.get("source_key"),
            "status": status if isinstance(status, int) else None,
            "page_class": "fetch_failed",
            "error": self.clean(f"{type(error).__name__}: {first_line}"),
            "attempts": self._attempts(request),
            "fetch": {"fetch_mode": "http", "browser": None, "proxy_tier": None},
            "fetched_at": now_iso(),
        }
        if ud.get("refetch_of"):
            item["refetch_of"] = ud["refetch_of"]
        if isinstance(status, int) and status in (404, 410):
            item["not_found"] = True
        if ud.get("challenged"):
            item["challenge"] = {"passed_at_step": None}
            item["page_class"] = "blocked"
        elif not isinstance(status, int) or status >= 500:
            try:  # timeouts, network errors and server errors count towards the host's breaker
                self._host_trouble(host_of(request.url), item["error"] or f"HTTP {status}")
                self._observe(host_of(request.url), False, None)
            except ValueError:
                pass
        await self.dataset.push_data(item)

    # ================================================================== follower
    async def _read_items(self, offset: int, limit: int) -> list[dict]:
        """Items ``offset+1 .. offset+limit`` of the run's dataset. Crawlee's file-system
        ``get_data`` lists and sorts every item file on each call, which made a long crawl
        slower and slower (40 % of a 60 000-page run); the files are numbered, so they
        are read by name, up to the dataset's item count."""
        client = getattr(self.dataset, "_client", None)
        path = getattr(client, "path_to_dataset", None)
        digits = getattr(client, "_ITEM_FILENAME_DIGITS", None)
        if path is None or digits is None:
            page = await self.dataset.get_data(offset=offset, limit=limit)
            return list(page.items)
        count = (await self.dataset.get_metadata()).item_count
        last = min(count, offset + limit)
        if last <= offset:
            return []

        def read() -> list[dict]:
            out = []
            for i in range(offset + 1, last + 1):
                try:
                    out.append(json.loads((Path(path) / f"{str(i).zfill(digits)}.json").read_text("utf-8")))
                except FileNotFoundError:
                    break  # not written yet: the next round reads it
            return out

        return await asyncio.to_thread(read)

    async def _follow(self) -> None:
        while True:
            try:
                offset = int(self.rt.get("follower_offset", 0))
                items = await self._read_items(offset, 200)
                if items:
                    async with self.lock:
                        self._apply_batch(items, offset)
                if self.svc.db.one("SELECT 1 FROM link_backlog WHERE run_id=? LIMIT 1", (self.run_id,)):
                    await self._drain_backlog()
                if not items:
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except UrlPatternError as exc:
                await self._url_pattern_failed(exc)
                return
            except Exception:
                log.exception("follower of %s failed; retrying", self.run_id)
                await asyncio.sleep(1)

    @url_batch
    def _apply_batch(self, items: list[dict], offset: int) -> None:
        """One synchronous transaction per batch: outcomes, counters and the offset commit
        together or not at all; on failure the in-memory job is restored."""
        snap = self._snapshot()
        try:
            with self.svc.db.tx():
                for item in items:
                    self._apply_item(item)
                self.rt["follower_offset"] = offset + len(items)
                self._persist()
        except Exception:
            self._restore(snap)
            raise
        for item in items:
            if item.get("ukey"):
                self._pending_records.pop(item["ukey"], None)

    async def follower_caught_up(self) -> bool:
        meta = await self.dataset.get_metadata()
        backlog = self.svc.db.one("SELECT 1 FROM link_backlog WHERE run_id=? LIMIT 1", (self.run_id,))
        return int(self.rt.get("follower_offset", 0)) >= meta.item_count and backlog is None

    def _apply_item(self, item: dict) -> None:
        if item.get("kind") != "page":
            return
        self.last_progress = time.monotonic()
        if self.canon.rewrite_pending:
            self._canon_rewrite()
        if item.get("own_url") and self.canon.distrusted:
            from app.jobs.canon import rekey

            fixed = [rekey(r, item["own_url"], r.get("requested_url")) for r in item.get("records") or [item["record"]]]
            item["records"] = [{k: r[k] for k in RECORD_ORDER if k in r} for r in fixed]
            item.pop("canonical", None)
        c = self.counters()
        cls = item["page_class"]
        canonical = item["url"]
        skey = item.get("source_key")
        site = item.get("site") or (site_key(canonical) if canonical.startswith("http") else self.doc["site"])
        pkey = page_key(canonical, skey)
        if cls == "not_target" and item.get("records_filtered", 0) > 0:
            self._drop_unextracted(canonical, skey)
        requested_key = page_key(canonical_url(item["requested_url"]) if item.get("requested_url") else canonical, skey)
        self.svc.db.execute("UPDATE urls SET status='done' WHERE run_id=? AND unique_key=?", (self.run_id, requested_key))
        if item.get("ukey") and item["ukey"] != requested_key:
            self.svc.db.execute("UPDATE urls SET status='done' WHERE run_id=? AND unique_key=?", (self.run_id, item["ukey"]))
        canon_url = item.get("canonical") or item.get("duplicate_of")
        if canon_url and canon_url != canonical:
            # a later link to the canonical address is a known duplicate: not loaded
            self._remember_url(self._ukey(canon_url, None), canon_url, "canonical", None, "done")
        # a refetched stored page leaves the store in every outcome path,
        # keyed by the stored page's key (not the final URL of the refetch)
        refetch_of = item.get("refetch_of")
        if refetch_of:
            self.svc.db.execute("DELETE FROM stored_pages WHERE run_id=? AND page_key=?", (self.run_id, refetch_of))
        else:
            refetch_row = self.svc.db.one("SELECT doc FROM stored_pages WHERE run_id=? AND page_key=?", (self.run_id, pkey))
            if refetch_row is not None and loads(refetch_row["doc"]).get("refetch_pending"):
                self.svc.db.execute("DELETE FROM stored_pages WHERE run_id=? AND page_key=?", (self.run_id, pkey))
        if cls == "private_address":
            c["urls_skipped"]["private_address"] += 1
            if item.get("source") in ("list", "sitemap", "refetch"):
                self._unextracted(canonical, item.get("requested_url"), skey, site, "private_address_refused", None, item.get("status"), item.get("attempts", 1),
                                  neutral=item.get("source") == "sitemap")
            self._refresh_store_counter()
            return
        c["pages_fetched"] += 1
        c["pages_by_class"][cls] = c["pages_by_class"].get(cls, 0) + 1
        fetch = item.get("fetch") or {}
        if fetch.get("fetch_mode") == "browser":
            c["browser_fetches"] += 1
        else:
            c["http_fetches"] += 1
        c["retries"] += max(0, int(item.get("attempts", 1)) - 1)
        # protection counters for challenged pages (hard blocks are not challenges)
        challenge = item.get("challenge")
        if challenge is not None:
            prot = self.prot()
            prot["challenges_met"] += 1
            step = challenge.get("passed_at_step")
            if step:
                prot["passed_by_step"][step] = prot["passed_by_step"].get(step, 0) + 1
            else:
                prot["failed"] += 1
        # block window: (blocked?, host) of the last pages
        try:
            item_host = host_of(item.get("requested_url") or canonical)
        except ValueError:
            item_host = ""
        bw = self.rt.setdefault("block_window", [])
        bw.append([1 if cls == "blocked" else 0, item_host])
        del bw[: -self.settings.block_window_pages]
        tid = item.get("template_id")
        if cls == "extracted":
            for rec in item.get("records") or [item["record"]]:  # one record per variant
                if not self._write_record(rec, item):
                    break
            t = self.template(tid)
            self._window_add(t, False, item)
            self._drop_stored(pkey)
        elif cls == "incomplete":
            t = self.template(tid)
            if t["status"] == "failed":
                self._unextracted(canonical, item.get("requested_url"), skey, site, "template_failed", tid, item.get("status"), item.get("attempts", 1), missing=item.get("missing_fields"))
            else:
                self._store_page(item, pkey, site)
                self._unextracted(canonical, item.get("requested_url"), skey, site, "incomplete", tid, item.get("status"), item.get("attempts", 1),
                                  gaps_text(item.get("gaps")), missing=item.get("missing_fields"), gaps=item.get("gaps"))
                if item.get("gaps"):
                    t["gap_pages"] = int(t.get("gap_pages", 0)) + 1
                self._window_add(t, True, item)
                self._maybe_repair(tid)
        elif cls == "no_template":
            nt = self.rt.setdefault("no_template", {"open_request": None, "failed": False})
            reason = "template_failed" if nt.get("failed") else "no_template"
            if not nt.get("failed"):
                self._store_page(item, pkey, site)
            self._unextracted(canonical, item.get("requested_url"), skey, site, reason, None, item.get("status"), item.get("attempts", 1))
            self._maybe_no_template_request()
        elif cls == "fetch_failed":
            reason = "not_found" if item.get("not_found") else "fetch_failed"
            # every failed page says why in errors[] (HTTP status, timeout, network error; no secrets)
            why = item.get("error") or (f"HTTP {item['status']}" if item.get("status") else "no response")
            if item.get("not_found") and "404" not in why and "410" not in why:
                why = f"HTTP {item.get('status') or 404} (not found)"
            attempts = int(item.get("attempts") or 1)
            self.add_error("page_fetch_failed", f"{canonical}: {why}" + (f" after {attempts} attempts" if attempts > 1 else ""),
                           url=canonical, severity="warning")
            self._unextracted(canonical, item.get("requested_url"), skey, site, reason, None, item.get("status"), item.get("attempts", 1), item.get("error"))
            if item.get("source") == "start" and self.doc["resolved"].get("discovery_mode") in ("crawl",) and not self.rt.get("sitemaps_used"):
                if c["pages_fetched"] == 1 and not item.get("not_found"):
                    self.rt["start_failed"] = True
        elif cls == "blocked":
            self._unextracted(canonical, item.get("requested_url"), skey, site, "blocked", None, item.get("status"), item.get("attempts", 1), "challenge or block page")
            if not self.rt.get("first_block_logged"):
                self.rt["first_block_logged"] = True
                self.timeline("first_block", f"first blocked page: {canonical}")
        self._confirm_seen(item, cls)
        self._check_sitemap_health(item)
        self._refresh_store_counter()
        if item.get("scope_skipped"):
            skipped_now = c["urls_skipped"]
            skipped_now["scope"] = int(skipped_now.get("scope", 0)) + int(item["scope_skipped"])
        if item.get("link_skips"):
            by_rule = c.setdefault("links_skipped", {})
            for rule, n in item["link_skips"].items():
                by_rule[rule] = int(by_rule.get(rule, 0)) + int(n)
        if cls == "duplicate":
            by_rule = c.setdefault("links_skipped", {})
            by_rule["canonical"] = int(by_rule.get("canonical", 0)) + 1
        self._trap_budget(item, cls)
        # links go to the backlog (enqueued after this transaction commits)
        if item.get("links") and self.doc["progress"]["discovery"] != "cut" and self.state == "running":
            self.svc.db.executemany(
                "INSERT INTO link_backlog(run_id, url, source, source_key, unique_key, forefront) VALUES (?,?,?,?,?,?)",
                [(self.run_id, u, "link", None, None, 1 if self._targetish(u) else 0) for u in item["links"]],
            )
        self._check_limits()
        self._check_block_rule()

    def _window_add(self, t: dict, incomplete: bool, item: dict) -> None:
        t["matched"] = t.get("matched", 0) + 1
        if not incomplete:
            t["filled_pages"] = t.get("filled_pages", 0) + 1
        if t["status"] in ("ok", "repaired"):
            t["window"].append({"i": 1 if incomplete else 0, "f": item.get("filled") or [], "u": item["url"], **({"g": 1} if item.get("gaps") else {})})
            del t["window"][: -self.settings.window_pages]
        if item.get("mapping_version") and item["mapping_version"] not in t["versions_used"]:
            t["versions_used"].append(item["mapping_version"])
        if item.get("mapping_version"):
            t["version"] = max(t.get("version") or 0, item["mapping_version"])

    def _write_record(self, record: dict, item: dict | None) -> bool:
        """Upsert by record_id: highest mapping_version, then latest extracted_at wins."""
        self.last_progress = time.monotonic()
        line = dumps(record)
        size = len(line.encode("utf-8")) + 1
        row = self.svc.db.one("SELECT mapping_version, extracted_at, line FROM records WHERE run_id=? AND record_id=?", (self.run_id, record["record_id"]))
        c = self.counters()
        limit = self.settings.limits["max_result_bytes"]
        if row is not None:
            if (row["mapping_version"], row["extracted_at"]) > (record["mapping_version"], record["extracted_at"]):
                return True
            old = len(row["line"].encode("utf-8")) + 1
            if c["result_bytes"] - old + size > limit:
                # the replacement would push the result over the limit: keep the stored line
                self._cut("max_result_size")
                return False
            self.svc.db.execute(
                "UPDATE records SET template_id=?, mapping_version=?, extracted_at=?, line=? WHERE run_id=? AND record_id=?",
                (record["template_id"], record["mapping_version"], record["extracted_at"], line, self.run_id, record["record_id"]),
            )
            c["result_bytes"] += size - old
            self._drop_unextracted(record["source_url"], record.get("source_key"))
            return True
        if c["result_bytes"] + size > limit:
            self._unextracted(record["source_url"], record.get("requested_url"), record.get("source_key"), record["site"], "stopped", record["template_id"], None, 1, "result size limit reached")
            self._cut("max_result_size")
            return False
        self.svc.db.execute(
            "INSERT INTO records(run_id, record_id, template_id, mapping_version, extracted_at, line) VALUES (?,?,?,?,?,?)",
            (self.run_id, record["record_id"], record["template_id"], record["mapping_version"], record["extracted_at"], line),
        )
        c["result_bytes"] += size
        c["records_extracted"] += 1
        t = self.template(record["template_id"])
        t["record_count"] = t.get("record_count", 0) + 1
        self._drop_unextracted(record["source_url"], record.get("source_key"))
        return True

    # ---------------------------------------------------------------- stored pages
    def _store_page(self, item: dict, pkey: str, site: str) -> None:
        limit = self.settings.limits["max_stored_pages_per_job"]
        exists = self.svc.db.one("SELECT 1 FROM stored_pages WHERE run_id=? AND page_key=?", (self.run_id, pkey))
        count = int(self.svc.db.scalar("SELECT COUNT(*) FROM stored_pages WHERE run_id=? AND has_html=1", (self.run_id,)) or 0)
        has_html = 1 if item.get("kvs_key") and (exists or count < limit) else 0
        doc = {
            "url": item["url"],
            "requested_url": item.get("requested_url"),
            "source_key": item.get("source_key"),
            "final_url": item.get("final_url") or item["url"],
            "site": site,
            "status": item.get("status"),
            "fetched_at": item.get("fetched_at") or now_iso(),
            "kvs_key": item.get("kvs_key") if has_html else None,
            "fetch": item.get("fetch"),
            "needs_reextract": False,
            "refetch_pending": False,
        }
        if item.get("gaps"):
            doc["gaps"] = item["gaps"]
        if item.get("missing_fields"):
            doc["missing_fields"] = item["missing_fields"]
        if not has_html:
            self.counters()["refetch_after_repair"] += 1
            if not self.rt.get("store_limit_problem"):
                self.rt["store_limit_problem"] = True
                self.svc.events.create(self.run_id, "problem", self.run_id, {"problem_code": "store_limit_reached", "message": f"more than {limit} stored pages; only URLs are kept"}, problem_key="store_limit_reached:")
        self.svc.db.execute(
            "INSERT INTO stored_pages(run_id, page_key, url, template_id, page_class, has_html, doc) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id, page_key) DO UPDATE SET template_id=excluded.template_id, page_class=excluded.page_class, has_html=excluded.has_html, doc=excluded.doc",
            (self.run_id, pkey, item["url"], item.get("template_id"), item["page_class"], has_html, dumps(doc)),
        )
        self._refresh_store_counter()

    def _drop_stored(self, pkey: str) -> None:
        self.svc.db.execute("DELETE FROM stored_pages WHERE run_id=? AND page_key=?", (self.run_id, pkey))
        self._refresh_store_counter()

    def _refresh_store_counter(self) -> None:
        self.counters()["stored_pages"] = int(self.svc.db.scalar("SELECT COUNT(*) FROM stored_pages WHERE run_id=?", (self.run_id,)) or 0)

    def pending_reextraction(self) -> int:
        rows = self.svc.db.all("SELECT doc FROM stored_pages WHERE run_id=?", (self.run_id,))
        return sum(1 for r in rows if (d := loads(r["doc"])).get("needs_reextract") or d.get("refetch_pending"))

    # ---------------------------------------------------------------- repair trigger
    def _repairs_total(self) -> int:
        return int(self.rt.get("repairs_total", 0))

    def _maybe_repair(self, tid: str, final: bool = False) -> None:
        """A repair opens when the share of pages missing required fields in the window passes the
        threshold, or when ``GAP_REPAIR_PAGES`` stored pages carry variant data or a shown
        price the mapping does not read — at the end of a finished crawl, one such page is enough."""
        t = self.template(tid)
        if t["status"] not in ("ok", "repaired") or t.get("open_request"):
            return
        window = t["window"]
        share = sum(1 for e in window if e["i"] and not e.get("g")) / len(window) if window else 0.0
        share_hit = len(window) >= self.settings.min_pages and share > 1 - float(self.config["required_fill_threshold"])
        gap_pages = int(t.get("gap_pages", 0))
        gap_hit = gap_pages >= GAP_REPAIR_PAGES or (final and gap_pages > 0)
        if not share_hit and not gap_hit:
            return
        if t.get("confirm"):
            return  # the pages that triggered it are being fetched again
        if not self._can_repair(t):
            if share_hit:
                self._template_failed(tid, "repair limits spent")
            else:
                self._accept_gaps(tid, "repair limits spent")
            return
        if share_hit and not gap_hit and not t.get("confirmed") and self._confirm_first(tid, t):
            return
        t["confirmed"] = False
        self._open_request("incomplete", tid)

    def _confirm_first(self, tid: str, t: dict) -> bool:
        """Before a repair, the incomplete pages that triggered it are fetched once more (a
        transient error page can look like a broken mapping). True when refetches were queued."""
        if self.doc["progress"]["discovery"] == "cut" or self.state != "running" or self._shutdown:
            return False
        urls = list(dict.fromkeys(e["u"] for e in reversed(t["window"]) if e["i"] and not e.get("g")))[:5]
        keys = []
        ms = int(time.time() * 1000)
        for u in urls:
            row = self.svc.db.one("SELECT page_key, doc FROM stored_pages WHERE run_id=? AND template_id=? AND url=?", (self.run_id, tid, u))
            if row is None:
                continue
            d = loads(row["doc"])
            unique = row["page_key"] + f"\x1frefetchconfirm{ms}"
            self.svc.db.execute("INSERT INTO link_backlog(run_id, url, source, source_key, unique_key, forefront) VALUES (?,?,?,?,?,1)",
                                (self.run_id, d.get("requested_url") or d["url"], "refetch", d.get("source_key"), unique))
            keys.append(row["page_key"])
        if not keys:
            return False
        t["confirm"] = {"keys": keys, "fixed": 0}
        self.timeline("repair_check", f"template {tid}: {len(keys)} incomplete pages are fetched again before a repair is asked for")
        return True

    def _confirm_seen(self, item: dict, cls: str) -> None:
        """A page fetched again to confirm a repair came back. Fine now: its window entry no
        longer counts. Once all are back, the repair is judged again (and opened only if needed)."""
        pkey = item.get("refetch_of")
        if not pkey or "\x1frefetchconfirm" not in str(item.get("ukey") or ""):
            return
        for tid, t in (self.rt.get("templates") or {}).items():
            conf = t.get("confirm")
            if not conf or pkey not in conf["keys"]:
                continue
            conf["keys"].remove(pkey)
            if cls == "extracted":
                conf["fixed"] += 1
                for e in t["window"]:
                    if e["u"] == item.get("url") and e["i"] and not e.get("g"):
                        e["i"] = 0
            if not conf["keys"]:
                self._confirm_done(tid, t)
            return

    def _confirm_done(self, tid: str, t: dict) -> None:
        fixed = int((t.get("confirm") or {}).get("fixed", 0))
        t["confirm"] = None
        t["confirmed"] = True
        before = self.counters().get("repairs_started", 0)
        self._maybe_repair(tid)
        if t.get("open_request") is None and self.counters().get("repairs_started", 0) == before:
            t["confirmed"] = False
            c = self.counters()
            c["repairs_avoided"] = int(c.get("repairs_avoided", 0)) + 1
            self.timeline("repair_check", f"template {tid}: no repair needed, {fixed} of the pages were fine when fetched again")

    def _gaps_waiting(self) -> bool:
        return any((t.get("gap_pages") and not t.get("gaps_off") and not t.get("open_request") and t["status"] in ("ok", "repaired"))
                   or t.get("confirm") for t in (self.rt.get("templates") or {}).values())

    def _gaps_at_end(self, discovery: str) -> None:
        """The crawl is over and pages wait only for their gaps: a finished crawl asks for the
        repair even below ``GAP_REPAIR_PAGES``; a cut or stopped one extracts them as they are."""
        for tid, t in list((self.rt.get("templates") or {}).items()):
            if t.get("confirm"):
                self._confirm_done(tid, t)  # the crawl is over: the confirming refetches that did not come back do not hold it up
        for tid, t in list((self.rt.get("templates") or {}).items()):
            if not t.get("gap_pages") or t.get("gaps_off") or t.get("open_request") or t["status"] not in ("ok", "repaired"):
                continue
            if discovery == "finished" and self.state == "running" and not self.rt.get("stop_reason"):
                self._maybe_repair(tid, final=True)
                if not self.template(tid).get("open_request") and not t.get("gaps_off"):
                    self._accept_gaps(tid, "no repair was opened")
            else:
                self._accept_gaps(tid, "the crawl ended before a repair")

    def _can_repair(self, t: dict) -> bool:
        return (t["status"] in ("ok", "repaired", "repairing") and t["repairs_used"] < self.config["max_repairs_per_template"]
                and self._repairs_total() < self.config["max_repairs_per_job"])

    def _mapping_gaps(self, tmpl: dict, page, rows: list, view) -> list[str]:
        """During a crawl: what the page shows that the active mapping does not read —
        ``variants_not_used`` (in-page variant data with two or more variants, no ``variants`` in
        the mapping) and ``price_not_extracted`` (the page shows a price or a range, no record of it
        got one; never for a price on request). Only while a repair can still fix it: once the
        repair limits are spent or the template is given up, pages are extracted as they are."""
        t = self.template(tmpl["template_id"])
        if t.get("gaps_off") or not self._can_repair(t):
            return []
        gaps = []
        if not tmpl.get("variants"):
            from app.extraction.variants import detect_sources

            try:
                found = detect_sources(page)
            except Exception:
                found = []
            if any((f or {}).get("count", 0) >= 2 for f in found):
                gaps.append("variants_not_used")
        name = price_field(tmpl)
        if name is not None:
            priced = any(isinstance(data.get(name), (int, float)) and not isinstance(data.get(name), bool) for data, _m, _f in rows)
            if not priced and view.signal in ("shown", "range"):
                gaps.append("price_not_extracted")
        return gaps

    def _accept_gaps(self, tid: str, why: str) -> None:
        """No repair can fix the gaps any more: pages stored only for them are extracted with the
        mapping as it is (records with ``price_status``), nothing is lost."""
        t = self.template(tid)
        if t.get("gaps_off"):
            return
        t["gaps_off"] = True
        n = 0
        for row in self.svc.db.all("SELECT page_key, doc FROM stored_pages WHERE run_id=? AND template_id=? AND page_class='incomplete'", (self.run_id, tid)):
            d = loads(row["doc"])
            if d.get("gaps"):
                d["needs_reextract"] = True
                n += 1
                self.svc.db.execute("UPDATE stored_pages SET doc=? WHERE run_id=? AND page_key=?", (dumps(d), self.run_id, row["page_key"]))
        self.timeline("mapping_gaps_accepted", f"template {tid}: {why}; {n} pages with variant data or a price the mapping does not read are extracted as they are")

    def _maybe_no_template_request(self) -> None:
        nt = self.rt.setdefault("no_template", {"open_request": None, "failed": False})
        if nt.get("open_request") or nt.get("failed"):
            return
        count = int(self.svc.db.scalar("SELECT COUNT(*) FROM stored_pages WHERE run_id=? AND page_class='no_template'", (self.run_id,)) or 0)
        if count < self.settings.no_template_pages:
            return
        if self._repairs_total() >= self.config["max_repairs_per_job"]:
            nt["failed"] = True
            self._give_up_no_template()
            return
        self._open_request("no_template", None)

    def _open_request(self, reason: str, tid: str | None) -> None:
        cfg = self.config
        ts = now()
        mrq_id = new_id("mrq")
        site = self.doc["site"]
        if tid:
            t = self.template(tid)
            rows = self.svc.db.all("SELECT * FROM stored_pages WHERE run_id=? AND template_id=? AND page_class='incomplete' ORDER BY rowid DESC", (self.run_id, tid))
        else:
            rows = self.svc.db.all("SELECT * FROM stored_pages WHERE run_id=? AND page_class='no_template' ORDER BY rowid", (self.run_id,))
        if rows:
            site = loads(rows[0]["doc"]).get("site") or site
        taken = set(m["template_id"] for m in self.svc.mappings.active_mappings(site))
        suggested = tid or suggest_template_id(loads(rows[0]["doc"])["url"] if rows else cfg.get("site_url") or "", taken)
        samples = []
        # pages stored for mapping gaps first: they show what the mapping must learn (variants, the price)
        rows = sorted(rows, key=lambda r: 0 if '"gaps"' in r["doc"] else 1)
        for row in rows[: cfg.get("sample_count", 5)]:
            sample = self._sample_from_stored(row, suggested, tid)
            if sample:
                samples.append(sample)
        active = self.svc.mappings.active(site, tid) if tid else None
        from app.extraction.store import version_doc

        if tid:
            window = t["window"]
            names = [f["name"] for f in (self._mapping_for_any_site(tid) or {}).get("mapping", {}).get("fields", [])]
            required = required_set((self._mapping_for_any_site(tid) or {}).get("mapping"), cfg.get("required_fields") or [])
            for name in cfg.get("required_fields") or []:
                if name not in names:
                    names.append(name)
            fields = []
            for name in names:
                filled = sum(1 for e in window if name in e.get("f", []))
                fields.append({"name": name, "required": name in required, "fill_rate": round(filled / len(window), 4) if window else 0.0})
            failures = {
                "pages_considered": len(window),
                "pages_failing": sum(e["i"] for e in window),
                "fields": fields,
                "example_urls": [e["u"] for e in window if e["i"]][:10],
            }
            gap_counts: dict[str, int] = {}
            gap_urls: list[str] = []
            for r in rows:
                d = loads(r["doc"])
                for g in d.get("gaps") or []:
                    gap_counts[g] = gap_counts.get(g, 0) + 1
                if d.get("gaps"):
                    gap_urls.append(d["url"])
            if gap_counts:
                # the repair is (also) about variant data / a shown price the mapping does not read
                failures["gaps"] = gap_counts
                failures["pages_failing"] = max(failures["pages_failing"], len(gap_urls))
                failures["pages_considered"] = max(failures["pages_considered"], failures["pages_failing"])
                failures["example_urls"] = list(dict.fromkeys(gap_urls[:5] + failures["example_urls"]))[:10]
        else:
            failures = {
                "pages_considered": len(rows),
                "pages_failing": len(rows),
                "fields": [{"name": n, "required": n in (cfg.get("required_fields") or []), "fill_rate": 0.0} for n in cfg["fields"]],
                "example_urls": [loads(r["doc"])["url"] for r in rows][:10],
            }
        doc = {
            "mapping_request_id": mrq_id,
            "run_id": self.run_id,
            "site": site,
            "reason": reason,
            "status": "open",
            "template_id": tid,
            "suggested_template_id": suggested,
            "created_at": iso(ts),
            "expires_at": iso(plus(self.settings.mapping_request_timeout_seconds, ts)),
            "closed_at": None,
            "previous": version_doc(active) if active else None,
            "expected_version": int(active["version"]) if active else 0,
            "samples": samples,
            "failures": failures,
            "instructions": {
                "extraction_instructions": cfg.get("extraction_instructions", ""),
                "fields": cfg["fields"],
                "required_fields": cfg.get("required_fields") or [],
                "required_fill_threshold": cfg["required_fill_threshold"],
                "extraction_mode": cfg["extraction_mode"],
                "structured_data_types": cfg["structured_data_types"],
                "target_page_selector": cfg.get("target_page_selector"),
                "max_refinements": cfg.get("max_refinements", 2),
                "ai_choose_fields": bool(cfg.get("ai_choose_fields")),
                "fields_hint": cfg.get("fields_hint") or "",
            },
            "resolution": None,
        }
        self.svc.db.execute(
            "INSERT INTO mapping_requests(mapping_request_id, run_id, status, template_id, created_at, expires_at, doc) VALUES (?,?,?,?,?,?,?)",
            (mrq_id, self.run_id, "open", tid, doc["created_at"], doc["expires_at"], dumps(doc)),
        )
        self.rt["repairs_total"] = self._repairs_total() + 1
        self.counters()["repairs_started"] += 1
        if tid:
            t["repairs_used"] += 1
            t["open_request"] = mrq_id
            t["status"] = "repairing"
            message = f"template {tid}: required fields missing on {failures['pages_failing']} of {failures['pages_considered']} pages"
        else:
            self.rt["no_template"]["open_request"] = mrq_id
            message = f"{len(rows)} target pages match no template"
        self.svc.events.create(self.run_id, "mapping_required", mrq_id, {"template_id": tid, "message": message[:500]})
        self.timeline("repair_requested", f"mapping requested for {tid or 'a new template'}: {message[:200]}")

    def _sample_from_stored(self, row, suggested: str, tid: str | None) -> dict | None:
        d = loads(row["doc"])
        if not d.get("kvs_key"):
            return None
        html = self._stored_html_sync(d["kvs_key"])
        if html is None:
            return None
        sid = new_id("smp")
        ts = now()
        page = Page(html, d.get("final_url") or d["url"], d.get("status") or 200)
        fetch = dict(d.get("fetch") or {"fetch_mode": "http", "browser": None, "proxy_tier": None})
        fetch.setdefault("passed_at_step", None)
        if fetch.get("fetch_mode") not in ("http", "browser"):
            fetch["fetch_mode"] = "http"
        extracted = None
        if tid:
            entry = self._mapping_for_any_site(tid)
            if entry:
                extracted = plain_prices(apply_mapping(entry["mapping"], page, self.config["extraction_mode"]).data)
        sample = sample_doc(
            sample_id=sid, page=page, requested_url=d.get("requested_url"), source_key=d.get("source_key"),
            fetched_at=d.get("fetched_at") or iso(ts), expires_at=iso(plus(self.settings.samples_hours * 3600, ts)),
            fetch={k: fetch.get(k) for k in ("fetch_mode", "browser", "proxy_tier", "passed_at_step")},
            matched_template_id=tid, suggested_template_id=suggested, structured_types=self.config["structured_data_types"],
            max_bytes=self.settings.limits["sample_html_max_bytes"], extracted=extracted,
        )
        # sample_doc carries the in-page variant sources; shop JSON is probed by POST /v1/samples
        if page.microdata_nodes:
            sample["microdata"] = [n for n in page.microdata_nodes if isinstance(n, dict)][:20]
        self.svc.samples.save(sample, html, page.url)
        return sample

    def _stored_html_sync(self, key: str) -> str | None:
        path = self.job_dir / "crawlee" / "key_value_stores" / "default"
        for candidate in path.glob(f"{key}*"):
            if candidate.name.endswith(".__metadata__.json"):
                continue
            try:
                return candidate.read_text("utf-8", errors="replace")
            except OSError:
                return None
        return None

    def _template_failed(self, tid: str, why: str) -> None:
        t = self.template(tid)
        if t["status"] == "failed":
            return
        t["status"] = "failed"
        t["open_request"] = None
        self._accept_gaps(tid, f"template given up ({why})")  # pages kept only for gaps keep their records
        rows = self.svc.db.all("SELECT page_key, doc FROM stored_pages WHERE run_id=? AND template_id=?", (self.run_id, tid))
        for row in rows:
            d = loads(row["doc"])
            if d.get("gaps") and d.get("needs_reextract"):
                continue
            self._unextracted(d["url"], d.get("requested_url"), d.get("source_key"), d.get("site") or self.doc["site"], "template_failed", tid, d.get("status"), 1, missing=d.get("missing_fields"))
            self.svc.db.execute("DELETE FROM stored_pages WHERE run_id=? AND page_key=?", (self.run_id, row["page_key"]))
        self._refresh_store_counter()
        self.svc.events.create(
            self.run_id, "problem", self.run_id, {"problem_code": "template_failed", "template_id": tid, "message": f"template {tid} given up: {why}"[:500]},
            problem_key=f"template_failed:{tid}",
        )

    def _give_up_no_template(self) -> None:
        rows = self.svc.db.all("SELECT page_key, doc FROM stored_pages WHERE run_id=? AND page_class='no_template'", (self.run_id,))
        for row in rows:
            d = loads(row["doc"])
            self._unextracted(d["url"], d.get("requested_url"), d.get("source_key"), d.get("site") or self.doc["site"], "template_failed", None, d.get("status"), 1)
        self.svc.db.execute("DELETE FROM stored_pages WHERE run_id=? AND page_class='no_template'", (self.run_id,))
        self._refresh_store_counter()
        self.svc.events.create(self.run_id, "problem", self.run_id, {"problem_code": "template_failed", "message": "pages without a template given up"}, problem_key="template_failed:")

    # ---------------------------------------------------------------- activation (called by the API)
    def on_activation(self, site: str, template_id: str, version: int, mapping: dict, resolved_ids: list[str], new_template: bool) -> None:
        self.timeline("repair_done", f"mapping {template_id} v{version} activated")
        self.mappings.setdefault(site, {})
        self.mappings[site][template_id] = {"template_id": template_id, "version": version, "mapping": mapping}
        self._target_patterns.pop(site, None)
        with self.svc.db.tx():
            t = self.template(template_id)
            t["version"] = version
            if t["status"] != "failed":
                if t.get("open_request") in resolved_ids or t["status"] == "repairing":
                    t["status"] = "repaired"
                elif t["status"] == "ok" and t["versions_used"] and version not in t["versions_used"]:
                    t["status"] = "repaired"
            if t.get("open_request") in resolved_ids:
                t["open_request"] = None
                self.counters()["repairs_succeeded"] += 1
            t["window"] = []
            t["gap_pages"] = 0  # the stored gap pages are re-extracted and counted again if still short
            nt = self.rt.setdefault("no_template", {"open_request": None, "failed": False})
            if nt.get("open_request") in resolved_ids:
                nt["open_request"] = None
                self.counters()["repairs_succeeded"] += 1
            # stored pages to re-extract
            rows = self.svc.db.all("SELECT page_key, doc, template_id, page_class FROM stored_pages WHERE run_id=?", (self.run_id,))
            for row in rows:
                if row["template_id"] == template_id or row["page_class"] == "no_template" or new_template:
                    d = loads(row["doc"])
                    d["needs_reextract"] = True
                    self.svc.db.execute("UPDATE stored_pages SET doc=? WHERE run_id=? AND page_key=?", (dumps(d), self.run_id, row["page_key"]))
            self._persist()

    def on_request_closed(self, mrq_id: str, status: str) -> None:
        """A request failed (POST …/fail) or expired: give its template up."""
        for tid, t in (self.rt.get("templates") or {}).items():
            if t.get("open_request") == mrq_id:
                t["open_request"] = None
                self._template_failed(tid, f"mapping request {status}")
        nt = self.rt.get("no_template") or {}
        if nt.get("open_request") == mrq_id:
            nt["open_request"] = None
            nt["failed"] = True
            self._give_up_no_template()
        self._persist()

    def _reconcile_requests(self) -> None:
        """Requests closed while this job had no live runner (e.g. failed during waiting_for_credentials)."""
        open_ids = [t.get("open_request") for t in (self.rt.get("templates") or {}).values()] + [(self.rt.get("no_template") or {}).get("open_request")]
        for mrq_id in [i for i in open_ids if i]:
            row = self.svc.db.one("SELECT status FROM mapping_requests WHERE mapping_request_id=?", (mrq_id,))
            if row is None or row["status"] == "open":
                continue
            if row["status"] in ("failed", "expired"):
                self.on_request_closed(mrq_id, row["status"])
            elif row["status"] == "resolved":
                for tid, t in (self.rt.get("templates") or {}).items():
                    if t.get("open_request") == mrq_id:
                        t["open_request"] = None
                        t["status"] = "repaired"
                nt = self.rt.get("no_template") or {}
                if nt.get("open_request") == mrq_id:
                    nt["open_request"] = None

    # ---------------------------------------------------------------- re-extraction
    async def _reextract_some(self, limit: int = 50) -> None:
        """Re-extracts stored pages after an activation. Reading the stored HTML awaits, so the
        reads come first and the writes follow in one transaction."""
        rows = self.svc.db.all("SELECT page_key, doc, template_id, page_class FROM stored_pages WHERE run_id=?", (self.run_id,))
        work: list[tuple[Any, dict, Any]] = []
        for row in rows:
            d = loads(row["doc"])
            if not d.get("needs_reextract"):
                continue
            if len(work) >= limit:
                break
            html = None
            if d.get("kvs_key"):
                html = await self.kvs.get_value(d["kvs_key"])
                if isinstance(html, bytes):
                    html = html.decode("utf-8", errors="replace")
            work.append((row, d, html))
        if not work:
            return
        crawl_open = self.doc["progress"]["discovery"] != "cut" and self.state == "running" and not self._shutdown
        snap = self._snapshot()
        try:
            with url_pattern_batch(), self.svc.db.tx():
                for row, d, html in work:
                    self._reextract_one(row, d, html, crawl_open)
                self._refresh_store_counter()
                self._persist()
        except Exception:
            self._restore(snap)
            raise

    def _reextract_one(self, row, d: dict, html: str | None, crawl_open: bool) -> None:
        if not d.get("kvs_key"):
            d["needs_reextract"] = False
            if crawl_open:
                # beyond the store limit: fetch again (README 4.5); enqueued from the backlog after commit
                d["refetch_pending"] = True
                unique = row["page_key"] + f"\x1frefetch{int(time.time() * 1000)}"
                self.svc.db.execute(
                    "INSERT INTO link_backlog(run_id, url, source, source_key, unique_key, forefront) VALUES (?,?,?,?,?,1)",
                    (self.run_id, d.get("requested_url") or d["url"], "refetch", d.get("source_key"), unique),
                )
            self.svc.db.execute("UPDATE stored_pages SET doc=? WHERE run_id=? AND page_key=?", (dumps(d), self.run_id, row["page_key"]))
            return
        if html is None:
            d["needs_reextract"] = False
            self.svc.db.execute("UPDATE stored_pages SET doc=? WHERE run_id=? AND page_key=?", (dumps(d), self.run_id, row["page_key"]))
            return
        page = Page(html, d.get("final_url") or d["url"], d.get("status") or 200)
        site = d.get("site") or self.doc["site"]
        tmpl = pick_template(self.mappings_for(site), page, d["url"])
        if tmpl is None:
            d["needs_reextract"] = False
            self.svc.db.execute("UPDATE stored_pages SET doc=? WHERE run_id=? AND page_key=?", (dumps(d), self.run_id, row["page_key"]))
            return
        ext = apply_mapping(tmpl, page, self.config["extraction_mode"])
        from app.extraction.variants import resolve_in_page, variant_rows

        # stored pages: the in-page variant sources only (a shop-JSON source needs the live page)
        variants, vinfo = resolve_in_page(page, tmpl.get("variants"))
        rows = variant_rows(tmpl, ext.data, variants, (vinfo or {}).get("kind"), page.base_url, page=page)
        required = required_set(tmpl, self.config.get("required_fields") or [])
        from app.extraction.record_filter import record_filter_missing, record_matches

        selected = [(index, row) for index, row in enumerate(rows) if record_matches(tmpl, row[0])]
        missing = sorted({name for _index, (data, _meta, _filled) in selected for name in required if data.get(name) is None or data.get(name) == []})
        missing = sorted(set(missing) | {name for data, _meta, _filled in rows for name in record_filter_missing(tmpl, data)})
        t = self.template(tmpl["template_id"])
        view = price_view(page, tmpl)
        gaps = (
            []
            if missing or not selected
            else self._mapping_gaps(tmpl, page, [row for _index, row in selected], view)
        )
        if not missing and not gaps:
            if tmpl["__version"] not in t["versions_used"]:
                t["versions_used"].append(tmpl["__version"])
            for index, (data, meta, _filled) in selected:
                price_keys = annotate_price(tmpl, data, view)
                rec: dict[str, Any] = {
                    "record_id": record_id(
                        d["url"], tmpl["template_id"], index, d.get("source_key")
                    ),
                    "source_url": d["url"],
                    "requested_url": d.get("requested_url"),
                    "source_key": d.get("source_key"),
                    "site": site,
                    "template_id": tmpl["template_id"],
                    "mapping_version": tmpl["__version"],
                    "item_index": index,
                    "variant": meta,
                    "extracted_at": now_iso(),
                    "data": data,
                }
                if not rec["requested_url"] or rec["requested_url"] == rec["source_url"]:
                    rec.pop("requested_url")
                if not rec["source_key"]:
                    rec.pop("source_key")
                if not rec["variant"]:
                    rec.pop("variant")
                rec.update(price_keys)
                if not self._write_record({k: rec[k] for k in RECORD_ORDER if k in rec}, None):
                    break
            self._drop_unextracted(d["url"], d.get("source_key"))
            self.svc.db.execute("DELETE FROM stored_pages WHERE run_id=? AND page_key=?", (self.run_id, row["page_key"]))
        else:
            d["needs_reextract"] = False
            d.pop("missing_fields", None)
            if missing:
                d["missing_fields"] = missing
            d.pop("gaps", None)
            if gaps:
                d["gaps"] = gaps
                t["gap_pages"] = int(t.get("gap_pages", 0)) + 1
            self.svc.db.execute(
                "UPDATE stored_pages SET template_id=?, page_class='incomplete', doc=? WHERE run_id=? AND page_key=?",
                (tmpl["template_id"], dumps(d), self.run_id, row["page_key"]),
            )
            self._unextracted(d["url"], d.get("requested_url"), d.get("source_key"), site, "incomplete", tmpl["template_id"], d.get("status"), 1,
                              gaps_text(gaps), missing=missing, gaps=gaps)

    def _abandon_refetches(self) -> None:
        """The crawl ended (a limit, a stop) while stored pages waited for their refetch: they
        are listed ``stopped`` and leave the store, so the job can complete."""
        rows = self.svc.db.all("SELECT page_key, doc, template_id FROM stored_pages WHERE run_id=?", (self.run_id,))
        pending = [(r, loads(r["doc"])) for r in rows]
        pending = [(r, d) for r, d in pending if d.get("refetch_pending")]
        if not pending:
            return
        for row, d in pending:
            self._unextracted(d["url"], d.get("requested_url"), d.get("source_key"), d.get("site") or self.doc["site"], "stopped", row["template_id"], d.get("status"), 1,
                              "the page was to be fetched again after a repair, but the crawl had ended")
            self.svc.db.execute("DELETE FROM stored_pages WHERE run_id=? AND page_key=?", (self.run_id, row["page_key"]))
        self.svc.db.execute("DELETE FROM link_backlog WHERE run_id=? AND source='refetch'", (self.run_id,))
        self._refresh_store_counter()

    # ================================================================== limits and rules
    def _cut(self, reason: str) -> None:
        if not (self.doc["progress"]["discovery"] == "cut" or self.rt.get("stop_reason")):
            self.rt["stop_reason"] = reason
            if reason != "site_unavailable":
                self.timeline("stop", f"stop: {reason.replace('_', ' ')}")
            self.doc["progress"]["discovery"] = "cut"
        # stop the crawler whenever one runs, also when the job was cut before
        if self.crawler is not None:
            try:
                self.crawler.stop(f"limit: {reason}")
            except Exception:
                pass
        if self.feeder_task and not self.feeder_task.done():
            self.feeder_task.cancel()

    def _check_limits(self) -> None:
        c = self.counters()
        cfg = self.config
        if cfg.get("max_records") and c["records_extracted"] >= cfg["max_records"]:
            self._cut("max_records")
        if c["pages_fetched"] + int(self.rt.get("navigation_actions", 0)) >= cfg["max_pages"]:
            self._cut("max_pages")

    def _check_block_rule(self) -> None:
        bw = self.rt.get("block_window") or []
        if len(bw) < self.settings.block_window_pages:
            return
        entries = [e if isinstance(e, list) else [e, ""] for e in bw]
        share = sum(e[0] for e in entries) / len(entries)
        if share < self.settings.block_share:
            return
        self.svc.events.create(self.run_id, "problem", self.run_id, {"problem_code": "block_rate_high", "message": f"{int(share * 100)}% of the last {len(bw)} pages were blocked"}, problem_key="block_rate_high:")
        if not self.rt.get("block_rate_logged"):
            self.rt["block_rate_logged"] = True
            self.timeline("problem", f"{int(share * 100)}% of the last {len(bw)} pages were blocked")
        # the tier of the hosts actually blocked, not of the site key
        blocked_hosts = {e[1] for e in entries if e[0] and e[1]}
        highest = (not self.tiers.enabled) or all(self.tiers.at_highest(h) for h in blocked_hosts or {self._first_host() or ""})
        if highest and not self.click_usable() and not self.solver_usable():
            self._cut("blocked")

    def _check_challenge_limits(self) -> None:
        prot = self.prot()
        spent = []
        if self.config.get("challenge_click") and self.settings.challenge_click_allowed and prot["clicks_used"] >= prot["clicks_limit"]:
            spent.append("click")
        if self.config.get("use_solver") and self.settings.solvers_allowed and self.solver is not None and prot["solves_used"] >= prot["solves_limit"]:
            spent.append("solver")
        if spent:
            self.svc.events.create(self.run_id, "problem", self.run_id, {"problem_code": "challenge_limit_reached", "message": f"limit reached: {', '.join(spent)}"}, problem_key="challenge_limit_reached:")

    def _check_requests_expiry(self) -> None:
        rows = self.svc.db.all("SELECT mapping_request_id, expires_at, doc FROM mapping_requests WHERE run_id=? AND status='open'", (self.run_id,))
        for row in rows:
            if parse(row["expires_at"]) <= now():
                d = loads(row["doc"])
                d["status"] = "expired"
                d["closed_at"] = now_iso()
                d["resolution"] = {"status": "expired", "version": None, "failure_reason": "timeout"}
                self.svc.db.execute("UPDATE mapping_requests SET status='expired', doc=? WHERE mapping_request_id=?", (dumps(d), row["mapping_request_id"]))
                self.on_request_closed(row["mapping_request_id"], "expired")

    # ================================================================== supervisor
    def _crawler_running(self) -> bool:
        return self.crawler_task is not None and not self.crawler_task.done()

    async def _crawl_idle(self) -> bool:
        if not self._crawler_running():
            return await self.follower_caught_up()
        return await self.rq.is_finished() and await self.follower_caught_up()

    def _progress_refresh(self, pending: int, in_progress: int) -> None:
        p = self.doc["progress"]
        p["queue_pending"] = pending
        p["in_progress"] = in_progress
        p["open_mapping_requests"] = int(self.svc.db.scalar("SELECT COUNT(*) FROM mapping_requests WHERE run_id=? AND status='open'", (self.run_id,)) or 0)
        p["pending_reextraction"] = self.pending_reextraction()

    async def queue_counts(self) -> tuple[int, int]:
        if self.rq is None:
            return 0, 0
        if self.doc["progress"]["discovery"] == "cut" and not self._crawler_running():
            return 0, 0
        try:
            total = await self.rq.get_total_count()
            handled = await self.rq.get_handled_count()
        except Exception:
            return 0, 0
        remaining = max(0, total - handled)
        in_progress = len(self._in_progress_keys())
        if not self._crawler_running():
            in_progress = 0
        return max(0, remaining - in_progress), in_progress

    def _ready(self, pending: int, in_progress: int, idle: bool) -> bool:
        discovery = self.doc["progress"]["discovery"]
        ready = (
            discovery in ("finished", "cut")
            and idle
            and (discovery == "cut" or (pending == 0 and in_progress == 0))
            and self.doc["progress"]["open_mapping_requests"] == 0
            and self.doc["progress"]["pending_reextraction"] == 0
        )
        if discovery == "cut" and self._crawler_running():
            ready = False
        return ready

    async def _supervise(self) -> None:
        interval = self.settings.supervisor_interval_seconds
        while True:
            await asyncio.sleep(interval)
            if self._shutdown:
                return
            async with self.lock:
                self.doc = self._reload_state_fields()
                if self.state in ("completed", "failed"):
                    return
                if self.state == "waiting_for_credentials":
                    return
                stopping = self.state == "stopping"
                with self.svc.db.tx():
                    if stopping and not self.rt.get("stop_handled"):
                        self._handle_operator_stop()
                    if self.running_seconds() >= self.config["max_duration_minutes"] * 60:
                        self._cut("max_duration")
                    self._check_requests_expiry()
                    self._check_challenge_limits()
                    if self.doc["progress"]["discovery"] == "cut" and not self._crawler_running():
                        self._abandon_refetches()
                    self._persist()
                if self.rt.get("start_failed") and not self.rt.get("stop_reason"):
                    await self._fail("start_url_unreachable", "site_url failed every retry and no other discovery source exists")
                    return
                await self._reextract_some()
                pending, in_progress = await self.queue_counts()
                self._progress_refresh(pending, in_progress)
                with self.svc.db.tx():
                    self._watchdog(pending, in_progress)
                idle = await self._crawl_idle()
                discovery = self.doc["progress"]["discovery"]
                if discovery == "running" and self.rt.get("feeder_done") and idle:
                    self.doc["progress"]["discovery"] = "finished"
                if idle and discovery in ("finished", "cut") and not self._crawler_running() and self._gaps_waiting():
                    with self.svc.db.tx():
                        self._gaps_at_end(discovery)
                        self._persist()
                    self._progress_refresh(pending, in_progress)
                ready = self._ready(pending, in_progress, idle)
                if time.monotonic() - self.last_persist > 2:
                    self._persist()
            if ready and await self._finish():
                return

    def _watchdog(self, pending: int, in_progress: int) -> None:
        """Nothing finished (no page, no record, no sitemap) for ``stall_seconds`` while work
        is queued or in flight: a ``problem`` event ``stalled`` and a timeline entry saying what
        is stuck; still nothing after a second period: the run stops with ``stalled``. A pause of
        the breaker or a 429 back-off, an open mapping request and stored pages waiting for a
        repair are waiting, not stalling."""
        limit = float(self.settings.stall_seconds or 0)
        if not limit or self.state not in ("running", "stopping"):
            return
        now_m = time.monotonic()
        p = self.doc["progress"]
        if self._stall_cut_at is not None:
            # the crawler did not end after the stall stop: cancel it, the run finishes without it
            if self._crawler_running() and now_m - self._stall_cut_at > min(60.0, limit):
                self.crawler_task.cancel()
            return
        waiting = (p["open_mapping_requests"] or p["pending_reextraction"] or now_m < self.paused_until
                   or (pending == 0 and in_progress == 0))
        if waiting:
            self.last_progress = now_m  # the clock counts only while work waits and nothing moves
            self.rt.pop("stall_reported", None)
            return
        quiet = now_m - self.last_progress
        if quiet < limit:
            self.rt.pop("stall_reported", None)
            return
        minutes = max(1, int(quiet // 60))
        stuck = sorted(k.split("\x1f", 1)[0] for k in self._in_progress_keys())[:3]
        what = (f"{in_progress} page(s) in flight, {pending} waiting"
                + (f"; in flight: {', '.join(stuck)}" if stuck else "")
                + (f"; browser: {self.browser}" if self.browser and self.config.get("fetch_mode") != "http" else ""))
        if not self.rt.get("stall_reported"):
            self.rt["stall_reported"] = True
            n = int(self.rt.get("stalls", 0)) + 1
            self.rt["stalls"] = n
            message = f"no progress for {minutes} min: no page finished and no record was written ({what})"
            self.svc.events.create(self.run_id, "problem", self.run_id, {"problem_code": "stalled", "message": self.clean(message)[:500]},
                                   problem_key=f"stalled:{n}")
            self.timeline("stalled", message)
        if quiet >= 2 * limit:
            message = f"stopped: no progress for {minutes} min ({what})"
            self.timeline("stalled", message)
            self._cut("stalled")
            self._stall_cut_at = now_m

    def _reload_state_fields(self) -> dict:
        """The API may change ``state`` (stop) while the job runs; everything else is ours."""
        row = self.svc.db.one("SELECT state, doc FROM runs WHERE run_id=?", (self.run_id,))
        if row is not None and row["state"] != self.doc["state"]:
            fresh = loads(row["doc"])
            self.doc["state"] = row["state"]
            self.doc["errors"] = fresh.get("errors", self.doc.get("errors"))
        return self.doc

    def _handle_operator_stop(self) -> None:
        self.rt["stop_handled"] = True
        self.rt["stop_reason"] = "stopped"
        self.doc["progress"]["discovery"] = "cut" if self.doc["progress"]["discovery"] != "finished" else "finished"
        if self.crawler is not None:
            try:
                self.crawler.stop("operator stop")
            except Exception:
                pass
        if self.feeder_task and not self.feeder_task.done():
            self.feeder_task.cancel()
        # open mapping requests are cancelled
        rows = self.svc.db.all("SELECT mapping_request_id, doc FROM mapping_requests WHERE run_id=? AND status='open'", (self.run_id,))
        for row in rows:
            d = loads(row["doc"])
            d["status"] = "cancelled"
            d["closed_at"] = now_iso()
            d["resolution"] = {"status": "cancelled", "version": None, "failure_reason": "operator stop"}
            self.svc.db.execute("UPDATE mapping_requests SET status='cancelled', doc=? WHERE mapping_request_id=?", (dumps(d), row["mapping_request_id"]))
        for t in (self.rt.get("templates") or {}).values():
            if t.get("open_request"):
                t["open_request"] = None
                if t["status"] == "repairing":
                    t["status"] = "failed"
        nt = self.rt.get("no_template") or {}
        nt["open_request"] = None
        # stored pages waiting for a repair -> unextracted 'stopped'
        rows = self.svc.db.all("SELECT page_key, doc, template_id FROM stored_pages WHERE run_id=?", (self.run_id,))
        for row in rows:
            d = loads(row["doc"])
            self._unextracted(d["url"], d.get("requested_url"), d.get("source_key"), d.get("site") or self.doc["site"], "stopped", row["template_id"], d.get("status"), 1)
        self.svc.db.execute("DELETE FROM stored_pages WHERE run_id=?", (self.run_id,))
        self.svc.db.execute("DELETE FROM link_backlog WHERE run_id=?", (self.run_id,))
        self._refresh_store_counter()

    # ================================================================== finishing
    async def _finish(self) -> bool:
        """Stops the crawler, drains the follower, then re-checks the completion condition under
        the lock before writing the result. False: not complete after all."""
        if self._crawler_running():
            try:
                self.crawler.stop("job complete")
            except Exception:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(self.crawler_task), timeout=120)
            except (asyncio.TimeoutError, Exception):
                self.crawler_task.cancel()
        for _ in range(80):
            if await self.follower_caught_up():
                break
            await asyncio.sleep(0.25)
        async with self.lock:
            self.doc = self._reload_state_fields()
            if self.state in ("completed", "failed", "waiting_for_credentials"):
                return True
            if not await self.follower_caught_up():
                return False
            self._progress_refresh(0, 0)
            p = self.doc["progress"]
            if p["open_mapping_requests"] or p["pending_reextraction"]:
                return False  # an activation or a new request arrived meanwhile; the supervisor goes on
            if self.follower_task:
                self.follower_task.cancel()
            self.trim_to_max_records()
            await self.svc.jobs.complete_run(self)
        return True

    def trim_to_max_records(self) -> None:
        """README 4.8 (as amended): the result never holds more than ``max_records`` records.
        Records beyond it (pages that were already in progress, re-extracted stored pages) are
        dropped deterministically — the first ``max_records`` in the result's order
        (``record_id``) are kept — and counted in ``counters.records_dropped_over_limit``."""
        limit = self.config.get("max_records")
        if not limit:
            return
        rows = self.svc.db.all("SELECT record_id, template_id, line FROM records WHERE run_id=? ORDER BY record_id", (self.run_id,))
        surplus = rows[int(limit):]
        if not surplus:
            return
        c = self.counters()
        with self.svc.db.tx():
            for row in surplus:
                self.svc.db.execute("DELETE FROM records WHERE run_id=? AND record_id=?", (self.run_id, row["record_id"]))
                c["records_extracted"] = max(0, c["records_extracted"] - 1)
                c["result_bytes"] = max(0, c["result_bytes"] - (len(row["line"].encode("utf-8")) + 1))
                t = self.template(row["template_id"])
                t["record_count"] = max(0, t.get("record_count", 0) - 1)
            c["records_dropped_over_limit"] = int(c.get("records_dropped_over_limit", 0)) + len(surplus)
            if not self.rt.get("stop_reason"):
                self.rt["stop_reason"] = "max_records"
            self._persist()

    async def _fail(self, code: str, message: str) -> None:
        await self.svc.jobs.fail_run(self, code, self.clean(message))

    async def _url_pattern_failed(self, error: UrlPatternError) -> None:
        if self.state not in ("failed", "completed"):
            await self._fail(error.pattern_code, error.message)
        if self.crawler is not None:
            self.crawler.stop("URL pattern policy failed")

    def leftovers_for_result(self, stop_reason: str) -> None:
        """Every target URL ends as a record or in the unextracted list (README 4.5)."""
        if stop_reason in STOP_LISTED_REASONS:
            # one query each instead of one per URL
            have_record = set()
            for r in self.svc.db.all("SELECT line FROM records WHERE run_id=?", (self.run_id,)):
                rec = loads(r["line"])
                have_record.add((rec["source_url"], rec.get("source_key")))
                have_record.add((rec["source_url"], None))
            listed = {r["url_key"] for r in self.svc.db.all("SELECT url_key FROM unextracted WHERE run_id=?", (self.run_id,))}
            rows = self.svc.db.all("SELECT unique_key, url, source, source_key FROM urls WHERE run_id=? AND status='queued' AND source IN ('list','sitemap','start')", (self.run_id,))
            for row in rows:
                try:
                    canonical = canonical_url(row["url"])
                    site = site_key(row["url"])
                except ValueError:
                    continue
                if (canonical, row["source_key"]) in have_record or (canonical, None) in have_record:
                    continue
                if page_key(canonical, row["source_key"]) in listed:
                    continue
                self._unextracted(canonical, row["url"], row["source_key"], site, "stopped", None, None, 0, "not fetched before the job stopped")
        rows = self.svc.db.all("SELECT doc, template_id, page_class FROM stored_pages WHERE run_id=?", (self.run_id,))
        for row in rows:
            d = loads(row["doc"])
            reason = row["page_class"] if row["page_class"] in ("incomplete", "no_template") else "stopped"
            self._unextracted(d["url"], d.get("requested_url"), d.get("source_key"), d.get("site") or self.doc["site"], reason, row["template_id"], d.get("status"), 1, missing=d.get("missing_fields"), gaps=d.get("gaps"))

    def cleanup_crawlee_storage(self) -> None:
        shutil.rmtree(self.job_dir / "crawlee" / "request_queues", ignore_errors=True)
        shutil.rmtree(self.job_dir / "crawlee" / "datasets", ignore_errors=True)

    def result_path(self) -> Path:
        return Path(self.settings.data_dir) / "results" / result_file_name(self.doc["site"], self.run_id)
