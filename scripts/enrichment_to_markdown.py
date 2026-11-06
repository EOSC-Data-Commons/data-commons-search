"""Convert an LLM enrichment JSONL file into a readable Markdown report, sorted from best to worst validation score.

Usage: uv run scripts/enrichment_to_markdown.py tmp/2_enrichment_exp_latest_dedicated_db_table.jsonl [output.md]
"""

import json
import sys
from pathlib import Path

SCORE_KEYS = [
    "accuracy_score",
    "completeness_score",
    "enriched_description_score",
    "semantic_usefulness_score",
    "input_quality_score",
    "output_quality_score",
]


def avg_score(rec: dict) -> float:
    """Mean of the validation scores."""
    val = rec.get("validation") or {}
    scores = [val[k] for k in SCORE_KEYS if isinstance(val.get(k), (int, float))]
    return sum(scores) / len(scores) if scores else 0.0


def sort_key(rec: dict) -> tuple:
    """Records without hallucination first, then by mean score."""
    return (not (rec.get("validation") or {}).get("hallucination_detected"), avg_score(rec))


def quote(text: str) -> str:
    """Markdown blockquote that keeps multi-line text inside the quote."""
    return "> " + (text or "_none_").strip().replace("\n", "\n> ")


def bullet_list(items: list) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- _none_"


def to_markdown(rec: dict, rank: int) -> str:
    val = rec.get("validation") or {}
    sem = rec.get("semantic_metadata") or {}
    halluc = " · **HALLUCINATION**" if val.get("hallucination_detected") else ""
    scores = " · ".join(f"{k.removesuffix('_score').replace('_', ' ')} {val.get(k, '?')}" for k in SCORE_KEYS)
    lines = [
        f"## {rank}. {rec.get('title', '?')} ({avg_score(rec):.2f}{halluc})",
        "",
        f"`{rec.get('source')}` · `{rec.get('id')}` · {rec.get('status')}",
        "",
        f"**Scores:** {scores}",
        "",
        "**Original description:**",
        "",
        quote(rec.get("description")),
        "",
        "**Enriched description:**",
        "",
        quote(rec.get("enriched_description")),
        "",
        f"**Input:** {rec.get('input_description') or '_none_'}",
        "",
        f"**Output:** {rec.get('output_description') or '_none_'}",
        "",
        f"**Keywords:** {', '.join(rec.get('keywords') or []) or '_none_'}",
        "",
    ]
    # Semantic metadata lists (supported_tasks, scientific_domains, methods_and_techniques)
    for key, items in sem.items():
        lines += [f"**{key.replace('_', ' ').capitalize()}:** {', '.join(items) or '_none_'}", ""]
    lines += [
        f"**Assessment:** {val.get('overall_assessment', '')}",
        "",
        "**Issues:**",
        "",
        bullet_list(val.get("issues") or []),
        "",
    ]
    if rec.get("error"):
        lines += [f"**Error:** `{rec['error']}`", ""]
    return "\n".join(lines)


def main() -> None:
    in_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else in_path.with_suffix(".md")
    records = [json.loads(line) for line in in_path.read_text().splitlines() if line.strip()]
    records.sort(key=sort_key, reverse=True)

    # Header with a summary table, then one section per record
    md = [
        f"# {in_path.stem}",
        "",
        f"{len(records)} records · score = mean of {', '.join(SCORE_KEYS)} (records with hallucination ranked last)",
        "",
        "| # | Title | Score | Halluc. |",
        "|---|---|---|---|",
    ]
    for i, r in enumerate(records, 1):
        halluc = "yes" if (r.get("validation") or {}).get("hallucination_detected") else ""
        md.append(f"| {i} | {r.get('title', '?')} | {avg_score(r):.2f} | {halluc} |")
    md += ["", "---", ""]
    md += [to_markdown(r, i) + "\n---\n" for i, r in enumerate(records, 1)]

    out_path.write_text("\n".join(md))
    print(f"Wrote {len(records)} records to {out_path}")


if __name__ == "__main__":
    main()
