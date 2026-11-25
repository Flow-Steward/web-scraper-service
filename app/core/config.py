"""Merging the configuration layers and applying the service rules (README 3.1, 12)."""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from typing import Any

from app.core.errors import field_error
from app.core.netguard import url_is_private
from app.core.schemas import CONTRACT_DIR
from app.settings import Settings

FORBIDDEN_HEADERS = {"authorization", "proxy-authorization", "cookie", "host", "content-length", "transfer-encoding"}
SECRET_FIELDS = {"username", "password", "proxy_urls", "api_key", "signing_secret"}
LAYERS = (("run_inputs", "run_inputs"), ("site_settings", "site_settings"), ("profile_defaults", None))
# Politeness presets (README 4.15): fields set in a layer win; robots.txt Crawl-delay is the floor
POLITENESS = {
    "gentle": {"delay_min_seconds": 5, "delay_max_seconds": 10, "max_concurrency": 1, "max_requests_per_minute": 10, "page_timeout_seconds": 120},
    "normal": {"delay_min_seconds": 2, "delay_max_seconds": 5, "max_concurrency": 2, "max_requests_per_minute": 30, "page_timeout_seconds": 60},
    "fast": {"delay_min_seconds": 0.5, "delay_max_seconds": 1.5, "max_concurrency": 4, "max_requests_per_minute": 120, "page_timeout_seconds": 30},
}


def politeness_text(cfg: dict) -> str:
    """"normal: 2–5 s, 2 at a time, 30/min, page timeout 60 s" — the effective politeness of a run."""
    def n(v):
        return f"{v:g}" if isinstance(v, (int, float)) else str(v)

    return (f"{cfg.get('politeness') or 'normal'}: {n(cfg['delay_min_seconds'])}–{n(cfg['delay_max_seconds'])} s, "
            f"{cfg['max_concurrency']} at a time, {cfg['max_requests_per_minute']}/min, page timeout {cfg.get('page_timeout_seconds', 60)} s")


@lru_cache(maxsize=1)
def job_config_schema() -> dict[str, Any]:
    return json.loads((CONTRACT_DIR / "schemas" / "job_config.schema.json").read_text("utf-8"))


def config_fields() -> list[str]:
    return list(job_config_schema()["properties"].keys())


def defaults() -> dict[str, Any]:
    return {name: copy.deepcopy(spec.get("default")) for name, spec in job_config_schema()["properties"].items()}


def is_unset(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def merge_layers(run_inputs: dict | None, site_settings: dict | None, profile_defaults: dict | None) -> tuple[dict, dict]:
    layers = [("run_inputs", run_inputs or {}), ("site_settings", site_settings or {}), ("profile_defaults", profile_defaults or {})]
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    base = defaults()
    for name in config_fields():
        for layer_name, layer in layers:
            if name in layer and not is_unset(layer[name]):
                merged[name] = copy.deepcopy(layer[name])
                sources[name] = layer_name
                break
        else:
            merged[name] = base[name]
            sources[name] = "default"
    # the politeness preset fills the politeness fields no layer set
    for name, value in POLITENESS.get(merged.get("politeness") or "normal", POLITENESS["normal"]).items():
        if sources.get(name) == "default":
            merged[name] = value
    if merged["delay_min_seconds"] > merged["delay_max_seconds"]:  # one bound set explicitly, the other from the preset
        if sources["delay_min_seconds"] == "default":
            merged["delay_min_seconds"] = merged["delay_max_seconds"]
        elif sources["delay_max_seconds"] == "default":
            merged["delay_max_seconds"] = merged["delay_min_seconds"]
    if sources["required_fields"] == "default":
        fields = merged["fields"] or []
        merged["required_fields"] = ["title"] if "title" in fields else fields[:1]
    return merged, sources


def _tiers(profile: dict) -> list[dict]:
    return list(profile.get("proxy_tiers") or [])


def selected_tiers(config: dict, profile: dict) -> list[dict]:
    tiers = sorted(_tiers(profile), key=lambda t: t.get("tier", 0))
    wanted = config.get("use_proxy_tiers") or []
    if wanted:
        tiers = [t for t in tiers if t.get("tier") in wanted]
    return tiers


async def apply_rules(
    config: dict,
    profile: dict,
    settings: Settings,
    browsers: dict[str, dict],
    sources: dict[str, str] | None = None,
) -> tuple[dict, list[dict], list[dict]]:
    """Returns (corrected config, errors, warnings). ``config`` is the merged form."""
    cfg = copy.deepcopy(config)
    errors: list[dict] = []
    warnings: list[dict] = []
    limits = settings.limits

    def limit(field: str, value: Any, maximum: int) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > maximum:
            errors.append(field_error(field, "limit_exceeded", f"{field} {value} is above the service limit {maximum}", maximum))

    limit("max_pages", cfg.get("max_pages"), limits["max_pages_per_job"])
    limit("max_sitemap_entries", cfg.get("max_sitemap_entries"), limits["max_sitemap_entries"])
    limit("max_duration_minutes", cfg.get("max_duration_minutes"), limits["max_duration_minutes"])
    limit("max_concurrency", cfg.get("max_concurrency"), limits["max_concurrency"])
    limit("max_requests_per_minute", cfg.get("max_requests_per_minute"), limits["max_requests_per_minute"])
    limit("max_clicks_per_job", cfg.get("max_clicks_per_job"), limits["max_clicks_per_job"])
    limit("sample_count", cfg.get("sample_count"), limits["max_sample_count"])
    limit("max_repairs_per_template", cfg.get("max_repairs_per_template"), limits["max_repairs_per_template"])
    limit("max_repairs_per_job", cfg.get("max_repairs_per_job"), limits["max_repairs_per_job"])
    if len(cfg.get("fields") or []) > limits["max_fields_per_template"]:
        errors.append(field_error("fields", "limit_exceeded", "too many fields", limits["max_fields_per_template"]))
    if len(cfg.get("urls") or []) > limits["max_url_list_entries"]:
        errors.append(field_error("urls", "limit_exceeded", f"more than {limits['max_url_list_entries']} URLs", limits["max_url_list_entries"]))
    solver = profile.get("solver") or None
    if solver and isinstance(solver.get("max_solves_per_job"), int):
        limit("profile.solver.max_solves_per_job", solver.get("max_solves_per_job"), limits["max_solves_per_job"])

    # "ignored unless allowed" -> warning + corrected value
    if cfg.get("challenge_click") and not settings.challenge_click_allowed:
        warnings.append(field_error("challenge_click", "challenge_click_not_allowed", "the service does not allow the checkbox click; set to false"))
        cfg["challenge_click"] = False
        if sources is not None:
            sources["challenge_click"] = "service_limit"
    if cfg.get("use_solver") and not settings.solvers_allowed:
        warnings.append(field_error("use_solver", "solver_not_allowed", "the service does not allow solvers; set to false"))
        cfg["use_solver"] = False
        if sources is not None:
            sources["use_solver"] = "service_limit"
    if cfg.get("respect_robots") is False and not settings.robots_override_allowed:
        warnings.append(field_error("respect_robots", "robots_override_not_allowed", "the service does not allow ignoring robots.txt; set to true"))
        cfg["respect_robots"] = True
        if sources is not None:
            sources["respect_robots"] = "service_limit"
    mode = cfg.get("discovery_mode")
    if mode != "url_list" and cfg.get("urls"):
        warnings.append(field_error("urls", "invalid_value", "urls are used only in url_list mode; ignored"))
        cfg["urls"] = []
    if mode == "url_list":
        if not cfg.get("urls"):
            errors.append(field_error("urls", "required", "url_list mode needs urls"))
    elif not cfg.get("site_url"):
        errors.append(field_error("site_url", "required", "site_url is required outside url_list mode"))

    if cfg.get("use_solver") and not solver:
        errors.append(field_error("use_solver", "solver_not_configured", "use_solver needs a solver in the connection profile"))

    tiers = _tiers(profile)
    seen_tiers: set[int] = set()
    for idx, tier in enumerate(tiers):
        t = tier.get("tier")
        if t in seen_tiers:
            errors.append(field_error(f"profile.proxy_tiers[{idx}].tier", "duplicate_proxy_tier", f"tier {t} is configured twice"))
        seen_tiers.add(t)
    for idx, wanted in enumerate(cfg.get("use_proxy_tiers") or []):
        if wanted not in seen_tiers:
            errors.append(field_error(f"use_proxy_tiers[{idx}]", "proxy_tier_not_configured", f"tier {wanted} is not configured in the profile"))
    rotating = [t for t in selected_tiers(cfg, profile) if t.get("session_mode") == "rotating"]
    if rotating:
        names = ", ".join(str(t.get("tier")) for t in rotating)
        for name in ("challenge_click", "use_solver"):
            if cfg.get(name):
                errors.append(field_error(name, "rotating_proxy_with_challenge_handling", f"{name} needs a sticky proxy session; tier {names} is rotating"))

    browser = cfg.get("browser")
    if browser in ("camoufox", "chromium") and not browsers.get(browser, {}).get("present"):
        errors.append(field_error("browser", "browser_not_available", f"{browser} is not in this service image"))

    fields = cfg.get("fields") or []
    for idx, name in enumerate(cfg.get("required_fields") or []):
        if name not in fields:
            errors.append(field_error(f"required_fields[{idx}]", "required_field_not_in_fields", f"{name} is not in fields"))
    dmin, dmax = cfg.get("delay_min_seconds"), cfg.get("delay_max_seconds")
    if isinstance(dmin, (int, float)) and isinstance(dmax, (int, float)) and dmin > dmax:
        errors.append(field_error("delay_min_seconds", "delay_range_invalid", "delay_min_seconds is greater than delay_max_seconds"))
    for name in ("url_include", "url_exclude", "crawl_include", "sitemap_source_include", "sitemap_source_exclude"):
        for idx, pattern in enumerate(cfg.get(name) or []):
            try:
                re.compile(pattern)
            except (re.error, TypeError):
                errors.append(field_error(f"{name}[{idx}]", "pattern_invalid", "the regular expression does not compile"))
    for header in (cfg.get("headers") or {}):
        if header.lower() in FORBIDDEN_HEADERS:
            errors.append(field_error("headers", "header_not_allowed", f"header {header} is not allowed"))

    for idx, tier in enumerate(tiers):
        if tier.get("provider") == "custom_list":
            if not tier.get("proxy_urls"):
                errors.append(field_error(f"profile.proxy_tiers[{idx}].proxy_urls", "secret_missing", "custom_list needs proxy_urls"))
        elif tier.get("auth_mode") == "user_password":
            for secret in ("username", "password"):
                if not tier.get(secret):
                    errors.append(field_error(f"profile.proxy_tiers[{idx}].{secret}", "secret_missing", f"{secret} is required for user_password"))
    if solver is not None and not solver.get("api_key"):
        errors.append(field_error("profile.solver.api_key", "secret_missing", "the solver needs an api_key"))

    # private targets
    targets: list[tuple[str, str]] = []
    if mode != "url_list" and cfg.get("site_url"):
        targets.append(("site_url", cfg["site_url"]))
    if cfg.get("sitemap_url") and mode != "url_list":
        targets.append(("sitemap_url", cfg["sitemap_url"]))
    if mode == "url_list":
        hosts_checked: dict[str, bool] = {}
        for idx, entry in enumerate(cfg.get("urls") or []):
            url = entry.get("url") if isinstance(entry, dict) else None
            if not url:
                continue
            from app.core.urls import host_of

            try:
                host = host_of(url)
            except ValueError:
                errors.append(field_error(f"urls[{idx}].url", "invalid_value", "not an http(s) URL"))
                continue
            if host not in hosts_checked:
                hosts_checked[host] = await url_is_private(url)
            if hosts_checked[host]:
                errors.append(field_error(f"urls[{idx}].url", "private_address_refused", "the host resolves to a private or special address, or does not resolve"))
    for field, url in targets:
        if await url_is_private(url):
            errors.append(field_error(field, "private_address_refused", "the host resolves to a private or special address, or does not resolve"))
    return cfg, errors, warnings


def split_secret_requirements(schema_errors: list[dict]) -> tuple[list[dict], list[dict]]:
    """Missing secrets reported by the schema become ``secret_missing`` rule errors."""
    secret, other = [], []
    for err in schema_errors:
        last = err["field"].rsplit(".", 1)[-1]
        if err["code"] == "required" and last in SECRET_FIELDS:
            secret.append(field_error(err["field"], "secret_missing", f"{last} is required"))
        else:
            other.append(err)
    return secret, other


def dedupe_url_list(urls: list[dict]) -> list[dict]:
    from app.core.urls import canonical_url

    seen: set[tuple[str, str | None]] = set()
    out = []
    for entry in urls:
        try:
            key = (canonical_url(entry["url"]), entry.get("key"))
        except ValueError:
            key = (entry["url"], entry.get("key"))
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def profile_summary(profile: dict) -> dict:
    tiers = []
    for tier in _tiers(profile):
        item = {k: tier[k] for k in ("tier", "provider", "protocol", "host", "port", "auth_mode", "country", "city", "session_mode", "sticky_minutes") if k in tier}
        if tier.get("provider") == "custom_list":
            item["proxy_url_count"] = len(tier.get("proxy_urls") or [])
        tiers.append(item)
    solver = profile.get("solver")
    summary: dict[str, Any] = {
        "proxy_tiers": tiers,
        "solver": None
        if not solver
        else {
            "provider": solver["provider"],
            "max_solves_per_job": solver.get("max_solves_per_job", 20),
            "challenge_types": solver.get("challenge_types", ["turnstile"]),
        },
        "webhook_url": (profile.get("webhook") or {}).get("url"),
    }
    if profile.get("profile_name"):
        summary["profile_name"] = profile["profile_name"]
    return summary


def secret_part(profile: dict) -> dict:
    """The proxy/solver part of a profile that is stored encrypted with a job."""
    return {"proxy_tiers": _tiers(profile), "solver": profile.get("solver")}


def needs_proxy_or_solver_secrets(config: dict, profile: dict) -> bool:
    """True when the job cannot run without decrypting stored proxy credentials or a solver key."""
    for tier in selected_tiers(config, profile):
        if tier.get("provider") == "custom_list" or tier.get("auth_mode") == "user_password":
            return True
    return bool(config.get("use_solver") and profile.get("solver"))
