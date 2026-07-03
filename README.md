# Payments Partner Reporting -- agentic weekly workflow

Stateful LangGraph pipeline that pulls weekly payment metrics from Azure
Application Insights, aggregates per-partner per-gateway, fans out one
`Send` per partner through LLM analysis + chart rendering + email
composition in parallel, and ships an HTML email to each partner.

## Architecture in one paragraph

A weekly trigger (cron / Logic App) hits `POST /run-weekly` on a FastAPI
service. The service runs a LangGraph `StateGraph` with five main
nodes (`trigger -> ingest -> aggregate -> partner_pipeline ->
dispatch_emails`) plus one terminal alert node. After `aggregate`, a
conditional edge returns one `Send` per partner, dispatching the
`partner_pipeline` node in parallel. Only the partner-pipeline's
`analyze` and `email` sub-steps touch MiniMax; everything else is
deterministic Python with graceful degradation. The container deploys
to Azure Container Apps with `minReplica=0` so cost outside the weekly
window is effectively zero.

## LangGraph features actually used

- `StateGraph` with a typed `GraphState` (TypedDict)
- `add_node` and `add_edge` for the linear skeleton
- `add_conditional_edges` with three routing functions
  - `route_after_ingest` -- skip to `END` if no data
  - `fan_out_partners` -- return `list[Send]` for parallel fan-out
  - `route_after_dispatch` -- route dispatch failures to `alert_failure`
- `Send` API from `langgraph.types` -- one invocation per partner, parallel
- `MemorySaver` checkpointer -- `thread_id` per run, replayable
- `graph.astream()` for live node-level event streaming
- `Annotated[dict, reducer]` on partner-keyed fields for concurrent write safety

## Local quickstart (no Azure, no spend)

```bash
cd payments-partner-reporting
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 1. seed sample data
python scripts/run_local.py --seed

# 2. dry-run the graph (no LLM, deterministic, all 3 partners)
python scripts/run_local.py --dry-run

# 3. stream events as nodes fire
python scripts/run_local.py --dry-run --stream

# 4. run with MiniMax (uses your LLM_API_KEY from .env)
cp .env.example .env   # then put your key in
python scripts/run_local.py

# 5. start the FastAPI service
python scripts/run_local.py --serve
```

The output is written to `out/<run-id>/`:
- `state.json` -- full graph state after each node (interview gold)
- `partners/<id>/email.html` -- final email body per partner
- `partners/<id>/charts/*.png` -- chart images

## Inputs/outputs of every node

### Main graph (6 nodes, 1 conditional terminal)

| Node                | Reads from state                                          | Writes to state                              |
|---------------------|-----------------------------------------------------------|----------------------------------------------|
| `trigger`           | (nothing; env / request overrides)                        | `week_start`, `week_end`, `partners`         |
| `ingest`            | `week_start`, `week_end`, `partners`                      | `raw_metrics`, `last_week_metrics`           |
| `aggregate`         | `raw_metrics`, `last_week_metrics`, partners              | `partner_summaries` (DTO per partner)        |
| `partner_pipeline`  | dispatched via `Send` once per partner (parallel)         | `analyses[pid]`, `charts[pid]`, `email_bodies[pid]` |
| `dispatch_emails`   | `email_bodies`, partner contact, `run_id`, `out_dir`      | `send_results` (list of `SendResult`)        |
| `alert_failure`     | `send_results` (failures only)                            | `errors` (append failed dispatch details)    |

### Per-partner sub-pipeline (called in parallel via Send)

| Sub-step            | Input                                            | Output                       |
|---------------------|--------------------------------------------------|------------------------------|
| `analyze`           | `PartnerSummary` (LLM)                           | `analyses[pid]`              |
| `chart`             | `PartnerSummary`, `analyses[pid]`                | `charts[pid]` (3 PNGs)       |
| `email`             | `PartnerSummary`, `analyses[pid]`, `charts[pid]` | `email_bodies[pid]` (LLM)    |

## Why LangGraph, why LLM

- **LangGraph** gives a typed, inspectable state machine with native
  parallel fan-out (`Send`), conditional edges, checkpointing, and
  streaming. The state is a `TypedDict` you can dump to JSON after
  every run. Interviewers can ask "show me the state after
  `aggregate`" and you can.
- **LLM is used twice per partner** inside the per-partner pipeline:
  once to interpret metrics (`analyze`), once to write email copy
  (`email`). The rest of the graph is deterministic so cost and
  latency stay predictable at 50+ partners.

## Why not RPA / Logic Apps

RPA and Logic Apps can run KQL queries, move files, and fire templated
emails. They cannot turn "Stripe 80% success, Braintree 40% success,
Braintree failures up 3x vs last week in region EU" into a
partner-specific explanation with recommended actions, weekly, for 50
partners. That is the core value of this system, and that is the
LLM's job.

## Deployment (Azure, scale to zero)

See `azure/README.md`. TL;DR:
1. `az containerapp up` from this repo (Dockerfile included).
2. Wire a Logic App weekly trigger to POST `/run-weekly`.

Outside the weekly window the Container App scales to 0 replicas, so
compute cost is zero.

## Project layout

```
src/payments_reporting/
  state.py              # TypedDict GraphState + PartnerPipelineState + Pydantic DTOs
  graph.py              # StateGraph wiring, conditional edges, Send fan-out
  partner_pipeline.py   # Per-partner pipeline node + module-level context
  llm.py                # MiniMax client + graceful degradation
  prompts/__init__.py   # analysis + email prompt templates
  tools/
    app_insights.py     # KQL client (real mode) + CSV fallback (dev mode)
    email_sender.py     # SMTP / dry-run
  nodes/
    _timing.py          # Node timing + error wrapper
    trigger.py          # resolve week_start, week_end, partners
    ingest.py           # fetch raw_metrics via App Insights or CSV
    aggregate.py        # build per-partner summaries + anomaly detection
    analysis.py         # analyze_one + fallback_analysis (LLM helper)
    charts.py           # render_partner_charts (matplotlib, no LLM)
    email.py            # compose_one + fallback_email (LLM helper)
    dispatch.py         # send via SMTP / dry-run

api/main.py             # FastAPI service: POST /run-weekly, /stream, /runs, /threads
scripts/run_local.py    # CLI runner with --stream, --serve, --dry-run flags
data/sample_week.csv    # sample 1-week telemetry (3 partners, seeded Braintree anomaly)
tests/                  # smoke tests covering graph, fan-out, checkpoint, streaming
azure/                  # Container App bicep + Logic App JSON
```

## Cost reality check

- Compute: 0 USD when scaled to zero (Container Apps).
- Storage: a few cents a month for App Insights workspace.
- LLM: 2 calls per partner per week. At 50 partners, ~100 calls/week.
  With `MiniMax-Text-01` on the Mavis subscription tier, this is well
  inside the daily quota. No cost.
- Email: dry-run by default. Set `SMTP_HOST` etc. only when actually
  sending.

## Voice rules (apply to all generated text)

- No em-dashes anywhere in code, comments, prompts, or generated emails.
- Short sentences. No "I dug into this."
- Name the document / metric before the partner.
- Same rules as the OSS PR push stream. Same anti-pattern set.