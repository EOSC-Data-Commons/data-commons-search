"""SQLAlchemy models of the appdb tables of the API: users, their conversations, rate limits and stats.

Only imports SQLAlchemy, like dataset_models.py, so the airflow worker can create these tables too
(create_appdb_tables task of the populate_appdb_dataset DAG). The queries on them are in db.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base ORM model for SQLAlchemy entities."""


class User(Base):
    """Authenticated user persisted from OIDC userinfo."""

    __tablename__ = "users"

    sub: Mapped[str] = mapped_column(String(255), primary_key=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    aup_accepted: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    conversations: Mapped[list[Conversation]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Conversation(Base):
    """A conversation session linked to a single authenticated user."""

    __tablename__ = "conversations"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.sub", ondelete="CASCADE"), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    label: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Additional metadata are directly stored on messages, but we could also have some conversation-level metadata if needed in the future
    # meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=True)

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    """A message persisted for a conversation."""

    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "thread_id"],
            ["conversations.user_id", "conversations.thread_id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    thread_id: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class RateLimit(Base):
    """Per-key request counter for the Postgres-backed rate limiter.

    Rows are read/written via an atomic UPSERT in `rate_limit.py`; this model
    exists so the table is created alongside the other ORM tables.
    """

    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StatsSnapshot(Base):
    """Append-only history of the aggregated datasetdb stats served by `/stats`.

    One row per run of `scripts/compute_stats.py`: the full `DbStats` payload is kept as JSONB so
    its shape can evolve freely, while `generated_at` is a real column (indexed) since every query
    on this table orders or filters by time to plot growth.
    """

    __tablename__ = "stats_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
