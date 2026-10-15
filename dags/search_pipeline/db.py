"""Engines of the source (datasetdb, read-only) and target (appdb) databases."""

from typing import Any

import psycopg2
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import NullPool

from search_pipeline.config import PipelineSettings


def _engine(dsn: str, options: str = "") -> Engine:
    """Engine on a libpq DSN. A task is a short lived process, no point in pooling."""

    def connect() -> Any:
        return psycopg2.connect(dsn, options=options or None, application_name="search_pipeline")

    return create_engine("postgresql+psycopg2://", creator=connect, poolclass=NullPool)


def datasetdb_engine(settings: PipelineSettings) -> Engine:
    """datasetdb is owned by the warehouse, the server rejects any write from this session."""
    return _engine(
        settings.datasetdb_dsn or settings.dsn(settings.datasetdb_name), "-c default_transaction_read_only=on"
    )


def appdb_engine(settings: PipelineSettings) -> Engine:
    return _engine(settings.appdb_dsn or settings.dsn(settings.appdb_name))
