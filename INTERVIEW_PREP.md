# Interview prep  --  payments partner reporting

The interview questions you flagged, and the answers you should be able to
give cold after implementing this project.

## Q1. Where does the runtime live in Azure?

**One-line answer:** A Python LangGraph service packaged as a Docker
image and deployed to Azure Container Apps with `minReplicas=0`. A Logic
App with a weekly recurrence trigger POSTs to the container's
`/run-weekly` endpoint.

**The expanded version:**

- The graph is wrapped in a FastAPI app (`api/main.py`) that exposes
  `POST /run-weekly`, `GET /runs/{run_id}`, `GET /healthz`.
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

This is the table. Memorize it.

| Node              | Reads from state                                | Writes to state                                |
|-------------------|------------------------------------------------|------------------------------------------------|
| `trigger`         | (nothing; env / request overrides)             | `week_start`, `week_end`, `partners`           |
| `ingest`          | `week_start`, `week_end`, `partners`            | `raw_metrics`, `last_week_metrics`             |
| `aggregate`       | `raw_metrics`, `last_week_metrics`, partners    | `partner_summaries` (DTO per partner)          |
| `analysis_agent`  | one `partner_summaries[pid]`                    | `analyses[pid]` (AnalysisOutput, JSON-shaped)  |
| `chart_generator` | `partner_summaries[pid]`, `analyses[pid]`      | `charts[pid]` (list of ChartFile paths)        |
| `email_agent`     | summary, analysis, charts, partner meta        | `email_bodies[pid]` (subject + html_body)      |
| `dispatch_emails` | `email_bodies`, partner contact, `run_id`      | `send_results` (SendResult list)               |

**Concrete JSON shapes (from `state.py`):**

- `PartnerSummary`: `{partner_id, partner_name, contact_email,
  week_start, week_end, total_requests, overall_success_rate,
  by_gateway: [GatewayStat], top_failures: [FailureBucket],
  trends: [TrendDelta]}`
- `AnalysisOutput`: `{overview, key_issues, likely_causes,
  recommended_actions}`
- `EmailOutput`: `{subject, html_body}`
- `SendResult`: `{partner_id, success, message_id, error}`
- `ChartFile`: `{path, title, kind}`

**Show the interviewer `out/<run_id>/state.json`**  --  the full graph
state after a run, with every node's contribution visible.

## Q3. What is the LLM actually used for?

**One-line answer:** Only to interpret structured metrics into a
partner-specific narrative and to compose the email copy. It never
touches data sources, never sends emails, never picks charts.

**The expanded version:**

- `analysis_agent` receives a `PartnerSummary` JSON and produces an
  `AnalysisOutput` JSON (overview / key issues / likely causes /
  recommended actions). The prompt instructs it to ground every claim
  in a number from the metrics, never invent, and never speculate when
  the data does not support a claim.
- `email_agent` receives the summary + analysis + chart titles and
  produces an `EmailOutput` (subject + HTML body) at the right tone
  for the partner.
- That's 2 LLM calls per partner per week. At 50 partners, ~100 calls.
  Well inside the MiniMax daily quota.
- If the LLM is unavailable (network outage, key revoked, model down),
  both nodes fall back to deterministic templates that are still
  grounded in the same DTOs. The weekly batch never blocks on a model
  outage.

## Q4. Why not RPA / Logic Apps?

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

## Q5. How is this enterprise-grade but free?

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

For development, the CSV fallback reads a local file. No Azure
needed. The full pipeline runs in <2 minutes on a laptop.

## Q6. What about state persistence and retries?

- LangGraph's `StateGraph` keeps state in memory for the duration of a
  run. We snapshot the final state to `out/<run_id>/state.json` so a
  failed or interrupted run can be inspected, replayed, or partially
  redone.
- `run_node()` wraps each node with a timing + error boundary. A node
  failure appends to `state.errors` and returns an empty partial so the
  graph still completes the rest of the work.
- For production retries, Container Apps has built-in KEDA scaling and
  restart policies. The Logic App also has a FAILED/TIMEDOUT branch
  that forwards the run ID to an alert webhook.

## Q7. How do I extend this to more partners?

- `PartnerMeta` is loaded from `_DEFAULT_PARTNERS` in `nodes/trigger.py`
  today. Replace with a database lookup or a config blob.
- The graph processes all partners in sequence inside one node. For
  100+ partners, parallelise the LLM calls with `asyncio.gather` inside
  `analysis_agent` and `email_agent`. The deterministic nodes
  (trigger, ingest, aggregate) stay sequential.
- Add a new chart kind in `nodes/charts.py` and reference it in the
  email prompt. The pipeline does not change.

## One-liner to memorise

> "A scheduled LangGraph `StateGraph` runs weekly, ingesting
>  KQL-queried metrics from Azure Application Insights, aggregating
>  per partner / per gateway, then handing the structured summaries to
>  two LLM agents  --  analysis and email  --  plus deterministic chart
>  generation and SMTP dispatch. Deployed as an Azure Container App
>  with `minReplicas=0`, triggered by a Logic App recurrence, so
>  outside the weekly window the cost is effectively zero."

That is the interview story. The project is what proves you actually
built it.