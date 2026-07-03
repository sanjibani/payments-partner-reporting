"""CLI entrypoint for local development and ops.

Examples:
    python scripts/run_local.py --seed
    python scripts/run_local.py --dry-run
    python scripts/run_local.py --dry-run --partner P-001
    python scripts/run_local.py --dry-run --stream
    python scripts/run_local.py --serve
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()


@app.command()
def main(
    seed: bool = typer.Option(False, "--seed", help="Generate sample CSV."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Skip LLM calls and SMTP send."
    ),
    partner: str | None = typer.Option(
        None, "--partner", help="Restrict to one partner id."
    ),
    serve: bool = typer.Option(
        False, "--serve", help="Start the FastAPI service on :8080."
    ),
    stream: bool = typer.Option(
        False, "--stream", help="Print node-level events live."
    ),
    out_dir: str | None = typer.Option(
        None, "--out-dir", help="Override the output directory."
    ),
) -> None:
    if seed:
        from scripts.seed_sample import main as seed_main

        seed_main()
        return
    if serve:
        _serve()
        return

    run_id = uuid.uuid4().hex[:12]
    out_dir = out_dir or f"out/{run_id}"
    os.environ.setdefault("WEEK_START_ISO", "2026-06-22T00:00:00+00:00")
    if partner:
        os.environ["PARTNERS_OVERRIDE"] = partner

    console.print(
        Panel(
            f"run_id: {run_id}\n"
            f"mode:   {'dry-run' if dry_run else 'live'}\n"
            f"out:    {out_dir}\n"
            f"stream: {stream}",
            title="payments-partner-reporting",
        )
    )

    from payments_reporting.graph import (
        dump_state_json,
        run_weekly,
        stream_weekly,
    )

    if stream:
        _run_stream(run_id, dry_run, out_dir)
    else:
        final = asyncio.run(
            run_weekly(run_id=run_id, dry_run=dry_run, out_dir=out_dir)
        )
        Path(out_dir, "state.json").write_text(
            dump_state_json(final), encoding="utf-8"
        )
        _summarise(final, out_dir)


def _run_stream(run_id: str, dry_run: bool, out_dir: str) -> None:
    from payments_reporting.graph import dump_state_json, stream_weekly

    async def _go() -> dict:
        merged: dict = {}
        async for event in stream_weekly(
            run_id=run_id, dry_run=dry_run, out_dir=out_dir
        ):
            for node, partial in event.items():
                merged.update(partial or {})
                keys = list((partial or {}).keys())
                console.print(f"  [cyan]{node}[/cyan] -> {keys}")
        return merged

    final = asyncio.run(_go())
    Path(out_dir, "state.json").write_text(
        dump_state_json(final), encoding="utf-8"
    )
    _summarise(final, out_dir)


def _summarise(final: dict, out_dir: str) -> None:
    errs = final.get("errors") or []
    if errs:
        console.print("[red]Errors:[/red]")
        for e in errs:
            console.print(f"  - {e}")

    console.print("\n[bold]Per-partner summary:[/bold]")
    for pid, summary in (final.get("partner_summaries") or {}).items():
        console.print(
            f"  [cyan]{pid}[/cyan] {summary.partner_name}: "
            f"{summary.total_requests:,} req, "
            f"{summary.overall_success_rate * 100:.1f}% success"
        )

    console.print("\n[bold]Dispatch:[/bold]")
    for r in final.get("send_results") or []:
        flag = "[green]OK[/green]" if r.success else "[red]FAIL[/red]"
        console.print(f"  {flag} {r.partner_id} -> {r.message_id or r.error}")

    console.print("\n[bold]Node timings (ms):[/bold]")
    for name, dur in (final.get("node_durations_ms") or {}).items():
        console.print(f"  {name:<20} {dur:>8.2f}")

    console.print(f"\nFull state at {out_dir}/state.json")


def _serve() -> None:
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=False,
    )


if __name__ == "__main__":
    app()