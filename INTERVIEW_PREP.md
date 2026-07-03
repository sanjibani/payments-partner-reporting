# Interview prep -- payments partner reporting

The interview questions you flagged, and the answers you should be able to
give cold after implementing this project.

## Q1. Where does the runtime live in Azure?

**One-line answer:** A Python LangGraph service packaged as a Docker
image and deployed to Azure Container Apps with `minReplicas=0`. A Logic
App with a weekly recurrence trigger POSTs to the container's
`/run-weekly` endpoint.

**The expanded version:**

- The graph is wrapped in a FastAPI app (`api/main.py`) that exposes
  `POST /run-weekly`, `POST /run-weekly/stream` (SSE), `GET /runs/<id>`,
  `GET /threads/<id>`.
- The Dockerfile builds the image. `az acr build` pushes it.
- The bicep template (`azure/main.bicep`) provisions Container Registry,
  Log Analytics workspace, Container Apps Environment, and the Container
  App itself with `minReplicas: 0`.
- A Logic App workflow (`azure/logic-app-workflow.json`) runs every
  Monday 06:00 UTC, POSTs to the container, and forwards failures to an
  alert webhook.
- When there are no requests, replicas scale to 0 and you pay no compute.
  Only the ACR Basic SKU + a slice of Log Analytics ingestion is
  always-on.

## Q2. What are the inputs and outputs of each node?

This is the table. Memorise it.

### Main graph (5 nodes + 1 terminal alert)

| Node                | Reads from state                                          | Writes to state                              |
|---------------------|-----------------------------------------------------------|----------------------------------------------|
| `trigger`           | (nothing; env / request overrides)                        | `week_start`, `week_end`, `partners`         |
| `ingest`            | `week_start`, `week_end`, `partners`                      | `raw_metrics`, `last_week_metrics`           |
| `aggregate`         | `raw_metrics`, `last_week_metrics`, partners              | `partner_summaries` (DTO per partner)        |
| `partner_pipeline`  | dispatched via `Send` once per partner (in parallel)      | `analyses[pid]`, `charts[pid]`, `email_bodies[pid]` |
| `dispatch_emails`   | `email_bodies`, partner contact, `run_id`, `out_dir`      | `send_results` (list of `SendResult`)        |
| `alert_failure`     | `send_results`                                            | `errors` (append failed dispatch details)    |

### Per-partner pipeline (inside `partner_pipeline`, runs in parallel via Send)

| Sub-node             | Input                                          | Output                       |
|----------------------|------------------------------------------------|------------------------------|
| `analyze`            | `PartnerSummary` (from Send state)             | `analyses[pid]`              |
| `chart`              | `PartnerSummary`, `analyses[pid]`              | `charts[pid]` (3 PNGs)       |
| `email`              | `PartnerSummary`, `analyses[pid]`, `charts[pid]` | `email_bodies[pid]`        |

### Conditional edges

- After `ingest`: if `state.raw_metrics` is empty, route to `END`
  (skip the rest of the run). Otherwise route to `aggregate`.
- After `aggregate`: conditional edge returns `list[Send]` -- one per
  partner -- to fan out in parallel via the Send API.
- After `dispatch_emails`: if any send failed, route to `alert_failure`.
  Otherwise `END`.

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

The three partner-keyed dicts (`analyses`, `charts`, `email_bodies`)
are typed as `Annotated[dict[str, T], _merge_dicts]`. The reducer
merges concurrent writes from parallel Send branches. Without the
reducer, LangGraph raises `INVALID_CONCURRENT_GRAPH_UPDATE` when
multiple fan-out branches try to write different keys to the same
dict in one step.

### Show the interviewer `out/<run_id>/state.json`

Full graph state after a run, every node's contribution visible,
including `node_durations_ms` for every node.

## Q3. What is the actual LangGraph code in here?

The project now uses five real LangGraph features, not just `add_node`.

| Feature                  | Where                                    | What it does                              |
|--------------------------|------------------------------------------|-------------------------------------------|
| `StateGraph(GraphState)` | `graph.py` line ~152                     | Typed state machine                       |
| `add_node` / `add_edge`  | `graph.py` lines ~155-180                | Linear pipeline edges                     |
| `add_conditional_edges`  | `graph.py` lines ~163, ~175, ~190        | Branching after ingest, aggregate, dispatch |
| `Send` API               | `graph.py::fan_out_partners` line ~85    | Parallel per-partner fan-out              |
| `MemorySaver` checkpoint | `graph.py::get_graph` line ~215          | Replay / inspect via `thread_id`          |
| `graph.astream()`        | `graph.py::stream_weekly` line ~256      | Live node-level event streaming           |
| `Annotated[..., reducer]`| `state.py` line ~160                     | Concurrent-write-safe dict merge          |

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
partner. LangGraph invokes the `partner_pipeline` node once per `Send`,
in parallel, each with the per-partner state dict. The partial returns
merge back into the main `GraphState` keyed by `partner_id`.

### The checkpointer pattern

```python
graph = g.compile(checkpointer=MemorySaver())
config = {"configurable": {"thread_id": "weekly-2026-06-29"}}
result = await graph.ainvoke(state, config=config)

# Later, post-mortem inspection:
snap = graph.get_state(config)  # full state at end of run
```

Each run gets a `thread_id` (defaults to `run_id`). The full state is
checkpointed after every node. `GET /threads/<thread_id>` returns the
saved snapshot.

## Q4. What is the LLM actually used for?

**One-line answer:** Only to interpret structured metrics into a
partner-specific narrative and to compose the email copy. It never
touches data sources, never sends emails, never picks charts.

**The expanded version:**

- `partner_pipeline.analyze` receives a `PartnerSummary` JSON and
  produces an `AnalysisOutput` JSON (overview / key issues / likely
  causes / recommended actions). The prompt instructs it to ground
  every claim in a number from the metrics, never invent, and never
  speculate when the data does not support a claim.
- `partner_pipeline.email` receives the summary + analysis + chart
  titles and produces an `EmailOutput` (subject + HTML body) at the
  right tone for the partner.
- That's 2 LLM calls per partner per week. At 50 partners, ~100 calls.
  Well inside the MiniMax daily quota.
- If the LLM is unavailable (network outage, key revoked, model down),
  both nodes fall back to deterministic templates (`fallback_analysis`
  and `fallback_email`) that produce the same DTO shape. The weekly
  batch never blocks on a model outage.

## Q5. Why not RPA / Logic Apps?

**One-line answer:** RPA and Logic Apps can move data and fire
templated emails; they cannot turn metrics into a partner-specific
narrative with recommended actions, weekly, at scale.

**The expanded version:**

- Logic Apps is the right tool for **scheduling and integration**. We
  use it for the weekly trigger and the failure webhook. We are not
  putting the agent flow inside the Logic App.
- RPA / Power Automate is the right tool for **deterministic
  workflows**: clicking through UIs, calling APIs in fixed sequences.
  The interviews this project replaces are "click this dashboard,
  download CSV, paste into email template." That's the part RPA could
  automate. The part RPA cannot automate is the interpretation: "Braintree
  EU failures spiked 3x this week. Likely cause: gateway outage
  window. Recommended action: replay 12 stuck charges."
- An agentic workflow gives you a typed state machine plus LLM
  reasoning at the exact points where reasoning adds value. The
  deterministic parts (KQL fetch, aggregation, chart rendering, email
  dispatch) stay deterministic. The LLM is bolted on only where it
  earns its keep.

## Q6. How is this enterprise-grade but free?

**One-line answer:** Scale-to-zero compute, deterministic tools for
non-LLM work, MiniMax credits for the LLM, dry-run by default for
email.

**Cost line items:**

| Resource                  | Idle                  | Weekly             |
|---------------------------|-----------------------|--------------------|
| Container App (minRepl=0) | $0                    | ~$0.001 / run      |
| Container Registry Basic  | ~$5 / month           | same               |
| Log Analytics (30d ret.)  | < $0.10 / GB          | < $0.01 / week     |
| Application Insights      | ~$2.30 / GB           | depends on volume  |
| LLM (MiniMax Text-01)     | n/a (subscription)    | ~100 calls / week  |
| SMTP                      | $0 if dry-run         | SES ~$0.0001 / msg |

For development, the CSV fallback reads a local file. No Azure needed.
The full pipeline runs in under two minutes on a laptop.

## Q7. What about state persistence and retries?

- LangGraph's `MemorySaver` checkpointer keeps state in memory keyed by
  `thread_id`. For production you'd swap it for `SqliteSaver` (single
  container) or `PostgresSaver` (multi-replica). Each node's output
  becomes a checkpoint you can replay from.
- `run_node()` wraps each node with a timing + error boundary. A node
  failure appends to `state.errors` and returns an empty partial so the
  graph still completes the rest of the work.
- For production retries, Container Apps has built-in KEDA scaling and
  restart policies. The Logic App also has a FAILED/TIMEDOUT branch
  that forwards the run ID to an alert webhook.
- The `alert_failure` node inside the graph routes dispatch failures
  into `state.errors` so a post-mortem can see exactly which partners
  failed and why.

## Q8. How do you extend this to more partners?

- `PartnerMeta` is loaded from `_DEFAULT_PARTNERS` in
  `nodes/trigger.py` today. Replace with a database lookup or a config
  blob.
- The Send fan-out already parallelises per-partner work, so adding
  partners is free up to the OpenAI rate limit (60 RPM on MiniMax) or
  the SMTP throughput limit.
- Add a new chart kind in `nodes/charts.py` and reference it in the
  email prompt. The pipeline does not change.

## Q9. Why LangGraph and not raw Python or another framework?

- **vs raw Python**: a typed state machine with checkpointing. Without
  LangGraph, you write a 7-step pipeline as 500 lines of nested
  function calls with no way to inspect or replay the state.
- **vs CrewAI / AutoGen**: those frameworks are designed for
  free-form multi-agent collaboration. This workflow is deterministic
  with two LLM touchpoints. The graph structure (typed state, edge
  transitions, partial returns) is a much better fit.
- **vs LangChain `AgentExecutor`**: AgentExecutor is for ReAct-style
  tool-using agents. This is a batch job with bounded LLM calls per
  run, not an interactive agent. `StateGraph` is the right primitive.

## Q10. Why a separate `partner_pipeline.py` instead of inline nodes?

The Send API dispatches one node at a time, not a subgraph. But we
still want the per-partner pipeline to read as a coherent unit: `analyze
-> chart -> email`. Keeping it in one module makes the LLM pattern
visible in one place instead of scattered across `nodes/`.

`partner_pipeline.py` exports three single-step node functions
(`partner_analyze`, `partner_chart`, `partner_email`) for tests and
callers that want sub-step introspection, plus the combined
`partner_pipeline_node` that the main graph dispatches.

## Q11. Why is `partner_pipeline.py` separate from `graph.py`?

The Send fan-out passes a different state shape (`PartnerPipelineState`)
than the main graph (`GraphState`). Keeping the partner pipeline in
its own module makes the state-shape boundary visible. The
module-level `set_pipeline_ctx` / `get_pipeline_ctx` pair carries the
read-only metadata (`run_id`, `dry_run`, `out_dir`) that would
otherwise collide with `GraphState` keys and trigger
`INVALID_CONCURRENT_GRAPH_UPDATE`.

## Q12. What's the difference between `run_weekly` and `stream_weekly`?

- `run_weekly` calls `graph.ainvoke(state)` and returns the final state
  once the entire graph completes. One-shot, blocking, simple.
- `stream_weekly` calls `graph.astream(state)` and yields
  `dict[node_name, partial_state]` events as each node fires. Useful
  for live observability, the CLI `--stream` flag, or the SSE endpoint.

## Q13. What happens when the LLM is down?

`fallback_analysis` and `fallback_email` (in `nodes/analysis.py` and
`nodes/email.py`) produce a deterministic `AnalysisOutput` /
`EmailOutput` grounded in the summary's computed metrics. The DTO
shape is identical to the LLM's output, so downstream nodes cannot
tell the difference. The run completes; the partners still get
emails; the audit trail records which partners used the fallback.

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