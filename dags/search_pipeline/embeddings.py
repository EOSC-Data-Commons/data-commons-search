"""Named embeddings of the datasets, stored in `dataset_embedding`.

Each dataset gets one vector per field, so the search can weight a title match differently from a
description match. Fields that fit in the model window (title, keywords) get a single chunk 0,
long fields (description) are split into several chunks, each with its own vector.
"""

import logging
import math
import random
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

import requests
from sqlalchemy import Connection, bindparam, delete, func, insert, select, text, update
from sqlalchemy.schema import DropIndex

from data_commons_search.config import settings as api_settings
from data_commons_search.dataset_models import SEARCH_FIELDS, Dataset, DatasetEmbedding, is_diskann, search_indexes
from search_pipeline.config import PipelineSettings
from search_pipeline.db import appdb_engine

logger = logging.getLogger(__name__)

# Guard against full papers pasted as a description
MAX_DESCRIPTION_CHUNKS = 8
# Datasets embedded and committed together
PAGE_SIZE = 1000
# A batched vector must match the same text embedded alone, some endpoints return wrong vectors
# above a batch size that depends on their load
VERIFY_MIN_COSINE = 0.99
MAX_RETRY_AFTER_S = 120.0

SENTENCE_END_RE = re.compile(r"(?<=[.!?;])\s+")


class TokenCounter:
    """Counts tokens like the embeddings server: the model tokenizer, task prefix and special tokens
    included (checked equal to the counts in vLLM context length errors)."""

    def __init__(self, settings: PipelineSettings) -> None:
        # Imported here, only the airflow worker needs it (the API and the tests do not)
        from tokenizers import Tokenizer  # noqa: PLC0415

        self.tokenizer = Tokenizer.from_pretrained(settings.embed_tokenizer)
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.prefix = api_settings.embed_document_prefix
        self.max_tokens = settings.embed_max_tokens

    def fits(self, text: str) -> bool:
        """Whether the text fits in the model window."""
        return len(self.tokenizer.encode(self.prefix + text).ids) <= self.max_tokens


def _cut(text: str, fits: Callable[[str], bool]) -> list[str]:
    """Cut a text with no space (URL, formula...) in the longest prefixes that fit."""
    parts = []
    while text:
        low, high = 1, len(text)
        # Largest prefix length that fits, a single character always does
        while low < high:
            mid = (low + high + 1) // 2
            low, high = (mid, high) if fits(text[:mid]) else (low, mid - 1)
        parts.append(text[:low])
        text = text[low:]
    return parts


def split_text(text: str, fits: Callable[[str], bool], max_chunks: int = MAX_DESCRIPTION_CHUNKS) -> list[str]:
    """Split a text in chunks that fit, packing whole sentences when possible."""
    if fits(text):
        return [text]
    # Sentences that do not fit are split on words, and words on characters
    pieces: list[str] = []
    for sentence in SENTENCE_END_RE.split(text):
        if fits(sentence):
            pieces.append(sentence)
            continue
        for word in sentence.split(" "):
            pieces.extend([word] if fits(word) else _cut(word, fits))
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}" if current else piece
        if current and not fits(candidate):
            chunks.append(current)
            if len(chunks) == max_chunks:
                return chunks
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks[:max_chunks]


@dataclass(frozen=True)
class Chunk:
    field: str
    index: int
    text: str


@dataclass
class RecordEmbeddings:
    """Named embeddings of one dataset: the text chunks of each field, then their vectors."""

    SINGLE_VECTOR_FIELDS: ClassVar[tuple[str, ...]] = ("title", "keywords")
    CHUNKED_FIELDS: ClassVar[tuple[str, ...]] = ("description",)

    dataset_id: int
    chunks: list[Chunk]
    vectors: list[list[float]] = field(default_factory=list)

    @classmethod
    def from_fields(
        cls, dataset_id: int, texts: dict[str, str | None], fits: Callable[[str], bool]
    ) -> "RecordEmbeddings":
        """Chunk the text of each search field, fields without text get no embedding. `fits` tells
        whether a text fits in one chunk (TokenCounter.fits)."""
        chunks: list[Chunk] = []
        for name in SEARCH_FIELDS:
            text = (texts.get(name) or "").strip()
            if not text:
                continue
            # The first chunk of a long title or keyword list stays representative, a description
            # would lose content
            parts = split_text(text, fits, MAX_DESCRIPTION_CHUNKS if name in cls.CHUNKED_FIELDS else 1)
            chunks.extend(Chunk(name, i, part) for i, part in enumerate(parts))
        return cls(dataset_id, chunks)

    def rows(self) -> list[dict[str, Any]]:
        """Rows of `dataset_embedding`, once the vectors are set."""
        return [
            {
                "dataset_id": self.dataset_id,
                "field": c.field,
                "chunk_index": c.index,
                "chunk_text": c.text,
                "embedding": v,
            }
            for c, v in zip(self.chunks, self.vectors, strict=True)
        ]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class EmbeddingClient:
    """OpenAI compatible /embeddings, `batch_size` texts per request, `concurrency` requests in flight."""

    def __init__(self, settings: PipelineSettings, batch_size: int = 64, concurrency: int = 4) -> None:
        if not settings.embed_api_key:
            raise ValueError("No embeddings API key, set EMBED_API_KEY (or FEDLLM_API_KEY for EGI)")
        self.url = f"{settings.embed_api_url.rstrip('/')}/embeddings"
        self.headers = {"Authorization": f"Bearer {settings.embed_api_key}"}
        self.model = settings.embed_api_model
        self.prefix = api_settings.embed_document_prefix
        self.batch_size = batch_size
        self.concurrency = concurrency
        # requests, rate_limited
        self.counts: Counter[str] = Counter()
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def _session(self) -> requests.Session:
        """One session per thread, requests sessions are not thread safe."""
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
        return self._local.session

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in order. Identical texts (frequent across a page) are sent once."""
        unique_texts = list(dict.fromkeys(texts))
        batches = [unique_texts[i : i + self.batch_size] for i in range(0, len(unique_texts), self.batch_size)]
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            vectors = [v for batch in pool.map(self._request, batches) for v in batch]
        by_text = dict(zip(unique_texts, vectors, strict=True))
        return [by_text[t] for t in texts]

    def verify(self, texts: list[str], vectors: list[list[float]], sample: int = 2) -> None:
        """Re-embed a few texts alone and compare: a batched vector must be the same vector."""
        for i in random.sample(range(len(texts)), min(sample, len(texts))):
            similarity = cosine(self._request([texts[i]])[0], vectors[i])
            if similarity < VERIFY_MIN_COSINE:
                raise RuntimeError(
                    f"The embeddings API returned a different vector for a text in a batch than alone "
                    f"(cosine {similarity:.4f}), lower the batch size"
                )

    def _count(self, key: str) -> None:
        with self._lock:
            self.counts[key] += 1

    def _request(self, texts: list[str], attempts: int = 6) -> list[list[float]]:
        """POST one batch, retrying rate limits (obeying Retry-After), server and network errors."""
        payload = {"model": self.model, "input": [self.prefix + t for t in texts]}
        for attempt in range(1, attempts + 1):
            wait = min(2.0**attempt, 60.0)
            try:
                resp = self._session.post(self.url, json=payload, headers=self.headers, timeout=120)
            except requests.RequestException as e:
                logger.warning(f"Embeddings request failed ({e}), attempt {attempt}/{attempts}")
            else:
                self._count("requests")
                if resp.ok:
                    data = sorted(resp.json()["data"], key=lambda d: d["index"])
                    if len(data) != len(texts):
                        raise RuntimeError(f"Got {len(data)} vectors for {len(texts)} texts")
                    return [d["embedding"] for d in data]
                if resp.status_code != 429 and resp.status_code < 500:
                    raise RuntimeError(f"Embeddings API returned {resp.status_code}: {resp.text[:300]}")
                if resp.status_code == 429:
                    self._count("rate_limited")
                    if (retry_after := resp.headers.get("Retry-After", "")).isdigit():
                        wait = min(float(retry_after), MAX_RETRY_AFTER_S)
                logger.warning(
                    f"Embeddings API returned {resp.status_code}, retry in {wait:.0f}s ({attempt}/{attempts})"
                )
            if attempt < attempts:
                time.sleep(wait)
        raise RuntimeError(f"Embeddings API still failing after {attempts} attempts")


# ---------- Generate and store ----------

embeddings = DatasetEmbedding.__table__
# Only mark the datasets left untouched while their page was embedded, a dataset updated in between
# by the transform stays in the queue
MARK_EMBEDDED = (
    update(Dataset)
    .where(Dataset.id == bindparam("dataset_id"), Dataset.updated_at == bindparam("read_updated_at"))
    .values(embedded_at=func.now())
)


@dataclass
class EmbeddingStats:
    datasets: int = 0
    chunks: int = 0
    per_field: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SEARCH_FIELDS, 0))

    def as_dict(self) -> dict[str, Any]:
        return {"datasets": self.datasets, "chunks": self.chunks, "per_field": self.per_field}


def reset_embeddings(appdb: Connection, dimensions: int) -> None:
    """Empty the table before a full run. The DiskANN indexes are dropped so the bulk load does not
    grow the graphs one insert at a time (destroys recall), rebuild_indexes recreates them."""
    with appdb.begin():
        for index in filter(is_diskann, search_indexes()):
            appdb.execute(DropIndex(index, if_exists=True))
        appdb.execute(text("TRUNCATE dataset_embedding"))
        # Lets a full run switch to a model with another number of dimensions
        appdb.execute(text(f"ALTER TABLE dataset_embedding ALTER COLUMN embedding TYPE vector({int(dimensions)})"))
        appdb.execute(update(Dataset).where(Dataset.embedded_at.is_not(None)).values(embedded_at=None))
    logger.info("Embeddings reset, all datasets queued for embedding")


def store_page(appdb: Connection, records: list[RecordEmbeddings], updated_at: dict[int, datetime]) -> None:
    """Replace the embeddings of a page of datasets and take them out of the queue, in one transaction."""
    with appdb.begin():
        appdb.execute(delete(embeddings).where(embeddings.c.dataset_id.in_([r.dataset_id for r in records])))
        appdb.execute(insert(embeddings), [row for r in records for row in r.rows()])
        appdb.execute(
            MARK_EMBEDDED, [{"dataset_id": r.dataset_id, "read_updated_at": updated_at[r.dataset_id]} for r in records]
        )


def queued_count(settings: PipelineSettings) -> int:
    """Datasets waiting for their embeddings: new, or with an embedded field changed since."""
    with appdb_engine(settings).connect() as appdb:
        return appdb.execute(select(func.count()).where(Dataset.embedded_at.is_(None))).scalar() or 0


def generate_embeddings(
    settings: PipelineSettings,
    full: bool = False,
    batch_size: int = 64,
    concurrency: int = 4,
    verify: bool = True,
    limit: int | None = None,
) -> EmbeddingStats:
    """Embed the datasets queued by the transform (embedded_at IS NULL), page by page.

    Each page is committed on its own, a failed run is resumed by the next one.
    """
    stats = EmbeddingStats()
    if not full:
        queued = queued_count(settings)
        logger.info(f"{queued} datasets queued for embedding")
        if not queued:
            return stats
    client = EmbeddingClient(settings, batch_size, concurrency)
    counter = TokenCounter(settings)
    # Fail fast on a model that does not match the column
    if len(dims := client.embed(["dimensions check"])[0]) != settings.embed_dimensions:
        raise ValueError(
            f"{settings.embed_api_model} returns {len(dims)} dimensions, EMBED_DIMENSIONS={settings.embed_dimensions}"
        )
    started = time.monotonic()
    with appdb_engine(settings).connect() as appdb:
        if full:
            reset_embeddings(appdb, settings.embed_dimensions)
        last_id = 0
        while not limit or stats.datasets < limit:
            page_size = min(PAGE_SIZE, limit - stats.datasets) if limit else PAGE_SIZE
            with appdb.begin():
                page = appdb.execute(
                    select(Dataset.id, Dataset.updated_at, *(getattr(Dataset, f) for f in SEARCH_FIELDS))
                    .where(Dataset.embedded_at.is_(None), Dataset.id > last_id)
                    .order_by(Dataset.id)
                    .limit(page_size)
                ).all()
            if not page:
                break
            last_id = page[-1].id
            records = [
                RecordEmbeddings.from_fields(row.id, {f: getattr(row, f) for f in SEARCH_FIELDS}, counter.fits)
                for row in page
            ]
            texts = [c.text for r in records for c in r.chunks]
            vectors = client.embed(texts)
            if verify:
                client.verify(texts, vectors)
            # Hand each record back its own vectors, in chunk order
            offset = 0
            for r in records:
                r.vectors = vectors[offset : offset + len(r.chunks)]
                offset += len(r.chunks)
                for c in r.chunks:
                    stats.per_field[c.field] += 1
            store_page(appdb, records, {row.id: row.updated_at for row in page})
            stats.datasets += len(records)
            stats.chunks += len(texts)
            rate = stats.chunks / (time.monotonic() - started)
            logger.info(
                f"{stats.datasets} datasets embedded ({stats.chunks} chunks, {rate:.0f} chunks/s, {dict(client.counts)})"
            )
    logger.info(f"Embeddings done: {stats.as_dict()}")
    return stats
