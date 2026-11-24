"""sparx.db: a failed write must not keep the write lock, and an INSERT that changed nothing returns 0."""
import sqlite3
import threading
import time

import pytest

from sparx import config, db

T = "CREATE TABLE IF NOT EXISTS dbt (id INTEGER PRIMARY KEY, k TEXT UNIQUE)"


def other_process_writes(key: str):
    """What another Sparx process is: a second connection to the same file, which must be able to write."""
    other = sqlite3.connect(config.path("sparx.db"), timeout=0.5)
    try:
        other.execute("INSERT INTO dbt (k) VALUES (?)", (key,))
        other.commit()
    finally:
        other.close()


def test_failed_x_releases_the_write_lock():
    db.schema(T)
    db.x("INSERT INTO dbt (k) VALUES (?)", ("a",))
    with pytest.raises(sqlite3.IntegrityError):
        db.x("INSERT INTO dbt (k) VALUES (?)", ("a",))
    assert not db.connect().in_transaction
    other_process_writes("b")
    assert [r["k"] for r in db.q("SELECT k FROM dbt ORDER BY id")] == ["a", "b"]


def test_failed_many_rolls_back_and_releases_the_write_lock():
    db.schema(T)
    with pytest.raises(sqlite3.IntegrityError):
        db.many("INSERT INTO dbt (k) VALUES (?)", [("a",), ("b",), ("a",)])
    assert not db.connect().in_transaction
    assert db.q("SELECT k FROM dbt") == []
    other_process_writes("c")
    assert db.many("INSERT INTO dbt (k) VALUES (?)", [("a",), ("b",)]) == 2


def test_failed_commit_releases_the_write_lock():
    db.schema(T)
    db.schema("CREATE TABLE IF NOT EXISTS dbt_child (id INTEGER PRIMARY KEY, "
              "p INTEGER REFERENCES dbt(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError):      # a deferred foreign key is only checked at commit
        db.x("INSERT INTO dbt_child (p) VALUES (?)", (99,))
    assert not db.connect().in_transaction
    assert db.q("SELECT * FROM dbt_child") == []
    other_process_writes("a")


def test_q_does_not_leave_a_transaction_open():
    db.schema(T)
    db.q("INSERT INTO dbt (k) VALUES (?) RETURNING id", ("a",))
    assert not db.connect().in_transaction
    other_process_writes("b")


def test_insert_that_changed_no_row_returns_zero():
    db.schema(T)
    assert db.x("INSERT INTO dbt (k) VALUES (?)", ("a",)) == 1
    assert db.x("INSERT INTO dbt (k) VALUES (?)", ("b",)) == 2
    assert db.x("INSERT INTO dbt (k) VALUES (?)", ("c",)) == 3
    assert db.x("INSERT OR IGNORE INTO dbt (k) VALUES (?)", ("a",)) == 0
    assert db.x("INSERT INTO dbt (k) VALUES (?) ON CONFLICT(k) DO NOTHING", ("b",)) == 0
    assert db.x("INSERT OR IGNORE INTO dbt (k) VALUES (?)", ("d",)) == 4
    assert db.x("  insert into dbt (k) values (?)", ("e",)) == 5
    assert db.x("UPDATE dbt SET k = k || '!' WHERE id > ?", (3,)) == 2
    assert db.x("DELETE FROM dbt WHERE id = ?", (99,)) == 0


def test_twenty_threads_lose_no_insert():
    db.schema(T)
    ids, errors = [], []

    def work(n: int):
        try:
            for i in range(50):
                ids.append(db.x("INSERT INTO dbt (k) VALUES (?)", (f"{n}-{i}",)))
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=work, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert db.one("SELECT COUNT(*) AS n FROM dbt")["n"] == 1000
    assert sorted(ids) == list(range(1, 1001))


def test_x_waits_for_another_writer_instead_of_failing():
    db.schema(T)
    db.connect()
    other = sqlite3.connect(config.path("sparx.db"), timeout=0.5, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO dbt (k) VALUES ('theirs')")

    def release():
        time.sleep(1)
        other.commit()
    t = threading.Thread(target=release)
    start = time.monotonic()
    t.start()
    try:
        new = db.x("INSERT INTO dbt (k) VALUES (?)", ("ours",))
    finally:
        t.join()
        other.close()
    assert time.monotonic() - start >= 0.9
    assert new == 2
    assert [r["k"] for r in db.q("SELECT k FROM dbt ORDER BY id")] == ["theirs", "ours"]
