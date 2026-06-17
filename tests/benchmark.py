"""Benchmarking script for the search API.

Run with: uv run python tests/benchmark.py

Requires the server to be running on port 8000 and OpenSearch to be reachable.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, TypedDict

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from data_commons_search.config import settings
from data_commons_search.models import SummarizedSearchResponse

console = Console()

SERVER_PORT = 8000
MAX_RETRIES = 3
TIMEOUT = 120


class ExpectedResult(TypedDict):
    id: str
    file_extensions: list[str]


class TestItem(TypedDict):
    input: str
    expected_results: list[ExpectedResult]
    lang: str


test_items: list[TestItem] = [
    {
        "input": "CO2 saturation in Amazonian rivers",
        "lang": "en",
        "expected_results": [
            {
                "id": "https://doi.org/10.17026/DANS-ZMD-STSG",
                "file_extensions": ["tab-separated-values", "xml", "pdf"],
            }
        ],
    },
    {
        "input": "What datasets are relevant for my project on climate-driven changes in bird competition?",
        "lang": "en",
        "expected_results": [
            {
                "id": "https://doi.org/10.34894/85Y1HY",
                "file_extensions": [],
            },
        ],
    },
    {
        "input": "Wader breeding densities in Europe",
        "lang": "en",
        "expected_results": [
            {
                "id": "https://doi.org/10.17026/DANS-XSV-355J",
                "file_extensions": ["vnd.oasis.opendocument.spreadsheet", "zip", "plain"],
            },
        ],
    },
    {
        "input": "Covid 19 in EU",
        "lang": "en",
        "expected_results": [
            {
                "id": "https://doi.org/10.34894/KNSTJ6",
                "file_extensions": [],
            },
        ],
    },
    {
        "input": "Datasets about sources of greenhouse gas emissions in polders published in the last 3 years",
        "lang": "en",
        "expected_results": [
            {
                "id": "https://doi.org/10.17026/LS/OWBB7V",
                "file_extensions": [],
            },
        ],
    },
]
# TODO: add "Atmospheric CO2 concentration and carbon cycle monitoring data"

# --- Conditions ---
# Each Condition defines one combination of parameters to benchmark.
# To add a new axis (e.g. embedding_model), add a field here and update
# `all_conditions()` and `label()` below.


@dataclass
class Condition:
    llm_model: str
    # Future axes:
    # embedding_model: str = "default"
    # chunking_strategy: str = "default"

    def label(self) -> str:
        model = self.llm_model.split("/")[-1] if "/" in self.llm_model else self.llm_model
        return f"llm={model}"


def all_conditions() -> list[Condition]:
    llm_models = [
        "cesnet/qwen3-coder",
        "mistralai/mistral-medium-latest",
        # "cesnet/gpt-oss-120b",
        # "cesnet/kimi-k2.5",
        # "cesnet/deepseek-v3.2",
        # "cesnet/glm-5",
    ]
    # When adding a new axis, nest loops here:
    # for llm in llm_models:
    #     for emb in embedding_models:
    #         yield Condition(llm_model=llm, embedding_model=emb)
    return [Condition(llm_model=m) for m in llm_models]


# --- Result tracking ---


@dataclass
class ConditionResult:
    condition: Condition
    successes: int = 0
    total: int = 0
    failures: int = 0
    runtimes: list[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total else 0.0

    @property
    def avg_runtime(self) -> float:
        return sum(self.runtimes) / len(self.runtimes) if self.runtimes else 0.0


# --- HTTP helpers ---


def opensearch_is_available() -> bool:
    try:
        r = httpx.get(settings.opensearch_url.rstrip("/") + "/_cluster/health", timeout=2.0)
        return 200 <= r.status_code < 400
    except Exception:
        return False


def process_stream(resp: httpx.Response) -> list[Any]:
    events = []
    for raw_line in resp.iter_lines():
        if raw_line:
            stripped = raw_line.strip()
            if stripped.startswith("data: "):
                try:
                    event = json.loads(stripped[6:])
                    events.append(event)
                except json.JSONDecodeError:
                    continue
    return events


def run_search(test_item: TestItem, condition: Condition) -> tuple[list[str], float]:
    """Run one search query. Returns (found_ids, elapsed_seconds)."""
    payload = {
        "items": [{"type": "message", "role": "user", "content": [{"text": test_item["input"]}]}],
        "model": condition.llm_model,
    }
    headers = {"Content-Type": "application/json", "Authorization": "Bearer SECRET_KEY"}

    t0 = time.monotonic()
    # console.log(f"[dim]-> QUERY")
    with httpx.stream(
        "POST",
        f"http://127.0.0.1:{SERVER_PORT}/chat",
        headers=headers,
        json=payload,
        follow_redirects=True,
        timeout=TIMEOUT,
    ) as resp:
        # console.log(f"[dim]-> status={resp.status_code}, reading stream...[/]")
        resp.raise_for_status()
        events = process_stream(resp)
    # console.log(f"[dim]-> done, {len(events)} events in {time.monotonic() - t0:.1f}s[/]")
    elapsed = time.monotonic() - t0
    found_ids: list[str] = []
    for event in events:
        if event.get("type") == "TOOL_CALL_RESULT" and event.get("tool_call_id") == "rerank_results":
            ranked = SummarizedSearchResponse.model_validate_json(event["content"])
            found_ids = [hit.id for hit in ranked.hits]
            break
    return found_ids, elapsed


def check_success(test_item: TestItem, found_ids: list[str]) -> bool:
    """Return True if all expected IDs appear somewhere in the result list."""
    expected_ids = {r["id"] for r in test_item["expected_results"]}
    return expected_ids.issubset(set(found_ids))


# --- Main benchmark loop ---


def run_benchmark() -> None:
    if not opensearch_is_available():
        console.print(Panel(f"[bold red]OpenSearch unreachable at {settings.opensearch_url}[/]", title="Error"))
        return

    conditions = all_conditions()
    results: list[ConditionResult] = [ConditionResult(condition=c) for c in conditions]
    total_runs = len(conditions) * len(test_items)

    # Track per-item detail rows for a live log table
    detail_rows: list[tuple[str, str, str, str, str]] = []  # condition, query, status, time, note

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        overall = progress.add_task("[bold]Overall", total=total_runs)
        for cr in results:
            cond_task = progress.add_task(f"[cyan]{cr.condition.label()}", total=len(test_items))
            for item in test_items:
                short_query = item["input"][:52] + "..." if len(item["input"]) > 52 else item["input"]
                progress.update(cond_task, description=f"[cyan]{cr.condition.label()}[/]  [dim]{short_query}[/]")
                found_ids: list[str] = []
                elapsed = 0.0
                last_exc: Exception | None = None
                for _attempt in range(MAX_RETRIES):
                    try:
                        found_ids, elapsed = run_search(item, cr.condition)
                        last_exc = None
                        break
                    except Exception as e:
                        last_exc = e
                cr.total += 1
                if last_exc is not None:
                    cr.failures += 1
                    note = str(last_exc)[:80]
                    detail_rows.append((cr.condition.label(), short_query, "[red]FAILED[/]", "-", note))
                else:
                    cr.runtimes.append(elapsed)
                    success = check_success(item, found_ids)
                    if success:
                        cr.successes += 1
                        status_str = "[green]OK[/]"
                        note = ""
                    else:
                        expected = {r["id"] for r in item["expected_results"]}
                        missing = expected - set(found_ids)
                        status_str = "[yellow]MISS[/]"
                        retrieved = ", ".join(found_ids) if found_ids else "[]"
                        note = f"missing: {', '.join(missing)} | retrieved: {retrieved}"
                    detail_rows.append((cr.condition.label(), short_query, status_str, f"{elapsed:.1f}s", note))
                progress.advance(cond_task)
                progress.advance(overall)
            progress.update(cond_task, description=f"[cyan]{cr.condition.label()}[/]  [dim]done[/]")

    # --- Detail log ---
    detail_table = Table(title="Run Details", show_lines=False, highlight=True)
    detail_table.add_column("Condition", style="cyan", no_wrap=True)
    detail_table.add_column("Query", max_width=40)
    detail_table.add_column("Status", justify="center")
    detail_table.add_column("Time", justify="right")
    detail_table.add_column("Note", style="dim")

    for row in detail_rows:
        detail_table.add_row(*row)

    console.print()
    console.print(detail_table)

    # --- Summary table ---
    summary = Table(title="Summary", show_lines=True)
    summary.add_column("Condition", style="cyan", no_wrap=True)
    summary.add_column("Success", justify="center")
    summary.add_column("Rate", justify="center")
    summary.add_column("Failed", justify="center")
    summary.add_column("Avg (s)", justify="center")

    for cr in results:
        rate = cr.success_rate * 100
        rate_style = "green" if rate >= 80 else "yellow" if rate >= 40 else "red"
        summary.add_row(
            cr.condition.label(),
            f"{cr.successes}/{cr.total}",
            Text(f"{rate:.0f}%", style=rate_style),
            str(cr.failures) if cr.failures else "-",
            f"{cr.avg_runtime:.1f}" if cr.runtimes else "-",
        )

    console.print()
    console.print(summary)


if __name__ == "__main__":
    run_benchmark()
