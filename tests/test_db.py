import asyncio
import sqlite3

import pytest

from shopsim.common.db import Database


class FlakyConnection:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def execute(self, sql, params):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("database is locked")
        return "result"


@pytest.mark.parametrize("message, expected", [("database is locked", True), ("no such table", False)])
def test_is_locked_only_matches_transient_lock(message, expected):
    error = sqlite3.OperationalError(message)
    assert Database._is_locked(error) is expected


def test_execute_with_retry_recovers_from_transient_lock():
    database = object.__new__(Database)
    database.conn = FlakyConnection(Database.LOCK_RETRIES)

    result = asyncio.run(database._execute_with_retry("UPDATE products SET stock = stock"))

    assert result == "result"
    assert database.conn.calls == Database.LOCK_RETRIES + 1


def test_execute_with_retry_preserves_non_lock_errors():
    database = object.__new__(Database)
    database.conn = FlakyConnection(0)

    def fail(sql, params):
        raise sqlite3.OperationalError("no such table: products")

    database.conn.execute = fail
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        asyncio.run(database._execute_with_retry("UPDATE products SET stock = stock"))
    assert database.conn.calls == 0
