"""Embeddings and reranking through the LiteLLM API at `settings.embed_api_url`.

Two endpoints, one client: OpenAI-compatible `/embeddings` and Cohere-style `/rerank`. They share
`_post`, which retries a 429 (obeying `Retry-After`) and transient network/5xx errors.

Nothing here raises: any failure is logged and returns None, so the caller degrades (no reranking,
or fall back to local embeddings) rather than breaking a search.
"""

import asyncio
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from data_commons_search.config import settings
from data_commons_search.utils import logger

TIMEOUT_S = 30.0
MAX_ATTEMPTS = 3
# A shared endpoint sometimes asks us to wait minutes. A search request is interactive, so we give
# up instead of holding the user's connection open for longer than this.
MAX_RETRY_AFTER_S = 5.0


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """How long the server asked us to wait, from its `Retry-After` header, or None if it sent none.

    Both forms RFC 9110 allows: a number of seconds, or an HTTP date.
    """
    header = resp.headers.get("Retry-After")
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        seconds = (when - datetime.now(when.tzinfo)).total_seconds()
    return max(seconds, 0.0)


async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST a payload to the API and return its JSON, or None if every attempt failed."""
    if not settings.embed_api_key:
        logger.warning(f"No `embed_api_key` configured, skipping the {path} call")
        return None
    url = f"{settings.embed_api_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {settings.embed_api_key}", "Content-Type": "application/json"}
    backoff = 1.0
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # Network-level failures (timeout, connection reset, DNS) are always worth another try
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                logger.warning(f"{path} request failed ({e}), attempt {attempt}/{MAX_ATTEMPTS}")
                if attempt == MAX_ATTEMPTS:
                    return None
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            if resp.is_success:
                return resp.json()
            # Only a rate limit or a server-side error can succeed on a retry: a 400 (bad payload,
            # context length) or a 401 (bad key) would fail again identically.
            if resp.status_code != 429 and resp.status_code < 500:
                logger.error(f"{path} returned {resp.status_code}: {resp.text[:300]}")
                return None
            wait = _retry_after_seconds(resp) if resp.status_code == 429 else None
            if wait is not None and wait > MAX_RETRY_AFTER_S:
                logger.warning(f"{path} rate limited, asking for {wait:.0f}s - longer than we wait, giving up")
                return None
            if attempt == MAX_ATTEMPTS:
                logger.error(f"{path} still failing with {resp.status_code} after {MAX_ATTEMPTS} attempts")
                return None
            delay = wait if wait is not None else backoff
            logger.warning(f"{path} returned {resp.status_code}, retrying in {delay:.1f}s ({attempt}/{MAX_ATTEMPTS})")
            await asyncio.sleep(delay)
            backoff *= 2
    return None


async def embed_texts(texts: list[str], model: str = "") -> list[list[float]] | None:
    """Embed texts with `settings.embed_api_model`, keeping the input order. None if unavailable.

    Callers must prepend the task prefix their model family expects (e.g. "search_query: "), it
    belongs to the text, not to this transport.
    """
    if not texts:
        return []
    payload = {"model": model or settings.embed_api_model, "input": texts}
    data = await _post("embeddings", payload)
    if not data:
        return None
    # OpenAI shape: {"data": [{"index": 0, "embedding": [...]}, ...]}, order not guaranteed
    try:
        items = sorted(data["data"], key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in items]
    except (KeyError, TypeError) as e:
        logger.error(f"Unexpected /embeddings response shape: {e}")
        return None
    if len(vectors) != len(texts):
        logger.error(f"/embeddings returned {len(vectors)} vectors for {len(texts)} texts")
        return None
    return vectors


# Qwen3-Reranker prompt template. The model scores by reading its `no`/`yes` logits for this exact
# chat-formatted prompt, and vLLM does NOT apply the template itself - the client must, on both the
# query and every document (see the vLLM Qwen3-Reranker example). Sent unformatted, the model scores
# roughly by document length instead of relevance.
RERANK_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and "
    'the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
RERANK_INSTRUCT = (
    "Given a user's request for scientific datasets, judge whether the dataset record satisfies it, "
    "taking into account the specific subject, place and time period the request asks for."
)


def _rerank_query(query: str) -> str:
    """The query half of the template, carrying the system prefix and the instruction."""
    return f"{RERANK_PREFIX}<Instruct>: {RERANK_INSTRUCT}\n<Query>: {query}\n"


def _rerank_document(doc: str) -> str:
    """The document half, carrying the generation suffix. The server concatenates it after the
    query, which reassembles the full template the model was trained on."""
    return f"<Document>: {doc}{RERANK_SUFFIX}"


async def rerank(query: str, documents: list[str], top_n: int | None = None) -> list[tuple[int, float]] | None:
    """Score `documents` against `query` with the cross-encoder `settings.reranker_model`.

    Returns `(index into documents, relevance score)` pairs, best first, or None if unavailable.

    Applies the Qwen3 prompt template when `rerank_prompt_template` is on. Turn it off for a
    reranker that templates server-side (Cohere, or a vLLM `--chat-template`), or the prompt is
    wrapped twice.
    """
    if not documents:
        return []
    payload: dict[str, Any] = {
        "model": settings.reranker_model,
        "query": _rerank_query(query) if settings.rerank_prompt_template else query,
        "documents": [_rerank_document(d) for d in documents] if settings.rerank_prompt_template else documents,
        "top_n": top_n if top_n is not None else len(documents),
    }
    data = await _post("rerank", payload)
    if not data:
        return None
    try:
        # Cohere shape: {"results": [{"index": 3, "relevance_score": 0.98}, ...]}, already sorted
        ranked = [(int(r["index"]), float(r.get("relevance_score", r.get("score", 0.0)))) for r in data["results"]]
    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"Unexpected /rerank response shape: {e}")
        return None
    # Trust the scores rather than the response order, and drop indexes we did not send
    ranked = [(i, score) for i, score in ranked if 0 <= i < len(documents)]
    return sorted(ranked, key=lambda pair: pair[1], reverse=True)
