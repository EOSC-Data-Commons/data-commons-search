"""SQLAlchemy models of the appdb search tables, and the indexes built on top of them.

Written by the DAGs in `dags/` (search_pipeline), queried by the API in dataset_search.py. Only
imports SQLAlchemy and pgvector, not the app settings, so the airflow worker can import it without the API deps.

`Dataset` is the source of truth for the API: one row per dataset, cleaned up from the DataCite
JSON harvested in datasetdb. Its lists live in their own tables, written by the transform:
`Repository` (`dataset.harvested_from` is the one of the record the dataset was built from), `Agent` (creators, one per ORCID, ROR or name, searched by their `AgentName`) and
`Subject`, linked to the datasets by `DatasetAgent` and `DatasetSubject`, and `DatasetDate`. The link
rows keep what the record wrote (name, identifiers, subject scheme). Only the lists nothing queries
yet stay JSONB arrays (`descriptions`, `rights`). `DatasetRecord` lists the records of each dataset:
the repositories it was found in, with its identifier there.

`DatasetEmbedding` holds one vector per text chunk of each searched field of a dataset (see
dags/search_pipeline/embeddings.py).

The search indexes are declared on the models but marked DEFERRED: `create_all` skips them and
the rebuild_indexes DAG builds them after a bulk load. The VectorChord clusters are computed from
the rows present when the index is built, and BM25 also builds faster in one pass.
"""

import datetime as dt
import os
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Fields that get their own embeddings and their own BM25 index
SEARCH_FIELDS = ("title", "keywords", "description")
TEXT_CONFIG = "english"
# Vector size of the embedding model (EMBED_DIMENSIONS of the DAGs). Read at import: the column type depends on it
EMBED_DIMENSIONS = int(os.environ.get("EMBED_DIMENSIONS", "768"))
# Marks the indexes built by rebuild_indexes instead of `create_all`
DEFERRED = {"deferred": True}

# Comparable form of the name of a person or organization: lowercase, no accents, words sorted, so
# "Ionita, Monica" and "Monica Ionita" get the same key. IMMUTABLE (unaccent with an explicit dictionary
# is) so it can make generated columns, and a query on name_key(:name) is computed once and uses the index
NAME_KEY_DDL = """
CREATE OR REPLACE FUNCTION name_key(name text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    SELECT string_agg(word, ' ' ORDER BY word)
    FROM regexp_split_to_table(lower(public.unaccent('public.unaccent'::regdictionary, name)), '[^[:alnum:]]+') AS word
    WHERE word <> ''
)
"""
# Comparable form of a subject name: lowercase, no accents. Punctuation and word order are kept, they
# tell subjects apart ("C", "C++" and "C#")
SUBJECT_KEY_DDL = """
CREATE OR REPLACE FUNCTION subject_key(name text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN lower(public.unaccent('public.unaccent'::regdictionary, name))
"""
# Functions used by the generated columns, created before the tables
FUNCTIONS_DDL = (NAME_KEY_DDL, SUBJECT_KEY_DDL)


def bm25_index_name(field: str) -> str:
    """BM25 index of a field, `to_bm25query()` takes its name."""
    return f"dataset_{field}_bm25_idx"


# VectorChord vchordrq index of each field: IVF with RaBitQ codes, reranked with the vectors of the table
# (rerank_in_table, else the index holds a copy of them, 15GB instead of 0.7GB for 3.7M vectors). 2000
# lists for 1M-2M rows per field (VectorChord docs, ~10000 above 2M)
VCHORDRQ_OPTIONS = """$$
residual_quantization = true
rerank_in_table = true
[build.internal]
lists = [2000]
spherical_centroids = true
build_threads = 8
$$"""
# Lists scanned per search, stored on the index (`ALTER INDEX ... SET (probes = '...')` changes it). 800
# for recall@100 0.97 (0.84 at 200): the queries fall far from the dense clusters of the short texts
VCHORDRQ_PROBES = 800


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012
        str: Text,
        datetime: DateTime(timezone=True),
        list[str]: ARRAY(Text),
        list[dict[str, Any]]: JSONB,
    }


class Dataset(Base):
    __tablename__ = "dataset"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # https://doi.org/<doi> when the record has a DOI, else its landing page
    url: Mapped[str] = mapped_column(unique=True)
    doi: Mapped[str | None]
    title: Mapped[str]
    # Main description, the one searched and displayed: the texts of the best type and language in
    # `descriptions` (abstract and English first), separated by a blank line
    description: Mapped[str | None]
    # [{"text", "type", "lang"}], all the cleaned descriptions, translations and notes included
    descriptions: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'"))
    # [{"name", "uri"}]
    rights: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'"))
    publication_year: Mapped[int | None] = mapped_column(SmallInteger)
    publication_date: Mapped[date | None]
    languages: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    formats: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    # Repository of the record this dataset was built from. All the repositories it was found in, and the
    # endpoints it was harvested from, are in `dataset_record`
    harvested_from: Mapped[str] = mapped_column(ForeignKey("repository.code"))
    # Names of the subjects, comma separated, the text searched and embedded
    keywords: Mapped[str | None]
    # How many of description, subjects, creators and publication date the record fills: of the records
    # sharing a URL (the same DOI harvested from two repositories), the most complete makes the dataset
    completeness: Mapped[int] = mapped_column(SmallInteger)
    # Hash of the creators, subjects and dates of the record, its link rows are rewritten when it changes
    links_hash: Mapped[str]
    # datasetdb.records row this dataset was built from
    source_record_id: Mapped[str]
    source_updated_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Reset to NULL when an embedded field changes, the queue of the generate_embeddings DAG
    embedded_at: Mapped[datetime | None]

    __table_args__ = (
        Index("dataset_to_embed_idx", "id", postgresql_where=text("embedded_at IS NULL")),
        # Filters. The repository filter reads `dataset_record`, a dataset can be in several
        Index("dataset_publication_date_idx", "publication_date", info=DEFERRED),
        Index("dataset_doi_idx", "doi", info=DEFERRED),
        # Lexical search, one BM25 index per field
        *(
            Index(
                bm25_index_name(field),
                field,
                postgresql_using="bm25",
                postgresql_with={"text_config": f"'{TEXT_CONFIG}'"},
                info=DEFERRED,
            )
            for field in SEARCH_FIELDS
        ),
    )


class DatasetEmbedding(Base):
    """One vector per piece of text of a dataset field, several rows per dataset.

    Rows of a dataset with a title, keywords and a long description:

        dataset_id | field       | chunk_index | chunk_text                       | embedding
        -----------+-------------+-------------+----------------------------------+----------
        42         | title       | 0           | Soil moisture in the Alps        | [...]
        42         | keywords    | 0           | soil, hydrology, climate         | [...]
        42         | description | 0           | We measured soil moisture at ... | [...]
        42         | description | 1           | The sensors were calibrated ...  | [...]

    Title and keywords fit in the model window and get a single chunk 0. The description is split
    in chunks of whole sentences, its best matching chunk gives the score of the dataset. A field
    without text has no row.
    """

    __tablename__ = "dataset_embedding"

    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"), primary_key=True)
    field: Mapped[str] = mapped_column(primary_key=True, comment="Dataset field the text comes from")
    chunk_index: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        server_default=text("0"),
        comment="Position of the chunk in the field text, always 0 for fields embedded whole (title, keywords)",
    )
    chunk_text: Mapped[str] = mapped_column(comment="Exact text sent to the embedding model, without the task prefix")
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBED_DIMENSIONS), comment="Vector of chunk_text, compared to the query vector by cosine distance"
    )

    __table_args__ = (
        CheckConstraint(
            f"field IN ({', '.join(repr(f) for f in SEARCH_FIELDS)})", name="dataset_embedding_field_check"
        ),
        # Semantic search, one partial index per field: each field always contributes candidates
        *(
            Index(
                f"dataset_embedding_{field}_vchordrq_idx",
                "embedding",
                postgresql_using="vchordrq",
                postgresql_ops={"embedding": "vector_cosine_ops"},
                postgresql_with={"options": VCHORDRQ_OPTIONS, "probes": f"'{VCHORDRQ_PROBES}'"},
                postgresql_where=text(f"field = '{field}'"),
                info=DEFERRED,
            )
            for field in SEARCH_FIELDS
        ),
        {"comment": "Embeddings of the searched fields of each dataset, one row per text chunk"},
    )


class Repository(Base):
    """A repository the datasets are harvested from, copied from datasetdb by the transform."""

    __tablename__ = "repository"

    code: Mapped[str] = mapped_column(primary_key=True, comment="datasetdb code, e.g. ZENODO")
    name: Mapped[str]
    description: Mapped[str | None]
    url: Mapped[str | None]


class Agent(Base):
    """A person or organization credited on datasets, one row per ORCID, else ROR, else exact name.

    The same person appears twice when only some of their datasets carry their ORCID. The creator
    filter searches the names an agent is credited under (`AgentName`), so it finds both.
    """

    __tablename__ = "agent"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # First name seen for this identity, a label: the names as written are on the links
    name: Mapped[str]
    # DataCite nameType: Personal or Organizational
    type: Mapped[str | None]
    orcid: Mapped[str | None] = mapped_column(comment="Bare ORCID, e.g. 0000-0002-1825-0097")
    ror: Mapped[str | None] = mapped_column(comment="Bare ROR id, e.g. 05gq02987")

    __table_args__ = (
        # Identity, the transform inserts the agents ON CONFLICT DO NOTHING
        Index("agent_orcid_idx", "orcid", unique=True),
        Index("agent_ror_idx", "ror", unique=True, postgresql_where=text("orcid IS NULL")),
        Index("agent_name_idx", "name", unique=True, postgresql_where=text("orcid IS NULL AND ror IS NULL")),
    )


class AgentName(Base):
    """A name an agent is credited under on some dataset, where the creator filter searches.

    An agent with an ORCID or a ROR has one row per spelling ("ETH Zurich", "ETH Zürich"): found by any
    of them, it brings all its datasets.
    """

    __tablename__ = "agent_name"

    agent_id: Mapped[int] = mapped_column(ForeignKey("agent.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(primary_key=True)
    name_key: Mapped[str | None] = mapped_column(Computed("name_key(name)", persisted=True))

    __table_args__ = (
        # Fuzzy creator filter: the keys closest to name_key(:name) by trigram distance
        Index(
            "agent_name_key_trgm_idx",
            "name_key",
            postgresql_using="gist",
            postgresql_ops={"name_key": "gist_trgm_ops"},
            info=DEFERRED,
        ),
        # Then every agent credited under those keys
        Index("agent_name_key_idx", "name_key", info=DEFERRED),
    )


class DatasetAgent(Base):
    """An agent credited on a dataset, in the order of the record, as written there."""

    __tablename__ = "dataset_agent"

    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(primary_key=True, server_default=text("'creator'"))
    # Some physics datasets credit thousands of authors
    position: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agent.id"))
    name: Mapped[str]
    given_name: Mapped[str | None]
    family_name: Mapped[str | None]
    # [{"scheme", "value"}], every scheme (ISNI, Scopus...), ORCID and ROR as bare ids
    identifiers: Mapped[list[dict[str, Any]] | None]

    # Datasets of an agent, read from the index only
    __table_args__ = (Index("dataset_agent_agent_idx", "agent_id", "dataset_id"),)


class Subject(Base):
    """A subject of datasets, one row per key: "Ecology" and "ecology" are the same subject."""

    __tablename__ = "subject"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # First name seen for this key, a label: the names as written are on the links
    name: Mapped[str]
    key: Mapped[str] = mapped_column(Computed("subject_key(name)", persisted=True), unique=True)


class DatasetSubject(Base):
    """A subject of a dataset, with the name, scheme and URI its record gives it."""

    __tablename__ = "dataset_subject"

    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"), primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subject.id"), primary_key=True)
    name: Mapped[str]
    scheme: Mapped[str | None]
    uri: Mapped[str | None]

    # Datasets of a subject, read from the index only
    __table_args__ = (Index("dataset_subject_subject_idx", "subject_id", "dataset_id"),)


class DatasetRecord(Base):
    """A datasetdb record of a dataset, which tells the repositories the dataset was found in.

    The same DOI harvested from two repositories makes one dataset with two records. Its content
    comes from one of them, `dataset.source_record_id`.
    """

    __tablename__ = "dataset_record"

    record_id: Mapped[str] = mapped_column(primary_key=True, comment="datasetdb records.id")
    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"))
    repository: Mapped[str] = mapped_column(ForeignKey("repository.code"))
    # The endpoint of the repository the record was harvested from, identifiers are unique within it
    harvest_url: Mapped[str | None]
    record_identifier: Mapped[str] = mapped_column(
        comment="Identifier of the record in its repository: OAI-PMH identifier or unique record ID"
    )

    # Repository filter, both read from the index only: the repositories of a dataset, the datasets of
    # a repository
    __table_args__ = (
        Index("dataset_record_dataset_idx", "dataset_id", "repository"),
        Index("dataset_record_repository_idx", "repository", "dataset_id"),
    )


class DatasetDate(Base):
    """A date of a dataset, typed as in DataCite: Issued, Collected, Coverage..."""

    __tablename__ = "dataset_date"

    dataset_id: Mapped[int] = mapped_column(ForeignKey("dataset.id", ondelete="CASCADE"), primary_key=True)
    type: Mapped[str] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(primary_key=True)


def search_indexes() -> list[Index]:
    """The deferred indexes, in creation order: filters first, the heavy vector index builds last."""
    return [
        i
        for table in Base.metadata.sorted_tables
        for i in sorted(table.indexes, key=lambda i: i.name or "")
        if i.info.get("deferred")
    ]


def is_vector_index(index: Index) -> bool:
    return index.dialect_options["postgresql"]["using"] == "vchordrq"


# `create_all` skips the deferred indexes, rebuild_indexes executes their CreateIndex directly (not filtered)
for _index in search_indexes():
    _index.ddl_if(callable_=lambda *_, **__: False)
