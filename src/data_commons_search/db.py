"""Database models and persistence helpers for PostgreSQL storage."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, create_engine, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from data_commons_search.config import settings
from data_commons_search.models import (
    ConversationDetail,
    ConversationItem,
    ConversationSummary,
    MessageItem,
    UserInfo,
)
from data_commons_search.utils import logger


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


engine = create_engine(settings.postgres_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_postgres_storage() -> None:
    """Create PostgreSQL tables if they do not already exist."""
    Base.metadata.create_all(bind=engine)


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


# def generate_unique_thread_id(user_sub: str) -> str:
#     """Generate a thread ID that does not yet exist for the given user."""
#     while True:
#         thread_id = str(uuid.uuid4())
#         with SessionLocal() as db:
#             exists = db.execute(
#                 select(Conversation).where(Conversation.user_id == user_sub, Conversation.thread_id == thread_id)
#             ).scalar_one_or_none()
#         if exists is None:
#             return thread_id
