"""Resume applies approved proxy authentication modes and endpoints to new runners."""

import asyncio
import copy
from urllib.parse import urlsplit

import pytest

from app.core.config import profile_summary
from app.core.db import loads
from app.jobs.runner import JobRunner
from tests.conftest import profile
from tests.unit.test_proxy_auth_pause import make_run


def tier(mode, host="original-proxy.test"):
    value = {"tier": 1, "provider": "generic", "protocol": "http", "host": host, "port": 8080, "auth_mode": mode}
    if mode == "user_password":
        value.update(username="fixture-user", password="fixture-password")
    return value


@pytest.mark.parametrize("initial_mode,replacement_mode", [("ip_allowlist", "user_password"), ("user_password", "ip_allowlist")])
@pytest.mark.parametrize("new_host", ["original-proxy.test", "approved-proxy.test"])
async def test_resume_applies_approved_auth_mode_and_endpoint(tmp_path, monkeypatch, initial_mode, replacement_mode, new_host):
    initial = profile(proxy_tiers=[tier(initial_mode)])
    approved = {"profile_name": "approved", "proxy_tiers": [tier(replacement_mode, new_host)]}
    svc, runner = await make_run(tmp_path, monkeypatch, connection_profile=initial, config_overrides={"use_proxy_tiers": [1]})
    runner.rt.update(navigation_actions=7, pages_started=4, running_seconds=12, sitemap_traversal={"queue": ["https://target.test/next.xml"]})
    runner.counters()["records_extracted"] = 1
    runner.prot().update(clicks_used=2, solves_used=3)
    before_config = copy.deepcopy(runner.config)
    await runner._pause_for_proxy_auth()
    monkeypatch.setattr(svc.jobs, "_start_runner", lambda _: None)
    try:
        assert runner.rt["needs_secrets"] == (initial_mode == "user_password")
        await svc.jobs.resume(runner.run_id, approved)
        resumed = JobRunner(svc, runner.run_id)
        assert resumed.state == "running"
        assert resumed.rt["needs_secrets"] == (replacement_mode == "user_password")
        loaded = resumed._load_secret()
        resumed.apply_secret(loaded)
        actual = resumed.tiers.tier_for("target.test")
        assert actual["host"] == new_host
        assert actual["auth_mode"] == replacement_mode
        proxy_url = urlsplit(resumed.tiers.url_for("target.test", "fixture-session"))
        assert proxy_url.hostname == new_host
        if replacement_mode == "user_password":
            assert actual["username"] == "fixture-user"
            assert actual["password"] == "fixture-password"
            assert proxy_url.username == "fixture-user" and proxy_url.password == "fixture-password"
        else:
            assert proxy_url.username is None and proxy_url.password is None
            assert "username" not in actual and "password" not in actual
        assert resumed.doc["profile"]["proxy_tiers"] == profile_summary(approved)["proxy_tiers"]
        assert resumed.doc["profile"]["profile_name"] == "approved"
        assert resumed.doc["profile"]["webhook_url"] == initial["webhook"]["url"]
        assert "fixture-password" not in str(resumed.doc)
        assert resumed.config == before_config
        assert resumed.rt["pages_started"] == 4
        assert resumed.rt["navigation_actions"] == 7
        assert resumed.rt["running_seconds"] == 12
        assert resumed.counters()["records_extracted"] == 1
        assert resumed.prot()["clicks_used"] == 2 and resumed.prot()["solves_used"] == 3
        assert resumed.rt["sitemap_traversal"] == {"queue": ["https://target.test/next.xml"]}
    finally:
        svc.db.close()


@pytest.mark.parametrize("initial_mode", ["ip_allowlist", "user_password"])
@pytest.mark.parametrize("empty_proxy_fields", [False, True])
async def test_webhook_only_resume_preserves_proxy_profile_and_secret_requirements(tmp_path, monkeypatch, initial_mode, empty_proxy_fields):
    initial = profile(proxy_tiers=[tier(initial_mode)])
    svc, runner = await make_run(tmp_path, monkeypatch, connection_profile=initial, config_overrides={"use_proxy_tiers": [1]})
    await runner._pause_for_proxy_auth()
    monkeypatch.setattr(svc.jobs, "_start_runner", lambda _: None)
    try:
        before_secret = svc.secrets.get(runner.run_id, "proxy_solver")
        webhook_only = {"webhook": {"url": "http://hook.test/approved", "signing_secret": "fixture-webhook-secret"}}
        if empty_proxy_fields:
            webhook_only.update(proxy_tiers=[], solver=None)
        await svc.jobs.resume(runner.run_id, webhook_only)
        resumed = JobRunner(svc, runner.run_id)
        assert resumed.rt["needs_secrets"] == (initial_mode == "user_password")
        assert resumed.doc["profile"]["proxy_tiers"] == runner.doc["profile"]["proxy_tiers"]
        assert svc.secrets.get(runner.run_id, "proxy_solver") == before_secret
        assert resumed._load_secret()["proxy_tiers"][0]["host"] == "original-proxy.test"
        assert svc.secrets.get(runner.run_id, "webhook")["url"] == "http://hook.test/approved"
    finally:
        svc.db.close()


async def test_approved_profile_survives_old_runner_teardown_persist(tmp_path, monkeypatch):
    initial = profile(proxy_tiers=[tier("ip_allowlist")])
    approved = {"proxy_tiers": [tier("user_password", "approved-proxy.test")]}
    svc, runner = await make_run(tmp_path, monkeypatch, connection_profile=initial, config_overrides={"use_proxy_tiers": [1]})
    await runner._pause_for_proxy_auth()
    svc.jobs.runners[runner.run_id] = runner
    released = asyncio.Event()

    async def teardown():
        await released.wait()
        runner.rt["running_seconds"] = 17
        runner._persist()
        svc.jobs.runner_done(runner.run_id)

    runner.task = asyncio.create_task(teardown())
    monkeypatch.setattr(svc.jobs, "_start_runner", lambda _: None)
    try:
        resuming = asyncio.create_task(svc.jobs.resume(runner.run_id, approved))
        await asyncio.sleep(0)
        released.set()
        await resuming
        resumed = JobRunner(svc, runner.run_id)
        assert resumed.rt["needs_secrets"] is True
        assert resumed.rt["running_seconds"] == 17
        assert resumed.doc["profile"]["proxy_tiers"][0]["host"] == "approved-proxy.test"
        assert resumed._load_secret()["proxy_tiers"][0]["auth_mode"] == "user_password"
    finally:
        released.set()
        await runner.task
        svc.db.close()


async def test_ip_allowlist_replacement_must_cover_selected_tiers(tmp_path, monkeypatch):
    initial = profile(proxy_tiers=[tier("ip_allowlist")])
    svc, runner = await make_run(tmp_path, monkeypatch, connection_profile=initial, config_overrides={"use_proxy_tiers": [1]})
    await runner._pause_for_proxy_auth()
    starts = []
    monkeypatch.setattr(svc.jobs, "_start_runner", lambda run_id: starts.append(run_id))
    unrelated = tier("ip_allowlist", "other-tier.test")
    unrelated["tier"] = 2
    try:
        result = await svc.jobs.resume(runner.run_id, {"proxy_tiers": [unrelated]})
        assert result["state"] == "waiting_for_credentials"
        assert starts == []
    finally:
        svc.db.close()


async def test_proxy_replacement_without_required_solver_stays_paused(tmp_path, monkeypatch):
    initial = profile(proxy_tiers=[tier("ip_allowlist")])
    svc, runner = await make_run(tmp_path, monkeypatch, connection_profile=initial, config_overrides={"use_proxy_tiers": [1]})
    runner.config["use_solver"] = True
    await runner._pause_for_proxy_auth()
    monkeypatch.setattr(svc.jobs, "_start_runner", lambda _: None)
    try:
        result = await svc.jobs.resume(runner.run_id, {"proxy_tiers": [tier("ip_allowlist", "approved-proxy.test")]})
        assert result["state"] == "waiting_for_credentials"
        assert loads(svc.jobs._row(runner.run_id)["runtime"])["needs_secrets"] is True
    finally:
        svc.db.close()


async def test_running_profile_rotation_uses_updated_requirements_and_persists_them(tmp_path, monkeypatch):
    initial = profile(proxy_tiers=[tier("ip_allowlist")])
    svc, runner = await make_run(tmp_path, monkeypatch, connection_profile=initial, config_overrides={"use_proxy_tiers": [1]})
    svc.jobs.runners[runner.run_id] = runner
    runner.apply_secret(runner._load_secret())
    approved = {"proxy_tiers": [tier("user_password", "approved-proxy.test")]}
    try:
        await svc.jobs.resume(runner.run_id, approved)
        assert runner.rt["needs_secrets"] is True
        assert runner.tiers.tier_for("target.test")["host"] == "approved-proxy.test"
        assert runner.tiers.tier_for("target.test")["password"] == "fixture-password"
        runner._persist()
        reconstructed = JobRunner(svc, runner.run_id)
        assert reconstructed._load_secret()["proxy_tiers"][0]["host"] == "approved-proxy.test"
        assert reconstructed.doc["profile"]["proxy_tiers"][0]["auth_mode"] == "user_password"
    finally:
        svc.db.close()


async def test_solver_replacement_updates_public_summary_without_resetting_solve_budget(tmp_path, monkeypatch):
    initial = profile(proxy_tiers=[tier("ip_allowlist")])
    svc, runner = await make_run(tmp_path, monkeypatch, connection_profile=initial, config_overrides={"use_proxy_tiers": [1]})
    runner.config["use_solver"] = True
    runner.prot().update(solves_used=2, solves_limit=3)
    await runner._pause_for_proxy_auth()
    monkeypatch.setattr(svc.jobs, "_start_runner", lambda _: None)
    approved = {"proxy_tiers": [tier("ip_allowlist", "approved-proxy.test")], "solver": {"provider": "capsolver", "api_key": "fixture-solver-key", "max_solves_per_job": 99}}
    try:
        await svc.jobs.resume(runner.run_id, approved)
        reconstructed = JobRunner(svc, runner.run_id)
        assert reconstructed.rt["needs_secrets"] is True
        assert reconstructed.doc["profile"]["solver"]["provider"] == "capsolver"
        assert "fixture-solver-key" not in str(reconstructed.doc)
        assert reconstructed._load_secret()["solver"]["api_key"] == "fixture-solver-key"
        assert reconstructed.prot()["solves_used"] == 2
        assert reconstructed.prot()["solves_limit"] == 3
    finally:
        svc.db.close()


async def test_resume_rechecks_latest_profile_coverage_after_old_runner_releases_queue(tmp_path, monkeypatch):
    initial = profile(proxy_tiers=[tier("ip_allowlist")])
    svc, runner = await make_run(tmp_path, monkeypatch, connection_profile=initial, config_overrides={"use_proxy_tiers": [1]})
    await runner._pause_for_proxy_auth()
    svc.jobs.runners[runner.run_id] = runner
    released = asyncio.Event()

    async def teardown():
        await released.wait()
        runner._persist()
        svc.jobs.runner_done(runner.run_id)

    runner.task = asyncio.create_task(teardown())
    starts = []
    monkeypatch.setattr(svc.jobs, "_start_runner", lambda run_id: starts.append(run_id))
    unrelated = tier("ip_allowlist", "other-tier.test")
    unrelated["tier"] = 2
    try:
        first = asyncio.create_task(svc.jobs.resume(runner.run_id, {"proxy_tiers": [tier("user_password", "approved-proxy.test")]}))
        await asyncio.sleep(0)
        second = await svc.jobs.resume(runner.run_id, {"proxy_tiers": [unrelated]})
        assert second["state"] == "waiting_for_credentials"
        released.set()
        final = await first
        assert final["state"] == "waiting_for_credentials"
        assert starts == []
    finally:
        released.set()
        await runner.task
        svc.db.close()
