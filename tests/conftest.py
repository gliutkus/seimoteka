"""CI requires a working database so integration tests cannot silently skip."""

import os

import psycopg
import pytest

from ingest.config import DATABASE_URL


def pytest_sessionstart(session):
    if os.getenv("REQUIRE_DATABASE") == "1":
        try:
            with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
                conn.execute("SELECT 1 FROM chunks LIMIT 0")
        except psycopg.Error as exc:
            raise pytest.UsageError("Required test database is unavailable") from exc
