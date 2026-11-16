"""One SQLite file on the volume for jobs, mappings, events and deliveries.

Concurrency model: one connection, used only from the event-loop thread
(every API route is ``async``), in WAL mode. A transaction is a synchronous block:
**no ``await`` may happen inside ``tx()``**, so no other coroutine can run while it is
open and every transaction has exactly one writer. ``tx()`` enforces this: entering it
from another asyncio task while one is open raises ``TransactionSpansAwait`` instead of
silently joining (and later committing or rolling back) the other task's writes.
Statements outside ``tx()`` autocommit. Work that has to await (DNS, robots.txt, the
request queue) is done before or after the transaction, never inside it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    site TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    project_id TEXT, connection_id TEXT, platform_job_id TEXT,
    doc TEXT NOT NULL,           -- the run summary fields kept as JSON
    runtime TEXT NOT NULL        -- internal job state (windows, tiers, follower offset ...)
);
CREATE INDEX IF NOT EXISTS runs_order ON runs(created_at DESC, run_id DESC);

CREATE TABLE IF NOT EXISTS run_secrets (
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,          -- proxy_solver | webhook
    key_id TEXT NOT NULL,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    PRIMARY KEY (run_id, kind)
);

CREATE TABLE IF NOT EXISTS records (
    run_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    mapping_version INTEGER NOT NULL,
    extracted_at TEXT NOT NULL,
    line TEXT NOT NULL,
    PRIMARY KEY (run_id, record_id)
);

CREATE TABLE IF NOT EXISTS unextracted (
    run_id TEXT NOT NULL,
    url_key TEXT NOT NULL,
    url TEXT NOT NULL,
    reason TEXT NOT NULL,
    template_id TEXT,
    doc TEXT NOT NULL,
    PRIMARY KEY (run_id, url_key)
);

CREATE TABLE IF NOT EXISTS stored_pages (
    run_id TEXT NOT NULL,
    page_key TEXT NOT NULL,
    url TEXT NOT NULL,
    template_id TEXT,
    page_class TEXT NOT NULL,
    has_html INTEGER NOT NULL,
    doc TEXT NOT NULL,
    PRIMARY KEY (run_id, page_key)
);

CREATE TABLE IF NOT EXISTS urls (
    run_id TEXT NOT NULL,
    unique_key TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT,
    status TEXT NOT NULL,
    PRIMARY KEY (run_id, unique_key)
);

CREATE TABLE IF NOT EXISTS mapping_versions (
    site TEXT NOT NULL,
    template_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    origin TEXT,
    mapping TEXT NOT NULL,
    PRIMARY KEY (site, template_id, version)
);

CREATE TABLE IF NOT EXISTS mapping_requests (
    mapping_request_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    template_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    doc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    final_url TEXT NOT NULL,
    doc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    problem_key TEXT,
    next_attempt_at TEXT,
    doc TEXT NOT NULL,
    UNIQUE (run_id, problem_key)
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, created_at);

-- links and refetches waiting to be enqueued (written in the follower's transaction,
-- enqueued after it commits, deleted in the transaction that records the enqueue)
CREATE TABLE IF NOT EXISTS link_backlog (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT,
    unique_key TEXT,
    forefront INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS link_backlog_run ON link_backlog(run_id, seq);
-- links that look like target pages are drained first, wherever they are in the backlog
CREATE INDEX IF NOT EXISTS link_backlog_front ON link_backlog(run_id, forefront DESC, seq);

-- files the platform has fetched: the service sent the file's last byte in a download answer
-- (kind result = the result in JSON Lines or CSV); POST /v1/admin/cleanup keeps result files
-- the platform has not fetched unless asked
CREATE TABLE IF NOT EXISTS file_fetches (
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (run_id, kind)
);
"""

# columns added after the first release of the schema: (table, column, definition)
MIGRATIONS = [
    ("unextracted", "neutral", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "parent_run_id", "TEXT"),  # a re-collect run's parent (README 4.10)
]


class TransactionSpansAwait(RuntimeError):
    """A second asyncio task tried to use the connection while a transaction was open."""


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self._owner = None
        with self._lock:
            self._conn.executescript(SCHEMA)
            for table, column, definition in MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self._conn.execute("CREATE INDEX IF NOT EXISTS runs_parent ON runs(parent_run_id)")
            # earlier versions kept result fetches in their own table
            if self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_fetches'").fetchone():
                self._conn.execute("INSERT OR IGNORE INTO file_fetches(run_id, kind, fetched_at) SELECT run_id, 'result', fetched_at FROM result_fetches")
                self._conn.execute("DROP TABLE result_fetches")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _check_owner(self) -> None:
        if self._depth > 0 and self._owner is not None and _current_task() is not self._owner:
            raise TransactionSpansAwait("a transaction of another task is open (an await inside db.tx())")

    @contextmanager
    def tx(self) -> Iterator["Database"]:
        with self._lock:
            self._check_owner()
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
                self._owner = _current_task()
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    self._owner = None
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._owner = None
                    self._conn.execute("COMMIT")

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self._lock:
            self._check_owner()
            cur = self._conn.execute(sql, params)
            return cur.rowcount

    def executemany(self, sql: str, rows: list) -> None:
        with self._lock:
            self._check_owner()
            self._conn.executemany(sql, rows)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            self._check_owner()
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            self._check_owner()
            return self._conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)
