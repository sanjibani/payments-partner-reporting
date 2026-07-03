# Payments Partner Reporting -- agentic weekly workflow

Stateful LangGraph pipeline that pulls weekly payment metrics from Azure
Application Insights, aggregates per-partner per-gateway, fans out one
`Send` per partner through LLM analysis + chart rendering + email
composition in parallel, and ships an HTML email to each partner.

The full design contract lives in [`plan.md`](./plan.md). Read that
first. This README is the public-facing summary.

## Architecture in one paragraph

A weekly trigger (cron / Logic App) hits `POST /run-weekly` on a FastAPI
service. The service runs a LangGraph `StateGraph` with six main-graph
nodes (`trigger -> ingest -> aggregate -> partner_pipeline ->
dispatch_emails`, plus the terminal `alert_failure`). After `aggregate`,
a conditional edge returns one `Send` per partner, dispatching the
`partner_pipeline` node in parallel via the Send API. Only the
`analyze` and `email` sub-steps of `partner_pipeline` touch MiniMax;
everything else is deterministic Python with graceful fallback
templates. The container deploys to Azure Container Apps with
`minReplica=0` so cost outside the weekly window is effectively zero.

## LangGraph features actually used

Six real LangGraph features, all in `src/payments_reporting/graph.py`:

- `StateGraph(GraphState)` with typed `TypedDict`
- `add_node` and `add_edge` for the linear skeleton
- `add_conditional_edges` with three routing functions:
  - `route_after_ingest` -- skip to END if no data
  - `fan_out_partners` -- return `list[Send]` for parallel fan-out
  - `route_after_dispatch` -- route dispatch failures to `alert_failure`
- `Send` API from `langgraph.types` -- one invocation per partner, parallel
- `Annotated[dict, merge_dicts]` reducer on three partner-keyed fields
  for concurrent-write safety
- `MemorySaver` checkpointer -- `thread_id` per run, replayable
- `graph.astream()` for live node-level event streaming

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

# 6. run the test suite
PYTHONPATH=src:. pytest tests/
```

Output is written to `out/<run-id>/`:
- `state.json` -- full graph state after each node (interview gold)
- `partners/<id>/email.html` -- final email body per partner
- `partners/<id>/charts/*.png` -- chart images

## Per-node I/O contract

The state contract. The code must match this table. If you change a
node's writes, update this table and `plan.md` in the same commit.

| Node                | Reads from state                                          | Writes to state                              |
|---------------------|-----------------------------------------------------------|----------------------------------------------|
| `trigger`           | (nothing; env / request overrides)                        | `week_start`, `week_end`, `partners`         |
| `ingest`            | `week_start`, `week_end`, `partners`                      | `raw_metrics`, `last_week_metrics`           |
| `aggregate`         | `raw_metrics`, `last_week_metrics`, partners              | `partner_summaries` (DTO per partner)        |
| `partner_pipeline`  | dispatched via `Send` once per partner (parallel)         | `analyses[pid]`, `charts[pid]`, `email_bodies[pid]` |
| `dispatch_emails`   | `email_bodies`, summaries, `run_id`, `out_dir`           | `send_results` (list of `SendResult`)        |
| `alert_failure`     | `send_results` (failures only)                            | `errors` (appended)                          |

### Conditional edges

- After `ingest`: if `state.raw_metrics` is empty, route to `END`.
  Otherwise route to `aggregate`.
- After `aggregate`: conditional edge returns `list[Send]` -- one per
  partner. If empty, returns `[]` and LangGraph routes to END.
- After `dispatch_emails`: if any send failed, route to `alert_failure`.
  Otherwise `END`.

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
  __init__.py             # __version__
  py.typed                # marker for downstream mypy
  state.py                # DTOs + TypedDicts + reducers + snapshot helper
  graph.py                # StateGraph wiring + run/stream/checkpoint entrypoints
  partner_pipeline.py     # per-partner analyze -> chart -> email + module ctx
  llm.py                  # MiniMax client + graceful degradation
  prompts/__init__.py     # analysis + email prompt templates
  tools/
    __init__.py
    app_insights.py       # KQL client (real mode) + CSV fallback (dev mode)
    email_sender.py       # SMTP / dry-run
  nodes/
    __init__.py           # re-exports main-graph nodes only
    _timing.py            # run_node wrapper for main-graph nodes
    trigger.py            # main-graph node
    ingest.py             # main-graph node
    aggregate.py          # main-graph node + summary builders (tested directly)
    analysis.py           # analyze_one + fallback_analysis helpers
    charts.py             # chart helpers + render_partner_charts
    email.py              # compose_one + fallback_email helpers
    dispatch.py           # main-graph node

api/
  __init__.py
  main.py                 # FastAPI: /run-weekly, /run-weekly/stream, /runs, /threads

scripts/
  __init__.py
  run_local.py            # CLI: --seed --dry-run --partner --stream --serve
  seed_sample.py          # generates data/sample_week.csv with seeded anomaly

tests/
  __init__.py
  test_smoke.py           # 16 smoke tests covering every LangGraph feature

data/
  sample_week.csv         # generated by seed_sample.py

azure/
  main.bicep              # Container Apps + ACR + Log Analytics
  logic-app-workflow.json # weekly trigger

plan.md                   # source-of-truth spec
README.md                 # this file
INTERVIEW_PREP.md         # interview Q&A
SPEC.md                   # problem / outcome / non-goals
.env.example
.gitignore
Dockerfile
pyproject.toml
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

## Anti-patterns the test suite enforces

These were the bugs in the first pass. `tests/test_smoke.py` will
catch them if they ever come back:

1. **No duplicate edges** from the same source. Either
   `add_edge` OR `add_conditional_edges`, never both.
2. **No missing reducers** on partner-keyed fields. Without
   `Annotated[dict, merge_dicts]`, parallel Send branches raise
   `INVALID_CONCURRENT_GRAPH_UPDATE`.
3. **No overlap** between `PartnerPipelineState` and `GraphState`.
   Read-only metadata goes through `_PIPELINE_CTX`, never the subgraph
   state.
4. **No em-dashes** anywhere in `src/`, `api/`, `scripts/`.