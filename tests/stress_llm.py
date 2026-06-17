"""Stress test the LLM provider with concurrent requests.

Fires a batch of concurrent chat-completion requests directly at the provider
(reusing `load_chat_model`, so it honours your configured provider/model/keys)
and reports latency, time-to-first-token, throughput and failures. Each query is
made unique with a nonce so the provider cache never serves a stored answer.

Usage:
    uv run --env-file keys.env tests/stress_llm.py                 # 5 concurrent, 1 round
    uv run --env-file keys.env tests/stress_llm.py -c 20           # 20 concurrent
    uv run --env-file keys.env tests/stress_llm.py -c 10 -r 3      # 10 concurrent x 3 rounds
    uv run --env-file keys.env tests/stress_llm.py --ramp 5,10,20  # escalate to find the limit
    uv run --env-file keys.env tests/stress_llm.py -m openrouter/qwen/qwen3-coder-flash
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field
from statistics import mean, median

from langchain.messages import HumanMessage

from data_commons_search.config import settings
from data_commons_search.utils import load_chat_model

try:
    from rich.console import Console
    from rich.table import Table

    console = Console()
except ImportError:  # rich is a project dep, but stay graceful
    console = None  # type: ignore[assignment]

# Queries to rotate through (a nonce is appended to bust the provider cache).
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

PROMPT = (
    "You are helping a data search engine. In 2-3 sentences, describe what kind of "
    "research datasets would be relevant to this query: {query}"
)


@dataclass
class RequestResult:
    index: int
    ok: bool = False
    ttft: float | None = None  # time to first token (s)
    total: float = 0.0  # total latency (s)
    n_chunks: int = 0
    chars: int = 0
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


async def one_request(model, index: int, query: str) -> RequestResult:
    """Fire a single streaming request and time it."""
    res = RequestResult(index=index)
    # Unique nonce so the provider can't return a cached completion.
    msg = HumanMessage(PROMPT.format(query=f"{query} [req#{index} t={time.time_ns()}]"))
    t0 = time.monotonic()
    try:
        async for chunk in model.astream([msg]):
            if res.ttft is None:
                res.ttft = time.monotonic() - t0
            res.n_chunks += 1
            res.chars += len(chunk.content or "")
        res.total = time.monotonic() - t0
        res.ok = True
    except Exception as e:
        res.total = time.monotonic() - t0
        res.error = f"{type(e).__name__}: {e}"[:160]
    return res


async def run_round(model, model_name: str, concurrency: int, round_no: int) -> RoundResult:
    if console:
        console.print(f"[bold cyan]Round {round_no}[/]  ·  {concurrency} concurrent  ·  [dim]{model_name}[/]")
    else:
        print(f"Round {round_no} · {concurrency} concurrent · {model_name}")
    tasks = [one_request(model, i, QUERIES[i % len(QUERIES)]) for i in range(concurrency)]
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
            print(
                f"conc={rd.concurrency} ok={len(ok)} fail={len(fail)} wall={rd.wall:.1f}s "
                f"avg={mean(lat):.1f}s p95={pct(lat, 95):.1f}s thr={len(ok) / rd.wall:.2f}req/s"
                if ok
                else f"conc={rd.concurrency} ok=0 fail={len(fail)} wall={rd.wall:.1f}s"
            )
            for f in fail:
                print(f"  FAIL #{f.index}: {f.error}")
        return

    table = Table(title="LLM provider stress test", show_lines=True)
    for col in ("Conc", "OK", "Fail", "Wall", "Throughput", "TTFT avg", "Latency avg", "p50", "p95", "max"):
        table.add_column(col, justify="right")

    for rd in rounds:
        ok = rd.ok
        lat = [r.total for r in ok]
        ttft = [r.ttft for r in ok if r.ttft is not None]
        thr = len(ok) / rd.wall if rd.wall else 0.0
        fail_style = "red" if rd.failures else "dim"
        table.add_row(
            str(rd.concurrency),
            f"[green]{len(ok)}[/]",
            f"[{fail_style}]{len(rd.failures)}[/]",
            f"{rd.wall:.1f}s",
            f"{thr:.2f} req/s",
            f"{mean(ttft):.2f}s" if ttft else "-",
            f"{mean(lat):.1f}s" if lat else "-",
            f"{median(lat):.1f}s" if lat else "-",
            f"{pct(lat, 95):.1f}s" if lat else "-",
            f"{max(lat):.1f}s" if lat else "-",
        )
    console.print()
    console.print(table)

    # List failures (most useful part of a stress test).
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
    model_name = args.model or settings.default_llm_model
    model = load_chat_model(model_name)

    levels = [int(x) for x in args.ramp.split(",")] if args.ramp else [args.concurrency] * args.rounds

    if console:
        console.print(f"[bold]Model:[/] {model_name}   [bold]levels:[/] {levels}")

    rounds: list[RoundResult] = []
    for i, conc in enumerate(levels, 1):
        rounds.append(await run_round(model, model_name, conc, i))
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
    ap.add_argument("-m", "--model", default=None, help=f"provider/model (default {settings.default_llm_model})")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
