"""Transform the datasetdb records into rows of the appdb `dataset` table.

The DataCite JSON in datasetdb has already been harmonized by the warehouse (see
metadata-warehouse src/utils/normalize_datacite_json.py), this only picks the fields search needs,
cleans them up, and gives each dataset a unique URL: https://doi.org/<doi> when it has a DOI.
"""

import html
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Connection,
    Integer,
    case,
    cast,
    column,
    delete,
    exists,
    func,
    insert,
    literal_column,
    select,
    table,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from data_commons_search.dataset_models import Dataset
from search_pipeline.config import PipelineSettings
from search_pipeline.db import appdb_engine, datasetdb_engine

logger = logging.getLogger(__name__)

# datasetdb resource types that make a dataset
RESOURCE_TYPES = ("Dataset",)
# Records read from datasetdb per round trip
READ_BATCH_SIZE = 2000

DOI_RE = re.compile(r"10\.\d{4,9}/\S+")
ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")
# Share of letters in the non-space characters of a description, below it is not prose
MIN_LETTERS_RATIO = 0.5
HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# Dates harvested as descriptions (e.g. DANS "Date Submitted: 2010-01-12"), already in `dates`
DATE_NOTE_RE = re.compile(r"^(date( \w+)?|issued|modified|available|created|submitted|updated)\s*:", re.IGNORECASE)
DATE_NOTE_MAX_CHARS = 100
# The main description is taken from the first of these types the record has, others after
DESCRIPTION_TYPES = ("Abstract", "Methods", "TechnicalInfo", "Other", "SeriesInformation")
# Order in which date types are taken as the publication date
PUBLICATION_DATE_TYPES = ("Issued", "Available", "Created", "Submitted", "Updated")

# Only the keys used below are extracted server side: the full JSON is ~10x bigger because of the
# additional metadata, and it is the bulk of the read time. Never ORDER BY, sorting this table is minutes
DATACITE_KEYS = (
    "titles",
    "descriptions",
    "subjects",
    "creators",
    "dates",
    "rightsList",
    "formats",
    "publicationYear",
    "doi",
    "url",
)
# datasetdb belongs to the warehouse, no model for it
SELECT_RECORDS_SQL = text(f"""
SELECT
    rec.id, rec.url, rec.title, rec.updated_at,
    jsonb_build_object({", ".join(f"'{key}', rec.datacite_json -> '{key}'" for key in DATACITE_KEYS)}) AS datacite,
    repo.code AS repository, endp.harvest_url
FROM records rec
JOIN repositories repo ON repo.id = rec.repository_id
JOIN endpoints endp ON endp.id = rec.endpoint_id
WHERE rec.resource_type::text = ANY(:resource_types)
  AND rec.datacite_json IS NOT NULL
  AND (CAST(:since AS timestamptz) IS NULL OR rec.updated_at > CAST(:since AS timestamptz))
  AND (CAST(:repositories AS text[]) IS NULL OR repo.code = ANY(CAST(:repositories AS text[])))
""")  # noqa: S608

# Columns set by the transform, the others are generated or managed by the pipeline
MANAGED_COLUMNS = ("created_at", "updated_at", "embedded_at")
WRITTEN_COLUMNS = [
    c for c in Dataset.__table__.c if c.identity is None and c.computed is None and c.name not in MANAGED_COLUMNS
]
# Fields the embeddings are computed from: a change queues the dataset for re-embedding
EMBEDDED_COLUMNS = ("title", "subjects", "description")


def completeness(row: Any) -> Any:
    """How many of the optional fields a row fills, to pick between records sharing a URL.

    Works on a Dataset instance (python) and on a table or EXCLUDED (SQL expression).
    """
    if isinstance(row, Dataset):
        return sum(map(bool, (row.description, row.subjects, row.creators, row.publication_date)))
    return (
        cast(row.description.is_not(None), Integer)
        + cast(func.jsonb_array_length(row.subjects) > 0, Integer)
        + cast(func.jsonb_array_length(row.creators) > 0, Integer)
        + cast(row.publication_date.is_not(None), Integer)
    )


def upsert_statement() -> Any:
    """Insert, or update the existing dataset with the same URL.

    A row is replaced by its own record, or by a strictly more complete record with the same URL
    (the same DOI harvested from two repositories), so the winner does not depend on the harvest
    order. Only rows whose content changed are updated, so updated_at and the embedding queue stay
    meaningful. Returns whether each written row was inserted (else updated).
    """
    stmt = pg_insert(Dataset)
    current, new = Dataset.__table__.c, stmt.excluded
    compared = [c.name for c in WRITTEN_COLUMNS if c.name != "source_updated_at"]
    embedded_unchanged = tuple_(*(current[c] for c in EMBEDDED_COLUMNS)).is_not_distinct_from(
        tuple_(*(new[c] for c in EMBEDDED_COLUMNS))
    )
    return stmt.on_conflict_do_update(
        index_elements=[Dataset.url],
        set_={
            **{c.name: new[c.name] for c in WRITTEN_COLUMNS if c.name != "url"},
            "updated_at": func.now(),
            "embedded_at": case((embedded_unchanged, current.embedded_at), else_=None),
        },
        where=tuple_(*(current[c] for c in compared)).is_distinct_from(tuple_(*(new[c] for c in compared)))
        & ((current.source_record_id == new.source_record_id) | (completeness(new) > completeness(current))),
    ).returning(literal_column("xmax = 0").label("inserted"))


class SkipRecordError(ValueError):
    """A record that cannot make a dataset, the message is the reason counted in the stats."""


@dataclass
class TransformStats:
    read: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    # Records merged into another one with the same URL
    duplicates: int = 0
    deleted: int = 0
    skipped: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "read": self.read,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "duplicates": self.duplicates,
            "deleted": self.deleted,
            "skipped": dict(self.skipped),
        }


# ---------- Cleaning helpers ----------


def clean_text(value: Any) -> str | None:
    """Collapse whitespace, None for anything that is not a non-empty string."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def clean_markup(value: Any) -> str | None:
    """Like clean_text, also removing HTML tags and entities, common in titles and descriptions."""
    if not isinstance(value, str):
        return None
    return clean_text(html.unescape(HTML_TAG_RE.sub(" ", value)))


def entries(datacite: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """A list field of the DataCite JSON, keeping only the objects."""
    value = datacite.get(key)
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def unique_by(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Deduplicate on one key, case insensitive, keeping the first occurrence."""
    seen: set[str] = set()
    result = []
    for item in items:
        k = str(item.get(key, "")).lower()
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


def normalize_doi(value: str | None) -> str | None:
    """Bare lowercase DOI from '10.x/y', 'doi:10.x/y' or any resolver URL. DOIs are case insensitive."""
    if not value:
        return None
    match = DOI_RE.search(value.strip())
    return match.group().rstrip(".").lower() if match else None


def lang(entry: dict[str, Any]) -> str | None:
    return value[:2].lower() if (value := clean_text(entry.get("lang"))) else None


def pick_title(titles: list[dict[str, Any]]) -> str | None:
    """Main title: untyped (not Alternative/Translated/...) and English first."""
    candidates = [
        (t.get("titleType") is not None, lang(t) not in (None, "en"), clean_markup(t.get("title"))) for t in titles
    ]
    candidates = [c for c in candidates if c[2]]
    return min(candidates, key=lambda c: c[:2])[2] if candidates else None


def is_prose(text: str) -> bool:
    """Whether a text reads as prose. Some repositories put machine metadata in descriptions (e.g.
    Onedata STAC JSON, ~25% letters), noise for search that also overflows the embedding model window.
    Real prose measured at 65% letters or more."""
    chars = [c for c in text if not c.isspace()]
    return sum(c.isalpha() for c in chars) >= MIN_LETTERS_RATIO * len(chars)


def clean_descriptions(descriptions: list[dict[str, Any]]) -> list[dict[str, str]]:
    """[{"text", "type", "lang"}], without empty, non prose, date notes and duplicate texts.

    Records with several descriptions mostly carry translations (SWISS, HAL, FINBIF...), notes
    (funding, formats, dates) or a text about the series, kept here with their type and language.
    """
    cleaned: dict[str, dict[str, str]] = {}
    for d in descriptions:
        text = clean_markup(d.get("description"))
        if not text or not is_prose(text) or (len(text) <= DATE_NOTE_MAX_CHARS and DATE_NOTE_RE.match(text)):
            continue
        item = cleaned.setdefault(text, {"text": text})
        # A duplicate text can bring the type or language the first one missed (FINBIF untagged + en)
        for key, value in (("type", clean_text(d.get("descriptionType"))), ("lang", lang(d))):
            if value and key not in item:
                item[key] = value
    return list(cleaned.values())


def main_description(descriptions: list[dict[str, str]]) -> str | None:
    """The description searched and displayed: all the texts of the best (type, language) pair.

    Type in DESCRIPTION_TYPES order, then English, untagged, other languages. Joining the texts of
    that pair keeps complementary paragraphs (DANS abstracts, EMPIAR TechnicalInfo parts) without
    translations nor notes.
    """
    if not descriptions:
        return None

    def rank(d: dict[str, str]) -> tuple[int, int]:
        type_rank = DESCRIPTION_TYPES.index(d["type"]) if d.get("type") in DESCRIPTION_TYPES else len(DESCRIPTION_TYPES)
        return type_rank, {"en": 0, None: 1}.get(d.get("lang"), 2)

    best = min(descriptions, key=rank)
    group = (best.get("type"), best.get("lang"))
    return "\n\n".join(d["text"] for d in descriptions if (d.get("type"), d.get("lang")) == group)


def clean_subjects(subjects: list[dict[str, Any]]) -> list[dict[str, str]]:
    cleaned = []
    for s in subjects:
        if name := clean_text(s.get("subject")):
            item = {"name": name}
            if scheme := clean_text(s.get("subjectScheme")):
                item["scheme"] = scheme
            if uri := clean_text(s.get("valueUri")):
                item["uri"] = uri
            cleaned.append(item)
    return unique_by(cleaned, "name")


def clean_identifier(identifier: dict[str, Any]) -> dict[str, str] | None:
    """A creator identifier, ORCIDs reduced to the bare id so they can be matched exactly."""
    value = clean_text(identifier.get("nameIdentifier"))
    if not value:
        return None
    scheme = clean_text(identifier.get("nameIdentifierScheme"))
    is_orcid = (scheme and scheme.upper() == "ORCID") or "orcid.org" in value
    if is_orcid and (match := ORCID_RE.search(value)):
        return {"scheme": "ORCID", "value": match.group()}
    return {"scheme": scheme or "unknown", "value": value}


def clean_creators(creators: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = []
    for c in creators:
        if not (name := clean_text(c.get("creatorName"))):
            continue
        item: dict[str, Any] = {"name": name}
        for source, target in (("givenName", "given_name"), ("familyName", "family_name"), ("nameType", "type")):
            if value := clean_text(c.get(source)):
                item[target] = value
        identifiers = [i for i in map(clean_identifier, entries(c, "nameIdentifiers")) if i]
        if identifiers:
            item["identifiers"] = unique_by(identifiers, "value")
        cleaned.append(item)
    return cleaned


def clean_dates(dates: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Dates are already normalized to yyyy-mm-dd by the warehouse, drop anything else."""
    cleaned = []
    for d in dates:
        value = clean_text(d.get("date"))
        try:
            date.fromisoformat(value or "")
        except ValueError:
            continue
        cleaned.append({"date": value, "type": clean_text(d.get("dateType")) or "Other"})
    return list({(d["date"], d["type"]): d for d in cleaned}.values())


def pick_publication_date(dates: list[dict[str, str]], year: int | None) -> date | None:
    """Best date for when the dataset was published, falling back to January 1st of its year."""
    by_type = {}
    for d in dates:
        by_type.setdefault(d["type"], d["date"])
    for date_type in PUBLICATION_DATE_TYPES:
        if value := by_type.get(date_type):
            return date.fromisoformat(value)
    return date(year, 1, 1) if year else None


def parse_year(value: Any) -> int | None:
    match = re.search(r"\d{4}", str(value)) if value is not None else None
    year = int(match.group()) if match else None
    return year if year and 1000 <= year <= 2100 else None


def clean_rights(rights: list[dict[str, Any]]) -> list[dict[str, str]]:
    cleaned = []
    for r in rights:
        item = {k: v for k, v in (("name", clean_text(r.get("rights"))), ("uri", clean_text(r.get("rightsURI")))) if v}
        if item:
            cleaned.append(item)
    return cleaned


def build_dataset(record: dict[str, Any]) -> Dataset:
    """Build the `dataset` row of one datasetdb record, raise SkipRecordError if it cannot make one."""
    datacite = record["datacite"] or {}
    record_url, datacite_url = clean_text(record["url"]), clean_text(datacite.get("url"))
    # The DataCite DOI (datasetdb has no DOI column since metadata-warehouse#171), else a doi.org URL
    doi = normalize_doi(datacite.get("doi")) or next(
        (d for u in (record_url, datacite_url) if u and "doi.org/" in u and (d := normalize_doi(u))), None
    )
    url = f"https://doi.org/{doi}" if doi else record_url or datacite_url
    if not url or not url.startswith(("http://", "https://")):
        raise SkipRecordError("no DOI nor http URL")
    titles = entries(datacite, "titles")
    title = pick_title(titles) or clean_markup(record["title"])
    if not title:
        raise SkipRecordError("no title")

    descriptions = clean_descriptions(entries(datacite, "descriptions"))
    subjects = entries(datacite, "subjects")
    dates = clean_dates(entries(datacite, "dates"))
    pub_date = pick_publication_date(dates, parse_year(datacite.get("publicationYear")))
    languages = sorted({code for e in [*titles, *descriptions, *subjects] if (code := lang(e))})
    formats = datacite.get("formats") if isinstance(datacite.get("formats"), list) else []
    return Dataset(
        url=url,
        doi=doi,
        title=title,
        description=main_description(descriptions),
        descriptions=descriptions,
        subjects=clean_subjects(subjects),
        creators=clean_creators(entries(datacite, "creators")),
        dates=dates,
        rights=clean_rights(entries(datacite, "rightsList")),
        publication_year=parse_year(datacite.get("publicationYear")) or (pub_date.year if pub_date else None),
        publication_date=pub_date,
        languages=languages,
        formats=list(dict.fromkeys(f for f in map(clean_text, formats) if f)),
        repository=record["repository"],
        harvest_url=record["harvest_url"],
        source_record_id=record["id"],
        source_updated_at=record["updated_at"],
    )


# ---------- Load ----------


def upsert(appdb: Connection, rows: list[Dataset], stats: TransformStats) -> None:
    """Upsert a batch in one transaction, keeping the most complete of the records sharing a URL."""
    by_url: dict[str, Dataset] = {}
    for row in sorted(rows, key=lambda r: (completeness(r), r.source_updated_at)):
        by_url[row.url] = row
    stats.duplicates += len(rows) - len(by_url)
    if not by_url:
        return
    values = [{c.name: getattr(row, c.key) for c in WRITTEN_COLUMNS} for row in by_url.values()]
    with appdb.begin():
        results = appdb.execute(upsert_statement(), values).all()
    inserted = sum(1 for (is_insert,) in results if is_insert)
    stats.inserted += inserted
    stats.updated += len(results) - inserted
    stats.unchanged += len(by_url) - len(results)


def has_datasets(settings: PipelineSettings, repositories: list[str] | None = None) -> bool:
    """Whether the dataset table holds any row, from the given repositories only when given."""
    condition = Dataset.repository.in_(repositories) if repositories else Dataset.id.is_not(None)
    with appdb_engine(settings).connect() as appdb:
        return bool(appdb.execute(select(exists().where(condition))).scalar())


def prune(appdb: Connection, seen_urls: set[str], repositories: list[str] | None = None) -> int:
    """Delete the datasets whose record is gone from datasetdb (embeddings cascade), only within
    `repositories` when given."""
    seen = table("seen_url", column("url"))
    stmt = delete(Dataset).where(~exists(select(seen.c.url).where(seen.c.url == Dataset.url)))
    if repositories:
        stmt = stmt.where(Dataset.repository.in_(repositories))
    with appdb.begin():
        appdb.execute(text("CREATE TEMP TABLE seen_url (url text PRIMARY KEY) ON COMMIT DROP"))
        appdb.execute(insert(seen), [{"url": u} for u in seen_urls])
        result = appdb.execute(stmt)
    return result.rowcount


def refresh_creator_names(settings: PipelineSettings) -> None:
    """Rebuild the creator_name view from dataset.creators, readable by the API meanwhile."""
    started = time.monotonic()
    # Autocommit: run as its own statement, not inside a transaction
    with appdb_engine(settings).connect().execution_options(isolation_level="AUTOCOMMIT") as appdb:
        appdb.execute(text("SET statement_timeout = 0"))
        appdb.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY creator_name"))
    logger.info(f"creator_name refreshed in {time.monotonic() - started:.0f}s")


def populate_appdb_dataset(
    settings: PipelineSettings,
    since: datetime | None = None,
    limit: int | None = None,
    repositories: list[str] | None = None,
) -> TransformStats:
    """Upsert the datasets of the records updated after `since` (all of them when None), from the
    given repository codes only (all repositories when None).

    A full run (no `since`, no `limit`) also deletes the datasets whose record no longer exists.
    Each batch is committed on its own, a failed run is resumed by the next one since `since`
    comes from the last successful run.
    """
    stats = TransformStats()
    seen_urls: set[str] = set()
    logger.info(
        f"Transforming the datasetdb records of {repositories or 'all repositories'} "
        f"updated after {since or 'the beginning'}"
    )
    with datasetdb_engine(settings).connect() as datasetdb, appdb_engine(settings).connect() as appdb:
        # Server side cursor, the records do not fit in memory
        records = datasetdb.execution_options(stream_results=True, max_row_buffer=READ_BATCH_SIZE).execute(
            SELECT_RECORDS_SQL,
            {"resource_types": list(RESOURCE_TYPES), "since": since, "repositories": repositories or None},
        )
        for batch in records.mappings().partitions(READ_BATCH_SIZE):
            rows = []
            for record in batch:
                stats.read += 1
                try:
                    rows.append(build_dataset(dict(record)))
                except SkipRecordError as e:
                    stats.skipped[str(e)] += 1
            upsert(appdb, rows, stats)
            seen_urls.update(r.url for r in rows)
            logger.info(f"{stats.read} records read: {stats.as_dict()}")
            if limit and stats.read >= limit:
                break
        records.close()
        # Guard against wiping the table when the source returned nothing
        if since is None and not limit and seen_urls:
            stats.deleted = prune(appdb, seen_urls, repositories)
    if stats.inserted or stats.updated or stats.deleted:
        refresh_creator_names(settings)
    logger.info(f"Transform done: {stats.as_dict()}")
    return stats
