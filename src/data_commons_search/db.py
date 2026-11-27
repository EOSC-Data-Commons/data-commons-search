"""Persistence helpers for PostgreSQL storage, on the models of db_models.py."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import TypeAdapter
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from data_commons_search import __version__
from data_commons_search.config import settings
from data_commons_search.db_models import Conversation, Message, StatsSnapshot, User
from data_commons_search.models import (
    ConversationDetail,
    ConversationItem,
    ConversationSummary,
    DbStats,
    MessageItem,
    UserInfo,
)
from data_commons_search.utils import logger

# The API connects as the search role, which cannot create tables
engine = create_engine(settings.appdb_search_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _get_or_create_user(db: Session, user: UserInfo) -> User:
    """Get or create a `User` row for the given `UserInfo`."""
    db_user = db.execute(select(User).where(User.sub == user.sub)).scalar_one_or_none()
    if db_user is None:
        db_user = User(sub=user.sub, email=user.email, name=user.name, username=user.preferred_username)
        db.add(db_user)
        db.flush()
        return db_user

    changed = False
    if user.email and db_user.email != user.email:
        db_user.email = user.email
        changed = True
    if user.name and db_user.name != user.name:
        db_user.name = user.name
        changed = True
    if user.preferred_username and db_user.username != user.preferred_username:
        db_user.username = user.preferred_username
        changed = True
    if changed:
        db.flush()
    return db_user


def ensure_user_exists(user: UserInfo) -> None:
    """Ensure a user row exists for the authenticated OIDC subject."""
    try:
        with SessionLocal.begin() as db:
            _get_or_create_user(db, user)
    except Exception as exc:
        logger.exception("Failed to ensure user in database: %s", exc)


def _get_or_create_conversation(
    db: Session, user_sub: str, thread_id: str, items: Sequence[ConversationItem]
) -> Conversation:
    """Get or create a `Conversation` row for the given user and thread ID."""
    conversation = db.execute(
        select(Conversation).where(Conversation.user_id == user_sub, Conversation.thread_id == thread_id)
    ).scalar_one_or_none()
    if conversation is not None:
        return conversation

    conversation = Conversation(
        user_id=user_sub,
        thread_id=thread_id,
        label=make_conversation_label(items) or "New conversation",
    )
    db.add(conversation)
    db.flush()
    return conversation


def store_messages(
    *,
    user: UserInfo,
    thread_id: str,
    items: Sequence[ConversationItem],
) -> None:
    """Persist conversation items, creating user and conversation rows if needed."""
    if not items:
        return
    # TODO: make sure we don't re-add messages already in the DB when appending to an existing conversation
    try:
        with SessionLocal.begin() as db:
            db_user = _get_or_create_user(db, user)
            _get_or_create_conversation(db, user_sub=db_user.sub, thread_id=thread_id, items=items)

            for item in items:
                db.add(
                    Message(
                        user_id=db_user.sub, thread_id=thread_id, type=item.type, content=item.model_dump(mode="json")
                    )
                )
    except Exception as exc:
        logger.exception("Failed to store messages in database: %s", exc)


_LABEL_MAX_LEN = 100


def make_conversation_label(items: Sequence[ConversationItem]) -> str | None:
    """Derive a short conversation label from the first user message item."""
    first_user = next(
        (item for item in items if isinstance(item, MessageItem) and item.role == "user"),
        None,
    )
    if first_user is None or not first_user.content:
        return None
    text = " ".join(part.text for part in first_user.content if part.type == "text").strip()
    if not text:
        return None
    return (text[: _LABEL_MAX_LEN - 1] + "…") if len(text) > _LABEL_MAX_LEN else text


def get_conversations(user_sub: str) -> list[ConversationSummary]:
    """Return a summary list of all conversations for `user_sub`, newest first."""
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(Conversation).where(Conversation.user_id == user_sub).order_by(Conversation.created_at.desc())
            )
            .scalars()
            .all()
        )
        return [
            ConversationSummary(thread_id=c.thread_id, label=c.label, created_at=c.created_at, updated_at=c.updated_at)
            for c in rows
        ]


_item_adapter: TypeAdapter[ConversationItem] = TypeAdapter(ConversationItem)


def get_conversation(user_sub: str, thread_id: str) -> ConversationDetail | None:
    """Return a full `ConversationDetail` for a `thread_id`, or `None` if not found."""
    with SessionLocal() as db:
        conversation = db.execute(
            select(Conversation).where(
                Conversation.user_id == user_sub,
                Conversation.thread_id == thread_id,
            )
        ).scalar_one_or_none()
        if conversation is None:
            return None

        msg_rows = (
            db.execute(
                select(Message).where(Message.user_id == user_sub, Message.thread_id == thread_id).order_by(Message.id)
            )
            .scalars()
            .all()
        )
        return ConversationDetail(
            thread_id=conversation.thread_id,
            label=conversation.label,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            items=[
                _item_adapter.validate_python(
                    {
                        # "type": m.type,
                        **m.content,
                    }
                )
                for m in msg_rows
            ],
        )


def delete_conversations(user_sub: str, thread_ids: list[str]) -> None:
    """Delete conversations (and their messages) owned by `user_sub`.

    If `thread_ids` is empty, all conversations for the user are deleted.
    """
    with SessionLocal() as db:
        stmt = delete(Conversation).where(Conversation.user_id == user_sub)
        if thread_ids:
            stmt = stmt.where(Conversation.thread_id.in_(thread_ids))
        db.execute(stmt)
        db.commit()


def store_stats_snapshot(stats: DbStats) -> None:
    """Append a `DbStats` snapshot to the history table in appdb, as the ingest role (the search role only reads it)."""
    ingest_engine = create_engine(settings.appdb_ingest_url)
    try:
        with Session(ingest_engine) as db, db.begin():
            db.add(StatsSnapshot(generated_at=stats.generated_at, stats=stats.model_dump(mode="json")))
    finally:
        ingest_engine.dispose()


def get_latest_stats() -> DbStats | None:
    """Return the most recent stats snapshot, or `None` if the history table is empty."""
    with SessionLocal() as db:
        row = db.execute(
            select(StatsSnapshot).order_by(StatsSnapshot.generated_at.desc()).limit(1)
        ).scalar_one_or_none()
    if row is None:
        logger.warning("No stats snapshot found, run scripts/compute_stats.py to generate one")
        return None
    stats = DbStats.model_validate(row.stats)
    # Stamped at response time: the snapshot was written by whichever version computed it
    stats.api_version = __version__
    return stats
