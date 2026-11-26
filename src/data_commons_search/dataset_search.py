"""Hybrid search over the appdb `dataset` tables, built by the DAGs in the `dags/` folder.

One SQL statement retrieves a candidate list per (method, field):

- semantic: cosine similarity of the query to the named embeddings of each field (VectorChord
  vchordrq, one partial index per field in `dataset_embedding`)
- lexical: BM25 on each field of `dataset` (pg_textsearch, one index per field)

Each list is min-max normalized over its own candidates, then the lists are combined with
weights: `hybrid_weights` between the methods, `search_field_weights` between the fields. A dataset
missing from a list scores 0 there, so a dataset matching on several fields ranks above one
matching on a single field (e.g. a bare title with no description).
"""

import asyncio
from typing import Any

from sqlalchemy import (
    CTE,
    JSON,
    ColumnElement,
    Connection,
    Date,
    Float,
    Integer,
    Label,
    Select,
    String,
    Text,
    and_,
    any_,
    bindparam,
    case,
    cast,
    exists,
    func,
    literal,
    select,
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by
from sqlalchemy.orm import Session, aliased

from data_commons_search.config import settings
from data_commons_search.dataset_models import (
    SEARCH_FIELDS,
    Agent,
    AgentName,
    Dataset,
    DatasetAgent,
    DatasetDate,
    DatasetEmbedding,
    DatasetRecord,
    DatasetSubject,
    Repository,
    Subject,
    bm25_index_name,
)
from data_commons_search.db import SessionLocal
from data_commons_search.models import SearchFilters, SearchHit, SearchResults

# Filter strategy, from the number of datasets passing the hard filters. Up to these sizes the filter is
# selective: the scores of the filtered datasets are computed exactly (~3 ms per 1000 datasets per semantic
# list, ~4 ms per lexical list), an index scan filtered after would return too few of them. Above, the
# index scans are filtered on each row, their candidates hold enough of the filtered datasets. Lexical
# stays exact further: the BM25 index scan stops after ~100k rows, and missed all of a 120k creator's
# datasets on a common term
SEMANTIC_EXACT_MAX_DATASETS = 10_000
LEXICAL_EXACT_MAX_DATASETS = 200_000
# Creator filter: the names closest to the requested one (typos, accents, word order), kept above a
# trigram similarity. A typo-ed full name scored 0.50 on its person, 0.39 on the next one
CREATOR_CANDIDATES = 20
CREATOR_MIN_SIMILARITY = 0.45
# DataCite date types describing the period the data covers, used for the soft date boost.
# Publication dates are not: a 1960-2020 series published in 2023 matches a 1960-2020 question
COVERAGE_DATE_TYPES = ("Collected", "Coverage", "Valid")

# Bound once per statement, the values are given at execution
QUERY = bindparam("query", type_=String)
VECTOR = bindparam("vector", type_=DatasetEmbedding.embedding.type)
POOL = bindparam("pool", type_=Integer)
CREATOR = bindparam("creator", type_=String)
SUBJECTS = bindparam("subjects", type_=ARRAY(Text))
REPOSITORIES = bindparam("repositories", type_=ARRAY(String))
PUBLISHED_AFTER = bindparam("published_after", type_=Date)
PUBLISHED_BEFORE = bindparam("published_before", type_=Date)


def _semantic_list(field: str, filtered: CTE | None, post_filter: ColumnElement[bool] | None) -> Select[Any]:
    """Best chunk similarity per dataset for one embedded field.

    `filtered`: exact scores of these datasets only. `post_filter`: index scan filtered on each row.
    """
    distance = DatasetEmbedding.embedding.cosine_distance(VECTOR)
    if filtered is not None:
        # Exact distances over the filtered datasets, ordering on the score (not on the distance
        # operator) keeps the planner off the ANN index
        score = func.max(1 - distance).label("score")
        return (
            select(DatasetEmbedding.dataset_id, score)
            .join(filtered, filtered.c.id == DatasetEmbedding.dataset_id)
            .where(DatasetEmbedding.field == field)
            .group_by(DatasetEmbedding.dataset_id)
            .order_by(score.desc())
            .limit(POOL)
        )
    # ORDER BY distance LIMIT is what the vchordrq partial index of the field serves. The field is
    # rendered inline, not bound: a generic plan of a prepared statement cannot match the partial index
    chunks = (
        select(DatasetEmbedding.dataset_id, (1 - distance).label("score"))
        .where(DatasetEmbedding.field == literal(field, literal_execute=True))
        .order_by(distance)
        .limit(POOL)
    )
    if post_filter is not None:
        # Large filtered set, the index scan keeps going until it found POOL chunks of these datasets
        chunks = chunks.where(post_filter)
    best = chunks.subquery()
    return select(best.c.dataset_id, func.max(best.c.score).label("score")).group_by(best.c.dataset_id)


def _lexical_list(field: str, filtered: CTE | None, post_filter: ColumnElement[bool] | None) -> Select[Any]:
    """BM25 score per dataset for one field. pg_textsearch returns negated scores, lowest first.

    `filtered`: exact scores of these datasets only. `post_filter`: index scan filtered on each row.
    """
    bm25 = getattr(Dataset, field).op("<@>", return_type=Float)(func.to_bm25query(QUERY, bm25_index_name(field)))
    score = (-bm25).label("score")
    if filtered is not None:
        # Scores of the filtered datasets only, ordering on the score (not on the <@> operator) keeps the
        # planner off the BM25 index: its top-k scan stops after ~100k rows, often before the filtered ones
        matches = (
            select(Dataset.id.label("dataset_id"), score)
            .join(filtered, filtered.c.id == Dataset.id)
            .order_by(score.desc())
            .limit(POOL)
        )
    else:
        # ORDER BY <@> LIMIT is what the BM25 index of the field serves. A large filtered set is
        # checked on its candidates
        matches = select(Dataset.id.label("dataset_id"), score).order_by(bm25).limit(POOL)
        if post_filter is not None:
            matches = matches.where(post_filter)
    subquery = matches.subquery()
    return select(subquery.c.dataset_id, subquery.c.score).where(subquery.c.score > 0)


def _weights() -> dict[str, float]:
    """Weight of each list, summing to 1 so the fused score stays in 0..1 (before the date boost)."""
    semantic, lexical = settings.hybrid_weights
    fields_total = sum(settings.search_field_weights.get(f, 0.0) for f in SEARCH_FIELDS) or 1.0
    weights = {}
    for field in SEARCH_FIELDS:
        share = settings.search_field_weights.get(field, 0.0) / fields_total
        weights[f"semantic_{field}"] = semantic * share
        weights[f"lexical_{field}"] = lexical * share
    return weights


def _match_ids(filters: SearchFilters) -> Select[Any]:
    """One row with the ids of the agents credited under the names closest to the creator filter
    (`agent_ids`, typos, accents and word order tolerated), of the subjects of the subject filter
    (`subject_ids`, case and accents ignored) and the codes of the repository filter that exist
    (`repository_codes`), NULL when none matches."""
    columns = []
    if filters.creator_name:
        query_key = func.name_key(CREATOR)
        candidate = aliased(AgentName)
        # KNN on the trigram index first, the similarity cut after: a cut inside the index scan would
        # read the whole index when no name is close enough
        nearest = (
            select(candidate.name_key, func.similarity(candidate.name_key, query_key).label("similarity"))
            .order_by(candidate.name_key.op("<->")(query_key))
            .limit(CREATOR_CANDIDATES)
            .subquery()
        )
        # Then every agent credited under the close keys. Many agents share a key (a common name has one
        # per ORCID, plus one per spelling without identifier): the nearest rows alone would cut some
        close_keys = select(nearest.c.name_key).where(nearest.c.similarity >= CREATOR_MIN_SIMILARITY)
        close = select(func.array_agg(AgentName.agent_id.distinct())).where(AgentName.name_key.in_(close_keys))
        columns.append(close.scalar_subquery().label("agent_ids"))
    if filters.subjects:
        keys = select(func.subject_key(func.unnest(SUBJECTS)))
        columns.append(
            select(func.array_agg(Subject.id)).where(Subject.key.in_(keys)).scalar_subquery().label("subject_ids")
        )
    if filters.repositories:
        known = select(func.array_agg(Repository.code)).where(Repository.code == any_(REPOSITORIES))
        columns.append(known.scalar_subquery().label("repository_codes"))
    return select(*columns)


def _linked_ids(dataset_id: Any, linked_id: Any, ids: list[Any]) -> Select[Any]:
    """Datasets linked to these agents, subjects or repositories. The ids are inline, not bound: the
    planner reads how many datasets they have from the statistics of the link table (a generic plan
    would guess). They are read from the database by `_match_ids`, never the values of the request."""
    return select(dataset_id.label("id")).where(
        linked_id.in_(bindparam(linked_id.key, ids, expanding=True, literal_execute=True))
    )


# Link table columns of each column of `_match_ids`, most selective first. A dataset passes the repository
# filter when any of its records comes from one of the repositories, not only the record that made it
LINKS = {
    "agent_ids": (DatasetAgent.dataset_id, DatasetAgent.agent_id),
    "subject_ids": (DatasetSubject.dataset_id, DatasetSubject.subject_id),
    "repository_codes": (DatasetRecord.dataset_id, DatasetRecord.repository),
}


def _match_links(db: Session | Connection, filters: SearchFilters, params: dict[str, Any]) -> list[Select[Any]] | None:
    """The datasets of the agents, subjects and repositories the filters match, one select per filter.
    None when one of them matches nothing: no dataset passes."""
    if not (filters.creator_name or filters.subjects or filters.repositories):
        return []
    matched = db.execute(_match_ids(filters), params).mappings().one()
    if not all(matched.values()):
        return None
    return [_linked_ids(*LINKS[name], matched[name]) for name in LINKS if name in matched]


def _row_conditions(dataset: Any, filters: SearchFilters) -> list[ColumnElement[bool]]:
    """Hard filters on the columns of a dataset row (`Dataset` or an alias of it)."""
    conditions = []
    if filters.published_after:
        conditions.append(dataset.publication_date >= PUBLISHED_AFTER)
    if filters.published_before:
        conditions.append(dataset.publication_date <= PUBLISHED_BEFORE)
    return conditions


def _filtered_ids(filters: SearchFilters, linked: list[Select[Any]]) -> Select[Any]:
    """Ids of the datasets passing all the hard filters, `linked` the datasets of the matched agents,
    subjects and repositories."""
    conditions = _row_conditions(Dataset, filters)
    if not conditions:
        # Agents, subjects or repositories only, read from the link tables, no lookup in the dataset table
        first, *rest = linked
        first_id = first.selected_columns[0]
        return first.where(*(first_id.in_(other) for other in rest)).distinct()
    return select(Dataset.id).where(*conditions, *(Dataset.id.in_(ids) for ids in linked))


def _post_filter(filters: SearchFilters, linked: list[Select[Any]], on_embedding: bool) -> ColumnElement[bool]:
    """Hard filters checked on each row of an index scan, of `dataset_embedding` or of `dataset`.

    `IS TRUE` keeps them out of the plan: the planner would otherwise turn them into joins driven from
    the linked ids or the filter indexes, and then score all the filtered datasets exactly instead of
    using the search index.
    """
    dataset = aliased(Dataset) if on_embedding else Dataset
    dataset_id = DatasetEmbedding.dataset_id if on_embedding else Dataset.id
    conditions: list[ColumnElement[bool]] = _row_conditions(dataset, filters)
    if on_embedding and conditions:
        # The columns of an embedding row are on its dataset
        conditions = [exists().where(dataset.id == dataset_id, *conditions)]
    # A lookup of the row's dataset in the link table, not `IN (linked ids)`: too large to hash in
    # work_mem (300k ids of a broad subject), the ids are scanned again for each row
    on_links = [exists(ids.where(ids.selected_columns[0] == dataset_id)) for ids in linked]
    return and_(*on_links, *conditions).is_(true())


def _build_query(
    with_vector: bool,
    filters: SearchFilters,
    linked: list[Select[Any]],
    date_range: tuple[str, str] | None = None,
    filtered: CTE | None = None,
    count: int = 0,
) -> Select[Any]:
    """Hybrid query, returning the best datasets with their fused score and the candidates count.

    `filtered`: the datasets passing the hard filters, `count` of them, `linked` the datasets of the
    agents, subjects and repositories they matched. The count picks for each kind of list between scoring them exactly
    (few datasets) and filtering the index scans on each row (many).

    Typical SQL generated, condensed (aliases shortened, only the title lists shown: the keywords and
    description lists are the same with their field). Bind parameters are written `:name`.

    Hybrid search without filter, each list is an index scan (vchordrq, BM25). A lexical only search
    (query not embedded) is the same without the semantic lists:

        WITH candidates AS (
            SELECT 'semantic_title' AS list, dataset_id, max(score) AS score
            FROM (
                SELECT dataset_id, 1 - (embedding <=> :vector) AS score
                FROM dataset_embedding
                WHERE field = 'title'  -- inline, not bound, to match the partial vchordrq index
                ORDER BY embedding <=> :vector LIMIT :pool
            ) GROUP BY dataset_id
            UNION ALL
            SELECT 'lexical_title' AS list, dataset_id, score
            FROM (
                SELECT id AS dataset_id, -(title <@> to_bm25query(:query, 'dataset_title_bm25_idx')) AS score
                FROM dataset
                ORDER BY title <@> to_bm25query(:query, 'dataset_title_bm25_idx') LIMIT :pool
            ) WHERE score > 0
            UNION ALL ...  -- semantic_keywords, lexical_keywords, semantic_description, lexical_description
        ),
        normalized AS (  -- min-max within each list
            SELECT list, dataset_id,
                coalesce((score - min(score) OVER w) / nullif(max(score) OVER w - min(score) OVER w, 0), 1.0) AS score
            FROM candidates WINDOW w AS (PARTITION BY list)
        ),
        fused AS (  -- weights from _weights()
            SELECT dataset_id, sum(score * CASE list WHEN 'semantic_title' THEN 0.24 ... ELSE 0 END) AS score
            FROM normalized GROUP BY dataset_id
        )
        top AS (
            SELECT dataset_id, score, count(*) OVER () AS total FROM fused ORDER BY score DESC LIMIT :limit
        )
        SELECT dataset.*, top.score, top.total,
            (SELECT json_strip_nulls(json_agg(json_build_object('name', dataset_agent.name, 'type', agent.type, ...)
                ORDER BY position))
             FROM dataset_agent JOIN agent ON agent.id = agent_id WHERE dataset_id = dataset.id) AS creators,
            ...  -- subjects, dates
        FROM dataset JOIN top ON top.dataset_id = dataset.id
        ORDER BY top.score DESC

    With hard filters, search_rows first matches the creator, the subjects and the repositories (_match_ids):

        SELECT
            (SELECT array_agg(DISTINCT agent_id) FROM agent_name WHERE name_key IN (
                SELECT name_key FROM (
                    SELECT name_key, similarity(name_key, name_key(:creator)) AS similarity
                    FROM agent_name ORDER BY name_key <-> name_key(:creator) LIMIT 20  -- trigram index
                ) WHERE similarity >= 0.45
            )) AS agent_ids,
            (SELECT array_agg(id) FROM subject
             WHERE key IN (SELECT subject_key(unnest(:subjects)))) AS subject_ids,
            (SELECT array_agg(code) FROM repository WHERE code = ANY(:repositories)) AS repository_codes

    then counts the datasets passing all the filters, the matched ids inline. A dataset is in a
    repository when any of its records is, not only the one that made it:

        SELECT count(*) FROM (
            SELECT id FROM dataset
            WHERE publication_date >= :published_after AND publication_date <= :published_before
              AND id IN (SELECT dataset_id FROM dataset_agent WHERE agent_id IN (17, 42))
              AND id IN (SELECT dataset_id FROM dataset_subject WHERE subject_id IN (3))
              AND id IN (SELECT dataset_id FROM dataset_record WHERE repository IN ('ZENODO'))
            LIMIT 200001
        )

    Without date filters, the ids are read from the link tables, without the dataset table:
    `SELECT DISTINCT dataset_id FROM dataset_agent WHERE agent_id IN (...) AND dataset_id IN (...)`.

    The lists then score them exactly when the count is up to SEMANTIC_EXACT_MAX_DATASETS (semantic)
    or LEXICAL_EXACT_MAX_DATASETS (lexical), from the same query as a CTE:

        WITH filtered AS MATERIALIZED (SELECT id FROM dataset WHERE ... LIMIT 4000),  -- as counted, LIMIT the count
        candidates AS (...), normalized AS (...), fused AS (...)  -- as above, with these lists:

        -- semantic list, exact distances of the filtered datasets
        SELECT dataset_id, max(1 - (embedding <=> :vector)) AS score
        FROM dataset_embedding JOIN filtered ON filtered.id = dataset_embedding.dataset_id
        WHERE field = :field GROUP BY dataset_id
        ORDER BY score DESC LIMIT :pool

        -- lexical list, exact scores of the filtered datasets
        SELECT id AS dataset_id, -(title <@> to_bm25query(:query, 'dataset_title_bm25_idx')) AS score
        FROM dataset JOIN filtered ON filtered.id = dataset.id
        ORDER BY score DESC LIMIT :pool

    Above, the index scans of the lists are filtered on each row:

        -- semantic list, the conditions on the dataset of each embedding row
        SELECT dataset_id, 1 - (embedding <=> :vector) AS score
        FROM dataset_embedding
        WHERE field = 'title' AND (
            EXISTS (SELECT 1 FROM dataset_agent WHERE agent_id IN (17, 42)
                    AND dataset_agent.dataset_id = dataset_embedding.dataset_id)
            AND EXISTS (SELECT 1 FROM dataset WHERE dataset.id = dataset_embedding.dataset_id AND ...)
        ) IS TRUE
        ORDER BY embedding <=> :vector LIMIT :pool

        -- lexical list
        SELECT id AS dataset_id, -(title <@> to_bm25query(:query, 'dataset_title_bm25_idx')) AS score
        FROM dataset
        WHERE (EXISTS (SELECT 1 FROM dataset_agent WHERE ... AND dataset_agent.dataset_id = dataset.id)
               AND publication_date >= :published_after ...) IS TRUE
        ORDER BY title <@> to_bm25query(:query, 'dataset_title_bm25_idx') LIMIT :pool

    With a date range, the fused score is boosted for the datasets covering it:

        SELECT dataset_id, score * CASE WHEN EXISTS (
            SELECT 1 FROM dataset_date WHERE dataset_date.dataset_id = fused.dataset_id
            AND type IN ('Collected', 'Coverage', 'Valid') AND CAST(date AS text) BETWEEN :start AND :end
        ) THEN :date_boost ELSE 1.0 END AS score, ...
    """
    # Each kind of list scores the filtered datasets exactly up to its threshold, above its index scan
    # is filtered on each row
    semantic_filtered = lexical_filtered = semantic_post = lexical_post = None
    if filtered is not None:
        if count <= SEMANTIC_EXACT_MAX_DATASETS:
            semantic_filtered = filtered
        else:
            semantic_post = _post_filter(filters, linked, on_embedding=True)
        if count <= LEXICAL_EXACT_MAX_DATASETS:
            lexical_filtered = filtered
        else:
            lexical_post = _post_filter(filters, linked, on_embedding=False)
    lists: list[tuple[str, Select[Any]]] = []
    for field in SEARCH_FIELDS:
        if with_vector:
            lists.append((f"semantic_{field}", _semantic_list(field, semantic_filtered, semantic_post)))
        lists.append((f"lexical_{field}", _lexical_list(field, lexical_filtered, lexical_post)))
    subqueries = [(name, query.subquery(name)) for name, query in lists]
    candidates = union_all(
        *(select(literal(name).label("list"), sq.c.dataset_id, sq.c.score) for name, sq in subqueries)
    ).cte("candidates")

    # Min-max normalization within each list, a list with a single candidate gives it 1
    low = func.min(candidates.c.score).over(partition_by=candidates.c.list)
    high = func.max(candidates.c.score).over(partition_by=candidates.c.list)
    normalized = select(
        candidates.c.list,
        candidates.c.dataset_id,
        func.coalesce((candidates.c.score - low) / func.nullif(high - low, 0), 1.0).label("score"),
    ).cte("normalized")
    weight = case({name: literal(w) for name, w in _weights().items()}, value=normalized.c.list, else_=literal(0.0))
    fused = (
        select(normalized.c.dataset_id, func.sum(normalized.c.score * weight).label("score"))
        .group_by(normalized.c.dataset_id)
        .cte("fused")
    )

    score: ColumnElement[Any] = fused.c.score
    if date_range:
        # Compared as yyyy-mm-dd strings, the dates of the filter may be partial (a year)
        in_range = exists().where(
            DatasetDate.dataset_id == fused.c.dataset_id,
            DatasetDate.type.in_(COVERAGE_DATE_TYPES),
            cast(DatasetDate.date, String).between(*date_range),
        )
        score = score * case((in_range, settings.date_boost), else_=1.0)
    score = score.label("score")
    top = (
        select(fused.c.dataset_id, score, func.count().over().label("total"))
        .order_by(score.desc())
        .limit(bindparam("limit"))
        .cte("top")
    )
    # The lists of the returned datasets only, read from their link tables
    return (
        select(Dataset, top.c.score, top.c.total, *_dataset_lists())
        .join(top, top.c.dataset_id == Dataset.id)
        .order_by(top.c.score.desc())
    )


def _json_list(name: str, fields: dict[str, Any], order_by: Any, link: Any, *joins: tuple[Any, Any]) -> Label[Any]:
    """JSON array of the rows linked to the outer dataset, one object of `fields` per row, nulls omitted."""
    item = func.json_build_object(*(arg for key, column in fields.items() for arg in (key, column)))
    query = select(func.json_strip_nulls(func.json_agg(aggregate_order_by(item, order_by)), type_=JSON))
    query = query.select_from(link.__table__)
    for target, on in joins:
        query = query.join(target, on)
    return query.where(link.dataset_id == Dataset.id).scalar_subquery().label(name)


def _dataset_lists() -> list[Label[Any]]:
    """Creators, subjects, dates and harvest endpoint of the outer dataset, as `_to_hit` reads them."""
    # Endpoint of the record the dataset was built from
    harvest_url = select(DatasetRecord.harvest_url).where(DatasetRecord.record_id == Dataset.source_record_id)
    return [
        harvest_url.scalar_subquery().label("harvest_url"),
        _json_list(
            "creators",
            {
                "name": DatasetAgent.name,
                "given_name": DatasetAgent.given_name,
                "family_name": DatasetAgent.family_name,
                "type": Agent.type,
                "identifiers": DatasetAgent.identifiers,
            },
            DatasetAgent.position,
            DatasetAgent,
            (Agent, Agent.id == DatasetAgent.agent_id),
        ),
        _json_list(
            "subjects",
            {"subject": DatasetSubject.name, "subjectScheme": DatasetSubject.scheme, "valueUri": DatasetSubject.uri},
            DatasetSubject.name,
            DatasetSubject,
        ),
        _json_list("dates", {"date": DatasetDate.date, "dateType": DatasetDate.type}, DatasetDate.date, DatasetDate),
    ]


def _date_range(start_date: str | None, end_date: str | None) -> tuple[str, str] | None:
    """Bounds of the date boost, dates compare as yyyy-mm-dd strings."""
    if not start_date and not end_date:
        return None
    return start_date or "0000-01-01", end_date or "9999-12-31"


def _to_hit(row: Any) -> SearchHit:
    """Map a result row onto the `SearchHit` shape the agent and the frontend already use."""
    dataset: Dataset = row.Dataset
    dates = row.dates or []
    # SearchHit reads the publication date from the Issued date, which the transform may have derived
    if dataset.publication_date and not any(d["dateType"] == "Issued" for d in dates):
        dates.append({"date": dataset.publication_date.isoformat(), "dateType": "Issued"})
    return SearchHit.model_validate(
        {
            "_id": dataset.url,
            "_score": row.score,
            "_source": {
                "url": dataset.url,
                "doi": dataset.doi,
                "_repo": dataset.harvested_from,
                "_harvest_url": row.harvest_url,
                "titles": [{"title": dataset.title}],
                "descriptions": [{"description": dataset.description}] if dataset.description else [],
                "subjects": row.subjects or [],
                "creators": [{"creatorName": c["name"], **c} for c in row.creators or []],
                "dates": dates,
                "publicationYear": str(dataset.publication_year) if dataset.publication_year else None,
            },
        }
    )


def _filter_params(filters: SearchFilters) -> dict[str, Any]:
    """Values of the bind parameters of the hard filters that are set."""
    params: dict[str, Any] = {}
    if filters.creator_name:
        params["creator"] = filters.creator_name
    if filters.subjects:
        params["subjects"] = [" ".join(subject.split()) for subject in filters.subjects]
    if filters.repositories:
        params["repositories"] = [code.strip().upper() for code in filters.repositories]
    if filters.published_after:
        params["published_after"] = filters.published_after
    if filters.published_before:
        params["published_before"] = filters.published_before
    return params


def search_rows(
    db: Session | Connection, with_vector: bool, filters: SearchFilters, params: dict[str, Any]
) -> list[Any]:
    """Run the hybrid query, the filter strategy picked from the number of datasets passing the filters."""
    params = {**params, **_filter_params(filters)}
    date_range = _date_range(filters.start_date, filters.end_date)
    if not filters.has_hard_filter:
        return list(db.execute(_build_query(with_vector, filters, [], date_range), params))

    linked = _match_links(db, filters, params)
    if linked is None:
        return []
    ids = _filtered_ids(filters, linked)
    # Counted up to the largest threshold, a broad filter (e.g. a big repository) is not counted in full
    capped = ids.limit(LEXICAL_EXACT_MAX_DATASETS + 1).subquery()
    count = db.execute(select(func.count()).select_from(capped), params).scalar() or 0
    if not count:
        return []
    # LIMIT the exact count drops no row, it gives the planner the size of the set: guessed, it hash
    # joins it to a sequential scan of the dataset table instead of looking up the filtered rows
    filtered = ids.limit(literal(count, literal_execute=True)).cte("filtered").prefix_with("MATERIALIZED")
    return list(db.execute(_build_query(with_vector, filters, linked, date_range, filtered, count), params))


def _run_query(with_vector: bool, filters: SearchFilters, params: dict[str, Any]) -> list[Any]:
    with SessionLocal() as session:
        return search_rows(session, with_vector, filters, params)


async def search_datasets(
    query: str,
    vector: list[float] | None,
    limit: int,
    filters: SearchFilters | None = None,
) -> SearchResults:
    """Hybrid search, lexical only when the query could not be embedded.

    `total_found` is the number of candidates considered: both methods stop at `candidate_pool`
    per field, there is no cheap exact count of all the matching datasets.
    """
    params: dict[str, Any] = {"query": query, "pool": settings.candidate_pool, "limit": limit}
    if vector is not None:
        params["vector"] = vector
    rows = await asyncio.to_thread(_run_query, vector is not None, filters or SearchFilters(), params)
    return SearchResults(total_found=rows[0].total if rows else 0, hits=[_to_hit(r) for r in rows])
