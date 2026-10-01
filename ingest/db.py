"""Apply db/schema.sql to the configured database. Every statement is idempotent,
so this both creates a fresh database and upgrades one made from an older schema."""

from __future__ import annotations

import psycopg

from .config import DATABASE_URL, ROOT

SCHEMA = ROOT / "db" / "schema.sql"


def ensure_schema() -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text("utf-8"))
