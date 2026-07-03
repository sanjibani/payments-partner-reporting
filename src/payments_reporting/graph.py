"""LangGraph StateGraph wiring.

Linear flow with per-node timing:

    trigger -> ingest -> aggregate -> analysis_agent -> chart_generator ->
    email_agent -> dispatch_emails

Each node is a pure async function over GraphState. We wrap them in
run_node() for timing + error capture so a single node failure does not
kill the whole batch.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from .nodes.aggregate import aggregate
from .nodes.analysis import analysis_agent
from .nodes.charts import chart_generator
from .nodes.dispatch import dispatch_emails
from .nodes.email import email_agent
from .nodes.ingest import ingest
from .nodes.trigger import trigger
from .state import GraphState, initial_state, state_snapshot

log = logging.getLogger(__name__)


def _wrap(name: str, fn):  # type: ignore[no-untyped-def]
    """Wrap a node so timing + error capture is uniform across the graph."""
    from .nodes._timing import run_node

    async def wrapped(state: GraphState) -> dict[str, Any]:
        return await run_node(state, name, fn)

    return wrapped


def build_graph():
    """Compile the LangGraph StateGraph. Returns a CompiledStateGraph."""
    g = StateGraph(GraphState)

    g.add_node("trigger", _wrap("trigger", trigger))
    g.add_node("ingest", _wrap("ingest", ingest))
    g.add_node("aggregate", _wrap("aggregate", aggregate))
    g.add_node("analysis_agent", _wrap("analysis_agent", analysis_agent))
    g.add_node("chart_generator", _wrap("chart_generator", chart_generator))
    g.add_node("email_agent", _wrap("email_agent", email_agent))
    g.add_node("dispatch_emails", _wrap("dispatch_emails", dispatch_emails))

    g.set_entry_point("trigger")
    g.add_edge("trigger", "ingest")
    g.add_edge("ingest", "aggregate")
    g.add_edge("aggregate", "analysis_agent")
    g.add_edge("analysis_agent", "chart_generator")
    g.add_edge("chart_generator", "email_agent")
    g.add_edge("email_agent", "dispatch_emails")
    g.add_edge("dispatch_emails", END)

    return g.compile()


_compiled = None


def get_graph():
    """Cached compiled graph for the FastAPI app."""
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


async def run_weekly(
    run_id: str,
    dry_run: bool = False,
    out_dir: str | None = None,
) -> GraphState:
    """Top-level entrypoint: kick off the weekly batch and return final state."""
    state = initial_state(run_id=run_id, dry_run=dry_run)
    if out_dir:
        state["out_dir"] = out_dir  # type: ignore[typeddict-unknown-key]
    graph = get_graph()
    final = await graph.ainvoke(state)
    return final  # type: ignore[return-value]


def dump_state_json(state: GraphState) -> str:
    """Convenience for tests and the FastAPI response."""
    import json

    return json.dumps(state_snapshot(state), indent=2, default=str)


__all__ = ["build_graph", "dump_state_json", "get_graph", "run_weekly"]