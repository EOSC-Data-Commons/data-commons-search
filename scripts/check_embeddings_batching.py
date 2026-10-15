"""Check that an OpenAI compatible /embeddings endpoint returns the right vector for every text of a
batch, also when the batch mixes texts the endpoint has already seen with new ones.

Found on a LiteLLM proxy with embedding caching: a batch made only of new texts, or only of cached
texts, is right, but a batch mixing both gets its vectors shifted (a text receives the vector of
another text of the batch). The model server behind the proxy, queried directly, is right.

Steps, with texts unique to this run so nothing is cached at the start:
1. Embed half of the texts one per request (they are now cached by a caching proxy)
2. Embed all the texts in batches mixing those with the other half, never sent before
3. Embed the other half one per request, the reference for the batched vectors
4. Send the same batches again, every text is cached by now

Standard library only:

    python check_embeddings_batching.py --url http://localhost:8000/v1/embeddings --model nomic-embed-text-v2-moe
    EMBED_API_KEY=sk-... python check_embeddings_batching.py --url https://llm.ai.egi.eu/v1/embeddings
"""

# Runs on python 3.9+ to be easy to share, so no zip(strict=)
# ruff: noqa: B905, SIM905
import argparse
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

# Same vector up to float noise
MIN_COSINE = 0.999

WORDS = (
    "climate temperature precipitation soil ocean salinity river discharge forest biomass species "
    "occurrence survey interview population census archaeology excavation pottery coin protein "
    "structure microscopy genome sequencing sample station measurement satellite radar aerosol "
    "glacier sediment pollen vegetation drought flood seismic volcano magnetic field laboratory "
    "experiment simulation model dataset collected between monitoring network annual monthly daily "
    "regional national european coastal alpine urban rural historical modern analysis results"
).split()


def make_texts(count: int, run_id: str) -> list[str]:
    """Distinct texts of varied lengths (5 to 200 words), like titles and descriptions, unique to the run."""
    rng = random.Random(run_id)  # noqa: S311
    texts = []
    for i in range(count):
        words = [rng.choice(WORDS) for _ in range(rng.choice((5, 12, 40, 100, 200)))]
        texts.append(f"Dataset {i} of run {run_id}: " + " ".join(words).capitalize() + ".")
    return texts


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def embed(args: argparse.Namespace, texts: list[str]) -> list[list[float]]:
    """One request, retrying rate limits and server errors."""
    body = json.dumps({"model": args.model, "input": [args.prefix + t for t in texts]}).encode()
    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"
    for attempt in range(6):
        request = urllib.request.Request(args.url, data=body, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310
                data = sorted(json.load(resp)["data"], key=lambda d: d["index"])
                return [d["embedding"] for d in data]
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                sys.exit(f"HTTP {e.code}: {e.read()[:300]!r}")
            time.sleep(min(2**attempt, 60))
    sys.exit("Endpoint still failing after 6 attempts")


def report(name: str, batches: list[list[str]], vectors: list[list[float]], reference: dict) -> bool:
    """Print the texts whose batched vector differs from their own vector, return whether all are right."""
    texts = [t for batch in batches for t in batch]
    wrong = 0
    print(f"\n{name}")
    for b, batch in enumerate(batches):
        offset = b * len(batches[0])
        for position, text in enumerate(batch):
            vector = vectors[offset + position]
            similarity = cosine(reference[text], vector)
            if similarity < MIN_COSINE:
                wrong += 1
                # Whose vector it got instead, if it is exactly the vector of another text of the run
                owner = next((texts.index(t) for t in texts if cosine(reference[t], vector) >= MIN_COSINE), None)
                got = f"text {owner} (batch {owner // len(batches[0])} position {owner % len(batches[0])})"
                print(
                    f"  text {offset + position} (batch {b} position {position}): cosine {similarity:.3f}, "
                    f"got the vector of {got if owner is not None else 'no text of this run'}"
                )
    print(f"  {len(texts) - wrong}/{len(texts)} vectors right")
    return wrong == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Full URL of the embeddings endpoint, .../v1/embeddings")
    parser.add_argument("--model", default="nomic-embed-text-v2-moe")
    parser.add_argument("--api-key", default=os.environ.get("EMBED_API_KEY", ""), help="Default: $EMBED_API_KEY")
    parser.add_argument("--prefix", default="search_document: ", help="Task prefix added to every text")
    parser.add_argument("--texts", type=int, default=64)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=4, help="Requests in flight")
    args = parser.parse_args()

    texts = make_texts(args.texts, uuid.uuid4().hex[:8])
    # Every other text is sent alone first, so each batch mixes seen and new texts
    seen, new = texts[0::2], texts[1::2]
    batches = [texts[i : i + args.batch_size] for i in range(0, len(texts), args.batch_size)]
    reference: dict[str, list[float]] = {}

    def embed_alone(group: list[str]) -> None:
        reference.update(zip(group, (v[0] for v in pool.map(lambda t: embed(args, [t]), group))))

    def embed_batches() -> list[list[float]]:
        return [v for batch in pool.map(lambda b: embed(args, b), batches) for v in batch]

    with ThreadPoolExecutor(args.concurrency) as pool:
        print(f"{len(texts)} new texts, batches of {args.batch_size}")
        embed_alone(seen)
        mixed = embed_batches()
        embed_alone(new)
        again = embed_batches()
    ok = report("Batches mixing texts sent before and new texts", batches, mixed, reference)
    ok &= report("Same batches again, every text sent before", batches, again, reference)
    print("\nAll vectors right" if ok else "\nWrong vectors returned: they depend on the other texts of the batch")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
