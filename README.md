# Payments Partner Reporting  --  agentic weekly workflow

Stateful LangGraph pipeline that pulls weekly payment metrics from Azure
Application Insights, aggregates per-partner per-gateway, and ships an
LLM-generated narrative + HTML email to each partner.

## Architecture in one paragraph

A weekly trigger (cron / Logic App) hits `POST /run-weekly` on a FastAPI
service. The service runs a LangGraph `StateGraph` with seven nodes:
`trigger -> ingest -> aggregate -> [analysis_agent -> chart_generator ->
email_agent] -> dispatch`. Only the two LLM nodes touch MiniMax; everything
else is deterministic Python. The container deploys to Azure Container
Apps with `minReplica=0` so cost outside the weekly window is effectively
zero.

## Local quickstart (no Azure, no spend)

```bash
cd payments-partner-reporting
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 1. seed sample data
python scripts/run_local.py --seed

# 2. dry-run the graph (no LLM, deterministic)
python scripts/run_local.py --dry-run --partner P-001

# 3. full run with MiniMax (uses your LLM_API_KEY from .env)
cp .env.example .env   # then put your key in
python scripts/run_local.py --partner P-001
```

The output is written to `out/<run-id>/`:
- `state.json`  --  full graph state after each node (interview gold)
- `partners/<id>/email.html`  --  final email body
- `partners/<id>/charts/*.png`  --  chart images

## Why LangGraph, why LLM

- **LangGraph** gives a typed, inspectable state machine. Each node has a
  clear JSON in / JSON out. Interviewers can ask "show me the state after
  `aggregate`" and you can.
- **LLM is used twice per partner**: once to interpret the metrics
  (`analysis_agent`), once to write the email copy (`email_agent`). The
  rest of the graph is deterministic so cost and latency stay predictable
  at 50+ partners.

## Why not RPA / Logic Apps

RPA and Logic Apps can run KQL queries, move files, and fire templated
emails. They cannot turn "Stripe 80% success, Braintree 40% success,
Braintree failures up 3× vs last week in region EU" into a partner-specific
explanation with recommended actions, weekly, for 50 partners. That is
the core value of this system, and that is the LLM's job.

## Deployment (Azure, scale to zero)

See `azure/README.md`. TL;DR:
1. `az containerapp up` from this repo (Dockerfile included).
2. Wire a Logic App weekly trigger to POST `/run-weekly`.

Outside the weekly window the Container App scales to 0 replicas, so
compute cost is zero.

## Project layout

```
src/payments_reporting/
  state.py          # TypedDict State + Pydantic DTOs
  graph.py          # StateGraph wiring
  llm.py            # MiniMax client + graceful degradation
  prompts/          # analysis + email prompt templates
  tools/
    app_insights.py # KQL client (real + CSV fallback)
    email_sender.py # SMTP / dry-run
  nodes/
    trigger.py      # resolve week_start, week_end, partners
    ingest.py       # fetch raw_metrics
    aggregate.py    # build per-partner summaries + anomalies
    analysis.py     # LLM narrative per partner
    charts.py       # deterministic PNG charts
    email.py        # LLM HTML email per partner
    dispatch.py     # send via SMTP / dry-run

api/main.py         # FastAPI service: POST /run-weekly
scripts/run_local.py  # CLI runner
data/sample_week.csv  # sample 1-week telemetry
tests/                # smoke tests
azure/                # Container App bicep + Logic App JSON
```

## Inputs/outputs of every node

| Node              | Reads from state                                | Writes to state                                |
|-------------------|------------------------------------------------|------------------------------------------------|
| `trigger`         | (nothing)                                       | `week_start`, `week_end`, `partners`           |
| `ingest`          | `week_start`, `week_end`, `partners`            | `raw_metrics: dict[partnerId, list[RawRow]]`   |
| `aggregate`       | `raw_metrics`                                   | `partner_summaries: dict[partnerId, Summary]`  |
| `analysis_agent`  | one `Summary` at a time                         | `analyses[partnerId]: markdown`                |
| `chart_generator` | `Summary` (+ `analyses` for titles)             | `charts[partnerId]: list[ChartFile]`           |
| `email_agent`     | `Summary`, `analyses`, `charts`, partner meta   | `email_bodies[partnerId]: html`                |
| `dispatch`        | `email_bodies`, partner contact                 | `send_results[partnerId]: SendResult`          |

That table is what you walk an interviewer through. Each row is a pure
function over a TypedDict, easy to reason about and to test.

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