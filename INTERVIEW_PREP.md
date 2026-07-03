# Interview prep -- payments partner reporting

The interview questions you flagged, and the answers you should be able to
give cold after implementing this project. The full design contract is
in `plan.md`.

## Q1. Where does the runtime live in Azure?

A Python LangGraph service packaged as a Docker image and deployed to
Azure Container Apps with `minReplicas=0`. A Logic App with a weekly
recurrence trigger POSTs to the container's `/run-weekly` endpoint.

The graph is wrapped in a FastAPI app (`api/main.py`) exposing
`POST /run-weekly`, `POST /run-weekly/stream` (SSE), `GET /runs/<id>`,
`GET /threads/<id>`. Outside the weekly window the container scales to
0 and you pay no compute.

## Q2. What are the inputs and outputs of each node?

The table. Memorise it.

### Main graph (6 nodes)

| Node              | Reads from state                                          | Writes to state                              |
|-------------------|-----------------------------------------------------------|----------------------------------------------|
| `trigger`         | (nothing; env / request overrides)                        | `week_start`, `week_end`, `partners`         |
| `ingest`          | `week_start`, `week_end`, `partners`                      | `raw_metrics`, `last_week_metrics`           |
| `aggregate`       | `raw_metrics`, `last_week_metrics`, partners              | `partner_summaries` (DTO per partner)        |
| `partner_pipeline`| dispatched via `Send` once per partner (parallel)         | `analyses[pid]`, `charts[pid]`, `email_bodies[pid]` |
| `dispatch_emails` | `email_bodies`, summaries, `run_id`, `out_dir`           | `send_results` (list of `SendResult`)        |
| `alert_failure`   | `send_results` (failures only)                            | `errors` (appended)                          |

### Conditional edges

- After `ingest`: empty raw_metrics -> END, else -> aggregate.
- After `aggregate`: list[Send] one per partner, parallel via Send API.
- After `dispatch_emails`: any failure -> alert_failure, else END.

### Concrete JSON shapes (from `state.py`)

- `PartnerSummary`: `{partner_id, partner_name, contact_email,
  week_start, week_end, total_requests, overall_success_rate,
  by_gateway: [GatewayStat], top_failures: [FailureBucket],
  trends: [TrendDelta]}`
- `AnalysisOutput`: `{overview, key_issues, likely_causes,
  recommended_actions}`
- `EmailOutput`: `{subject, html_body}`
- `SendResult`: `{partner_id, success, message_id, error}`
- `ChartFile`: `{path, title, kind}`

### State reducers (the concurrency trick)

The three partner-keyed dicts are typed as
`Annotated[dict[str, T], merge_dicts]`. Without the reducer, parallel
Send branches raise `INVALID_CONCURRENT_GRAPH_UPDATE`.

## Q3. What is the actual LangGraph code in here?

Six real features. Not just `add_node` and `add_edge`.

| Feature                  | Where                                    | What it does                              |
|--------------------------|------------------------------------------|-------------------------------------------|
| `StateGraph(GraphState)` | `graph.py::build_graph`                  | Typed state machine                       |
| `add_node` / `add_edge`  | `graph.py::build_graph`                  | Linear pipeline edges                     |
| `add_conditional_edges`  | 3 places in `build_graph`                | Branching after ingest / aggregate / dispatch |
| `Send` API               | `graph.py::fan_out_partners`             | Parallel per-partner fan-out              |
| `Annotated[..., reducer]`| `state.py` fields `analyses`/`charts`/`email_bodies` | Concurrent-write-safe merge |
| `MemorySaver` checkpoint | `graph.py::get_graph`                    | Replay via `thread_id`                    |
| `graph.astream()`        | `graph.py::stream_weekly`                | Live node-level events                    |

### The Send pattern (the marquee LangGraph feature)

```python
def fan_out_partners(state: GraphState) -> list[Send]:
    summaries = state.get("partner_summaries") or {}
    return [
        Send("partner_pipeline", {"partner_id": pid, "summary": summary})
        for pid, summary in summaries.items()
    ]

g.add_conditional_edges("aggregate", fan_out_partners)
```

After `aggregate` runs, this routing function returns one `Send` per
partner. LangGraph invokes `partner_pipeline` once per `Send`, in
parallel, each with the per-partner state dict. The partial returns
merge back into the main `GraphState` via the `merge_dicts` reducer,
keyed by `partner_id`.

### The checkpointer pattern

```python
graph = g.compile(checkpointer=MemorySaver())
config = {"configurable": {"thread_id": "weekly-2026-06-29"}}
result = await graph.ainvoke(state, config=config)

# Later, post-mortem:
snap = graph.get_state(config)
```

Each run gets a `thread_id` (defaults to `run_id`). `GET /threads/<tid>`
returns the saved snapshot.

## Q4. What is the LLM actually used for?

Only to interpret structured metrics into a partner-specific narrative
and to compose the email copy. It never touches data sources, never
sends emails, never picks charts.

- `partner_pipeline.analyze` receives a `PartnerSummary` JSON and
  produces an `AnalysisOutput` JSON.
- `partner_pipeline.email` receives the summary + analysis + chart
  titles and produces an `EmailOutput`.
- 2 LLM calls per partner per week. At 50 partners, ~100 calls.
- If the LLM is unavailable, both nodes fall back to deterministic
  templates (`fallback_analysis`, `fallback_email`) that produce the
  same DTO shape.

## Q5. Why not RPA / Logic Apps?

Logic Apps is the right tool for scheduling and integration. We use it
for the weekly trigger and the failure webhook. RPA / Logic Apps can
run KQL queries, move files, and fire templated emails. They cannot
turn "Braintree EU success rate dropped from 92% to 77% with 77
gateway_unavailable failures" into a partner-specific explanation with
three concrete remediation steps, weekly, at scale.

## Q6. How is this enterprise-grade but free?

Scale-to-zero compute, deterministic tools for non-LLM work, MiniMax
credits for the LLM, dry-run by default for email.

| Resource                  | Idle                  | Weekly             |
|---------------------------|-----------------------|--------------------|
| Container App (minRepl=0) | $0                    | ~$0.001 / run      |
| Container Registry Basic  | ~$5 / month           | same               |
| Log Analytics (30d ret.)  | < $0.10 / GB          | < $0.01 / week     |
| Application Insights      | ~$2.30 / GB           | depends on volume  |
| LLM (MiniMax Text-01)     | n/a (subscription)    | ~100 calls / week  |
| SMTP                      | $0 if dry-run         | SES ~$0.0001 / msg |

## Q7. What about state persistence and retries?

- LangGraph's `MemorySaver` checkpointer keeps state keyed by
  `thread_id`. For production you'd swap it for `SqliteSaver` (single
  container) or `PostgresSaver` (multi-replica).
- `run_node()` wraps each node with timing + error boundary. A node
  failure appends to `state.errors` and returns a partial so the graph
  still completes the rest of the work.
- The `alert_failure` node inside the graph routes dispatch failures
  into `state.errors` so post-mortem can see exactly which partners
  failed.
- The Logic App's FAILED/TIMEDOUT branch forwards the run ID to an
  alert webhook.

## Q8. How do you extend this to more partners?

- `PartnerMeta` is loaded from `_DEFAULT_PARTNERS` in `nodes/trigger.py`
  today. Replace with a database lookup or a config blob.
- The Send fan-out already parallelises per-partner work, so adding
  partners is free up to the MiniMax rate limit (60 RPM) or SMTP
  throughput.
- Add a new chart kind in `nodes/charts.py` and reference it in the
  email prompt. The pipeline does not change.

## Q9. Why LangGraph and not raw Python or another framework?

- **vs raw Python**: typed state machine with checkpointing. Without
  LangGraph, you write a 7-step pipeline as 500 lines of nested
  function calls with no way to inspect or replay the state.
- **vs CrewAI / AutoGen**: those frameworks are designed for
  free-form multi-agent collaboration. This workflow is deterministic
  with two LLM touchpoints. The graph structure is a much better fit.
- **vs LangChain `AgentExecutor`**: AgentExecutor is for ReAct-style
  tool-using agents. This is a batch job with bounded LLM calls per
  run, not an interactive agent.

## Q10. What are the four gotchas in this LangGraph wiring?

These are the failure modes that bit me on the first pass:

1. **Duplicate edges from same source.** `add_edge` + `add_conditional_edges`
   from the same source node. The linear edge wins silently. Send
   fan-out is ignored. Symptom: dispatched node receives `state={}`.
   Fix: ONE edge declaration per source.

2. **Missing reducer on concurrent-write field.** Partner-keyed dicts
   need `Annotated[dict, reducer]`. Without it, parallel Send branches
   raise `INVALID_CONCURRENT_GRAPH_UPDATE`.

3. **PartnerPipelineState overlaps GraphState.** If you put `run_id`,
   `dry_run`, or `out_dir` in `PartnerPipelineState`, fan-back causes
   concurrent write conflicts. Fix: keep them OUT, pass via
   module-level `_PIPELINE_CTX`.

4. **Send import location.** `from langgraph.types import Send` in 1.x.
   The old `langgraph.constants.Send` is deprecated.

The test suite (`tests/test_smoke.py`) catches each of these.

## Q11. Why a separate `partner_pipeline.py` instead of inline nodes?

The Send API dispatches one node at a time. But we still want the
per-partner pipeline to read as a coherent unit: `analyze -> chart ->
email`. Keeping it in one module makes the LLM pattern visible in one
place instead of scattered across `nodes/`.

## Q12. What's the difference between `run_weekly` and `stream_weekly`?

- `run_weekly` calls `graph.ainvoke(state)` and returns the final state
  once the entire graph completes.
- `stream_weekly` calls `graph.astream(state)` and yields
  `dict[node_name, partial_state]` events as each node fires.

## Q13. What happens when the LLM is down?

`fallback_analysis` and `fallback_email` produce a deterministic
`AnalysisOutput` / `EmailOutput` grounded in the summary's computed
metrics. The DTO shape is identical to the LLM's output, so downstream
nodes cannot tell the difference. The run completes; partners still
get emails; the audit trail records which partners used the fallback.

## Q14. Why is `partner_pipeline.py` separate from `graph.py`?

The Send fan-out passes a different state shape (`PartnerPipelineState`)
than the main graph (`GraphState`). Keeping the partner pipeline in its
own module makes the state-shape boundary visible. The module-level
`set_pipeline_ctx` / `get_pipeline_ctx` pair carries the read-only
metadata (`run_id`, `dry_run`, `out_dir`) that would otherwise collide
with `GraphState` keys and trigger
`INVALID_CONCURRENT_GRAPH_UPDATE`.

## Q15. How does the streaming endpoint work?

`POST /run-weekly/stream` returns a `StreamingResponse` with
`media_type="text/event-stream"`. The FastAPI handler wraps
`stream_weekly()` and emits one SSE event per node. Each event payload
is `{event: "node", payload: {node_name: [keys_in_partial]}}`.

## One-liner to memorise

> "A scheduled LangGraph `StateGraph` runs weekly, ingesting
>  KQL-queried metrics from Azure Application Insights, aggregating
>  per partner / per gateway, then fanning out one `Send` per partner
>  through an LLM analysis, deterministic chart rendering, and LLM
>  email composition -- in parallel -- before dispatching via SMTP.
>  Checkpointed via `MemorySaver` keyed by thread id, streamable via
>  `graph.astream`, deployed as an Azure Container App with
>  `minReplicas=0`, triggered by a Logic App recurrence, so outside
>  the weekly window the cost is effectively zero."

That is the interview story. The project is what proves you actually
built it.