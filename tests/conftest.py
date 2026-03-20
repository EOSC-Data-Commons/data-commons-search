"""Shared pytest configuration and fixtures."""

import pytest
from sqlalchemy import text

import data_commons_search.db as db_module
from data_commons_search.db import Base


def postgres_available() -> bool:
    """Return True when the configured PostgreSQL server is reachable."""
    try:
        with db_module.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def create_tables():
    """Create all ORM tables once per test session."""
    Base.metadata.create_all(bind=db_module.engine)
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    """Delete all rows after each test (cascade handles child tables)."""
    yield
    with db_module.SessionLocal.begin() as session:
        session.execute(text("DELETE FROM users"))
