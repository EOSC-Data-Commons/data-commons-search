"""Unit tests of the search pipeline code in dags/search_pipeline (no database needed)."""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

# The DAGs folder is not a package of this project, airflow puts it on the path
sys.path.insert(0, str(Path(__file__).parents[1] / "dags"))

from search_pipeline.embeddings import RecordEmbeddings, split_text
from search_pipeline.transform import SkipRecordError, build_dataset, normalize_doi


def record(**overrides: object) -> dict:
    """A datasetdb record as returned by SELECT_RECORDS_SQL."""
    base = {
        "id": "endpoint::r1",
        "url": "https://example.org/r1",
        "title": "Record title",
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "datacite": {"titles": [{"title": "Record title"}]},
        "repository": "ZENODO",
        "harvest_url": "https://zenodo.org/oai2d",
    }
    return {**base, **overrides}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.17026/SS/78HHDK", "10.17026/ss/78hhdk"),
        ("doi:10.5281/zenodo.1", "10.5281/zenodo.1"),
        ("https://dx.doi.org/10.5281/ZENODO.1.", "10.5281/zenodo.1"),
        ("https://example.org/no-doi", None),
        (None, None),
    ],
)
def test_normalize_doi(value: str | None, expected: str | None) -> None:
    assert normalize_doi(value) == expected


def test_doi_makes_the_url() -> None:
    row = build_dataset(record(datacite={"titles": [{"title": "Record title"}], "doi": "10.5281/ZENODO.1"}))
    assert (row.url, row.doi) == ("https://doi.org/10.5281/zenodo.1", "10.5281/zenodo.1")
    # A doi.org URL given as the record URL counts as the DOI
    assert build_dataset(record(url="https://doi.org/10.1234/ABC")).doi == "10.1234/abc"


def test_skipped_records() -> None:
    with pytest.raises(SkipRecordError, match="URL"):
        build_dataset(record(url=None))
    with pytest.raises(SkipRecordError, match="URL"):
        build_dataset(record(url="ftp://example.org/r1"))
    with pytest.raises(SkipRecordError, match="title"):
        build_dataset(record(title=None, datacite={"titles": []}))


def test_fields_are_cleaned_up() -> None:
    row = build_dataset(
        record(
            datacite={
                "titles": [
                    {"title": "Titel in het Nederlands", "lang": "nl"},
                    {"title": "  English   title ", "lang": "en"},
                    {"title": "Sub", "titleType": "Subtitle", "lang": "en"},
                ],
                "descriptions": [
                    {"description": "Methods.", "descriptionType": "Methods"},
                    {"description": "Samenvatting.", "descriptionType": "Abstract", "lang": "nl"},
                    {"description": "Abstract.", "descriptionType": "Abstract", "lang": "en"},
                    {"description": "Abstract.", "descriptionType": "Abstract"},
                ],
                "subjects": [{"subject": "Ecology", "subjectScheme": "FOS"}, {"subject": "ecology"}, {"subject": " "}],
                "creators": [
                    {
                        "creatorName": "Doe, Jane",
                        "nameIdentifiers": [
                            {"nameIdentifier": "https://orcid.org/0000-0002-1825-0097", "nameIdentifierScheme": "ORCID"}
                        ],
                    },
                    {"givenName": "No name"},
                ],
                "dates": [{"date": "2020-05-01", "dateType": "Available"}, {"date": "2020", "dateType": "Issued"}],
                "publicationYear": "2020",
                "formats": ["text/csv", "text/csv"],
            }
        )
    )
    assert row.title == "English title"
    assert row.description == "Abstract."
    assert row.descriptions == [
        {"text": "Methods.", "type": "Methods"},
        {"text": "Samenvatting.", "type": "Abstract", "lang": "nl"},
        {"text": "Abstract.", "type": "Abstract", "lang": "en"},
    ]
    assert row.subjects == [{"name": "Ecology", "scheme": "FOS"}]
    assert row.creators == [{"name": "Doe, Jane", "identifiers": [{"scheme": "ORCID", "value": "0000-0002-1825-0097"}]}]
    # The invalid "2020" date is dropped, Available is the best date left
    assert row.dates == [{"date": "2020-05-01", "type": "Available"}]
    assert (row.publication_year, row.publication_date) == (2020, date(2020, 5, 1))
    assert row.languages == ["en", "nl"]
    assert row.formats == ["text/csv"]


def test_machine_metadata_descriptions_are_dropped() -> None:
    stac = '{"gsd":0.0008333333333333334,"datetime":"2012-04-15T15:51:40Z","proj:bbox":[33.99958333,12.99958333]}'
    row = build_dataset(record(datacite={"titles": [{"title": "T"}], "descriptions": [{"description": stac}]}))
    assert row.description is None
    prose = "Data collected at 12 stations between 1960 and 2020 (CO2, ppm)."
    row = build_dataset(record(datacite={"titles": [{"title": "T"}], "descriptions": [{"description": prose}]}))
    assert row.description == prose


def test_main_description() -> None:
    def build(*descriptions: dict) -> tuple[str | None, list[dict]]:
        row = build_dataset(record(datacite={"titles": [{"title": "T"}], "descriptions": list(descriptions)}))
        return row.description, row.descriptions

    # DANS: HTML removed, date notes dropped, the abstracts of the same language joined
    description, descriptions = build(
        {"description": "<p>First part &amp; more.</p>", "descriptionType": "Abstract"},
        {"description": "Date Submitted: 2010-01-12", "descriptionType": "Abstract"},
        {"description": "Second part.", "descriptionType": "Abstract"},
        {"description": "About the whole series.", "descriptionType": "SeriesInformation"},
    )
    assert description == "First part & more.\n\nSecond part."
    assert [d["text"] for d in descriptions] == ["First part & more.", "Second part.", "About the whole series."]
    # Translations: English only, the others are kept in the list
    description, descriptions = build(
        {"description": "Auf Deutsch.", "descriptionType": "Abstract", "lang": "de"},
        {"description": "In English.", "descriptionType": "Abstract", "lang": "en"},
    )
    assert description == "In English."
    assert len(descriptions) == 2
    # No abstract: the next type, all its parts (EMPIAR)
    description, _ = build(
        {"description": "Funded by X.", "descriptionType": "Other"},
        {"description": "Raw movies.", "descriptionType": "TechnicalInfo"},
        {"description": "Aligned micrographs.", "descriptionType": "TechnicalInfo"},
    )
    assert description == "Raw movies.\n\nAligned micrographs."


def test_publication_date_falls_back_to_the_year() -> None:
    row = build_dataset(record(datacite={"titles": [{"title": "T"}], "publicationYear": "1999"}))
    assert row.publication_date == date(1999, 1, 1)


# Stand-in for TokenCounter.fits in most tests: a character is a token
MAX_CHUNK_CHARS = 1000


def char_fits(text: str) -> bool:
    return len(text) <= MAX_CHUNK_CHARS


def test_split_text_packs_sentences() -> None:
    text = " ".join(f"Sentence {i} is about some measured variable." for i in range(200))
    chunks = split_text(text, char_fits)
    assert 1 < len(chunks) <= 8
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    assert all(c.endswith(".") for c in chunks)
    # A single word longer than a chunk is cut
    assert [len(c) for c in split_text("x" * 2500, char_fits)] == [1000, 1000, 500]


def test_split_text_fits_the_token_window() -> None:
    # Stand-in for the tokenizer: a digit is a token, a letter a quarter of one, 100 tokens max
    def fits(text: str) -> bool:
        return sum(1 if c.isdigit() else 0.25 for c in text) <= 100

    text = "Values 12345 67890 12345 67890 at the station. " * 10
    chunks = split_text(text, fits)
    # Split, and nothing lost
    assert len(chunks) > 1
    assert all(fits(c) for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_record_embeddings_named_chunks() -> None:
    record_embeddings = RecordEmbeddings.from_fields(
        1, {"title": "T" * 2000, "keywords": None, "description": "A sentence. " * 300}, char_fits
    )
    fields = [(c.field, c.index) for c in record_embeddings.chunks]
    # Single vector fields get one chunk 0 (their first chunk), empty fields none, long fields several
    assert fields[0] == ("title", 0)
    assert len(record_embeddings.chunks[0].text) == MAX_CHUNK_CHARS
    assert not any(field == "keywords" for field, _ in fields)
    assert [i for field, i in fields if field == "description"] == list(range(len(fields) - 1))
    record_embeddings.vectors = [[0.5, 0.25]] * len(fields)
    assert record_embeddings.rows()[0] == {
        "dataset_id": 1,
        "field": "title",
        "chunk_index": 0,
        "chunk_text": "T" * MAX_CHUNK_CHARS,
        "embedding": [0.5, 0.25],
    }
