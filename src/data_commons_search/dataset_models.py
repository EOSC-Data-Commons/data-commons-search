"""SQLAlchemy models of the appdb search tables, and the indexes built on top of them.

Written by the DAGs in `dags/` (search_pipeline), queried by the API in dataset_search.py. Only
imports SQLAlchemy and pgvector, not the app settings, so the airflow worker can import it without the API deps.

`Dataset` is the source of truth for the API: one row per dataset, cleaned up from the DataCite
JSON harvested in datasetdb. List fields are JSONB arrays of objects (e.g. a creator carries its
own identifiers), the flat text the search needs is derived from them by generated columns, and
the creator names by the `creator_name` materialized view (fuzzy creator filter).

`DatasetEmbedding` holds one vector per text chunk of each searched field of a dataset (see
dags/search_pipeline/embeddings.py).

The search indexes are declared on the models but marked DEFERRED: `create_all` skips them and
the rebuild_indexes DAG builds them after a bulk load. A DiskANN graph filled by incremental inserts
from empty has catastrophic recall, and BM25 also builds faster in one pass.
"""

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
    column,
    func,
    table,
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

# Join one key of a JSONB array of objects, used by the generated text columns. Created before the tables
JSONB_ARRAY_JOIN_DDL = """
CREATE OR REPLACE FUNCTION jsonb_array_join(items jsonb, key text, sep text DEFAULT ', ')
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (SELECT string_agg(item ->> key, sep) FROM jsonb_array_elements(items) AS item)
"""
# Comparable form of a person or organization name: lowercase, no accents, words sorted, so
# "Ionita, Monica" and "Monica Ionita" get the same key. IMMUTABLE (unaccent with an explicit
# dictionary is) so a query on creator_key(:name) is computed once and uses the index
CREATOR_KEY_DDL = """
CREATE OR REPLACE FUNCTION creator_key(name text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
RETURN (
    SELECT string_agg(word, ' ' ORDER BY word)
    FROM regexp_split_to_table(lower(public.unaccent('public.unaccent'::regdictionary, name)), '[^[:alnum:]]+') AS word
    WHERE word <> ''
)
"""

# One row per distinct creator name of `dataset.creators`, with the datasets it created: the fuzzy
# creator filter finds the names closest to the requested one (trigram distance on `key`, typos
# and word order tolerated), then their datasets. Refreshed by the transform after each change, the
# unique index on name lets it refresh CONCURRENTLY (readable meanwhile)
CREATOR_NAME_DDL = (
    """CREATE MATERIALIZED VIEW IF NOT EXISTS creator_name AS
SELECT creator ->> 'name' AS name, creator_key(creator ->> 'name') AS key, array_agg(DISTINCT dataset.id) AS dataset_ids
FROM dataset, jsonb_array_elements(dataset.creators) AS creator
WHERE creator_key(creator ->> 'name') IS NOT NULL
GROUP BY creator ->> 'name'""",
    "CREATE UNIQUE INDEX IF NOT EXISTS creator_name_name_idx ON creator_name (name)",
    "CREATE INDEX IF NOT EXISTS creator_name_key_idx ON creator_name USING gist (key gist_trgm_ops)",
)
# Queried with SQLAlchemy Core, the view is created by the DDL above and not by the metadata
creator_name_view = table(
    "creator_name", column("name", Text), column("key", Text), column("dataset_ids", ARRAY(BigInteger))
)


def bm25_index_name(field: str) -> str:
    """BM25 index of a field, `to_bm25query()` takes its name."""
    return f"dataset_{field}_bm25_idx"


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
    # [{"name", "scheme", "uri"}]
    subjects: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'"))
    # [{"name", "given_name", "family_name", "type", "identifiers": [{"scheme", "value"}]}]
    creators: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'"))
    # [{"date": "yyyy-mm-dd", "type"}], type from DataCite (Issued, Collected, Coverage...)
    dates: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'"))
    # [{"name", "uri"}]
    rights: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'"))
    publication_year: Mapped[int | None] = mapped_column(SmallInteger)
    publication_date: Mapped[date | None]
    languages: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    formats: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    # Repository code in datasetdb (e.g. ZENODO), and the endpoint it was harvested from
    repository: Mapped[str]
    harvest_url: Mapped[str | None]
    keywords: Mapped[str | None] = mapped_column(Computed("jsonb_array_join(subjects, 'name')", persisted=True))
    # datasetdb.records row this dataset was built from
    source_record_id: Mapped[str]
    source_updated_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Reset to NULL when an embedded field changes, the queue of the generate_embeddings DAG
    embedded_at: Mapped[datetime | None]

    __table_args__ = (
        Index("dataset_to_embed_idx", "id", postgresql_where=text("embedded_at IS NULL")),
        # Filters
        Index("dataset_repository_idx", "repository", info=DEFERRED),
        Index("dataset_publication_date_idx", "publication_date", info=DEFERRED),
        Index("dataset_doi_idx", "doi", info=DEFERRED),
        Index(
            "dataset_subjects_idx",
            "subjects",
            postgresql_using="gin",
            postgresql_ops={"subjects": "jsonb_path_ops"},
            info=DEFERRED,
        ),
        Index(
            "dataset_creators_idx",
            "creators",
            postgresql_using="gin",
            postgresql_ops={"creators": "jsonb_path_ops"},
            info=DEFERRED,
        ),
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
        # Semantic search, one DiskANN graph per field: a denser graph than one shared label-filtered
        # graph, and each field always contributes candidates
        *(
            Index(
                f"dataset_embedding_{field}_diskann_idx",
                "embedding",
                postgresql_using="diskann",
                postgresql_ops={"embedding": "vector_cosine_ops"},
                postgresql_where=text(f"field = '{field}'"),
                info=DEFERRED,
            )
            for field in SEARCH_FIELDS
        ),
        {"comment": "Embeddings of the searched fields of each dataset, one row per text chunk"},
    )


def search_indexes() -> list[Index]:
    """The deferred indexes, in creation order: filters first, the heavy DiskANN builds last."""
    return [
        i
        for table in Base.metadata.sorted_tables
        for i in sorted(table.indexes, key=lambda i: i.name or "")
        if i.info.get("deferred")
    ]


def is_diskann(index: Index) -> bool:
    return index.dialect_options["postgresql"]["using"] == "diskann"


# `create_all` skips the deferred indexes, rebuild_indexes executes their CreateIndex directly (not filtered)
for _index in search_indexes():
    _index.ddl_if(callable_=lambda *_, **__: False)
