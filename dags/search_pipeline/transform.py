"""Transform the datasetdb records into rows of the appdb `dataset` table and of its link tables.

The DataCite JSON in datasetdb has already been harmonized by the warehouse (see
metadata-warehouse src/utils/normalize_datacite_json.py), this only picks the fields search needs,
cleans them up, and gives each dataset a unique URL: https://doi.org/<doi> when it has a DOI.
"""

import hashlib
import html
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Connection,
    bindparam,
    case,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from data_commons_search.dataset_models import (
    Dataset,
    DatasetAgent,
    DatasetDate,
    DatasetRecord,
    DatasetSubject,
    Repository,
)
from search_pipeline.config import PipelineSettings
from search_pipeline.db import appdb_engine, datasetdb_engine

logger = logging.getLogger(__name__)

# datasetdb resource types that make a dataset
RESOURCE_TYPES = ("Dataset",)
# Records read from datasetdb per round trip
READ_BATCH_SIZE = 2000

DOI_RE = re.compile(r"10\.\d{4,9}/\S+")
# Not inside a longer id (an ISNI is 16 digits too). ORCIDs are also found without their hyphens
ORCID_RE = re.compile(r"(?<!\d)(\d{4})-?(\d{4})-?(\d{4})-?(\d{3}[\dX])(?![\dX])")
ROR_RE = re.compile(r"(?<![a-z0-9])0[a-hj-km-np-tv-z0-9]{6}\d{2}(?![a-z0-9])")
# Share of letters in the non-space characters of a description, below it is not prose
MIN_LETTERS_RATIO = 0.5
HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# Dates harvested as descriptions (e.g. DANS "Date Submitted: 2010-01-12"), already in `dates`
DATE_NOTE_RE = re.compile(r"^(date( \w+)?|issued|modified|available|created|submitted|updated)\s*:", re.IGNORECASE)
DATE_NOTE_MAX_CHARS = 100
# Longer is not the name of one creator or subject, and would not fit in the unique indexes on agent.name
# and subject.key (2704 bytes)
MAX_NAME_CHARS = 500
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
# datasetdb belongs to the warehouse, no model for it. Each repository is read from its own `since`
# bound (all its records when it has none), the global :min_since lets the BRIN index on updated_at skip
# the old blocks when every repository has a bound
SELECT_RECORDS_SQL = text(f"""
SELECT
    rec.id, rec.record_identifier, rec.url, rec.title, rec.updated_at,
    jsonb_build_object({", ".join(f"'{key}', rec.datacite_json -> '{key}'" for key in DATACITE_KEYS)}) AS datacite,
    repo.code AS repository, endp.harvest_url
FROM records rec
JOIN repositories repo ON repo.id = rec.repository_id
JOIN endpoints endp ON endp.id = rec.endpoint_id
LEFT JOIN unnest(CAST(:since_codes AS text[]), CAST(:since_dates AS timestamptz[])) AS since(code, updated_at)
    ON since.code = repo.code
WHERE rec.resource_type::text = ANY(:resource_types)
  AND rec.datacite_json IS NOT NULL
  AND repo.code = ANY(CAST(:codes AS text[]))
  AND (since.updated_at IS NULL OR rec.updated_at > since.updated_at)
  AND (CAST(:min_since AS timestamptz) IS NULL OR rec.updated_at > CAST(:min_since AS timestamptz))
""")  # noqa: S608
# Start of the read on the datasetdb clock (transaction start, before the records snapshot)
READ_STARTED_SQL = text("SELECT now()")
# Which of these records still exist, to take over the datasets of the deleted ones
SELECT_EXISTING_RECORDS_SQL = text("SELECT id FROM records WHERE id = ANY(CAST(:ids AS text[]))")
SELECT_REPOSITORIES_SQL = text("SELECT code, name, description, base_url AS url FROM repositories ORDER BY code")

# Agents and subjects of a batch of datasets: the new ones are inserted, then the links joined to them.
# An agent is identified by its ORCID, else its ROR, else its exact name (unique indexes of the agent table)
INSERT_AGENTS_SQL = text("""
INSERT INTO agent (name, type, orcid, ror)
SELECT name, type, orcid, ror FROM jsonb_to_recordset(:rows) AS v(name text, type text, orcid text, ror text)
ON CONFLICT DO NOTHING
""").bindparams(bindparam("rows", type_=JSONB))
# The links, and the names their agents are credited under (agent_name, searched by the creator filter)
INSERT_AGENT_LINKS_SQL = text("""
WITH links AS (
    INSERT INTO dataset_agent (dataset_id, position, agent_id, name, given_name, family_name, identifiers)
    SELECT v.dataset_id, v.position, coalesce(by_orcid.id, by_ror.id, by_name.id),
        v.name, v.given_name, v.family_name, v.identifiers
    FROM jsonb_to_recordset(:rows) AS v(
        dataset_id bigint, position int, name text, given_name text, family_name text,
        orcid text, ror text, identifiers jsonb
    )
    LEFT JOIN agent by_orcid ON by_orcid.orcid = v.orcid
    LEFT JOIN agent by_ror ON v.orcid IS NULL AND by_ror.orcid IS NULL AND by_ror.ror = v.ror
    LEFT JOIN agent by_name
        ON v.orcid IS NULL AND v.ror IS NULL AND by_name.orcid IS NULL AND by_name.ror IS NULL AND by_name.name = v.name
    RETURNING agent_id, name
)
INSERT INTO agent_name (agent_id, name)
SELECT DISTINCT agent_id, name FROM links
ON CONFLICT DO NOTHING
""").bindparams(bindparam("rows", type_=JSONB))
INSERT_SUBJECTS_SQL = text("""
INSERT INTO subject (name)
SELECT name FROM jsonb_to_recordset(:rows) AS v(name text)
ON CONFLICT (key) DO NOTHING
""").bindparams(bindparam("rows", type_=JSONB))
# Two names of a dataset can share a key ("Ecologie", "Écologie"), the first is kept
INSERT_SUBJECT_LINKS_SQL = text("""
INSERT INTO dataset_subject (dataset_id, subject_id, name, scheme, uri)
SELECT v.dataset_id, subject.id, v.name, v.scheme, v.uri
FROM jsonb_to_recordset(:rows) AS v(dataset_id bigint, name text, scheme text, uri text)
JOIN subject ON subject.key = subject_key(v.name)
ON CONFLICT DO NOTHING
""").bindparams(bindparam("rows", type_=JSONB))
INSERT_DATES_SQL = text("""
INSERT INTO dataset_date (dataset_id, type, date)
SELECT dataset_id, type, date FROM jsonb_to_recordset(:rows) AS v(dataset_id bigint, type text, date date)
""").bindparams(bindparam("rows", type_=JSONB))
# The records of a batch of datasets, moved to the dataset of their URL when it changed
UPSERT_RECORDS_SQL = text("""
INSERT INTO dataset_record (record_id, dataset_id, repository, harvest_url, record_identifier)
SELECT record_id, dataset_id, repository, harvest_url, record_identifier
FROM jsonb_to_recordset(:rows)
    AS v(record_id text, dataset_id bigint, repository text, harvest_url text, record_identifier text)
ON CONFLICT (record_id) DO UPDATE SET
    dataset_id = EXCLUDED.dataset_id, repository = EXCLUDED.repository,
    harvest_url = EXCLUDED.harvest_url, record_identifier = EXCLUDED.record_identifier
WHERE (dataset_record.dataset_id, dataset_record.repository, dataset_record.harvest_url,
        dataset_record.record_identifier)
    IS DISTINCT FROM (EXCLUDED.dataset_id, EXCLUDED.repository, EXCLUDED.harvest_url, EXCLUDED.record_identifier)
""").bindparams(bindparam("rows", type_=JSONB))
# Agents (their names cascade), names and subjects no dataset links to anymore, after updates and deletions
DELETE_ORPHANS_SQL = {
    "agents": text("DELETE FROM agent WHERE NOT EXISTS (SELECT 1 FROM dataset_agent da WHERE da.agent_id = agent.id)"),
    "agent names": text("""
DELETE FROM agent_name WHERE NOT EXISTS (
    SELECT 1 FROM dataset_agent da WHERE da.agent_id = agent_name.agent_id AND da.name = agent_name.name
)"""),
    "subjects": text(
        "DELETE FROM subject WHERE NOT EXISTS (SELECT 1 FROM dataset_subject ds WHERE ds.subject_id = subject.id)"
    ),
}

# Columns set by the transform, the others are generated or managed by the pipeline
MANAGED_COLUMNS = ("created_at", "updated_at", "embedded_at")
WRITTEN_COLUMNS = [
    c for c in Dataset.__table__.c if c.identity is None and c.computed is None and c.name not in MANAGED_COLUMNS
]
# Fields the embeddings are computed from: a change queues the dataset for re-embedding
EMBEDDED_COLUMNS = ("title", "keywords", "description")


def links_hash(*lists: list[dict[str, Any]]) -> str:
    """Hash of the lists a record writes to the link tables, stored on its dataset."""
    return hashlib.md5(json.dumps(lists, sort_keys=True).encode(), usedforsecurity=False).hexdigest()


def upsert_statement(takeover: bool = False) -> Any:
    """Insert, or update the existing dataset with the same URL.

    A row is replaced by its own record, or by a strictly more complete record with the same URL
    (the same DOI harvested from two repositories), so the winner does not depend on the harvest
    order. With `takeover` by any record: the one the row was built from left the dataset. Only rows whose
    content changed are updated (links_hash stands for the link tables), so updated_at and the
    embedding queue stay meaningful. Returns the id and URL of each written row, and whether it was
    inserted (else updated).
    """
    stmt = pg_insert(Dataset)
    current, new = Dataset.__table__.c, stmt.excluded
    compared = [c.name for c in WRITTEN_COLUMNS if c.name != "source_updated_at"]
    embedded_unchanged = tuple_(*(current[c] for c in EMBEDDED_COLUMNS)).is_not_distinct_from(
        tuple_(*(new[c] for c in EMBEDDED_COLUMNS))
    )
    where = tuple_(*(current[c] for c in compared)).is_distinct_from(tuple_(*(new[c] for c in compared)))
    if not takeover:
        where &= (current.source_record_id == new.source_record_id) | (new.completeness > current.completeness)
    return stmt.on_conflict_do_update(
        index_elements=[Dataset.url],
        set_={
            **{c.name: new[c.name] for c in WRITTEN_COLUMNS if c.name != "url"},
            "updated_at": func.now(),
            "embedded_at": case((embedded_unchanged, current.embedded_at), else_=None),
        },
        where=where,
    ).returning(Dataset.id, Dataset.url, literal_column("xmax = 0").label("inserted"))


class SkipRecordError(ValueError):
    """A record that cannot make a dataset, the message is the reason counted in the stats."""


@dataclass
class BuiltRecord:
    """The `dataset` row of a record, its identifier in its repository and its lists written to the link tables."""

    dataset: Dataset
    # Endpoint the record was harvested from, and its identifier there
    harvest_url: str | None
    record_identifier: str
    # [{"name", "given_name", "family_name", "type", "identifiers": [{"scheme", "value"}]}]
    creators: list[dict[str, Any]]
    # [{"name", "scheme", "uri"}]
    subjects: list[dict[str, Any]]
    # [{"date": "yyyy-mm-dd", "type"}]
    dates: list[dict[str, Any]]


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
    # Repositories read and when the read started, their next `since` once the run completed (not in as_dict)
    repositories: list[str] = field(default_factory=list)
    read_started: datetime | None = None

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


def clean_subjects(subjects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = []
    for s in subjects:
        if (name := clean_text(s.get("subject"))) and len(name) <= MAX_NAME_CHARS:
            item = {"name": name}
            if scheme := clean_text(s.get("subjectScheme")):
                item["scheme"] = scheme
            if uri := clean_text(s.get("valueUri")):
                item["uri"] = uri
            cleaned.append(item)
    return unique_by(cleaned, "name")


def clean_identifier(identifier: dict[str, Any]) -> dict[str, str] | None:
    """A creator identifier. ORCIDs and RORs are reduced to the bare id, the identity of their agent: one
    that does not read as such (a placeholder, another id under that scheme) is dropped, it would merge
    unrelated creators into one agent."""
    value = clean_text(identifier.get("nameIdentifier"))
    if not value:
        return None
    scheme = clean_text(identifier.get("nameIdentifierScheme"))
    claimed = (scheme or "").upper()
    if claimed == "ORCID" or "orcid.org" in value.lower():
        match = ORCID_RE.search(value.upper())
        return {"scheme": "ORCID", "value": "-".join(match.groups())} if match else None
    if claimed == "ROR" or "ror.org" in value.lower():
        match = ROR_RE.search(value.lower())
        return {"scheme": "ROR", "value": match.group()} if match else None
    return {"scheme": scheme or "unknown", "value": value}


def clean_creators(creators: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned = []
    for c in creators:
        name = clean_text(c.get("creatorName"))
        if not name or len(name) > MAX_NAME_CHARS:
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


def clean_dates(dates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dates are already normalized to yyyy-mm-dd by the warehouse, drop anything else."""
    cleaned = []
    for d in dates:
        value = clean_text(d.get("date")) or ""
        try:
            date.fromisoformat(value)
        except ValueError:
            continue
        cleaned.append({"date": value, "type": clean_text(d.get("dateType")) or "Other"})
    return list({(d["date"], d["type"]): d for d in cleaned}.values())


def pick_publication_date(dates: list[dict[str, Any]], year: int | None) -> date | None:
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


def build_dataset(record: dict[str, Any]) -> BuiltRecord:
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
    raw_subjects = entries(datacite, "subjects")
    subjects = clean_subjects(raw_subjects)
    creators = clean_creators(entries(datacite, "creators"))
    dates = clean_dates(entries(datacite, "dates"))
    description = main_description(descriptions)
    pub_date = pick_publication_date(dates, parse_year(datacite.get("publicationYear")))
    languages = sorted({code for e in [*titles, *descriptions, *raw_subjects] if (code := lang(e))})
    formats = value if isinstance(value := datacite.get("formats"), list) else []
    dataset = Dataset(
        url=url,
        doi=doi,
        title=title,
        description=description,
        descriptions=descriptions,
        keywords=", ".join(s["name"] for s in subjects) or None,
        completeness=sum(map(bool, (description, subjects, creators, pub_date))),
        links_hash=links_hash(creators, subjects, dates),
        rights=clean_rights(entries(datacite, "rightsList")),
        publication_year=parse_year(datacite.get("publicationYear")) or (pub_date.year if pub_date else None),
        publication_date=pub_date,
        languages=languages,
        formats=list(dict.fromkeys(f for f in map(clean_text, formats) if f)),
        harvested_from=record["repository"],
        source_record_id=record["id"],
        source_updated_at=record["updated_at"],
    )
    return BuiltRecord(dataset, record["harvest_url"], record["record_identifier"], creators, subjects, dates)


# ---------- Load ----------


def upsert(appdb: Connection, datasetdb: Connection, rows: list[BuiltRecord], stats: TransformStats) -> None:
    """Upsert a batch and its links in one transaction, keeping the most complete of the records sharing a URL."""
    by_url: dict[str, BuiltRecord] = {}
    for row in sorted(rows, key=lambda r: (r.dataset.completeness, r.dataset.source_updated_at)):
        by_url[row.dataset.url] = row
    stats.duplicates += len(rows) - len(by_url)
    if not by_url:
        return
    values = {url: {c.name: getattr(row.dataset, c.key) for c in WRITTEN_COLUMNS} for url, row in by_url.items()}
    with appdb.begin():
        results = list(appdb.execute(upsert_statement(), list(values.values())))
        # URLs kept by the record of another repository: taken over when that record left the dataset,
        # which would keep its content forever
        unwritten = by_url.keys() - {r.url for r in results}
        # Whether the record a dataset was built from is still one of its records: not when it moved to
        # another URL, or when a full run stopped reading it (no longer a Dataset, skipped)
        owned = exists().where(
            DatasetRecord.record_id == Dataset.source_record_id, DatasetRecord.dataset_id == Dataset.id
        )
        select_kept = select(Dataset.id, Dataset.url, Dataset.source_record_id, owned.label("owned")).where(
            Dataset.url.in_(unwritten)
        )
        kept = list(appdb.execute(select_kept)) if unwritten else []
        owners = {d.url: d for d in kept if d.source_record_id != by_url[d.url].dataset.source_record_id}
        # Records deleted from datasetdb keep their dataset_record row until the end of a full run
        deleted: set[str] = set()
        if linked := {d.source_record_id for d in owners.values() if d.owned}:
            deleted = linked - set(datasetdb.execute(SELECT_EXISTING_RECORDS_SQL, {"ids": list(linked)}).scalars())
        if deleted:
            appdb.execute(delete(DatasetRecord).where(DatasetRecord.record_id.in_(deleted)))
        if orphaned := [values[url] for url, d in owners.items() if not d.owned or d.source_record_id in deleted]:
            results += appdb.execute(upsert_statement(takeover=True), orphaned)
        # Every written row is now the dataset of its record, its links are replaced
        write_links(appdb, [(r.id, by_url[r.url]) for r in results])
        # Every record of the batch is a record of the dataset of its URL, whichever record made it
        dataset_ids = {d.url: d.id for d in (*kept, *results)}
        records = [
            {
                "record_id": row.dataset.source_record_id,
                "dataset_id": dataset_ids[row.dataset.url],
                "repository": row.dataset.harvested_from,
                "harvest_url": row.harvest_url,
                "record_identifier": row.record_identifier,
            }
            for row in rows
        ]
        appdb.execute(UPSERT_RECORDS_SQL, {"rows": records})
    inserted = sum(1 for r in results if r.inserted)
    stats.inserted += inserted
    stats.updated += len(results) - inserted
    stats.unchanged += len(by_url) - len(results)


def write_links(appdb: Connection, datasets: list[tuple[int, BuiltRecord]]) -> None:
    """Replace the agents, subjects and dates of these (id, record) datasets."""
    if not datasets:
        return
    agents, subjects, dates = [], [], []
    for dataset_id, row in datasets:
        for position, creator in enumerate(row.creators):
            # First identifier of each scheme
            ids = {i["scheme"]: i["value"] for i in reversed(creator.get("identifiers", []))}
            agents.append(
                {
                    "dataset_id": dataset_id,
                    "position": position,
                    "name": creator["name"],
                    "given_name": creator.get("given_name"),
                    "family_name": creator.get("family_name"),
                    "type": creator.get("type"),
                    "orcid": ids.get("ORCID"),
                    "ror": ids.get("ROR"),
                    "identifiers": creator.get("identifiers"),
                }
            )
        subjects.extend({"dataset_id": dataset_id, **subject} for subject in row.subjects)
        dates.extend({"dataset_id": dataset_id, "type": d["type"], "date": d["date"]} for d in row.dates)
    ids = [dataset_id for dataset_id, _ in datasets]
    for model in (DatasetAgent, DatasetSubject, DatasetDate):
        appdb.execute(delete(model).where(model.dataset_id.in_(ids)))
    if agents:
        appdb.execute(INSERT_AGENTS_SQL, {"rows": agents})
        appdb.execute(INSERT_AGENT_LINKS_SQL, {"rows": agents})
    if subjects:
        appdb.execute(INSERT_SUBJECTS_SQL, {"rows": subjects})
        appdb.execute(INSERT_SUBJECT_LINKS_SQL, {"rows": subjects})
    if dates:
        appdb.execute(INSERT_DATES_SQL, {"rows": dates})


def has_datasets(settings: PipelineSettings) -> bool:
    """Whether the dataset table holds any row."""
    with appdb_engine(settings).connect() as appdb:
        return bool(appdb.execute(select(exists().where(Dataset.id.is_not(None)))).scalar())


def prune(appdb: Connection, seen_records: set[str], repositories: list[str] | None = None) -> int:
    """Delete the records gone from datasetdb, of `repositories` only when given, then the datasets left
    without any record (links and embeddings cascade). Returns the number of datasets deleted."""
    seen = table("seen_record", column("record_id"))
    gone_records = delete(DatasetRecord).where(
        ~exists(select(seen.c.record_id).where(seen.c.record_id == DatasetRecord.record_id))
    )
    # Whatever the repository it was harvested from: its last record can be the one of another repository
    orphans = delete(Dataset).where(~exists().where(DatasetRecord.dataset_id == Dataset.id))
    if repositories:
        gone_records = gone_records.where(DatasetRecord.repository.in_(repositories))
    with appdb.begin():
        appdb.execute(text("CREATE TEMP TABLE seen_record (record_id text PRIMARY KEY) ON COMMIT DROP"))
        appdb.execute(insert(seen), [{"record_id": r} for r in seen_records])
        appdb.execute(gone_records)
        result = appdb.execute(orphans)
    return result.rowcount


def sync_repositories(appdb: Connection, repositories: list[dict[str, Any]]) -> None:
    """Copy the datasetdb repositories, before the datasets referencing them."""
    stmt = pg_insert(Repository).values(repositories)
    with appdb.begin():
        appdb.execute(
            stmt.on_conflict_do_update(
                index_elements=[Repository.code],
                set_={c: stmt.excluded[c] for c in ("name", "description", "url")},
            )
        )


def delete_orphans(settings: PipelineSettings) -> None:
    """Delete the agents, agent names and subjects no dataset links to anymore."""
    started = time.monotonic()
    with appdb_engine(settings).begin() as appdb:
        deleted = {name: appdb.execute(stmt).rowcount for name, stmt in DELETE_ORPHANS_SQL.items()}
    logger.info(f"Orphans deleted in {time.monotonic() - started:.0f}s: {deleted}")


def populate_appdb_dataset(
    settings: PipelineSettings,
    since: dict[str, datetime] | None = None,
    limit: int | None = None,
    repositories: list[str] | None = None,
) -> TransformStats:
    """Upsert the datasets of the records of each repository updated after its `since` date (all
    its records when it has none), from the given repository codes only (all repositories when None).

    A full run (no `since`, no `limit`) also deletes the datasets whose record no longer exists.
    Each batch is committed on its own. `stats.read_started` is the next `since` of
    `stats.repositories`, to save only once the run completed without `limit`.
    """
    since = since or {}
    stats = TransformStats()
    seen_records: set[str] = set()
    with datasetdb_engine(settings).connect() as datasetdb, appdb_engine(settings).connect() as appdb:
        # Same transaction as the records read below, now() is the start of its snapshot
        stats.read_started = datasetdb.execute(READ_STARTED_SQL).scalar()
        all_repositories = [dict(r) for r in datasetdb.execute(SELECT_REPOSITORIES_SQL).mappings()]
        if all_repositories:
            sync_repositories(appdb, all_repositories)
        all_codes = [r["code"] for r in all_repositories]
        stats.repositories = [c for c in all_codes if not repositories or c in repositories]
        if unknown := set(repositories or []) - set(stats.repositories):
            logger.warning(f"Unknown repository codes, ignored: {sorted(unknown)}")
        # The global bound only applies when every repository has its own
        bounds = [since.get(code) for code in stats.repositories]
        min_since = min((b for b in bounds if b), default=None) if all(bounds) else None
        logger.info(
            f"Transforming the datasetdb records of {repositories or 'all repositories'}, "
            f"updated after {min_since or 'the beginning'} at the earliest"
        )
        # Server side cursor, the records do not fit in memory
        records = datasetdb.execution_options(stream_results=True, max_row_buffer=READ_BATCH_SIZE).execute(
            SELECT_RECORDS_SQL,
            {
                "resource_types": list(RESOURCE_TYPES),
                "codes": stats.repositories,
                "since_codes": list(since),
                "since_dates": list(since.values()),
                "min_since": min_since,
            },
        )
        for batch in records.mappings().partitions(READ_BATCH_SIZE):
            rows = []
            for record in batch:
                stats.read += 1
                try:
                    rows.append(build_dataset(dict(record)))
                except SkipRecordError as e:
                    stats.skipped[str(e)] += 1
            upsert(appdb, datasetdb, rows, stats)
            seen_records.update(r.dataset.source_record_id for r in rows)
            logger.info(f"{stats.read} records read: {stats.as_dict()}")
            if limit and stats.read >= limit:
                break
        records.close()
        # Only when every selected repository was read in full. Guard against wiping the table when
        # the source returned nothing
        if not any(bounds) and not limit and seen_records:
            stats.deleted = prune(appdb, seen_records, repositories)
    if stats.updated or stats.deleted:
        delete_orphans(settings)
    logger.info(f"Transform done: {stats.as_dict()}")
    return stats
