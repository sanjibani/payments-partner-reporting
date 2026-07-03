"""Shared node timing + error helpers."""

from __future__ import annotations

import time
from typing import Awaitable, Callable

from ..state import GraphState


async def run_node(
    state: GraphState,
    name: str,
    fn: Callable[[GraphState], Awaitable[dict]],
) -> dict:
    """Wrap a node with timing + error capture.

    Nodes are typed as `(state) -> partial state`. They never raise; on
    failure they append to `state.errors` and return a partial so the
    rest of the graph can still run. Timing is returned in the partial
    so LangGraph merges it into the running state.
    """
    t0 = time.perf_counter()
    try:
        partial = await fn(state)
    except Exception as e:  # noqa: BLE001  --  nodes are the failure boundary
        errs = list(state.get("errors") or [])
        errs.append(f"{name}: {type(e).__name__}: {e}")
        partial = {"errors": errs}
    finally:
        dur = round((time.perf_counter() - t0) * 1000, 2)
        durations = dict(state.get("node_durations_ms") or {})
        durations[name] = dur
        partial = dict(partial or {})
        partial["node_durations_ms"] = durations
    return partial