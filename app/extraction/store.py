"""Mapping versions per (site, template) (README 6.2)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.db import Database, dumps, loads
from app.core.timeutil import now_iso


def content_sha256(mapping: dict) -> str:
    text = json.dumps(mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def version_doc(row, include_content: bool = True) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "site": row["site"],
        "template_id": row["template_id"],
        "version": row["version"],
        "status": row["status"],
        "content_sha256": row["content_sha256"],
        "created_at": row["created_at"],
        "superseded_at": row["superseded_at"],
    }
    origin = loads(row["origin"])
    if origin is not None:
        doc["origin"] = origin
    if include_content:
        doc["mapping"] = loads(row["mapping"])
    return doc


class MappingStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def active(self, site: str, template_id: str):
        return self.db.one(
            "SELECT * FROM mapping_versions WHERE site=? AND template_id=? AND status='active'", (site, template_id)
        )

    def active_version(self, site: str, template_id: str) -> int:
        row = self.active(site, template_id)
        return int(row["version"]) if row else 0

    def active_mappings(self, site: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM mapping_versions WHERE site=? AND status='active' ORDER BY template_id", (site,))
        return [{"version": r["version"], "mapping": loads(r["mapping"]), "template_id": r["template_id"]} for r in rows]

    def get_version(self, site: str, template_id: str, version: int):
        return self.db.one(
            "SELECT * FROM mapping_versions WHERE site=? AND template_id=? AND version=?", (site, template_id, version)
        )

    def site_templates(self, site: str, template_id: str | None, versions: str, include_content: bool) -> list[dict]:
        sql = "SELECT DISTINCT template_id FROM mapping_versions WHERE site=?"
        params: list[Any] = [site]
        if template_id:
            sql += " AND template_id=?"
            params.append(template_id)
        out = []
        for row in self.db.all(sql + " ORDER BY template_id", tuple(params)):
            tid = row["template_id"]
            all_rows = self.db.all(
                "SELECT * FROM mapping_versions WHERE site=? AND template_id=? ORDER BY version DESC", (site, tid)
            )
            active = next((r for r in all_rows if r["status"] == "active"), None)
            if active is None:
                continue
            chosen = all_rows if versions == "all" else [active]
            out.append(
                {
                    "template_id": tid,
                    "active_version": active["version"],
                    "versions_total": len(all_rows),
                    "versions": [version_doc(r, include_content) for r in chosen],
                }
            )
        return out

    def sites(self) -> list[dict]:
        rows = self.db.all(
            "SELECT site, template_id, version, created_at FROM mapping_versions WHERE status='active' ORDER BY site, template_id"
        )
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(r["site"], []).append(
                {"template_id": r["template_id"], "active_version": r["version"], "updated_at": r["created_at"]}
            )
        return [{"site": s, "templates": t} for s, t in out.items()]

    def active_template_count(self, site: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM mapping_versions WHERE site=? AND status='active'", (site,)) or 0)

    def activate(self, site: str, template_id: str, expected_version: int, mapping: dict, origin: dict | None) -> tuple[str, Any]:
        """Returns ("unchanged"|"changed"|"conflict", row-or-active-version). Caller holds a transaction."""
        sha = content_sha256(mapping)
        active = self.active(site, template_id)
        if active is not None and active["content_sha256"] == sha:
            return "unchanged", active
        active_version = int(active["version"]) if active else 0
        if active_version != expected_version:
            return "conflict", active_version
        top = int(self.db.scalar("SELECT COALESCE(MAX(version),0) FROM mapping_versions WHERE site=? AND template_id=?", (site, template_id)) or 0)
        ts = now_iso()
        if active is not None:
            self.db.execute(
                "UPDATE mapping_versions SET status='superseded', superseded_at=? WHERE site=? AND template_id=? AND version=?",
                (ts, site, template_id, active_version),
            )
        new_version = top + 1
        self.db.execute(
            "INSERT INTO mapping_versions(site, template_id, version, status, content_sha256, created_at, superseded_at, origin, mapping) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (site, template_id, new_version, "active", sha, ts, None, dumps(origin) if origin is not None else None, dumps(mapping)),
        )
        return "changed", self.get_version(site, template_id, new_version)
