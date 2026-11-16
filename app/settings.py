"""Service configuration, read once from the environment.

Every variable starts with ``WSS_``. Values above the contract ceilings are refused at
start-up, so ``GET /health`` never reports a limit the contract does not allow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SERVICE_NAME = "web-scraper-service"
SERVICE_VERSION = "0.2.1"

# name -> (default, contract ceiling)
LIMIT_DEFAULTS: dict[str, tuple[int, int]] = {
    "max_pages_per_job": (100_000, 1_000_000),
    "max_duration_minutes": (1_440, 10_080),
    "max_result_bytes": (103_809_024, 1_073_741_824),
    "max_concurrency": (16, 64),
    "max_concurrent_jobs": (2, 32),
    "max_queued_jobs": (50, 1_000),
    "max_requests_per_minute": (600, 6_000),
    "max_url_list_entries": (50_000, 100_000),
    "max_sample_count": (20, 50),
    "max_clicks_per_job": (100, 1_000),
    "max_solves_per_job": (100, 1_000),
    "max_repairs_per_template": (5, 10),
    "max_repairs_per_job": (20, 50),
    "max_stored_pages_per_job": (2_000, 100_000),
    "max_request_body_bytes": (16_777_216, 67_108_864),
    "max_templates_per_site": (20, 100),
    "max_fields_per_template": (50, 50),
    "max_sources_per_field": (5, 5),
    "max_transforms_per_source": (5, 5),
    "sample_html_max_bytes": (65_536, 262_144),
}

DEFAULT_SCHEDULE = [0, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600]


class SettingsError(ValueError):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise SettingsError(f"{name}={value} outside {minimum}..{maximum}")
    return value


def _float(name: str, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a number") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise SettingsError(f"{name}={value} outside {minimum}..{maximum}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    api_token: str
    data_dir: Path
    secrets_key: str | None
    host: str = "0.0.0.0"
    port: int = 8080
    limits: dict[str, int] = field(default_factory=dict)
    challenge_click_allowed: bool = False
    solvers_allowed: bool = False
    robots_override_allowed: bool = False
    result_days: int = 14
    job_record_days: int = 30
    stored_pages_days: int = 7
    samples_hours: int = 24
    claim_timeout_seconds: int = 21_600
    mapping_request_timeout_seconds: int = 3_600
    window_pages: int = 50
    min_pages: int = 10
    no_template_pages: int = 5
    block_window_pages: int = 50
    block_share: float = 0.8
    webhook_timeout_seconds: int = 10
    webhook_schedule_seconds: list[int] = field(default_factory=lambda: list(DEFAULT_SCHEDULE))
    webhook_repeat_seconds: int = 3600
    webhook_jitter: float = 0.1
    # deliveries to private / loopback / plain-http webhook URLs (the normal self-hosted layout:
    # the platform reachable only on a Docker network). Crawl targets stay strictly public.
    webhook_allow_private: bool = True
    # Test-only: allow scrape targets on private/loopback addresses (the local fixture site).
    allow_private_targets: bool = False
    # Host names always refused as targets, also when private targets are allowed (an operator
    # deny-list; the tests use it to prove the refusal on the fixture world).
    denied_target_hosts: list[str] = field(default_factory=list)
    ip_echo_url: str = "https://api.ipify.org?format=json"
    geo_lookup_url: str | None = "https://ipinfo.io/{ip}/json"
    challenge_wait_seconds: float = 30.0
    # stop on trouble
    breaker_failures: int = 5
    breaker_pause_seconds: float = 30.0
    breaker_max_pauses: int = 3
    sitemap_outdated_min: int = 20
    sitemap_outdated_share: float = 0.3
    compare_drop_share: float = 0.2
    challenge_click_wait_seconds: float = 15.0  # polling after the one click on a widget
    navigation_timeout_seconds: int = 60
    headless: bool = False  # the browser runs on the image's virtual display (Xvfb)
    browser_proxy_server_override: str | None = None
    build: str | None = None
    log_level: str = "INFO"
    supervisor_interval_seconds: float = 0.5
    retention_interval_seconds: float = 60.0
    stall_seconds: int = 900  # stall watchdog: no progress this long -> problem; twice as long -> stop (0 = off)

    @property
    def protection_steps(self) -> list[str]:
        from app.fetching.browsers import browser_presence

        steps = ["http"]
        presence = browser_presence()
        if presence.get("chromium", {}).get("present"):
            steps.append("chromium")
        if presence.get("camoufox", {}).get("present"):
            steps.append("camoufox")
        steps += ["next_proxy_tier", "wait"]
        if self.challenge_click_allowed:
            steps.append("click")
        if self.solvers_allowed:
            steps.append("solver")
        return steps


def load_settings() -> Settings:
    token = _env("WSS_API_TOKEN")
    if not token:
        raise SettingsError("WSS_API_TOKEN is required")
    limits: dict[str, int] = {}
    for name, (default, ceiling) in LIMIT_DEFAULTS.items():
        limits[name] = _int(f"WSS_LIMIT_{name.upper()}", default, 1, ceiling)
    schedule_raw = _env("WSS_WEBHOOK_SCHEDULE_SECONDS")
    schedule = list(DEFAULT_SCHEDULE)
    if schedule_raw:
        try:
            schedule = [int(x) for x in schedule_raw.split(",") if x.strip()]
        except ValueError as exc:
            raise SettingsError("WSS_WEBHOOK_SCHEDULE_SECONDS must be comma-separated integers") from exc
    return Settings(
        api_token=token,
        data_dir=Path(_env("WSS_DATA_DIR", "/data") or "/data"),
        secrets_key=_env("WSS_SECRETS_KEY"),
        host=_env("WSS_HOST", "0.0.0.0") or "0.0.0.0",
        port=_int("WSS_PORT", 8080, 1, 65535),
        limits=limits,
        challenge_click_allowed=_bool("WSS_ALLOW_CHALLENGE_CLICK", False),
        solvers_allowed=_bool("WSS_ALLOW_SOLVERS", False),
        robots_override_allowed=_bool("WSS_ALLOW_ROBOTS_OVERRIDE", False),
        result_days=_int("WSS_RETENTION_RESULT_DAYS", 14, 1),
        job_record_days=_int("WSS_RETENTION_JOB_RECORD_DAYS", 30, 1),
        stored_pages_days=_int("WSS_RETENTION_STORED_PAGES_DAYS", 7, 1),
        samples_hours=_int("WSS_RETENTION_SAMPLES_HOURS", 24, 1),
        claim_timeout_seconds=_int("WSS_CLAIM_TIMEOUT_SECONDS", 21_600, 60),
        mapping_request_timeout_seconds=_int("WSS_MAPPING_REQUEST_TIMEOUT_SECONDS", 3_600, 60),
        window_pages=_int("WSS_REPAIR_WINDOW_PAGES", 50, 1),
        min_pages=_int("WSS_REPAIR_MIN_PAGES", 10, 1),
        no_template_pages=_int("WSS_REPAIR_NO_TEMPLATE_PAGES", 5, 1),
        block_window_pages=_int("WSS_REPAIR_BLOCK_WINDOW_PAGES", 50, 1),
        block_share=_float("WSS_REPAIR_BLOCK_SHARE", 0.8, 0.0, 1.0),
        breaker_failures=_int("WSS_BREAKER_FAILURES", 5, 1, 1000),
        breaker_pause_seconds=_float("WSS_BREAKER_PAUSE_SECONDS", 30.0, 0.0, 3600.0),
        breaker_max_pauses=_int("WSS_BREAKER_MAX_PAUSES", 3, 0, 100),
        sitemap_outdated_min=_int("WSS_SITEMAP_OUTDATED_MIN", 20, 1, 100000),
        sitemap_outdated_share=_float("WSS_SITEMAP_OUTDATED_SHARE", 0.3, 0.0, 1.0),
        compare_drop_share=_float("WSS_COMPARE_DROP_SHARE", 0.2, 0.0, 1.0),
        webhook_timeout_seconds=_int("WSS_WEBHOOK_TIMEOUT_SECONDS", 10, 1, 120),
        webhook_schedule_seconds=schedule,
        webhook_repeat_seconds=_int("WSS_WEBHOOK_REPEAT_SECONDS", 3600, 1),
        webhook_jitter=_float("WSS_WEBHOOK_JITTER", 0.1, 0.0, 0.5),
        webhook_allow_private=_bool("WSS_WEBHOOK_ALLOW_PRIVATE", True),
        allow_private_targets=_bool("WSS_UNSAFE_ALLOW_PRIVATE_TARGETS", False),
        denied_target_hosts=[h.strip().lower() for h in (_env("WSS_DENIED_TARGET_HOSTS", "") or "").split(",") if h.strip()],
        ip_echo_url=_env("WSS_IP_ECHO_URL", "https://api.ipify.org?format=json") or "",
        geo_lookup_url=_env("WSS_GEO_LOOKUP_URL", "https://ipinfo.io/{ip}/json"),
        # a managed challenge cleared itself after 29-30 s for the host-OS identity; the
        # wait stops as soon as the challenge clears, so a long ceiling costs nothing on a pass
        challenge_wait_seconds=_float("WSS_CHALLENGE_WAIT_SECONDS", 30.0, 0.0, 120.0),
        challenge_click_wait_seconds=_float("WSS_CHALLENGE_CLICK_WAIT_SECONDS", 15.0, 1.0, 120.0),
        navigation_timeout_seconds=_int("WSS_NAVIGATION_TIMEOUT_SECONDS", 60, 5, 600),
        headless=_bool("WSS_BROWSER_HEADLESS", False),
        build=_env("WSS_BUILD"),
        log_level=_env("WSS_LOG_LEVEL", "INFO") or "INFO",
        supervisor_interval_seconds=_float("WSS_SUPERVISOR_INTERVAL_SECONDS", 0.5, 0.05, 10),
        retention_interval_seconds=_float("WSS_RETENTION_INTERVAL_SECONDS", 60.0, 0.5, 86400),
        stall_seconds=_int("WSS_STALL_SECONDS", 900, 0, 86400),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def set_settings(settings: Settings | None) -> None:
    global _settings
    _settings = settings
