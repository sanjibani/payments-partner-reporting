"""LangGraph StateGraph wiring with Send API, conditional edges,
checkpointing, and streaming support.

Graph topology:

    trigger
      |
      v
    ingest -----> [no data] -> END
      |
      v
    aggregate
      |
      v   (conditional edge -> Send fan-out, one branch per partner)
    partner_pipeline (compiled subgraph, runs N times in parallel)
      |              analyze -> chart -> email per partner
      v
    dispatch_emails  -----> [send failures] -> alert_failure -> END
      |
      v
    END

LangGraph features actually used here:
- StateGraph with a TypedDict schema
- add_node / add_edge for linear nodes
- add_conditional_edges with a routing function for branching
- Send API for parallel per-partner fan-out (the partner_pipeline
  subgraph runs once per partner in parallel; partial returns merge
  into the main state keyed by partner_id)
- Compiled subgraph embedded as a node (partner_pipeline = build_partner_subgraph())
- MemorySaver checkpointer for replay/debug (thread_id per run)
- graph.stream() for live node-level events
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Send

from .nodes._timing import run_node
from .nodes.aggregate import aggregate
from .nodes.dispatch import dispatch_emails
from .nodes.ingest import ingest
from .nodes.trigger import trigger
from .partner_pipeline import partner_pipeline_node, set_pipeline_ctx
from .state import GraphState, initial_state, state_snapshot

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conditional edge routing functions
# ---------------------------------------------------------------------------


def route_after_ingest(state: GraphState) -> Literal["aggregate", END]:
    """If ingest produced no rows, skip to END. Otherwise aggregate."""
    raw = state.get("raw_metrics") or {}
    if not raw:
        log.warning("route.after_ingest.empty no_partners=%s", list(raw.keys()))
        return END
    return "aggregate"


def fan_out_partners(state: GraphState) -> list[Send]:
    """Conditional edge: dispatch one Send per partner to the subgraph.

    This is the marquee LangGraph pattern. After aggregate, we do not
    know how many partners we have. A linear edge would force us to
    bake that in at compile time. Instead, this function inspects the
    state at runtime and returns one Send per partner. LangGraph runs
    them in parallel and merges the partials back into GraphState.

    The Send state only carries partner_id + summary. Read-only
    metadata (run_id, dry_run, out_dir) is pushed into a module-level
    context the subgraph nodes read at invocation time. This avoids
    LangGraph's INVALID_CONCURRENT_GRAPH_UPDATE error when multiple
    branches try to write overlapping keys back into the parent state.
    """
    summaries = state.get("partner_summaries") or {}
    out_dir = state.get("out_dir") or f"out/{state['run_id']}"

    set_pipeline_ctx(
        run_id=state["run_id"],
        dry_run=bool(state.get("dry_run")),
        out_dir=out_dir,
    )

    sends: list[Send] = [
        Send(
            "partner_pipeline",
            {"partner_id": pid, "summary": summary},
        )
        for pid, summary in summaries.items()
    ]
    log.info("fan_out_partners count=%d", len(sends))
    return sends


def route_after_dispatch(state: GraphState) -> Literal["alert_failure", END]:
    """If any send failed, route to alert_failure. Otherwise END."""
    results = state.get("send_results") or []
    failed = [r for r in results if not getattr(r, "success", False)]
    if failed:
        log.warning("route.after_dispatch.failures=%d", len(failed))
        return "alert_failure"
    return END


async def alert_failure(state: GraphState) -> dict[str, Any]:
    """Terminal alert node. In production this would page on-call.

    In this build it appends a structured error record to state.errors
    so the run's audit trail captures the dispatch failure.
    """
    failed = [
        r for r in (state.get("send_results") or [])
        if not getattr(r, "success", False)
    ]
    errs = list(state.get("errors") or [])
    for r in failed:
        errs.append(
            f"dispatch_failed partner={r.partner_id} error={r.error}"
        )
    log.error("alert_failure dispatched=%d", len(failed))
    return {"errors": errs}


# ---------------------------------------------------------------------------
# Wrapper for main-graph nodes (timing + error capture)
# ---------------------------------------------------------------------------


def _wrap(name: str, fn):  # type: ignore[no-untyped-def]
    async def wrapped(state: GraphState) -> dict[str, Any]:
        return await run_node(state, name, fn)

    return wrapped


# ---------------------------------------------------------------------------
# Build the main graph
# ---------------------------------------------------------------------------


def build_graph(checkpointer: MemorySaver | None = None):
    """Compile the main graph. Optional checkpointer for replay/debug."""
    g = StateGraph(GraphState)

    # Linear nodes
    g.add_node("trigger", _wrap("trigger", trigger))
    g.add_node("ingest", _wrap("ingest", ingest))
    g.add_node("aggregate", _wrap("aggregate", aggregate))
    g.add_node("dispatch_emails", _wrap("dispatch_emails", dispatch_emails))
    g.add_node("alert_failure", _wrap("alert_failure", alert_failure))

    # Per-partner pipeline node. LangGraph dispatches this once per
    # Send invocation, in parallel. Each invocation runs the full
    # analyze -> chart -> email sequence for one partner.
    g.add_node("partner_pipeline", partner_pipeline_node)

    # Wiring
    g.set_entry_point("trigger")
    g.add_edge("trigger", "ingest")

    # Conditional edge after ingest: skip to END if no data.
    g.add_conditional_edges(
        "ingest",
        route_after_ingest,
        {"aggregate": "aggregate", END: END},
    )

    # Conditional edge after aggregate: fan out one branch per partner
    # via the Send API. LangGraph runs the partner_pipeline node once
    # per Send, in parallel. The partner_pipeline node then runs the
    # full analyze -> chart -> email sequence for that partner.
    g.add_conditional_edges("aggregate", fan_out_partners)

    # All parallel branches converge at dispatch_emails.
    g.add_edge("partner_pipeline", "dispatch_emails")

    # Conditional edge after dispatch: route failures to alert_failure.
    g.add_conditional_edges(
        "dispatch_emails",
        route_after_dispatch,
        {"alert_failure": "alert_failure", END: END},
    )

    g.add_edge("alert_failure", END)

    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Cached compiled graph + thread_id helper
# ---------------------------------------------------------------------------


_compiled_with_checkpoint = None
_compiled_no_checkpoint = None


def get_graph(with_checkpoint: bool = True):
    """Cached compiled graph.

    with_checkpoint=True: includes a MemorySaver so each run gets a
    thread_id and the run can be inspected / resumed after the fact.
    """
    global _compiled_with_checkpoint, _compiled_no_checkpoint
    if with_checkpoint:
        if _compiled_with_checkpoint is None:
            _compiled_with_checkpoint = build_graph(
                checkpointer=MemorySaver()
            )
        return _compiled_with_checkpoint
    if _compiled_no_checkpoint is None:
        _compiled_no_checkpoint = build_graph()
    return _compiled_no_checkpoint


# ---------------------------------------------------------------------------
# Top-level entrypoints
# ---------------------------------------------------------------------------


async def run_weekly(
    run_id: str,
    dry_run: bool = False,
    out_dir: str | None = None,
    thread_id: str | None = None,
) -> GraphState:
    """Run the weekly batch and return final state."""
    state = initial_state(run_id=run_id, dry_run=dry_run)
    if out_dir:
        state["out_dir"] = out_dir
    graph = get_graph(with_checkpoint=True)
    config = {"configurable": {"thread_id": thread_id or run_id}}
    final = await graph.ainvoke(state, config=config)
    return final  # type: ignore[return-value]


async def stream_weekly(
    run_id: str,
    dry_run: bool = False,
    out_dir: str | None = None,
    thread_id: str | None = None,
):
    """Yield node-level events as the graph runs.

    Yields tuples of (node_name, partial_state_dict). Useful for live
    observability in the CLI or for SSE in the FastAPI service.
    """
    state = initial_state(run_id=run_id, dry_run=dry_run)
    if out_dir:
        state["out_dir"] = out_dir
    graph = get_graph(with_checkpoint=True)
    config = {"configurable": {"thread_id": thread_id or run_id}}
    async for event in graph.astream(state, config=config):
        # event is dict[node_name, partial_state]
        yield event


def get_checkpoint_state(thread_id: str) -> dict[str, Any] | None:
    """Inspect the saved state for a thread_id (post-mortem / replay)."""
    graph = get_graph(with_checkpoint=True)
    config = {"configurable": {"thread_id": thread_id}}
    snap = graph.get_state(config)
    if snap is None:
        return None
    return state_snapshot(snap.values)  # type: ignore[arg-type]


def dump_state_json(state: GraphState) -> str:
    import json

    return json.dumps(state_snapshot(state), indent=2, default=str)


__all__ = [
    "build_graph",
    "dump_state_json",
    "fan_out_partners",
    "get_checkpoint_state",
    "get_graph",
    "route_after_dispatch",
    "route_after_ingest",
    "run_weekly",
    "stream_weekly",
]