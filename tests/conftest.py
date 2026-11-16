"""Test harness: the fixture world and the service as real subprocesses (kill/restart capable)."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-0123456789abcdef"
SECRETS_KEY = "test-secrets-key-0123456789abcdef"
SERVICE_PORT = int(os.environ.get("WSS_TEST_PORT", "8080"))
SHOP = "http://shop.test:8601"
PLAIN = "http://plain.test:8602"
GUARD = "http://guard.test:8603"
HOOK = "http://hook.test:8604"
SOLVER = "http://solver.test:8605"
SHOP_SITE = "shop.test:8601"
HOOK_SECRET = "fs_whsec_fixture_0000000000000001"


def _wait_port(host: str, port: int, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} did not open")


@pytest.fixture(scope="session")
def fixture_world():
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fixture" / "server.py")], cwd=str(ROOT))
    for port in (8601, 8602, 8603, 8604, 8605, 8606, 8607):
        _wait_port("127.0.0.1", port)
    yield Admin()
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


class Admin:
    def state(self, port: int = 8601) -> dict:
        return httpx.get(f"http://127.0.0.1:{port}/__admin/state", timeout=10).json()

    def set(self, port: int = 8601, **values) -> None:
        httpx.post(f"http://127.0.0.1:{port}/__admin/state", json=values, timeout=10).raise_for_status()

    def deliveries(self) -> list[dict]:
        return self.state(8604)["deliveries"]


def base_env(data_dir: str, WSS_PORT: str = str(SERVICE_PORT), **extra: str) -> dict:
    env = dict(os.environ)
    env.update({
        "WSS_API_TOKEN": TOKEN,
        "WSS_SECRETS_KEY": SECRETS_KEY,
        "WSS_DATA_DIR": data_dir,
        "WSS_PORT": WSS_PORT,
        "WSS_HOST": "127.0.0.1",
        "WSS_UNSAFE_ALLOW_PRIVATE_TARGETS": "1",
        "WSS_WEBHOOK_SCHEDULE_SECONDS": "0,1,2,3,5",
        "WSS_WEBHOOK_REPEAT_SECONDS": "5",
        "WSS_WEBHOOK_JITTER": "0",
        "WSS_REPAIR_WINDOW_PAGES": "6",
        "WSS_REPAIR_MIN_PAGES": "3",
        "WSS_REPAIR_NO_TEMPLATE_PAGES": "2",
        "WSS_REPAIR_BLOCK_WINDOW_PAGES": "4",
        "WSS_CHALLENGE_WAIT_SECONDS": "3",
        "WSS_IP_ECHO_URL": f"{SOLVER}/ip",
        "WSS_GEO_LOOKUP_URL": "",
        "WSS_SUPERVISOR_INTERVAL_SECONDS": "0.2",
        "WSS_LOG_LEVEL": "INFO",
    })
    env.update(extra)
    return env


class ServiceProc:
    def __init__(self, data_dir: str, port: int = SERVICE_PORT, **env: str) -> None:
        self.data_dir = data_dir
        self.port = port
        self.env = base_env(data_dir, WSS_PORT=str(port), **env)
        self.proc: subprocess.Popen | None = None
        self.log_path = Path(data_dir) / "service.log"
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)

    def start(self, **env: str) -> "ServiceProc":
        merged = dict(self.env)
        merged.update(env)
        self.env = merged
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=str(ROOT), env=merged, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if self.client.get("/health").status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError("service exited:\n" + self.log_path.read_text()[-4000:])
            time.sleep(0.2)
        raise RuntimeError("service did not start:\n" + self.log_path.read_text()[-4000:])

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(20)

    def terminate(self) -> int | None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                return self.proc.wait(120)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return self.proc.returncode if self.proc else None

    def log(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""

    # helpers ---------------------------------------------------------------
    def get(self, path: str, **kw):
        return self.client.get(path, **kw)

    def post(self, path: str, body=None, **kw):
        return self.client.post(path, json=body if body is not None else {}, **kw)

    def put(self, path: str, body, **kw):
        return self.client.put(path, json=body, **kw)

    def wait_run(self, run_id: str, states=("completed", "failed"), timeout: float = 240) -> dict:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            r = self.get(f"/v1/runs/{run_id}")
            if r.status_code == 200:
                last = r.json()
                if last["state"] in states:
                    return last
            time.sleep(0.5)
        raise AssertionError(f"run {run_id} did not reach {states}: {json.dumps(last)[:3000] if last else None}\n{self.log()[-5000:]}")


@pytest.fixture(scope="session")
def service(fixture_world):
    data = tempfile.mkdtemp(prefix="wss-test-")
    svc = ServiceProc(data).start()
    yield svc
    svc.terminate()


def profile(webhook_name: str = "default", **extra) -> dict:
    prof = {"profile_name": "fixture", "proxy_tiers": [], "solver": None, "webhook": {"url": f"{HOOK}/hook/{webhook_name}", "signing_secret": HOOK_SECRET}}
    prof.update(extra)
    return prof


def platform(job: str, step: str = "start_run") -> dict:
    return {"job_id": job, "step_id": step, "loop_position": None, "workflow_id": "wf_test", "project_id": "prj_test", "account_id": "acc", "connection_id": "conn_test"}


# the suite keeps its own speed and the old start rule unless a test says otherwise
TEST_RUN_DEFAULTS = {"politeness": "fast", "page_timeout_seconds": 45, "when_site_busy": "queue"}


def merged_config(svc: ServiceProc, run_inputs: dict, prof: dict | None = None, site_settings: dict | None = None) -> dict:
    run_inputs = {**TEST_RUN_DEFAULTS, **run_inputs}
    r = svc.post("/v1/config/validate", {"run_inputs": run_inputs, "site_settings": site_settings or {}, "profile": prof or profile()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"], body
    return body["config"]


def start(svc: ServiceProc, config: dict, key: str, prof: dict | None = None, job: str | None = None):
    return svc.client.post("/v1/runs", json={"config": config, "profile": prof or profile(), "platform": platform(job or key)}, headers={"Idempotency-Key": key})


PRODUCT_MAPPING = {
    "schema_version": 1,
    "template_id": "product",
    "label": "Lamp product page",
    "match": {"url_patterns": ["/produkt/[^/?]+$", "/js/", "/flaky/", "/slow/", "/tiered/", "/cf/", "/err5/", "/blocked/"], "selector": "div.product-detail, main.product-detail"},
    "fields": [
        {"name": "title", "type": "text", "required": True, "sources": [
            {"kind": "jsonld", "schema_type": "Product", "path": "name", "transforms": [{"op": "trim"}]},
            {"kind": "css", "selector": "h1.product-title", "extract": "text", "transforms": [{"op": "trim"}]}]},
        {"name": "description", "type": "html", "sources": [{"kind": "css", "selector": "div.product-description", "extract": "html", "transforms": [{"op": "html_clean"}]}]},
        {"name": "price", "type": "price", "required": True, "sources": [
            {"kind": "jsonld", "schema_type": "Product", "path": "offers.price", "transforms": [{"op": "price", "decimal_separator": "dot"}]},
            {"kind": "css", "selector": "span.price-current", "extract": "text", "transforms": [{"op": "price", "decimal_separator": "comma"}]}]},
        {"name": "currency", "type": "currency", "sources": [
            {"kind": "jsonld", "schema_type": "Product", "path": "offers.priceCurrency"},
            {"kind": "css", "selector": "span.price-current", "extract": "text", "transforms": [{"op": "currency"}]}]},
        {"name": "part_number", "type": "text", "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "sku"}]},
        {"name": "image_urls", "type": "image_url", "multiple": True, "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "image.*", "transforms": [{"op": "absolute_url"}]}]},
        {"name": "in_stock", "type": "boolean", "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "offers.availability"}]},
    ],
}

PART_MAPPING = {
    "schema_version": 1,
    "template_id": "spare_part",
    "match": {"url_patterns": ["/ersatzteile/"], "selector": "div.part-sheet"},
    "fields": [
        {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h2.part-name", "extract": "text"}]},
        {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".part-price", "extract": "text"}]},
        {"name": "currency", "type": "currency", "sources": [{"kind": "css", "selector": ".part-price", "extract": "text"}]},
    ],
}


def ensure_mappings(svc: ServiceProc, site: str, *mappings: dict) -> None:
    for m in mappings:
        cur = svc.get(f"/v1/sites/{site}/mappings", params={"template_id": m["template_id"]}).json()
        active = cur["templates"][0]["active_version"] if cur["templates"] else 0
        r = svc.put(f"/v1/sites/{site}/mappings", {"template_id": m["template_id"], "expected_version": active, "mapping": m})
        assert r.status_code == 200, r.text


@pytest.fixture(scope="session")
def guarded_service(fixture_world):
    """A second instance that allows the click and solvers (both off by default)."""
    data = tempfile.mkdtemp(prefix="wss-guard-")
    svc = ServiceProc(data, port=8090, WSS_ALLOW_CHALLENGE_CLICK="1", WSS_ALLOW_SOLVERS="1", WSS_REPAIR_BLOCK_WINDOW_PAGES="50").start()
    yield svc
    svc.terminate()


def new_service(port: int, **env: str) -> ServiceProc:
    data = tempfile.mkdtemp(prefix=f"wss-{port}-")
    return ServiceProc(data, port=port, **env)


def check(resp, ref: str, status: int | None = None):
    """Validates a real response body against the contract schema."""
    from app.core.schemas import validator

    if status is not None:
        assert resp.status_code == status, resp.text[:3000]
    body = resp.json()
    errors = list(validator(ref).iter_errors(body))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors][:5]
    assert resp.headers.get("x-request-id", "").startswith("req_")
    return body


def err(resp, status: int, code: str):
    body = check(resp, "error.schema.json", status)
    assert body["error"]["code"] == code, body
    return body


def fast(**extra) -> dict:
    base = {"delay_min_seconds": 0, "delay_max_seconds": 0, "max_concurrency": 2}
    base.update(extra)
    return base
