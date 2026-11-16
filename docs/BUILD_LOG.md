# Build log

This is a chronological record of how OpsPilot was designed and built. It is meant
to make the engineering decisions reviewable, not just the final code.

## Goal

Build a portfolio-ready LangGraph project that demonstrates:

- a real state graph with persistence and tool calling;
- human-in-the-loop approval as a graph primitive, not a prompt suggestion;
- deterministic evidence before any LLM narrative;
- safe, auditable, reversible actions;
- a demo that runs end to end without production access.

The chosen scenario was an **approval-gated SRE incident commander**. It has a
natural irreversible action, a natural human approval point, and a natural audit
trail — all useful for showing why the graph is shaped the way it is.

## Step 1 — Repository and mock telemetry

The previous project folder and its `.git` were removed, and a new `ops-pilot`
repository was created.

Mock data was designed first, because the analysis layer should be driven by data
rather than hard-coded per-incident branches. Six files under `data/` define the
world:

| File | Contents |
| --- | --- |
| `services.json` | 5 services with owners, tiers, dependencies and runbook IDs |
| `incidents.json` | 5 incidents, from clear failures to one healthy false alarm |
| `metrics.json` | 11 metric series with a baseline window and an incident window |
| `logs.json` | 25 logs with timestamps, levels and failure signatures |
| `changes.json` | 4 deploys, flag changes and jobs |
| `runbooks.json` | one runbook per service |

Each incident was deliberately given a different evidence pattern:

- `INC-1001`: a deploy shortly before a latency/error spike.
- `INC-1002`: connection pool saturation with matching logs.
- `INC-1003`: a feature flag enabled before a 5xx spike.
- `INC-1004`: disk pressure after a failed log-rotation job.
- `INC-1005`: healthy metrics and no change — the agent should ask, not act.

The fifth case is the most important for the safety story.

## Step 2 — Typed, read-only data access

`data.py` loads every JSON file into frozen dataclasses (`Service`, `Incident`,
`MetricSeries`, `LogEntry`, `Change`, `Runbook`). It validates required fields,
normalises timestamps to UTC and exposes time-windowed queries:

- `get_incident`, `get_service`, `get_runbook`
- `metric_series`
- `query_logs` by window, level and substring
- `query_changes` by window

Keeping this layer read-only enforces the rule that runtime writes never touch
`data/`.

## Step 3 — Deterministic evidence engine

`analysis.py` is the core of the project. It computes, in plain Python:

1. **Metric statistics** — median of the 30-minute baseline, peak inside the
   incident window, ratio, and whether the metric crossed a rule.
2. **Anomaly rules** — either a ratio threshold (`1.8x`) or an absolute threshold
   (`error_rate_pct >= 1`, `disk_used_pct >= 85`, `pool_wait_ms >= 100`,
   `active_connections >= 90`).
3. **Log signatures** — messages normalised by replacing numbers and hashes, then
   grouped and counted.
4. **Recent changes** — deploys, flags and jobs in the two hours before the window.
5. **Ranked hypotheses** — transparent scores from 0.10 (`unknown`) to 0.95
   (multiple independent signals agree).
6. **A recommendation gate** — only `high` confidence (score >= 0.80) produces an
   action; otherwise `needs_human_input` is true and the result includes a focused
   question.

Every hypothesis carries a human-readable `calculation` string such as:

```text
latency_p95_ms: baseline median 221.50 ms -> peak 1,240 ms at ... (5.60x; peak/baseline >= 1.80x)
```

The LLM receives this payload. It does not recompute or invent any of it.

## Step 4 — Sandbox with an append-only ledger

`sandbox.py` separates three concepts:

- **Draft** — a proposed remediation. Creating one does not change anything.
- **Ledger entry** — an applied action. Append-only.
- **Audit event** — `propose`, `apply`, `reject`, `rollback`, and explicit no-ops.

Validation is pure and runs before any pause: the action must be supported, the
target must belong to the incident service, and deploy/flag actions must match an
actual recent change. Duplicate pending or applied drafts are rejected.

Important behaviours:

- applying an already-applied draft is an `apply_noop`, not a second write;
- rejecting a draft writes no ledger entry;
- rollback appends a reversal instead of deleting history;
- a second rollback is a `rollback_noop`;
- all state is JSON on disk under `sandbox/`, so it survives process restarts.

## Step 5 — LangGraph tools and human approval

`tools.py` exposes 12 tools. The two write tools are the interesting ones:

```python
@tool
def apply_remediation(draft_id: str) -> dict:
    ...
    ctx.sandbox.validate_remediation(...)   # pure validation before the pause
    decision = _decision(interrupt({...}))  # graph pauses here
    ...
    return ctx.sandbox.apply_draft(...)     # side effect after approval
```

Two details matter:

1. **Validation before `interrupt()` is safe because it is pure.** LangGraph
   re-executes the node from the start when the graph resumes, so any side effect
   before the pause would happen twice.
2. **The model cannot bypass approval.** Even if the LLM calls
   `apply_remediation` on its own, the tool still stops at `interrupt()`. The
   instruction in the system prompt is a usability layer, not the safety boundary.

The graph itself is hand-built:

```python
builder = StateGraph(MessagesState)
builder.add_node("agent", agent_node)
builder.add_node("tools", ToolNode(tools))
builder.add_edge(START, "agent")
builder.add_conditional_edges("agent", tools_condition)
builder.add_edge("tools", "agent")
```

`graph.py` also provides `pending_interrupts()` and `resume()`, used by both
interfaces. State is checkpointed to `sandbox/checkpoints.sqlite` by default via
`SqliteSaver`, so conversations and pending approvals survive a restart. Setting
`OPS_PILOT_CHECKPOINT_DB=memory` switches to `MemorySaver` for ephemeral runs.

## Step 6 — Interfaces

Two interfaces share the same graph:

- **CLI** (`cli.py`): a REPL with `/incidents`, `/audit`, `/new`, `/quit`, and an
  `Approve? [y/N]` prompt when the graph pauses.
- **Streamlit** (`ui.py`, entrypoint `app.py`): sidebar incident buttons, expandable
  tool/evidence panels, Approve/Reject buttons, and an audit-log viewer.

The UI intentionally renders tool output as evidence, so the user can inspect the
same payload the model saw.

## Step 7 — Tests

The suite avoids depending on an LLM or API key:

- `test_data.py` checks counts, lookups, time windows and filters.
- `test_analysis.py` checks the expected cause/action/target for all five incidents,
  the baseline arithmetic, and that `INC-1005` stays low-confidence and asks for
  human input.
- `test_sandbox.py` checks validation, duplicates, idempotent apply, rejection,
  append-only rollback, audit events and persistence across instances.
- `test_graph.py` builds a minimal `StateGraph` with a real `ToolNode`, invokes
  `apply_remediation` / `rollback_remediation`, asserts the graph pauses, then
  resumes with `Command(resume="approve" | "reject")`. This exercises the actual
  LangGraph interrupt protocol without an LLM.
- `test_checkpointer.py` verifies that SQLite state survives a new saver instance,
  and that the `memory` sentinel falls back to `MemorySaver`.

Run:

```bash
uv run pytest
```

Result at the time of writing: **43 tests passing**.

## Step 8 — SQLite by default

The first version used `MemorySaver` and treated SQLite as opt-in. That is a weak
demo: a pending approval disappears when the process exits, which is exactly the
situation where persistence matters.

The checkpointer now defaults to `sandbox/checkpoints.sqlite` via `SqliteSaver`.
`OPS_PILOT_CHECKPOINT_DB` can point somewhere else, and the value `memory` opts out.
A dedicated test proves the important property: a remediation pauses at
`interrupt()`, the graph and checkpoint connection are recreated, the pending
approval is still readable, and resuming it applies the draft exactly once.

## Step 9 — Free models and LangGraph Studio

The project now defaults to OpenRouter's free auto-router:

```bash
MODEL=openrouter:free          # alias for openrouter/openrouter/free
OPENROUTER_API_KEY=...
```

`model.py` normalises `openrouter:free` and `openrouter:` to the free router, keeps
pinned `:free` model IDs intact, and adds OpenRouter attribution headers. Tests cover
the alias mapping and the missing-key error without making network calls.

For visual debugging, `langgraph-cli[inmem]` was added as a dev dependency along with
`langgraph.json` and `langgraph_app.py`:

```bash
uv run langgraph dev
```

The API server exposes the graph as `ops_pilot` and injects its own checkpointer.
`build_api_graph()` therefore compiles without the local SQLite saver, while the CLI
and Streamlit keep using it.

### Blocking-call bug

The first `langgraph dev` run failed inside a real run:

```text
BlockingError: Blocking call to os.mkdir
```

`build_graph()` created the data store and sandbox at graph-construction time, but
the LangGraph API builds graphs inside an async event loop and rejects synchronous
I/O there. The fix was a lazy context: `LazyAppContext` defers JSON loading and
directory creation until a tool actually runs, and sync tools execute in a worker
thread. The server then completed an end-to-end run: `list_incidents` returned the
five open incidents and the model replied `5`.

## Bugs found while testing

Building the tests surfaced three real issues:

1. `dict.fromkeys()` was given nested evidence lists, causing
   `TypeError: unhashable type: 'list'`. Evidence is now flattened before
   deduplication.
2. The feature-flag hypothesis described the error metric by ID but did not include
   its baseline/peak calculation. It now carries the same calculation string as the
   other hypotheses.
3. A rollback test expected an error on a second rollback, but the intended design
   is an explicit no-op. The test was corrected to match the append-only contract.

## Deliberate limitations

- Telemetry is static mock data; there is no Prometheus/Loki integration.
- Applying a remediation writes a JSON ledger entry; it does not call real APIs.
- Hypothesis rules are simple and explainable, not a general RCA engine.
- SQLite checkpointing is local-only.

These are documented in the README so reviewers can distinguish demo scope from
production claims.
