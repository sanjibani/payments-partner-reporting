"""Main-graph node timing and error capture wrapper.

Each main-graph node is wrapped in run_node() so a single node failure
appends to state.errors and returns a partial instead of crashing the
graph. Per-node duration is included in the returned partial so
LangGraph merges it into the running state.
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable

from ..state import GraphState


async def run_node(
    state: GraphState,
    name: str,
    fn: Callable[[GraphState], Awaitable[dict]],
) -> dict:
    """Wrap a node with timing + error capture."""
    t0 = time.perf_counter()
    try:
        partial = await fn(state)
    except Exception as e:  # noqa: BLE001 -- nodes are the failure boundary
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


__all__ = ["run_node"]