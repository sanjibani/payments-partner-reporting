# Spec  --  payments partner reporting

## Problem

Payments platform team serves insurance partners across ~50 countries.
Partners integrate the payments component into their own products and
need weekly visibility into success rates, failure types, and trends
per region / per gateway.

## Outcome

Each week, every active partner receives an HTML email with:
- Headline numbers (total requests, success rate, gateway breakdown)
- Top failure buckets this week (gateway + error code)
- Week-over-week delta vs last week
- Likely causes (LLM-generated, grounded in metrics)
- Recommended actions (LLM-generated)

## Non-goals

- Real-time alerting. This is a weekly batch.
- Per-merchant drill-down. Aggregate per partner only.
- Direct write back to partner systems. Email is the contract.

## Constraints

- Minimize cost. Use MiniMax credits via Mavis. No paid Azure services
  required for development.
- Enterprise-grade architecture. Type-safe state, deterministic tools,
  LLM only where it adds value.
- Runs locally for development. Deployed to Azure Container Apps for
  production, scale to zero.

## Weekly schedule

- Cron: every Monday 06:00 UTC.
- Trigger source: Logic App with recurrence schedule, or local cron.
- Endpoint: `POST /run-weekly` on the Container App.
- Timeout: 10 min Container App timeout (job runs in <2 min for 50
  partners).

## Failure modes

- LLM unavailable: analysis_agent + email_agent fall back to
  deterministic templates. Run completes.
- App Insights KQL fails: ingest node falls back to local CSV if
  configured; otherwise pipeline halts with clear error.
- SMTP fails: dispatch node retries 3x with exponential backoff,
  records `send_results` with status=failed.