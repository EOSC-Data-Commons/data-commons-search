"""Tests of the transform against a real PostgreSQL: agents and subjects deduplication, link tables,
records sharing a URL.

Skipped unless TEST_PIPELINE_DSN is the libpq DSN of an empty throwaway database with the unaccent,
pg_trgm and vector extensions. That database plays both datasetdb (a minimal copy of the tables the
transform reads) and appdb, its tables are dropped after each test.
"""

import os
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from data_commons_search import dataset_models
from data_commons_search.dataset_search import _filter_params, _filtered_ids, _match_ids, _match_links
from data_commons_search.models import SearchFilters
from search_pipeline.config import PipelineSettings
from search_pipeline.db import appdb_engine
from search_pipeline.transform import TransformStats, populate_appdb_dataset

DSN = os.environ.get("TEST_PIPELINE_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_PIPELINE_DSN not set")
SETTINGS = PipelineSettings(appdb_dsn=DSN, datasetdb_dsn=DSN)

# The datasetdb tables and columns SELECT_RECORDS_SQL and SELECT_REPOSITORIES_SQL read
DATASETDB_DDL = (
    "CREATE TABLE repositories (id int PRIMARY KEY, code text, name text, description text, base_url text)",
    "CREATE TABLE endpoints (id int PRIMARY KEY, harvest_url text)",
    """CREATE TABLE records (
        id text PRIMARY KEY, record_identifier text, url text, title text, updated_at timestamptz DEFAULT now(),
        datacite_json jsonb, repository_id int, endpoint_id int DEFAULT 1, resource_type text DEFAULT 'Dataset'
    )""",
    "INSERT INTO repositories VALUES (1, 'ZENODO', 'Zenodo', NULL, 'https://zenodo.org'), (2, 'DANS', 'DANS', NULL, NULL)",
    "INSERT INTO endpoints VALUES (1, 'https://example.org/oai')",
)
DROP_DATASETDB_DDL = "DROP TABLE IF EXISTS records, endpoints, repositories CASCADE"
UPSERT_RECORD_SQL = text("""
INSERT INTO records (id, record_identifier, url, title, datacite_json, repository_id)
VALUES (:id, 'oai:' || CAST(:id AS text), :url, 'Record title', :datacite, :repo)
ON CONFLICT (id) DO UPDATE SET datacite_json = EXCLUDED.datacite_json, updated_at = now()
""").bindparams(bindparam("datacite", type_=JSONB))

JANE_ORCID = "0000-0002-1825-0097"
ROR = "05gq02987"


def execute(sql: Any, params: dict[str, Any] | None = None) -> list[Any]:
    """Run one statement in its own transaction, returning its rows if any."""
    with appdb_engine(SETTINGS).begin() as conn:
        result = conn.execute(text(sql) if isinstance(sql, str) else sql, params or {})
        return list(result) if result.returns_rows else []


def scalars(sql: str) -> list[Any]:
    return [row[0] for row in execute(sql)]


def create_appdb_tables() -> None:
    with appdb_engine(SETTINGS).begin() as conn:
        for ddl in dataset_models.FUNCTIONS_DDL:
            conn.execute(text(ddl))
        dataset_models.Base.metadata.create_all(conn)


@pytest.fixture()
def datasetdb() -> Iterator[None]:
    """The datasetdb tables, in a database that must hold nothing of the pipeline."""
    if execute("SELECT to_regclass('dataset') IS NOT NULL OR to_regclass('records') IS NOT NULL")[0][0]:
        pytest.fail("TEST_PIPELINE_DSN must be an empty throwaway database, it has a dataset or a records table")
    try:
        for ddl in DATASETDB_DDL:
            execute(ddl)
        yield
    finally:
        with appdb_engine(SETTINGS).begin() as conn:
            dataset_models.Base.metadata.drop_all(conn)
            conn.execute(text(DROP_DATASETDB_DDL))


@pytest.fixture()
def appdb(datasetdb: None) -> None:
    """The current appdb tables next to the datasetdb ones."""
    create_appdb_tables()


def add_record(record_id: str, repo: int = 1, doi: str | None = None, **datacite: Any) -> None:
    """Insert or replace a datasetdb record, identified by oai:<id> in its repository. Its dataset URL
    is the DOI one, else https://example.org/<id>."""
    datacite = {"titles": [{"title": "Record title"}], "doi": doi, **datacite}
    params = {"id": record_id, "url": f"https://example.org/{record_id}", "datacite": datacite, "repo": repo}
    execute(UPSERT_RECORD_SQL, params)


def creator(name: str, identifier: str | None = None, scheme: str | None = None, **fields: str) -> dict[str, Any]:
    ids = [{"nameIdentifier": identifier, "nameIdentifierScheme": scheme}] if identifier else []
    return {"creatorName": name, "nameIdentifiers": ids, **fields}


def filtered(**filters: Any) -> list[str]:
    """Source records of the datasets passing these hard filters of the search."""
    search_filters = SearchFilters(**filters)
    params = _filter_params(search_filters)
    with appdb_engine(SETTINGS).connect() as conn:
        linked = _match_links(conn, search_filters, params)
        if linked is None:
            return []
        ids = conn.execute(_filtered_ids(search_filters, linked), params).scalars().all()
        owners = text("SELECT source_record_id FROM dataset WHERE id = ANY(:ids) ORDER BY 1")
        return list(conn.execute(owners, {"ids": ids}).scalars())


def transform() -> TransformStats:
    """A full run: every record read, the datasets without record deleted."""
    return populate_appdb_dataset(SETTINGS)


def test_agents_are_one_per_orcid_ror_or_name(appdb: None) -> None:
    add_record(
        "r1",
        creators=[
            creator("Doe, Jane", f"https://orcid.org/{JANE_ORCID}"),
            creator("ETH Zurich", f"https://ror.org/{ROR}", nameType="Organizational"),
            creator("Smith, John"),
        ],
    )
    add_record(
        "r2",
        creators=[
            creator("Jane Doe", JANE_ORCID, "ORCID"),
            creator("ETH Zürich", ROR.upper(), "ROR"),
            creator("Smith, John"),
            # A placeholder is not an identity, this creator is the agent of its name
            creator("Smith, John", "N/A", "ORCID"),
            creator("Smith, John", "0000 0001 2183 9049", "ISNI"),
        ],
    )
    assert transform().inserted == 2

    assert execute("SELECT name, type, orcid, ror FROM agent ORDER BY name") == [
        ("Doe, Jane", None, JANE_ORCID, None),
        ("ETH Zurich", "Organizational", None, ROR),
        ("Smith, John", None, None, None),
    ]
    # Every name an agent is credited under is searchable
    names = "SELECT agent.name, agent_name.name FROM agent_name JOIN agent ON agent.id = agent_id ORDER BY 1, 2"
    assert execute(names) == [
        ("Doe, Jane", "Doe, Jane"),
        ("Doe, Jane", "Jane Doe"),
        ("ETH Zurich", "ETH Zurich"),
        ("ETH Zurich", "ETH Zürich"),
        ("Smith, John", "Smith, John"),
    ]
    # The links keep the order, the name and the identifiers of the record
    links = """
        SELECT position, dataset_agent.name, agent.name, identifiers
        FROM dataset_agent JOIN agent ON agent.id = agent_id JOIN dataset ON dataset.id = dataset_id
        WHERE source_record_id = 'r2' ORDER BY position
    """
    assert execute(links) == [
        (0, "Jane Doe", "Doe, Jane", [{"scheme": "ORCID", "value": JANE_ORCID}]),
        (1, "ETH Zürich", "ETH Zurich", [{"scheme": "ROR", "value": ROR}]),
        (2, "Smith, John", "Smith, John", None),
        (3, "Smith, John", "Smith, John", None),
        (4, "Smith, John", "Smith, John", [{"scheme": "ISNI", "value": "0000 0001 2183 9049"}]),
    ]


def test_creator_filter_matches_every_agent_of_the_close_names(appdb: None) -> None:
    # More agents sharing a name key than the candidates of the trigram search
    people = [creator("Wang, Wei" if i % 2 else "Wei Wang", f"0000-0001-0000-{i:04d}", "ORCID") for i in range(30)]
    add_record("r1", creators=[*people, creator("Wang, Wei"), creator("Doe, Jane")])
    add_record("r2", creators=[creator("ETH Zurich", ROR, "ROR")])
    add_record("r3", creators=[creator("Eidgenössische Technische Hochschule Zürich", ROR, "ROR")])
    transform()

    def matched(name: str) -> set[str]:
        row = execute(_match_ids(SearchFilters(creator_name=name)), {"creator": name})[0]
        ids = row.agent_ids or []
        return {
            f"{n} {orcid or ror or ''}".strip()
            for n, orcid, ror in execute(text("SELECT name, orcid, ror FROM agent WHERE id = ANY(:ids)"), {"ids": ids})
        }

    assert len(matched("Wei Wang")) == 31
    # An agent is found by any of the names its records use
    assert matched("ETH Zurich") == matched("Eidgenossische Technische Hochschule Zurich") == {f"ETH Zurich {ROR}"}
    assert matched("Nobody Atall") == set()


def test_subjects_are_one_per_key_and_keep_what_the_record_wrote(appdb: None) -> None:
    add_record(
        "r1",
        subjects=[
            {"subject": "Ecology", "subjectScheme": "FOS", "valueUri": "https://example.org/fos/ecology"},
            {"subject": "C"},
            {"subject": "C++"},
        ],
    )
    add_record("r2", subjects=[{"subject": "ecology"}, {"subject": "Écologie"}, {"subject": "Ecologie"}])
    transform()

    assert sorted(scalars("SELECT key FROM subject")) == ["c", "c++", "ecologie", "ecology"]
    links = """
        SELECT source_record_id, dataset_subject.name, scheme, uri, subject.key
        FROM dataset_subject JOIN subject ON subject.id = subject_id JOIN dataset ON dataset.id = dataset_id
        ORDER BY 1, 5
    """
    assert execute(links) == [
        ("r1", "C", None, None, "c"),
        ("r1", "C++", None, None, "c++"),
        ("r1", "Ecology", "FOS", "https://example.org/fos/ecology", "ecology"),
        # Two names of a record with the same key make one link, the first
        ("r2", "Écologie", None, None, "ecologie"),
        ("r2", "ecology", None, None, "ecology"),
    ]
    assert scalars("SELECT keywords FROM dataset WHERE source_record_id = 'r2'") == ["ecology, Écologie, Ecologie"]


def test_a_change_of_the_links_updates_the_dataset(appdb: None) -> None:
    add_record("r1", creators=[creator("Doe, Jane"), creator("Smith, John")], subjects=[{"subject": "Ecology"}])
    assert transform().inserted == 1
    execute("UPDATE dataset SET embedded_at = now()")

    # Read again unchanged, nothing is written
    link_rows = "SELECT ctid FROM dataset_agent ORDER BY position"
    before = scalars(link_rows)
    stats = transform()
    assert (stats.unchanged, stats.updated) == (1, 0)
    assert scalars(link_rows) == before

    # Only the creators change: the dataset is updated, not queued for embedding, the orphans go
    add_record("r1", creators=[creator("Doe, Jane"), creator("Roe, Richard")], subjects=[{"subject": "Ecology"}])
    assert transform().updated == 1
    assert scalars("SELECT name FROM dataset_agent ORDER BY position") == ["Doe, Jane", "Roe, Richard"]
    assert sorted(scalars("SELECT name FROM agent")) == ["Doe, Jane", "Roe, Richard"]
    assert sorted(scalars("SELECT name FROM agent_name")) == ["Doe, Jane", "Roe, Richard"]
    assert scalars("SELECT embedded_at IS NOT NULL AND updated_at > created_at FROM dataset") == [True]

    # A new subject changes the keywords, which are embedded
    add_record("r1", creators=[creator("Doe, Jane")], subjects=[{"subject": "Botany"}])
    transform()
    assert scalars("SELECT key FROM subject") == ["botany"]
    assert scalars("SELECT embedded_at FROM dataset") == [None]


def test_records_sharing_a_url(appdb: None) -> None:
    owner = "SELECT source_record_id, harvested_from, completeness FROM dataset"
    # The repositories a dataset was found in, with its identifier there
    records = """
        SELECT dataset.url, dataset_record.repository, record_identifier, dataset_record.harvest_url
        FROM dataset_record JOIN dataset ON dataset.id = dataset_id ORDER BY 1, 2
    """
    url, oai = "https://doi.org/10.1234/shared", "https://example.org/oai"
    description = [{"description": "An abstract of this dataset.", "descriptionType": "Abstract"}]
    add_record("zenodo-1", repo=1, doi="10.1234/shared", descriptions=description)
    transform()
    assert execute(owner) == [("zenodo-1", "ZENODO", 1)]
    assert execute(records) == [(url, "ZENODO", "oai:zenodo-1", oai)]

    # The same DOI from another repository, more complete by its creators, takes the dataset and its links
    add_record("dans-1", repo=2, doi="10.1234/shared", descriptions=description, creators=[creator("Doe, Jane")])
    stats = transform()
    assert (stats.updated, stats.duplicates) == (1, 1)
    assert execute(owner) == [("dans-1", "DANS", 2)]
    assert scalars("SELECT name FROM dataset_agent") == ["Doe, Jane"]
    assert execute(records) == [(url, "DANS", "oai:dans-1", oai), (url, "ZENODO", "oai:zenodo-1", oai)]
    # The repository filter finds it in both, whichever record made it
    add_record("zenodo-2", repo=1)
    transform()
    assert filtered(repositories=["zenodo"]) == ["dans-1", "zenodo-2"]
    assert filtered(repositories=["DANS"]) == filtered(repositories=["DANS", "ZENODO"], creator_name="Jane Doe")
    assert filtered(repositories=["DANS"], published_before="2100-01-01") == []
    assert filtered(repositories=["DANS"]) == ["dans-1"]
    assert filtered(repositories=["UNKNOWN"]) == []
    execute("DELETE FROM records WHERE id = 'zenodo-2'")

    # A record that is not more complete does not take it back
    add_record("zenodo-1", repo=1, doi="10.1234/shared", descriptions=description, subjects=[{"subject": "Ecology"}])
    transform()
    assert scalars("SELECT source_record_id FROM dataset") == ["dans-1"]
    assert scalars("SELECT count(*) FROM dataset_subject") == [0]
    assert scalars("SELECT count(*) FROM dataset_record") == [2]

    # Until that record is gone from datasetdb
    execute("DELETE FROM records WHERE id = 'dans-1'")
    assert transform().updated == 1
    assert execute(owner) == [("zenodo-1", "ZENODO", 2)]
    assert scalars("SELECT name FROM dataset_subject") == ["Ecology"]
    assert scalars("SELECT count(*) FROM dataset_agent") == [0]
    assert scalars("SELECT count(*) FROM agent") == [0]
    assert execute(records) == [(url, "ZENODO", "oai:zenodo-1", oai)]
    assert (filtered(repositories=["DANS"]), filtered(repositories=["ZENODO"])) == ([], ["zenodo-1"])

    # A record whose DOI changes moves to the dataset of its new URL, the other dataset stays with its record
    add_record("dans-2", repo=2, doi="10.1234/shared")
    transform()
    assert len(execute(records)) == 2
    add_record("dans-2", repo=2, doi="10.1234/moved")
    assert transform().inserted == 1
    assert execute(records) == [
        ("https://doi.org/10.1234/moved", "DANS", "oai:dans-2", oai),
        (url, "ZENODO", "oai:zenodo-1", oai),
    ]
    execute("DELETE FROM records WHERE id = 'dans-2'")
    assert transform().deleted == 1

    # And the dataset goes with its last record
    execute("DELETE FROM records WHERE id = 'zenodo-1'")
    add_record("other")
    assert transform().deleted == 1
    assert scalars("SELECT source_record_id FROM dataset") == ["other"]
    assert scalars("SELECT record_identifier FROM dataset_record") == ["oai:other"]


def test_a_dataset_left_by_its_source_record_is_taken_over(appdb: None) -> None:
    shared = "https://doi.org/10.1234/shared"
    owner = f"SELECT source_record_id, harvested_from FROM dataset WHERE url = '{shared}'"  # noqa: S608
    creators = f"SELECT count(*) FROM dataset_agent JOIN dataset ON dataset.id = dataset_id WHERE url = '{shared}'"  # noqa: S608
    add_record("zenodo-1", repo=1, doi="10.1234/shared", creators=[creator("Doe, Jane")])
    add_record("dans-1", repo=2, doi="10.1234/shared")
    transform()
    assert execute(owner) == [("zenodo-1", "ZENODO")]

    # The source record moves to another DOI: the record left takes the dataset the next time it is read
    add_record("zenodo-1", repo=1, doi="10.1234/moved", creators=[creator("Doe, Jane")])
    transform()
    assert transform().updated == 1
    assert execute(owner) == [("dans-1", "DANS")]
    assert scalars(creators) == [0]
    assert scalars("SELECT count(*) FROM dataset") == [2]

    # The source record is not read anymore: same, once a full run removed it from the records of the dataset
    add_record("zenodo-2", repo=1, doi="10.1234/shared", creators=[creator("Doe, Jane")])
    transform()
    assert execute(owner) == [("zenodo-2", "ZENODO")]
    execute("UPDATE records SET resource_type = 'Software' WHERE id = 'zenodo-2'")
    transform()
    assert transform().updated == 1
    assert execute(owner) == [("dans-1", "DANS")]
    assert scalars(creators) == [0]


def test_a_run_on_some_repositories_deletes_the_datasets_left_without_record(appdb: None) -> None:
    shared = "SELECT count(*) FROM dataset WHERE url = 'https://doi.org/10.1234/shared'"
    add_record("zenodo-1", repo=1, doi="10.1234/shared", creators=[creator("Doe, Jane")])
    add_record("dans-1", repo=2, doi="10.1234/shared")
    add_record("zenodo-2", repo=1)
    add_record("dans-2", repo=2)
    transform()

    # Its source record is gone from its repository, the dataset stays with the record of the other one
    execute("DELETE FROM records WHERE id = 'zenodo-1'")
    assert populate_appdb_dataset(SETTINGS, repositories=["ZENODO"]).deleted == 0
    assert scalars(shared) == [1]
    # Its last record is gone from another repository than the one it was harvested from
    execute("DELETE FROM records WHERE id = 'dans-1'")
    assert populate_appdb_dataset(SETTINGS, repositories=["DANS"]).deleted == 1
    assert scalars(shared) == [0]
    assert sorted(scalars("SELECT source_record_id FROM dataset")) == ["dans-2", "zenodo-2"]
