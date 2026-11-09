"""SQLite state: run history, run locks, seen keys (idempotency) and a small key/value store per job.

One file (``state.path`` in the config, default ``state/automation.db``). Safe to share between the scheduler's
threads and between processes (cron, systemd, a manual ``automation run``): writes are short transactions and
SQLite's own locking serialises them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,          -- running | success | failed | abandoned
    summary     TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS runs_job_started ON runs (job, started_at);
CREATE TABLE IF NOT EXISTS seen (
    job        TEXT NOT NULL,
    key        TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (job, key)
);
CREATE TABLE IF NOT EXISTS kv (
    job        TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,           -- JSON
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job, key)
);
CREATE TABLE IF NOT EXISTS locks (
    job         TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,          -- the run id holding the lock
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    job: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    summary: str | None
    error: str | None

    @property
    def seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True)
class LockInfo:
    job: str
    owner: str
    acquired_at: datetime
    expires_at: datetime


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.executescript(SCHEMA)

    # -- plumbing ---------------------------------------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def check_writable(self) -> None:
        """Raise sqlite3.Error or OSError when the database cannot be written (used by ``automation doctor``)."""
        with self._tx() as db:
            db.execute("CREATE TEMP TABLE IF NOT EXISTS _doctor (x)")
            db.execute("INSERT INTO _doctor VALUES (1)")
            db.execute("DROP TABLE _doctor")

    # -- runs -------------------------------------------------------------------------------------------------

    def start_run(self, job: str, run_id: str, started_at: datetime | None = None) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO runs (run_id, job, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, job, _ts(started_at or utcnow())),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        summary: str | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE runs SET status = ?, summary = ?, error = ?, finished_at = ? WHERE run_id = ?",
                (status, summary, error, _ts(finished_at or utcnow()), run_id),
            )

    @staticmethod
    def _run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            job=row["job"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=_parse(row["finished_at"]),
            status=row["status"],
            summary=row["summary"],
            error=row["error"],
        )

    def runs(self, job: str | None = None, limit: int = 20) -> list[RunRecord]:
        if job:
            rows = self._query(
                "SELECT * FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT ?", (job, limit)
            )
        else:
            rows = self._query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [self._run(r) for r in rows]

    def last_run(self, job: str, status: str | None = None) -> RunRecord | None:
        if status:
            rows = self._query(
                "SELECT * FROM runs WHERE job = ? AND status = ? ORDER BY started_at DESC LIMIT 1",
                (job, status),
            )
        else:
            rows = self._query("SELECT * FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT 1", (job,))
        return self._run(rows[0]) if rows else None

    # -- locks ------------------------------------------------------------------------------------------------

    def acquire_lock(self, job: str, owner: str, ttl: timedelta, now: datetime | None = None) -> bool:
        """Take the job's run lock unless another run holds it and it has not expired.

        An expired lock means its run died without cleaning up (killed, machine rebooted); its run record is
        marked ``abandoned`` and the lock passes to ``owner``.
        """
        now = now or utcnow()
        with self._tx() as db:
            row = db.execute("SELECT owner, expires_at FROM locks WHERE job = ?", (job,)).fetchone()
            if row is not None:
                if datetime.fromisoformat(row["expires_at"]) > now:
                    return False
                db.execute(
                    "UPDATE runs SET status = 'abandoned', finished_at = ?, error = ? "
                    "WHERE run_id = ? AND status = 'running'",
                    (_ts(now), "lock expired before the run finished", row["owner"]),
                )
            db.execute(
                "INSERT INTO locks (job, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (job) DO UPDATE SET owner = excluded.owner, acquired_at = excluded.acquired_at, "
                "expires_at = excluded.expires_at",
                (job, owner, _ts(now), _ts(now + ttl)),
            )
        return True

    def release_lock(self, job: str, owner: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM locks WHERE job = ? AND owner = ?", (job, owner))

    def lock_info(self, job: str) -> LockInfo | None:
        rows = self._query("SELECT * FROM locks WHERE job = ?", (job,))
        if not rows:
            return None
        r = rows[0]
        return LockInfo(
            r["job"],
            r["owner"],
            datetime.fromisoformat(r["acquired_at"]),
            datetime.fromisoformat(r["expires_at"]),
        )

    # -- seen keys --------------------------------------------------------------------------------------------

    def seen_keys(self, job: str, keys: Iterable[str]) -> set[str]:
        wanted = list(dict.fromkeys(keys))
        found: set[str] = set()
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._query(f"SELECT key FROM seen WHERE job = ? AND key IN ({marks})", [job, *chunk])  # noqa: S608
            found.update(r["key"] for r in rows)
        return found

    def mark_seen(self, job: str, keys: Iterable[str], now: datetime | None = None) -> int:
        stamp = _ts(now or utcnow())
        rows = [(job, key, stamp) for key in dict.fromkeys(keys)]
        with self._tx() as db:
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO seen (job, key, first_seen) VALUES (?, ?, ?)", rows)
            return db.total_changes - before

    def count_seen(self, job: str) -> int:
        return int(self._query("SELECT COUNT(*) AS n FROM seen WHERE job = ?", (job,))[0]["n"])

    # -- key/value --------------------------------------------------------------------------------------------

    def get_value(self, job: str, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM kv WHERE job = ? AND key = ?", (job, key))
        return json.loads(rows[0]["value"]) if rows else default

    def set_value(self, job: str, key: str, value: Any, now: datetime | None = None) -> None:
        encoded = json.dumps(value, default=str, sort_keys=True)
        with self._tx() as db:
            db.execute(
                "INSERT INTO kv (job, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (job, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (job, key, encoded, _ts(now or utcnow())),
            )

    def delete_value(self, job: str, key: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM kv WHERE job = ? AND key = ?", (job, key))

    def values(self, job: str) -> dict[str, Any]:
        rows = self._query("SELECT key, value FROM kv WHERE job = ? ORDER BY key", (job,))
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # -- admin ------------------------------------------------------------------------------------------------

    def job_stats(self, job: str) -> dict[str, Any]:
        runs = self._query("SELECT COUNT(*) AS n FROM runs WHERE job = ?", (job,))[0]["n"]
        values = self._query("SELECT COUNT(*) AS n FROM kv WHERE job = ?", (job,))[0]["n"]
        last = self.last_run(job, status="success")
        return {
            "job": job,
            "runs": int(runs),
            "last_success": last.started_at if last else None,
            "seen": self.count_seen(job),
            "values": int(values),
        }

    def jobs(self) -> list[str]:
        rows = self._query(
            "SELECT job FROM runs UNION SELECT job FROM seen UNION SELECT job FROM kv ORDER BY job"
        )
        return [r["job"] for r in rows]

    def clear(self, job: str | None = None, *, history: bool = False) -> dict[str, int]:
        """Forget seen keys and stored values (and, with ``history=True``, run history) for one job or all."""
        tables = ["seen", "kv"] + (["runs", "locks"] if history else [])
        counts: dict[str, int] = {}
        with self._tx() as db:
            for table in tables:
                if job:
                    cur = db.execute(f"DELETE FROM {table} WHERE job = ?", (job,))  # noqa: S608
                else:
                    cur = db.execute(f"DELETE FROM {table}")  # noqa: S608
                counts[table] = cur.rowcount
        return counts


@dataclass
class JobState:
    """The state a job sees as ``ctx.state``: scoped to one job name.

    In a dry run nothing is written. Writes go to an in-memory overlay (so the rest of the run sees them) and are
    listed in ``planned`` for the CLI to print.
    """

    store: StateStore
    job: str
    dry_run: bool = False
    now: datetime = field(default_factory=utcnow)
    planned: list[str] = field(default_factory=list)
    _seen_overlay: set[str] = field(default_factory=set)
    _kv_overlay: dict[str, Any] = field(default_factory=dict)

    def seen(self, key: str) -> bool:
        return key in self._seen_overlay or bool(self.store.seen_keys(self.job, [key]))

    def unseen(self, keys: Iterable[str]) -> list[str]:
        """The keys not seen before, in their original order, without duplicates."""
        ordered = list(dict.fromkeys(keys))
        known = self.store.seen_keys(self.job, ordered) | self._seen_overlay
        return [k for k in ordered if k not in known]

    def mark_seen(self, keys: Iterable[str]) -> int:
        keys = list(dict.fromkeys(keys))
        if self.dry_run:
            new = [k for k in keys if k not in self._seen_overlay]
            self._seen_overlay.update(new)
            if new:
                self.planned.append(f"mark {len(new)} key(s) as seen: {_preview(new)}")
            return len(new)
        return self.store.mark_seen(self.job, keys, self.now)

    def count_seen(self) -> int:
        return self.store.count_seen(self.job) + len(self._seen_overlay)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._kv_overlay:
            return self._kv_overlay[key]
        return self.store.get_value(self.job, key, default)

    def set(self, key: str, value: Any) -> None:
        if self.dry_run:
            self._kv_overlay[key] = value
            self.planned.append(f"set {key} = {json.dumps(value, default=str, sort_keys=True)}")
            return
        self.store.set_value(self.job, key, value, self.now)

    def last_success(self) -> RunRecord | None:
        return self.store.last_run(self.job, status="success")


def _preview(keys: list[str], limit: int = 3) -> str:
    shown = ", ".join(keys[:limit])
    return shown + (f" (+{len(keys) - limit} more)" if len(keys) > limit else "")
