# PLAN.md -- payments-partner-reporting

This is the contract. Every implementation decision below must be followed.
If you find yourself wanting to deviate, update this file FIRST, then code.

## 1. Goals and non-goals

**Goals**

- Stateful LangGraph weekly batch that ingests payment metrics,
  aggregates per partner, fans out one `Send` per partner through LLM
  analysis + chart rendering + email composition in parallel, then
  dispatches per-partner HTML emails.
- Run end-to-end on a laptop with no Azure and no spend, via a CSV
  fallback for the App Insights data source.
- Deployed to Azure Container Apps with `minReplicas=0` so cost outside
  the weekly window is effectively zero.
- Defensible in an interview: typed state, deterministic tools,
  graceful LLM degradation, structured observability, scale-to-zero
  cost, automated weekly trigger.

**Non-goals**

- Real-time alerting. Weekly batch only.
- Per-merchant drill-down. Aggregate per partner only.
- Write back to partner systems. Email is the contract.
- Production Postgres / Service Bus / durable storage. SQLite-or-disk
  checkpointing + JSON-on-disk state is enough at this volume.

## 2. Architecture diagram

```
                       Weekly cron / Logic App
                                |
                                v
                       POST /run-weekly
                                |
                                v
                     +-------------------+
                     |   FastAPI app     |   api/main.py
                     |   (api/main.py)   |
                     +---------+---------+
                               |
                               v
                     +-------------------+
                     |   StateGraph      |   graph.py
                     |   GraphState      |
                     +---------+---------+
                               |
        +------+-------+-------+------+-------+
        |      |       |       |      |       |
        v      v       v       v      v       v
     trigger  ingest  aggregate partner_  dispatch_  alert_
                       |        pipeline  emails     failure
                       |          |
                       |          v (Send API fan-out, parallel)
                       |     +----+----+----+
                       |     |    |    |    |
                       |     v    v    v    v   (one branch per partner)
                       |  analyze chart email
                       |
                       v
                  fan-out
                  Send API
                  (one Send per partner)

Per-partner pipeline runs in PARALLEL inside the Send fan-out.
Each branch returns partner_id-keyed partials.
Reducers in GraphState merge concurrent writes safely.
MemorySaver checkpoints after every node, keyed by thread_id.
graph.astream() emits node-level events for live observability.
```

## 3. LangGraph feature inventory

These five features MUST be present in `graph.py` and exercised by tests:

| Feature                  | Wired at                                 | Tested by                          |
|--------------------------|------------------------------------------|------------------------------------|
| `StateGraph(GraphState)` | `graph.py::build_graph`                  | `test_graph_compiles`              |
| `add_conditional_edges`  | `graph.py::build_graph` (3 places)       | `test_conditional_edge_routing_logic` |
| `Send` API fan-out       | `graph.py::fan_out_partners`             | `test_fan_out_runs_per_partner`    |
| `Annotated[..., reducer]`| `state.py::_merge_dicts` on 3 fields     | `test_concurrent_writes_merge`     |
| `MemorySaver` checkpoint | `graph.py::get_graph(with_checkpoint=True)` | `test_checkpoint_state_persisted` |
| `graph.astream()`        | `graph.py::stream_weekly`                | `test_streaming_emits_node_events` |

Anti-patterns (will fail tests or runtime):

- NEVER declare both `add_edge` AND `add_conditional_edges` from the
  same source node. The linear edge wins, Send is silently ignored.
  Verified failure mode: `state={}` KeyError in the dispatched node.
- NEVER put a field in `PartnerPipelineState` that is also a key in
  `GraphState`. Causes `INVALID_CONCURRENT_GRAPH_UPDATE` on fan-back.
  Fix: keep run_id / dry_run / out_dir OUT of PartnerPipelineState.
  Pass them via the module-level `_PIPELINE_CTX` instead.
- NEVER write to a partner-keyed dict field without an
  `Annotated[dict, reducer]` type. Concurrent Send branches will
  conflict. Use `_merge_dicts` from `state.py`.

## 4. State design

`src/payments_reporting/state.py`:

### DTOs (Pydantic v2 BaseModel)

- `RawRow` -- one row from App Insights requests
- `GatewayStat` -- per-gateway aggregate
- `FailureBucket` -- (gateway, result_code, count)
- `TrendDelta` -- week-over-week comparison
- `PartnerSummary` -- one per partner, deterministic aggregate
- `AnalysisOutput` -- LLM #1 output
- `EmailOutput` -- LLM #2 output
- `SendResult` -- dispatch result
- `ChartFile` -- rendered chart on disk
- `PartnerMeta` -- static per-partner metadata

### TypedDicts

- `GraphState` -- main graph state
- `PartnerPipelineState` -- per-partner subgraph input
  - MUST contain only: `partner_id`, `summary`
  - MUST NOT contain: `run_id`, `dry_run`, `out_dir`, anything else
    that overlaps with `GraphState` keys

### Reducers

`_merge_dicts(old: dict, new: dict) -> dict` -- shallow merge, new
wins on key conflict. Used on the three partner-keyed dicts:
`analyses`, `charts`, `email_bodies`. Without these reducers, parallel
Send branches raise `INVALID_CONCURRENT_GRAPH_UPDATE`.

### State helper

`initial_state(run_id, dry_run) -> GraphState` -- returns the empty
state dict that `graph.ainvoke(state)` starts with.

`state_snapshot(state) -> dict` -- JSON-safe recursive converter for
`out/<run_id>/state.json`. `_dump(v)` handles BaseModel, dict, list,
tuple, datetime. NO `default=str` fallback -- convert everything
explicitly so the snapshot is stable.

## 5. Per-node contracts

These are the contracts. If a node writes a field not listed in its
"writes" column, the test that catches it is `test_state_keys_match_plan`.

### Main graph (6 nodes)

| Node              | Reads                                      | Writes                                      |
|-------------------|--------------------------------------------|---------------------------------------------|
| `trigger`         | env / request overrides                    | `week_start`, `week_end`, `partners`        |
| `ingest`          | `week_start`, `week_end`, `partners`       | `raw_metrics`, `last_week_metrics`          |
| `aggregate`       | `raw_metrics`, `last_week_metrics`, partners | `partner_summaries`                       |
| `partner_pipeline`| dispatched via Send (per partner)          | `analyses[pid]`, `charts[pid]`, `email_bodies[pid]` |
| `dispatch_emails` | `email_bodies`, summaries, `run_id`, `out_dir` | `send_results`                          |
| `alert_failure`   | `send_results` (failures only)             | `errors` (appended)                         |

### Per-partner pipeline (inside `partner_pipeline.py`)

Single node `partner_pipeline_node(state: PartnerPipelineState) -> dict`
runs the full analyze -> chart -> email sequence. Returns a partial
with all three partner-keyed fields.

### Conditional edges

- After `ingest`: `route_after_ingest` returns `"aggregate"` if
  `state.raw_metrics` is non-empty, else `END`.
- After `aggregate`: `fan_out_partners` returns `list[Send]` -- one per
  partner. If empty, returns `[]` and LangGraph routes to END.
- After `dispatch_emails`: `route_after_dispatch` returns
  `"alert_failure"` if any `SendResult.success` is False, else `END`.

## 6. Module layout

```
src/payments_reporting/
  __init__.py             # __version__
  py.typed                # marker for downstream mypy
  state.py                # DTOs + TypedDicts + reducers + snapshot helper
  graph.py                # StateGraph wiring + run/stream/checkpoint entrypoints
  partner_pipeline.py     # per-partner analyze -> chart -> email + module ctx
  llm.py                  # MiniMax client + graceful degradation
  prompts/
    __init__.py           # analysis + email prompt templates
  tools/
    __init__.py
    app_insights.py       # AppInsightsClient: real KQL + CSV fallback
    email_sender.py       # EmailSender: SMTP + dry-run
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
  main.py                 # FastAPI: POST /run-weekly, /run-weekly/stream, /runs, /threads, /healthz

scripts/
  __init__.py
  run_local.py            # CLI: --seed --dry-run --partner --stream --serve
  seed_sample.py          # generates data/sample_week.csv with seeded anomaly

tests/
  __init__.py
  test_smoke.py           # 11 smoke tests covering every LangGraph feature

data/
  sample_week.csv         # generated by seed_sample.py

azure/
  main.bicep              # Container Apps + ACR + Log Analytics
  logic-app-workflow.json # weekly trigger
  README.md

plan.md                   # THIS FILE -- the contract
README.md                 # public-facing summary
INTERVIEW_PREP.md         # interview Q&A
SPEC.md                   # problem / outcome / non-goals
.env.example
.gitignore
Dockerfile
pyproject.toml
```

## 7. Implementation order (with verification gates)

Each step has a verification gate. Do NOT proceed to the next step
until the gate passes.

| # | Step                              | Verification gate                                                  |
|---|-----------------------------------|--------------------------------------------------------------------|
| 1 | `plan.md` (this file)             | exists in repo root                                                |
| 2 | clear `src/`                      | `ls src/payments_reporting/` returns empty or near-empty           |
| 3 | `state.py`                        | `python -c "from payments_reporting.state import GraphState"` works |
| 4 | `tools/app_insights.py`           | CSV loader test passes against `data/sample_week.csv`              |
| 5 | `tools/email_sender.py`           | dry-run writes `email.html` to `out/test/partners/<pid>/`          |
| 6 | `llm.py`                          | `is_available()` returns False when key unset, True when set       |
| 7 | `prompts/__init__.py`             | both `analysis_user_prompt` and `email_user_prompt` importable     |
| 8 | `nodes/_timing.py`                | `run_node` returns timing in partial even on exception             |
| 9 | `nodes/trigger.py`                | `trigger()` returns ISO week_start + week_end + partners list      |
|10 | `nodes/ingest.py`                 | `ingest()` populates `raw_metrics` from CSV fallback               |
|11 | `nodes/aggregate.py`              | `aggregate()` produces `PartnerSummary` with anomaly flagged       |
|12 | `nodes/analysis.py`               | `fallback_analysis()` returns valid `AnalysisOutput`               |
|13 | `nodes/charts.py`                 | `render_partner_charts()` writes 3 PNG files                       |
|14 | `nodes/email.py`                  | `fallback_email()` returns valid `EmailOutput`                     |
|15 | `partner_pipeline.py`             | `partner_pipeline_node` runs end-to-end for one partner            |
|16 | `graph.py`                        | `build_graph()` compiles, 6 nodes, 3 conditional edges, 1 Send    |
|17 | full graph end-to-end             | all 11 tests pass                                                  |
|18 | `api/main.py`                     | `curl POST /run-weekly` returns 200 with run_id + thread_id        |
|19 | SSE stream endpoint               | `curl POST /run-weekly/stream` emits SSE events                    |
|20 | `scripts/run_local.py`            | `--dry-run` exits 0; `--stream` prints node events                 |
|21 | full test suite                   | `pytest` shows 11 passed                                           |
|22 | `README.md`, `INTERVIEW_PREP.md`  | node I/O table matches `state.py` exactly                          |
|23 | git commit + push                 | `git log --oneline -3` shows the new commit on main                |

## 8. Risk register (gotchas to AVOID)

These are the bugs I hit on the first pass. Each one is a real failure
mode. If you see any of these in code review, reject the change.

1. **Duplicate edges from same source.** `add_edge` + `add_conditional_edges`
   from the same source node. The linear edge wins silently. Send
   fan-out is ignored. Symptom: dispatched node receives `state={}`.
   Fix: ONE edge declaration per source, either linear OR conditional.

2. **Missing reducer on concurrent-write field.** Partner-keyed dicts
   (`analyses`, `charts`, `email_bodies`) need `Annotated[dict, reducer]`.
   Without it, parallel Send branches raise
   `INVALID_CONCURRENT_GRAPH_UPDATE`. Fix: declare with reducer.

3. **PartnerPipelineState overlaps GraphState.** If you put `run_id`,
   `dry_run`, or `out_dir` in `PartnerPipelineState`, the subgraph
   final state has those keys, and fan-back causes concurrent writes
   against the parent's same keys. Fix: keep them OUT of the subgraph
   state, pass via module-level `_PIPELINE_CTX`.

4. **Send import location.** `from langgraph.types import Send` in 1.x.
   The old `langgraph.constants.Send` is deprecated.

5. **Compiled-subgraph-as-node drops Send state.** When you embed a
   compiled `g.compile()` subgraph via `add_node("name", subgraph)`,
   the Send dispatch sometimes loses the dict (we hit this in the
   first pass). Fix: use a single async function, not a compiled
   subgraph. `partner_pipeline_node(state)` is one function.

6. **state.json serialization.** `json.dumps` on Pydantic v2 models
   without `model_dump()` raises TypeError. `_dump()` in `state.py`
   MUST recurse through dict and list, calling `model_dump(mode="json")`
   on every leaf BaseModel.

7. **Em-dashes anywhere.** Voice rule. Grep `\u2014` before every commit.

8. **Force-push to main.** Don't. Never `git push --force` to main.

9. **CLA bot email.** Not relevant for this repo (it's our own), but
   if you ever PR upstream: use the verified primary email, not the
   `users.noreply.github.com` form.

10. **pydantic-core pin.** `pydantic-core==2.41.5` exact pin for
    Python 3.14. Newer versions break `openai` import.

## 9. Definition of done

The implementation is "done" when ALL of these are true:

- `pytest tests/` shows 11 passed, 0 failed, 0 warnings other than
  the pydantic v1 deprecation warning.
- `python scripts/run_local.py --dry-run --stream` runs end-to-end,
  prints events for trigger -> ingest -> aggregate -> partner_pipeline
  -> dispatch_emails, writes `state.json` plus 3 charts per partner
  plus 3 dry-run emails.
- `curl -X POST http://127.0.0.1:8080/run-weekly -d '{"dry_run": true}'`
  returns 200 with `run_id`, `thread_id`, `partners=3`,
  `emails_sent=3`, `errors=[]`.
- `curl http://127.0.0.1:8080/threads/<thread_id>` returns the saved
  checkpoint state.
- `grep -rn '—' src/ api/ scripts/ tests/ azure/ *.md` returns zero
  hits.
- `git log --oneline -3` on main shows the implementation commit.
- No file in `src/payments_reporting/` references `graph.py` symbols
  not yet defined (no forward references that would break at runtime).
- The code matches this plan. No drift.

## 10. If you find a deviation

1. STOP. Do not commit.
2. Update `plan.md` to reflect the new design.
3. Update the corresponding test.
4. Update `INTERVIEW_PREP.md` if interview answers change.
5. Commit the plan change separately from the code change so the diff
   is reviewable.

The plan is the contract. The code follows the plan. The tests prove
the code matches the plan.