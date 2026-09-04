"""Database models and persistence helpers for PostgreSQL storage."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import (
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    create_engine,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.types import UserDefinedType

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
    exists so the table is created and exported alongside the other ORM tables.
    """

    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# =====================================================================================
# HYBRID SEARCH SCHEMA
# =====================================================================================
# The search API only ever reads these tables: they are written by index_datasets.py in
# metadata-warehouse, which also provisions the extensions they need. They therefore live in
# their own MetaData so `init_postgres_storage()` never tries to create them (that would fail
# without the vector/vectorscale/pg_textsearch extensions and would take ownership of tables
# this service does not own). They are defined here so this file stays the single source of
# truth for the schema, and `scripts/export_db_schema.py` renders them for metadata-warehouse.


class SearchBase(DeclarativeBase):
    """Base for the read-only hybrid search tables, kept out of `Base.metadata` on purpose."""

    # Spell out the constraint names instead of letting the DDL stay anonymous. The patterns are
    # the ones PostgreSQL derives by itself, so this changes no existing database: it only makes
    # the generated SQL state the names that index_datasets.py and any future migration rely on.
    metadata = MetaData(
        naming_convention={
            "pk": "%(table_name)s_pkey",
            "uq": "%(table_name)s_%(column_0_name)s_key",
            "fk": "%(table_name)s_%(column_0_name)s_fkey",
            "ck": "%(table_name)s_%(constraint_name)s",
            "ix": "%(table_name)s_%(column_0_name)s_idx",
        }
    )


class Vector(UserDefinedType):
    """Minimal `vector(N)` column type, enough to render DDL.

    The search queries are raw SQL and pass vectors as text literals cast with `::vector`, so no
    bind/result processing is needed here and the `pgvector` package stays out of the runtime
    dependencies. Add it if the ORM ever has to read or write embeddings directly.
    """

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **_kw: Any) -> str:
        return f"VECTOR({self.dim})"


# Named embeddings stored per dataset. Each value needs its own diskann label number below.
DATASET_EMBEDDING_FIELDS = ("title", "description", "keywords")
DATASET_EMBEDDING_FIELD_ENUM = "dataset_embedding_field"
# Dimensions of the indexed vectors. Must match EMBEDDING_MODEL.dims in index_datasets.py
# (nomic-embed-text-v2-moe, 768). Switching model means regenerating every vector
EMBEDDING_DIMS = 768

# `labels` is generated from `field` so the two can never drift. pgvectorscale filters on
# smallint[] labels with the && operator.
_LABELS_EXPR = (
    "ARRAY[CASE field "
    + " ".join(f"WHEN '{name}' THEN {i}" for i, name in enumerate(DATASET_EMBEDDING_FIELDS, start=1))
    + " END]::SMALLINT[]"
)


class Dataset(SearchBase):
    """Denormalized, query-ready projection of datasetdb.records for the search API.

    One row per dataset, built from records.datacite_json + the repository it came from.
    """

    __tablename__ = "datasets"
    __table_args__ = (
        # DOI must be the bare id, not a resolver URL
        CheckConstraint("doi IS NULL OR doi NOT LIKE 'http%'", name="doi_not_url_check"),
        {"comment": "Query-ready datasets for hybrid search, projected from datasetdb.records"},
    )

    # Identity: the landing page URL of the dataset. Records that only have a DOI get
    # https://doi.org/<doi>, so there is always exactly one canonical URL per dataset.
    url: Mapped[str] = mapped_column(
        String(2048),
        primary_key=True,
        comment="Landing page URL, or https://doi.org/<doi> for records that only have a DOI",
    )
    doi: Mapped[str | None] = mapped_column(String(255), comment="Bare DOI without resolver prefix, nullable")

    # Descriptive fields, flattened from the DataCite JSON to the single value the search API needs
    title: Mapped[str] = mapped_column(Text, nullable=False)
    alt_titles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    creators: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    creator_identifiers: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'"),
        comment="ORCID/ROR/etc identifiers of the creators, same order as creators when known",
    )
    alternate_identifiers: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'"),
        comment="Other identifiers of the dataset itself (alternateIdentifiers), for lookups and dedup",
    )

    # Filterable/facetable fields
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    publication_year: Mapped[int | None] = mapped_column(SmallInteger)
    publication_date: Mapped[datetime | None] = mapped_column(Date)
    languages: Mapped[list[str]] = mapped_column(ARRAY(String(8)), nullable=False, server_default=text("'{}'"))
    formats: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    license: Mapped[str | None] = mapped_column(Text)
    license_url: Mapped[str | None] = mapped_column(Text)
    repository_code: Mapped[str] = mapped_column(String(50), nullable=False)
    repository_name: Mapped[str] = mapped_column(String(255), nullable=False)

    search_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Text fields concatenated for BM25 (pg_textsearch)",
    )

    # Provenance, used to make indexing resumable and incremental
    source_record_id: Mapped[str] = mapped_column(
        String(510),
        nullable=False,
        unique=True,
        comment="datasetdb.records.id this row was built from",
    )
    source_datestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="datasetdb.records.updated_at at indexing time, to detect outdated rows",
    )
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RecordEmbedding(SearchBase):
    """Named embeddings, one row per (record, field, chunk).

    Fields that fit in a single vector (title, keywords) get a single chunk 0, long fields
    (description) are split into several chunks. Named for records rather than datasets because
    the same named-embedding layout is meant to carry tools and files too; the foreign key on
    `record_url` is the one piece that still ties a row to `datasets` (see the note on it).
    """

    __tablename__ = "record_embeddings"
    __table_args__ = ({"comment": "Named embeddings per record, several chunks per field when the text is too long"},)

    # TODO: Adding tools and files means dropping this foreign key (a row
    # cannot reference several parent tables) and adding a record type discriminator, or pointing
    # it at a shared `records` table. Renaming the table does not by itself make that possible.
    record_url: Mapped[str] = mapped_column(
        String(2048),
        ForeignKey("datasets.url", ondelete="CASCADE"),
        primary_key=True,
    )
    field: Mapped[str] = mapped_column(
        ENUM(*DATASET_EMBEDDING_FIELDS, name=DATASET_EMBEDDING_FIELD_ENUM, create_type=False),
        primary_key=True,
        comment="Name of the embedded field, mapped to a diskann label",
    )
    chunk_index: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("0"))
    # TODO: the description chunks concatenate every DataCite description (abstract, methods,
    # technical info, ...) and their descriptionType is not kept, so a match cannot be attributed
    # to a specific kind of description. Add a `chunk_type` column here if the search API needs it.
    chunk_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Exact text sent to the embedding model",
    )
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIMS), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    labels: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger),
        Computed(_LABELS_EXPR, persisted=True),
        nullable=False,
        comment="Generated from field, for pgvectorscale filtered search: WHERE labels && ARRAY[1]",
    )


# Lexical search (pg_textsearch, BM25). Query with:
#   ORDER BY search_text <@> 'user query' LIMIT 10
# Scores are negative, so lower is a better match (ascending index scan).
Index(
    "datasets_bm25_search_text_idx",
    Dataset.__table__.c.search_text,
    postgresql_using="bm25",
    postgresql_with={"text_config": "'english'"},
)
# Separate index on the title alone, to let the search API boost title matches
Index(
    "datasets_bm25_title_idx",
    Dataset.__table__.c.title,
    postgresql_using="bm25",
    postgresql_with={"text_config": "'english'"},
)

# Vector search (pgvectorscale, StreamingDiskANN). Single index over all named embeddings;
# `labels` restricts the search to a given field inside the index (filtered DiskANN) instead of
# filtering after the scan:
#   WHERE labels && ARRAY[1]::SMALLINT[] ORDER BY embedding <=> $query LIMIT 10
# Cosine distance, to match the normalized embeddings returned by the Cesnet API.
# IMPORTANT for bulk loads: build this index AFTER the rows are inserted
Index(
    "record_embeddings_diskann_idx",
    RecordEmbedding.__table__.c.embedding,
    RecordEmbedding.__table__.c.labels,
    postgresql_using="diskann",
    postgresql_ops={"embedding": "vector_cosine_ops"},
)

# Filters, facets and lookups
Index("datasets_doi_idx", Dataset.__table__.c.doi, postgresql_where=Dataset.__table__.c.doi.isnot(None))
Index("datasets_repository_code_idx", Dataset.__table__.c.repository_code)
Index("datasets_resource_type_idx", Dataset.__table__.c.resource_type)
Index("datasets_publication_year_idx", Dataset.__table__.c.publication_year)
Index("datasets_keywords_idx", Dataset.__table__.c.keywords, postgresql_using="gin")
Index("datasets_creators_idx", Dataset.__table__.c.creators, postgresql_using="gin")
Index("datasets_alternate_identifiers_idx", Dataset.__table__.c.alternate_identifiers, postgresql_using="gin")

# Incremental indexing: find rows that are outdated compared to datasetdb
Index("datasets_source_updated_at_idx", Dataset.__table__.c.source_updated_at)

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
