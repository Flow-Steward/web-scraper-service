"""Final smoke run against a public practice site (run inside the container only).

books.toscrape.com, max_records 5, default route, click and solvers OFF, delay >= 1 s.
Usage: python scripts/smoke_real.py books
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

import httpx

TOKEN = "smoke-token-0123456789abcdef"
PORT = 8180


def start_service() -> subprocess.Popen:
    data = tempfile.mkdtemp(prefix="wss-smoke-")
    env = dict(os.environ, WSS_API_TOKEN=TOKEN, WSS_SECRETS_KEY="smoke-secrets-key-0123456789", WSS_DATA_DIR=data, WSS_PORT=str(PORT), WSS_HOST="127.0.0.1")
    proc = subprocess.Popen([sys.executable, "-m", "app"], env=env, stdout=open(f"{data}/service.log", "wb"), stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/health", headers=H).status_code == 200:
                print("service up, data", data)
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise SystemExit("service did not start")


H = {"Authorization": f"Bearer {TOKEN}"}
C = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", headers=H, timeout=180)
PROFILE = {"profile_name": "smoke", "proxy_tiers": [], "solver": None, "webhook": {"url": "http://127.0.0.1:9/hook", "signing_secret": "smoke-signing-secret-000"}}


def validate(inputs: dict) -> dict:
    body = C.post("/v1/config/validate", json={"run_inputs": inputs, "profile": PROFILE}).json()
    assert body["valid"], body
    return body["config"]


def wait(run_id: str, timeout: float = 900) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = C.get(f"/v1/runs/{run_id}").json()
        if run["state"] in ("completed", "failed"):
            return run
        time.sleep(2)
    C.post(f"/v1/runs/{run_id}/stop", json={})
    return wait(run_id, 120)


def report(run: dict) -> None:
    keep = {k: run[k] for k in ("state", "outcome", "stop_reason", "resolved", "counters", "protection", "error")}
    keep["errors"] = run["errors"][-5:]
    print(json.dumps(keep, indent=1))
    if run.get("result"):
        lines = C.get(run["result"]["download_path"]).text.splitlines()
        for line in lines[:5]:
            rec = json.loads(line)
            print(rec["source_url"], json.dumps({k: (v[:80] if isinstance(v, str) else v) for k, v in rec["data"].items()}, ensure_ascii=False))
    unex = C.get(f"/v1/runs/{run['run_id']}/unextracted").json()
    print("unextracted:", unex["total"], [(i["url"], i["reason"], i["http_status"]) for i in unex["items"][:10]])


def books() -> None:
    mapping = {
        "schema_version": 1, "template_id": "book", "match": {"url_patterns": ["/catalogue/[^/]+_\\d+/index\\.html$"], "selector": "article.product_page"},
        "fields": [
            {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "div.product_main h1", "extract": "text"}]},
            {"name": "price", "type": "price", "required": True, "sources": [{"kind": "css", "selector": "p.price_color", "extract": "text"}]},
            {"name": "currency", "type": "currency", "sources": [{"kind": "css", "selector": "p.price_color", "extract": "text"}]},
            {"name": "description", "type": "html", "sources": [{"kind": "css", "selector": "#product_description + p", "extract": "html"}]},
            {"name": "upc", "type": "text", "sources": [{"kind": "css", "selector": "table.table tr:first-child td", "extract": "text"}]},
            {"name": "image_url", "type": "image_url", "sources": [{"kind": "css", "selector": "div.item.active img", "extract": "attribute", "attribute": "src"}]},
        ],
    }
    r = C.put("/v1/sites/books.toscrape.com/mappings", json={"template_id": "book", "expected_version": 0, "mapping": mapping})
    print("activate", r.status_code)
    cfg = validate({"site_url": "https://books.toscrape.com/", "max_records": 5, "fields": ["title", "price", "currency", "description", "upc", "image_url"],
                    "required_fields": ["title", "price"], "delay_min_seconds": 1, "delay_max_seconds": 2, "max_concurrency": 2, "max_pages": 60})
    print("analyze:", json.dumps(C.post("/v1/analyze", json={"config": cfg, "profile": PROFILE}).json()["recommended"]))
    r = C.post("/v1/runs", json={"config": cfg, "profile": PROFILE, "platform": {"job_id": "smoke", "step_id": "books", "project_id": "p", "connection_id": "c"}}, headers={"Idempotency-Key": "smoke:books"})
    report(wait(r.json()["run_id"]))


if __name__ == "__main__":
    proc = start_service()
    try:
        {"books": books}[sys.argv[1]]()
    finally:
        proc.terminate()
        proc.wait(60)
