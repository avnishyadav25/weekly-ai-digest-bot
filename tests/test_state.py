from __future__ import annotations

import threading
from datetime import timedelta

from automation.state import JobState
from tests.conftest import NOW


def test_run_history(open_store):
    store = open_store("s.db")
    store.start_run("a", "r1", NOW)
    store.finish_run("r1", "failed", error="boom", finished_at=NOW + timedelta(seconds=2))
    store.start_run("a", "r2", NOW + timedelta(minutes=5))
    store.finish_run("r2", "success", summary="ok", finished_at=NOW + timedelta(minutes=5, seconds=1))
    store.start_run("b", "r3", NOW + timedelta(minutes=6))
    runs = store.runs("a")
    assert [r.run_id for r in runs] == ["r2", "r1"]
    assert runs[1].error == "boom"
    assert runs[1].seconds == 2
    assert store.last_run("a").run_id == "r2"
    assert store.last_run("a", status="failed").run_id == "r1"
    assert store.last_run("b").status == "running"
    assert [r.run_id for r in store.runs(limit=1)] == ["r3"]


def test_seen_keys_survive_a_restart(open_store):
    store = open_store("s.db")
    assert store.mark_seen("feed", ["a", "b", "a"]) == 2
    assert store.mark_seen("feed", ["b", "c"]) == 1
    store.close()
    store = open_store("s.db")
    assert store.seen_keys("feed", ["a", "c", "z"]) == {"a", "c"}
    assert store.seen_keys("other", ["a"]) == set()
    assert store.count_seen("feed") == 3


def test_many_seen_keys_are_checked_in_chunks(open_store):
    store = open_store("s.db")
    keys = [f"k{i}" for i in range(1200)]
    store.mark_seen("j", keys[:700])
    assert len(store.seen_keys("j", keys)) == 700


def test_key_value_round_trip(open_store):
    store = open_store("s.db")
    assert store.get_value("j", "missing", default=5) == 5
    store.set_value("j", "status", {"state": "up", "n": 2})
    store.set_value("j", "status", {"state": "down", "n": 3})
    assert store.get_value("j", "status") == {"state": "down", "n": 3}
    assert store.values("j") == {"status": {"state": "down", "n": 3}}
    store.delete_value("j", "status")
    assert store.values("j") == {}


def test_lock_blocks_a_second_run_until_released_or_expired(open_store):
    store = open_store("s.db")
    ttl = timedelta(minutes=10)
    store.start_run("j", "first", NOW)
    assert store.acquire_lock("j", "first", ttl, NOW)
    assert not store.acquire_lock("j", "second", ttl, NOW + timedelta(minutes=1))
    assert store.lock_info("j").owner == "first"
    # the first run died without releasing: after the TTL the lock passes on and the old run is marked
    assert store.acquire_lock("j", "third", ttl, NOW + timedelta(minutes=11))
    assert store.last_run("j").status == "abandoned"
    store.release_lock("j", "third")
    assert store.lock_info("j") is None
    assert store.acquire_lock("j", "fourth", ttl, NOW + timedelta(minutes=12))


def test_lock_is_shared_between_processes_through_the_file(open_store):
    a = open_store("s.db")
    b = open_store("s.db")
    assert a.acquire_lock("j", "run-a", timedelta(minutes=5), NOW)
    assert not b.acquire_lock("j", "run-b", timedelta(minutes=5), NOW)


def test_job_state_writes_through(open_store):
    store = open_store("s.db")
    state = JobState(store, "feed", now=NOW)
    assert state.unseen(["x", "y", "x"]) == ["x", "y"]
    state.mark_seen(["x"])
    state.set("cursor", 10)
    assert state.unseen(["x", "y"]) == ["y"]
    assert state.seen("x")
    assert store.get_value("feed", "cursor") == 10
    assert state.planned == []


def test_dry_run_job_state_writes_nothing(open_store):
    store = open_store("s.db")
    store.mark_seen("feed", ["old"])
    state = JobState(store, "feed", dry_run=True, now=NOW)
    state.mark_seen(["new1", "new2"])
    state.set("cursor", 3)
    # the run sees its own writes...
    assert state.unseen(["old", "new1", "new3"]) == ["new3"]
    assert state.get("cursor") == 3
    assert state.count_seen() == 3
    # ...but nothing reached the database
    assert store.count_seen("feed") == 1
    assert store.get_value("feed", "cursor") is None
    assert state.planned == ["mark 2 key(s) as seen: new1, new2", "set cursor = 3"]


def test_clear_one_job_or_everything(open_store):
    store = open_store("s.db")
    for job in ("a", "b"):
        store.mark_seen(job, ["k"])
        store.set_value(job, "v", 1)
        store.start_run(job, f"run-{job}", NOW)
    assert store.clear("a") == {"seen": 1, "kv": 1}
    assert store.count_seen("a") == 0
    assert store.count_seen("b") == 1
    assert len(store.runs("a")) == 1  # history kept by default
    counts = store.clear(history=True)
    assert counts["runs"] == 2
    assert store.jobs() == []


def test_concurrent_writers(open_store):
    store = open_store("s.db")

    def work(n: int) -> None:
        for i in range(50):
            store.mark_seen("j", [f"{n}-{i}"])
            store.set_value("j", f"last-{n}", i)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.count_seen("j") == 200
    assert store.get_value("j", "last-3") == 49


def test_check_writable_and_stats(open_store):
    store = open_store("nested/dir/s.db")
    store.check_writable()
    store.start_run("j", "r1", NOW)
    store.finish_run("r1", "success", finished_at=NOW)
    stats = store.job_stats("j")
    assert stats["runs"] == 1
    assert stats["last_success"] == NOW
