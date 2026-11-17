"""Shared pytest helpers. The DB fixtures live in test_db.py, so the unit tests never touch postgres."""

from sqlalchemy import text

import data_commons_search.db as db_module


def postgres_available() -> bool:
    """Return True when the configured PostgreSQL server is reachable."""
    try:
        with db_module.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
