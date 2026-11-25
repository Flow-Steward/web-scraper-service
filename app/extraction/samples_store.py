"""Stored samples (``smp_…``): the raw page on the volume, the sample object in SQLite."""

from __future__ import annotations

import re
import time
from pathlib import Path

from app.core.db import Database, dumps, loads
from app.core.timeutil import now, now_iso, parse


class SampleStore:
    SHOT_MAX_BYTES = 4 * 1024 * 1024
    SHOT_RE = re.compile(r"^shot_[A-Za-z0-9]{1,64}$")

    def __init__(self, db: Database, data_dir: Path, shot_hours: int = 24) -> None:
        self.db = db
        self.dir = data_dir / "samples"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.shot_dir = self.dir / "shots"
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.shot_hours = shot_hours

    # screenshots of the page after the last route step (config ``debug: true``); same retention as samples
    def save_shot(self, png: bytes) -> str | None:
        from app.core.ids import new_id

        if not png or len(png) > self.SHOT_MAX_BYTES:
            return None
        shot_id = new_id("shot")
        (self.shot_dir / f"{shot_id}.png").write_bytes(png)
        return shot_id

    def load_shot(self, shot_id: str) -> bytes | None:
        if not self.SHOT_RE.match(shot_id or ""):
            return None
        path = self.shot_dir / f"{shot_id}.png"
        if not path.exists() or time.time() - path.stat().st_mtime > self.shot_hours * 3600:
            return None
        return path.read_bytes()

    def save(self, sample: dict, html: str, final_url: str) -> None:
        (self.dir / f"{sample['sample_id']}.html").write_text(html, "utf-8")
        site = sample.get("url", "")
        from app.core.urls import site_key

        try:
            site = site_key(final_url)
        except ValueError:
            pass
        self.db.execute(
            "INSERT OR REPLACE INTO samples(sample_id, site, expires_at, final_url, doc) VALUES (?,?,?,?,?)",
            (sample["sample_id"], site, sample["expires_at"], final_url, dumps(sample)),
        )

    def save_extra(self, sample_id: str, extra: dict) -> None:
        """The shop JSON fetched for the sample's variant sources, so a mapping test
        can apply a ``shop_json`` source without fetching again; not part of the API answer."""
        import json

        existing = self.load_extra(sample_id)
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(existing.get(key), dict):
                existing[key].update(value)
            else:
                existing[key] = value
        (self.dir / f"{sample_id}.extra.json").write_text(json.dumps(existing), "utf-8")

    def load_extra(self, sample_id: str) -> dict:
        import json

        path = self.dir / f"{sample_id}.extra.json"
        try:
            return json.loads(path.read_text("utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            return {}

    def load(self, sample_id: str) -> tuple[dict, str, str] | None:
        row = self.db.one("SELECT * FROM samples WHERE sample_id=?", (sample_id,))
        if row is None or parse(row["expires_at"]) <= now():
            return None
        path = self.dir / f"{sample_id}.html"
        if not path.exists():
            return None
        return loads(row["doc"]), path.read_text("utf-8", errors="replace"), row["final_url"]

    def expire(self) -> dict[str, tuple[int, int]]:
        """Removes expired samples and screenshots; answers ``{kind: (items, bytes)}`` for the
        sweeper's summary."""
        cutoff = time.time() - self.shot_hours * 3600
        shots = [0, 0]
        for path in self.shot_dir.glob("shot_*.png"):
            try:
                st = path.stat()
                if st.st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    shots[0] += 1
                    shots[1] += st.st_size
            except OSError:
                pass
        samples = [0, 0]
        for row in self.db.all("SELECT sample_id FROM samples WHERE expires_at <= ?", (now_iso(),)):
            for name in (f"{row['sample_id']}.html", f"{row['sample_id']}.extra.json"):
                path = self.dir / name
                try:
                    samples[1] += path.stat().st_size
                except OSError:
                    pass
                path.unlink(missing_ok=True)
            self.db.execute("DELETE FROM samples WHERE sample_id=?", (row["sample_id"],))
            samples[0] += 1
        return {"samples": (samples[0], samples[1]), "protection_screenshots": (shots[0], shots[1])}
