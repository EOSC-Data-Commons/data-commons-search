"""Zenodo search, federated into `search_data` at query time.

Their hits are merged with ours and reranked there,
alongside every other result list. Zenodo's own search is lexical, so it is queried with the raw user text. Its API caps `size` at 25
and its rate limit is 30 requests/min for the whole service (see `x-ratelimit-*` response headers),
so failures here are expected and always degrade to "no Zenodo results" rather than to an error.
"""

import html
import re
from pathlib import PurePosixPath
from typing import Any

import httpx

from data_commons_search.config import settings
from data_commons_search.models import SearchHit, SearchResults
from data_commons_search.utils import logger

ZENODO_API = "https://zenodo.org/api/records"
ZENODO_REPO = "Zenodo"
TIMEOUT_S = 12.0
# Hard cap of their API on the number of results per page
MAX_SIZE = 25

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str | None) -> str | None:
    """Zenodo descriptions are HTML; our index stores plain text."""
    if not text:
        return None
    # Unescape after stripping tags, so entity-encoded angle brackets are not mistaken for markup.
    plain = html.unescape(_HTML_TAG_RE.sub(" ", text))
    return re.sub(r"\s+", " ", plain).strip() or None


def _subjects(metadata: dict[str, Any]) -> list[dict[str, str]]:
    """Keywords and subjects, both optional and shaped differently, into our `subjects`."""
    out: list[dict[str, str]] = []
    for kw in metadata.get("keywords") or []:
        if isinstance(kw, str) and kw.strip():
            out.append({"subject": kw.strip()})
    for subj in metadata.get("subjects") or []:
        if isinstance(subj, dict):
            term = subj.get("subject") or subj.get("term")
            if term:
                out.append({"subject": str(term)})
    return out


def _file_extensions(record: dict[str, Any]) -> list[str]:
    """Extensions of the record's files. Zenodo returns the file list inline in search results,
    which our own harvested records do not currently carry."""
    exts = set()
    for f in record.get("files") or []:
        suffix = PurePosixPath(str(f.get("key") or "")).suffix.lstrip(".").lower()
        if suffix:
            exts.add(suffix)
    return sorted(exts)


def _to_search_hit(record: dict[str, Any], rank: int, total: int) -> SearchHit | None:
    """Map one Zenodo record onto our `SearchHit`, or None if it has no usable identity."""
    metadata = record.get("metadata") or {}
    # Normalized rank score in (0, 1]: 1.0, 0.9, 0.8 ... for 10 results. Zenodo returns no relevance
    # score of its own, so this only preserves their ordering; it is not comparable with our
    # OpenSearch scores, which is why the two lists are concatenated rather than merged by score.
    rank_score = (total - rank) / max(total, 1)
    # `doi_url` is already `https://doi.org/<doi>`, which is exactly how `SearchHit.dataset_url`
    # renders a DOI - so dedup against our own hits is a plain string match.
    doi = metadata.get("doi") or record.get("doi")
    landing = (record.get("links") or {}).get("self_html")
    if not doi and not landing:
        return None
    title = metadata.get("title") or record.get("title")
    pub_date = metadata.get("publication_date")
    return SearchHit.model_validate(
        {
            "_id": record.get("doi_url") or landing,
            "_score": rank_score,
            "_source": {
                "doi": doi,
                "url": landing,
                "_repo": ZENODO_REPO,
                "titles": [{"title": title}] if title else [],
                "descriptions": ([{"description": desc}] if (desc := _strip_html(metadata.get("description"))) else []),
                "publicationYear": pub_date[:4] if pub_date else None,
                "dates": [{"date": pub_date, "dateType": "Issued"}] if pub_date else None,
                "subjects": _subjects(metadata) or None,
                "creators": [
                    {"creatorName": name}
                    for c in (metadata.get("creators") or [])
                    if (name := (c.get("name") if isinstance(c, dict) else None))
                ]
                or None,
                "resourceType": "dataset",
            },
            "fileExtensions": _file_extensions(record),
        }
    )


def _build_query(search_input: str, start_date: str | None, end_date: str | None, creator_name: str | None) -> str:
    """Zenodo query string. Their syntax is Lucene-like over `metadata.*` fields, documented at
    https://zenodo.org/help/search - unbounded range ends are written as `*`."""
    parts = [search_input.strip()] if search_input.strip() else []
    if start_date or end_date:
        parts.append(f"+metadata.publication_date:[{start_date or '*'} TO {end_date or '*'}]")
    if creator_name:
        # Zenodo indexes InvenioRDM's internal shape, so the searchable creator field is
        # `person_or_org.name` even though the JSON response serializes it as `creators[].name`.
        # Quoted so a multi-word name stays one phrase rather than several optional terms.
        parts.append(f'+metadata.creators.person_or_org.name:"{creator_name}"')
    return " ".join(parts)


async def search_zenodo_datasets(
    search_input: str,
    limit: int = 10,
    start_date: str | None = None,
    end_date: str | None = None,
    creator_name: str | None = None,
) -> SearchResults:
    """Search Zenodo for datasets and return them in our own format.

    Never raises: any failure (timeout, rate limit, schema change) is logged and returns no hits, so
    a Zenodo outage can only shrink the result list, never break a search.
    """
    query = _build_query(search_input, start_date, end_date, creator_name)
    params = {
        "q": query,
        "resource_type": "dataset",
        # Their API rejects a `size` above 25
        "size": str(min(limit, MAX_SIZE)),
        "page": "1",
    }
    headers = {"accept": "application/json"}
    # Authenticate when a token is configured: the search endpoint is public, but an authenticated
    # request gets a higher rate limit, which is the failure mode we actually hit.
    if settings.zenodo_api_key:
        headers["authorization"] = f"Bearer {settings.zenodo_api_key}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(ZENODO_API, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        # A timeout stringifies to "", so log the class too or the reason is invisible
        logger.warning(f"Zenodo search failed, continuing without it: {type(e).__name__} {e}")
        return SearchResults(total_found=0, hits=[])

    records = ((payload.get("hits") or {}).get("hits")) or []
    total = len(records)
    hits = [hit for rank, rec in enumerate(records) if (hit := _to_search_hit(rec, rank, total)) is not None]
    logger.debug(f"Zenodo search returned {len(hits)} hits for {query!r}")
    return SearchResults(total_found=int((payload.get("hits") or {}).get("total") or 0), hits=hits)
