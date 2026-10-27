"""Command line: `lmplab <step>` or `lmplab all`."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable

from lmplab.config import Settings, settings


def _fetch(s: Settings, args: argparse.Namespace) -> str:
    from lmplab import fetch

    return "; ".join(fetch.run(s, force=getattr(args, "force", False)))


def _ingest(s: Settings, args: argparse.Namespace) -> str:
    from lmplab import ingest

    report = ingest.run(s)
    return (f"{report['warehouse_rows']:,} area-hours, {report['areas']} areas, join coverage "
            f"{report['join_coverage']:.1%}, {len(report['findings'])} findings")


def _analyze(s: Settings, args: argparse.Namespace) -> str:
    from lmplab import analytics

    summary = analytics.run(s)
    return (f"annual ALP ${summary['annual_alp']}/MWh, any separation in {summary['share_any_separation']:.0%} of hours, "
            f"uniform-price surplus ${summary['annual_surplus_musd']}M")


def _forecast(s: Settings, args: argparse.Namespace) -> str:
    from lmplab import forecast

    report = forecast.run(s)
    surrogate = report["point"]["LightGBM (P50)"]
    return (f"test MAE ${surrogate['mae']}/MWh ({surrogate['skill_vs_uniform']:.0%} better than a uniform price), "
            f"80% interval coverage {report['intervals']['conformal']['coverage']:.1%}, "
            f"separation AUC {report['separation']['auc']:.3f}")


def _sced(s: Settings, args: argparse.Namespace) -> str:
    from lmplab import grid

    report = grid.run(s)
    checks = report["aeso_examples"]
    deltas = report["deltas"]
    return (f"AESO examples matched {sum(c['matches_published'] for c in checks)}/{len(checks)} "
            f"(see Example 2), +400 MW SE wind lowers SE price ${deltas['se_price_drop_with_wind']}/MWh, "
            f"uprating {deltas['top_ranked_line']} saves ${deltas['upgrade_savings_musd_per_year']}M/yr "
            f"(duals estimated ${deltas['dual_estimate_musd_per_year']}M)")


def _pdup(s: Settings, args: argparse.Namespace) -> str:
    from lmplab import pdup

    report = pdup.run(s)
    return "; ".join(f"{r['project']['name']}: {r['outcome']} ({r['errors']} deficiencies, {r['warnings']} clarifications, "
                     f"summary by {r['summary_source']})" for r in report["reviews"])


def _agent(s: Settings, args: argparse.Namespace) -> str:
    from lmplab import agent

    report = agent.run(s)
    modes = sorted({t["mode"] for t in report["transcripts"]})
    calls = sum(len(t["steps"]) for t in report["transcripts"])
    return f"{len(report['transcripts'])} demo questions answered with {calls} tool calls ({', '.join(modes)})"


def _report(s: Settings, args: argparse.Namespace) -> str:
    from lmplab.report import build

    page = build.run(s)
    return f"dashboard at {page} ({page.stat().st_size / 1e6:.1f} MB)"


STEPS: dict[str, tuple[str, Callable[[Settings, argparse.Namespace], str]]] = {
    "fetch": ("Download AESO public data", _fetch),
    "ingest": ("Load into DuckDB with data-quality checks", _ingest),
    "analyze": ("Price separation, settlement surplus, pricing hubs", _analyze),
    "forecast": ("Day-ahead basis forecast (LightGBM) with conformal intervals", _forecast),
    "sced": ("SCED engine: AESO examples and Alberta what-ifs", _sced),
    "pdup": ("PDUP model validation and review letters", _pdup),
    "agent": ("Agentic analyst: demo questions answered with tools", _agent),
    "report": ("Build the static dashboard", _report),
}


def _run_steps(names: list[str], args: argparse.Namespace) -> int:
    s = settings()
    for position, name in enumerate(names, start=1):
        title, step = STEPS[name]
        print(f"[{position}/{len(names)}] {title} ...", flush=True)
        started = time.time()
        print(f"      {step(s, args)}  ({time.time() - started:.1f}s)", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lmplab", description="Alberta LMP Lab")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (title, _) in STEPS.items():
        step = sub.add_parser(name, help=title)
        if name == "fetch":
            step.add_argument("--force", action="store_true", help="download again even if present")
    everything = sub.add_parser("all", help="run every step in order")
    everything.add_argument("--no-llm", action="store_true", help="never call the local LLM")
    question = sub.add_parser("ask", help="ask the agentic analyst a question")
    question.add_argument("question", help="the question, in quotes")
    question.add_argument("--no-llm", action="store_true", help="use the deterministic planner")
    args = parser.parse_args(argv)
    if getattr(args, "no_llm", False):
        os.environ["LMPLAB_NO_LLM"] = "1"
    if args.command == "ask":
        from lmplab import agent

        result = agent.ask(settings(), args.question)
        for step in result["steps"]:
            print(f"  -> {step['tool']}({step['arguments']})")
        print(f"\n{result['answer']}\n\n[{result['mode']}, {result['seconds']}s]")
        return 0
    if args.command == "all":
        return _run_steps(list(STEPS), args)
    return _run_steps([args.command], args)


if __name__ == "__main__":
    sys.exit(main())
