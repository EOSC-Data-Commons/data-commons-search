"""Stress test a local deployment by firing concurrent /chat requests.

Hits the running server's streaming `/chat` endpoint (LLM + OpenSearch + rerank,
the whole pipeline) with a batch of concurrent requests and reports time-to-first
-byte, total latency, throughput and failures. Each query gets a nonce appended so
the provider/response cache never serves a stored answer.

Start the server first (e.g. `uv run data-commons-search` on port 8000), then:
    uv run --env-file keys.env tests/stress_api.py                 # 5 concurrent, 1 round
    uv run --env-file keys.env tests/stress_api.py -c 20           # 20 concurrent
    uv run --env-file keys.env tests/stress_api.py -c 10 -r 3      # 10 concurrent x 3 rounds
    uv run --env-file keys.env tests/stress_api.py --ramp 5,10,20  # escalate to find the limit
    uv run --env-file keys.env tests/stress_api.py --url http://127.0.0.1:8000 -m cesnet/qwen3-coder
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from statistics import mean, median

import httpx

from data_commons_search.config import settings

try:
    from rich.console import Console
    from rich.table import Table

    console = Console()
except ImportError:  # rich is a project dep, but stay graceful
    console = None  # type: ignore[assignment]

# Queries to rotate through (a nonce is appended to bust the response cache).
QUERIES: list[str] = [
    "Archaeological excavation and field survey reports from sites in the Netherlands",
    "Roman and medieval coins and metal artefacts found by detectorists",
    "Cadmium and heavy metal contamination in French agricultural soils",
    "Atmospheric CO2 concentration and carbon cycle monitoring data",
    "Swiss public opinion surveys on the environment and politics",
    "Microbial communities and microbiota of cheese and milk",
    "Neutron scattering studies of proton-conducting materials",
    "Soil microbiology and plant root traits across urban land uses",
    "CO2 saturation in Amazonian rivers",
    "Wader breeding densities in Europe",
]

REQUEST_TIMEOUT = 180.0


@dataclass
class RequestResult:
    index: int
    ok: bool = False
    status: int | None = None
    ttfb: float | None = None  # time to first SSE event (s)
    total: float = 0.0  # total latency (s)
    n_events: int = 0
    n_hits: int = 0  # results returned by rerank_results
    error: str | None = None


@dataclass
class RoundResult:
    concurrency: int
    results: list[RequestResult] = field(default_factory=list)
    wall: float = 0.0

    @property
    def ok(self) -> list[RequestResult]:
        return [r for r in self.results if r.ok]

    @property
    def failures(self) -> list[RequestResult]:
        return [r for r in self.results if not r.ok]


async def one_request(
    client: httpx.AsyncClient, url: str, token: str, model: str, index: int, query: str
) -> RequestResult:
    """Fire a single streaming /chat request and time it."""
    res = RequestResult(index=index)
    # Unique nonce so neither the response cache nor the LLM returns a stored answer.
    text = f"{query} [req#{index} t={time.time_ns()}]"
    payload = {
        "items": [{"type": "message", "role": "user", "content": [{"text": text}]}],
        "model": model,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    t0 = time.monotonic()
    try:
        async with client.stream("POST", url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT) as resp:
            res.status = resp.status_code
            if resp.status_code >= 400:
                body = (await resp.aread()).decode(errors="replace")
                res.total = time.monotonic() - t0
                res.error = f"HTTP {resp.status_code}: {body[:140]}"
                return res
            async for raw_line in resp.aiter_lines():
                if res.ttfb is None:
                    res.ttfb = time.monotonic() - t0
                stripped = raw_line.strip()
                if not stripped.startswith("data: "):
                    continue
                res.n_events += 1
                try:
                    event = json.loads(stripped[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "TOOL_CALL_RESULT" and str(event.get("tool_call_id", "")).startswith("rerank_"):
                    try:
                        ranked = json.loads(event["content"])
                        res.n_hits += len(ranked.get("hits", []))
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass
        res.total = time.monotonic() - t0
        res.ok = True
    except Exception as e:
        res.total = time.monotonic() - t0
        res.error = f"{type(e).__name__}: {e}"[:160]
    return res


async def run_round(
    client: httpx.AsyncClient, url: str, token: str, model: str, concurrency: int, round_no: int
) -> RoundResult:
    if console:
        console.print(f"[bold cyan]Round {round_no}[/]  ·  {concurrency} concurrent  ·  [dim]{model}[/]")
    else:
        print(f"Round {round_no} · {concurrency} concurrent · {model}")
    tasks = [one_request(client, url, token, model, i, QUERIES[i % len(QUERIES)]) for i in range(concurrency)]
    t0 = time.monotonic()
    results = await asyncio.gather(*tasks)
    wall = time.monotonic() - t0
    return RoundResult(concurrency=concurrency, results=results, wall=wall)


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, round((p / 100) * (len(s) - 1))))
    return s[k]


def report(rounds: list[RoundResult]) -> None:
    if console is None:
        for rd in rounds:
            ok, fail = rd.ok, rd.failures
            lat = [r.total for r in ok]
            if ok:
                print(
                    f"conc={rd.concurrency} ok={len(ok)} fail={len(fail)} wall={rd.wall:.1f}s "
                    f"avg={mean(lat):.1f}s p95={pct(lat, 95):.1f}s thr={len(ok) / rd.wall:.2f}req/s"
                )
            else:
                print(f"conc={rd.concurrency} ok=0 fail={len(fail)} wall={rd.wall:.1f}s")
            for f in fail:
                print(f"  FAIL #{f.index}: {f.error}")
        return

    table = Table(title="Local deployment stress test (/chat)", show_lines=True)
    for col in ("Conc", "OK", "Fail", "Wall", "Throughput", "TTFB avg", "Latency avg", "p50", "p95", "max", "hits avg"):
        table.add_column(col, justify="right")

    for rd in rounds:
        ok = rd.ok
        lat = [r.total for r in ok]
        ttfb = [r.ttfb for r in ok if r.ttfb is not None]
        hits = [r.n_hits for r in ok]
        thr = len(ok) / rd.wall if rd.wall else 0.0
        fail_style = "red" if rd.failures else "dim"
        table.add_row(
            str(rd.concurrency),
            f"[green]{len(ok)}[/]",
            f"[{fail_style}]{len(rd.failures)}[/]",
            f"{rd.wall:.1f}s",
            f"{thr:.2f} req/s",
            f"{mean(ttfb):.2f}s" if ttfb else "-",
            f"{mean(lat):.1f}s" if lat else "-",
            f"{median(lat):.1f}s" if lat else "-",
            f"{pct(lat, 95):.1f}s" if lat else "-",
            f"{max(lat):.1f}s" if lat else "-",
            f"{mean(hits):.1f}" if hits else "-",
        )
    console.print()
    console.print(table)

    fails = [(rd.concurrency, f) for rd in rounds for f in rd.failures]
    if fails:
        ft = Table(title="Failures", show_lines=False)
        ft.add_column("Conc", justify="right", style="cyan")
        ft.add_column("Req", justify="right")
        ft.add_column("After", justify="right")
        ft.add_column("Error", style="red")
        for conc, f in fails:
            ft.add_row(str(conc), f"#{f.index}", f"{f.total:.1f}s", f.error or "")
        console.print()
        console.print(ft)


async def main_async(args: argparse.Namespace) -> None:
    base = (args.url or settings.server_url).rstrip("/")
    url = f"{base}/chat"
    model = args.model or settings.default_llm_model
    token = args.token or settings.chat_api_key or "SECRET_KEY"

    levels = [int(x) for x in args.ramp.split(",")] if args.ramp else [args.concurrency] * args.rounds

    if console:
        console.print(f"[bold]Target:[/] {url}   [bold]model:[/] {model}   [bold]levels:[/] {levels}")

    rounds: list[RoundResult] = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, conc in enumerate(levels, 1):
            rounds.append(await run_round(client, url, token, model, conc, i))
    report(rounds)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--concurrency", type=int, default=5, help="concurrent requests per round (default 5)")
    ap.add_argument("-r", "--rounds", type=int, default=1, help="how many rounds to run (default 1)")
    ap.add_argument(
        "--ramp",
        default=None,
        help="comma-separated concurrency levels to escalate, e.g. 5,10,20,40 (overrides -c/-r)",
    )
    ap.add_argument("--url", default=None, help=f"base server URL (default {settings.server_url})")
    ap.add_argument("-m", "--model", default=None, help=f"provider/model (default {settings.default_llm_model})")
    ap.add_argument("--token", default=None, help="bearer token for /chat (default settings.chat_api_key)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
