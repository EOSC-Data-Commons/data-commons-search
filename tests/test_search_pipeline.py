"""Unit tests of the search pipeline code in dags/search_pipeline (no database needed)."""

from datetime import date, datetime, timezone

import pytest

from search_pipeline.embeddings import RecordEmbeddings, split_text
from search_pipeline.transform import SkipRecordError, build_dataset, clean_identifier, normalize_doi


def record(**overrides: object) -> dict:
    """A datasetdb record as returned by SELECT_RECORDS_SQL."""
    base = {
        "id": "endpoint::r1",
        "record_identifier": "oai:example.org:r1",
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
    row = build_dataset(record(datacite={"titles": [{"title": "Record title"}], "doi": "10.5281/ZENODO.1"})).dataset
    assert (row.url, row.doi) == ("https://doi.org/10.5281/zenodo.1", "10.5281/zenodo.1")
    # A doi.org URL given as the record URL counts as the DOI
    assert build_dataset(record(url="https://doi.org/10.1234/ABC")).dataset.doi == "10.1234/abc"


def test_skipped_records() -> None:
    with pytest.raises(SkipRecordError, match="URL"):
        build_dataset(record(url=None))
    with pytest.raises(SkipRecordError, match="URL"):
        build_dataset(record(url="ftp://example.org/r1"))
    with pytest.raises(SkipRecordError, match="title"):
        build_dataset(record(title=None, datacite={"titles": []}))


def test_fields_are_cleaned_up() -> None:
    built = build_dataset(
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
                    {
                        "creatorName": "University of Example",
                        "nameType": "Organizational",
                        "nameIdentifiers": [
                            {"nameIdentifier": "https://ror.org/05GQ02987", "nameIdentifierScheme": "ROR"}
                        ],
                    },
                ],
                "dates": [{"date": "2020-05-01", "dateType": "Available"}, {"date": "2020", "dateType": "Issued"}],
                "publicationYear": "2020",
                "formats": ["text/csv", "text/csv"],
            }
        )
    )
    row = built.dataset
    assert (row.source_record_id, built.record_identifier) == ("endpoint::r1", "oai:example.org:r1")
    assert row.title == "English title"
    assert row.description == "Abstract."
    assert row.descriptions == [
        {"text": "Methods.", "type": "Methods"},
        {"text": "Samenvatting.", "type": "Abstract", "lang": "nl"},
        {"text": "Abstract.", "type": "Abstract", "lang": "en"},
    ]
    assert built.subjects == [{"name": "Ecology", "scheme": "FOS"}]
    assert row.keywords == "Ecology"
    assert built.creators == [
        {"name": "Doe, Jane", "identifiers": [{"scheme": "ORCID", "value": "0000-0002-1825-0097"}]},
        {
            "name": "University of Example",
            "type": "Organizational",
            "identifiers": [{"scheme": "ROR", "value": "05gq02987"}],
        },
    ]
    # The invalid "2020" date is dropped, Available is the best date left
    assert built.dates == [{"date": "2020-05-01", "type": "Available"}]
    assert (row.publication_year, row.publication_date) == (2020, date(2020, 5, 1))
    assert row.languages == ["en", "nl"]
    assert row.formats == ["text/csv"]


@pytest.mark.parametrize(
    ("identifier", "scheme", "expected"),
    [
        ("https://orcid.org/0000-0002-1825-0097", None, {"scheme": "ORCID", "value": "0000-0002-1825-0097"}),
        # Lowercase checksum and missing hyphens are the same ORCID
        ("0000-0002-1694-233x", "ORCID", {"scheme": "ORCID", "value": "0000-0002-1694-233X"}),
        ("0000000218250097", "orcid", {"scheme": "ORCID", "value": "0000-0002-1825-0097"}),
        ("https://ror.org/05GQ02987", None, {"scheme": "ROR", "value": "05gq02987"}),
        # Not an identity: a placeholder, an ISNI or a GRID id under the ORCID or ROR scheme
        ("N/A", "ORCID", None),
        ("0000000121839049", "ROR", None),
        ("grid.5333.6", "ROR", None),
        # Other schemes are kept as written
        ("0000 0001 2183 9049", "ISNI", {"scheme": "ISNI", "value": "0000 0001 2183 9049"}),
    ],
)
def test_clean_identifier(identifier: str, scheme: str | None, expected: dict | None) -> None:
    assert clean_identifier({"nameIdentifier": identifier, "nameIdentifierScheme": scheme}) == expected


def test_names_too_long_for_one_creator_or_subject_are_dropped() -> None:
    long_name = "word " * 200
    built = build_dataset(
        record(
            datacite={
                "titles": [{"title": "T"}],
                "subjects": [{"subject": long_name}, {"subject": "Ecology"}],
                "creators": [{"creatorName": long_name}, {"creatorName": "Doe, Jane"}],
            }
        )
    )
    assert [s["name"] for s in built.subjects] == ["Ecology"]
    assert [c["name"] for c in built.creators] == ["Doe, Jane"]
    assert built.dataset.keywords == "Ecology"


def test_completeness_and_links_hash() -> None:
    def build(**datacite: object) -> object:
        return build_dataset(record(datacite={"titles": [{"title": "T"}], **datacite})).dataset

    bare = build()
    with_creator = build(creators=[{"creatorName": "Doe, Jane"}])
    assert (bare.completeness, with_creator.completeness) == (0, 1)
    full = build(
        creators=[{"creatorName": "Doe, Jane"}],
        subjects=[{"subject": "Ecology"}],
        descriptions=[{"description": "An abstract.", "descriptionType": "Abstract"}],
        publicationYear="2020",
    )
    assert full.completeness == 4
    # The hash follows what is written to the link tables: creators, their identifiers, subjects, dates
    assert bare.links_hash != with_creator.links_hash
    assert with_creator.links_hash == build(creators=[{"creatorName": "Doe,  Jane"}]).links_hash
    with_orcid = build(
        creators=[
            {"creatorName": "Doe, Jane", "nameIdentifiers": [{"nameIdentifier": "orcid.org/0000-0002-1825-0097"}]}
        ]
    )
    assert with_creator.links_hash != with_orcid.links_hash
    assert bare.links_hash != build(subjects=[{"subject": "Ecology", "subjectScheme": "FOS"}]).links_hash
    assert bare.links_hash != build(dates=[{"date": "2020-05-01", "dateType": "Collected"}]).links_hash


def test_machine_metadata_descriptions_are_dropped() -> None:
    stac = '{"gsd":0.0008333333333333334,"datetime":"2012-04-15T15:51:40Z","proj:bbox":[33.99958333,12.99958333]}'
    row = build_dataset(record(datacite={"titles": [{"title": "T"}], "descriptions": [{"description": stac}]})).dataset
    assert row.description is None
    prose = "Data collected at 12 stations between 1960 and 2020 (CO2, ppm)."
    row = build_dataset(record(datacite={"titles": [{"title": "T"}], "descriptions": [{"description": prose}]})).dataset
    assert row.description == prose


def test_main_description() -> None:
    def build(*descriptions: dict) -> tuple[str | None, list[dict]]:
        row = build_dataset(record(datacite={"titles": [{"title": "T"}], "descriptions": list(descriptions)})).dataset
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
    row = build_dataset(record(datacite={"titles": [{"title": "T"}], "publicationYear": "1999"})).dataset
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
