# RFC: Temporal Grounding and Native Tool Use for Project Imagine

**Status:** Draft for review. Architecture and design only.
**Baseline:** Imagine v12 (frozen). Repo: `aiassistsecure/sukuna-imagine`.
**Phase:** Design, no training, no GPU spend, no weight changes, no publishes.

## 1. Mission and non-goals

### Mission

Extend Imagine, a ~1B text-to-SQL model, with two capabilities while preserving every existing strength:

1. **Temporal grounding.** Relative time expressions ("last month", "this quarter", "so far today") must resolve against a fixed, explicit clock and produce correct `date_trunc` and `interval` SQL, interpreted in the correct timezone. The model never guesses what "now" is: it either works from a fixed clock provided by the host or requests the time via `clock.now`, and it reports uncertainty when the temporal reading is ambiguous.

2. **Native tool use.** When the model cannot answer directly from the schema it knows, it may request a tool call through a new `<<<TOOL_CALL>>>` envelope. The host validates, authorizes, and executes the call against a fixed v1 registry of read-only tools, then returns an observation the model uses to produce its final grounded output.

The guiding principle: **the model proposes, the host authorizes, the database verifies.**

### Non-goals

- No hosted-inference dependency. Tool use works with local inference (llama.cpp, Hearth residency) and does not require any external model API.
- No write path. The v1 registry is read-only. Writes (INSERT, UPDATE, DELETE) remain out of scope and, when eventually supported, require explicit human approval per operation.
- No schema migration, no corpus regeneration for v12, no retraining of v12. This RFC designs the future feature branch; v12 stays frozen.
- No changes to the host-side Hearth or AiAssist Secure implementations in this RFC. This document defines the protocol contract so the host track can build independently.

## 2. Non-negotiable constraints

1. **v12 is frozen.** Do not modify, retrain, republish, or re-slice v12 weights, checkpoints, GGUFs, eval results, or corpus. All work in this phase is architecture, protocol, schemas, prototypes, and test plans. Any future model built on this design is a new model with its own name and artifacts, not a v12 revision.
2. **No hosted-inference dependency.** The protocol must be implementable over a local socket against local tools. Cloud round trips are optional, never required.
3. **No fabricated outputs.** The model must not invent schema objects, column values, row counts, or timestamps. Where the existing protocol says UNANSWERABLE, tool use extends that discipline, never bypasses it: if the evidence is missing, the answer is a clarifying question or UNANSWERABLE.
4. **Writes need human approval.** v1 tools are read-only. Any future write-capable tool is gated on an explicit human approval step, per operation, before the host executes anything. No standing pre-approvals.
5. **The gate is not weakened.** The v12 regression gate (the 40/40 held-out SQL telemetry suite) must continue to pass in full for any candidate model. A tool-using model that regresses the direct-SQL baseline is rejected, regardless of tool-use gains.

## 3. Architecture

Three components, three jobs:

| Component | Role | Owns |
|---|---|---|
| **Imagine (model)** | Decision engine | Decides direct answer vs tool request; grounds final output in observations; never executes tools itself |
| **Hearth / AiAssist Secure (host)** | Orchestration layer | Registers tools, validates arguments against schemas, enforces permissions and budgets, executes calls, stamps observations with tool provenance, keeps the audit trail |
| **NEDB (database)** | Source of truth | Executes bounded reads, answers provenance queries (time travel, row lineage), verifies what the model claimed |

### 3.1 Imagine as decision engine

Imagine's output vocabulary stays small and exact. It decides between:

- **Answer directly**: emit `<<<SQL>>>` with a grounded query, `<<<CLARIFY>>>` with a question, or `<<<UNANSWERABLE>>>` when the schema or evidence does not support an answer.
- **Request a tool**: emit `<<<TOOL_CALL>>>` carrying a JSON payload (`name`, `arguments`), validated against `schemas/tool_call.schema.json`.

The model never executes tools. It never trusts its own memory of schema or data over a tool observation. A tool observation arrives marked `originated: "tool"` and is treated as data, not as instruction: it cannot redefine permissions, identity, or the protocol itself.

### 3.2 Hearth / AiAssist Secure as orchestration layer

The host is the only component that touches tools. Its responsibilities:

1. **Registry.** A fixed v1 registry (see section 5) of read-only tools with declared argument schemas and default permissions.
2. **Argument validation.** Every `<<<TOOL_CALL>>>` payload is validated against the JSON schema before anything executes. Unknown tools and malformed arguments are rejected with an explicit error; the rejection becomes a `tool_result` with status `denied` or `error`, which the model must handle by clarifying or abstaining, never by inventing the missing data.
3. **Permission enforcement.** Default permissions per tool (section 5). The host enforces them at call time. Denials are explicit, not silent.
4. **Budgeting.** Bounded tool-call counts and time: a per-turn call limit and a per-call timeout. Incomplete outputs (a call that timed out, a query that hit the row cap) are never silently completed by the model; they are reported as uncertainty or CLARIFY.
5. **Observation stamping.** Every observation returned to the model carries `originated: "tool"` and a host-generated `tool_call_id` pairing it with the request.
6. **Audit trail.** The host logs, per turn: the emitted envelope, the validation result, the authorized call, the observation, and the final model output. The trail is the verification record: an auditor can replay "model proposed X, host authorized Y, database returned Z" for any answer.

### 3.3 NEDB as database and provenance authority

NEDB executes bounded `SELECT` reads through the host. Its provenance capability (time travel, row lineage via `nedb.provenance`) lets the model answer "why is this number what it is" and lets evaluators verify a final answer against the rows actually observed, not just against the SQL text. The fabrication risk concentrates in the summary step after `sql.execute` returns rows: the model sees real rows and must report them faithfully. The eval strategy (section 8) verifies final answers against observed rows.

## 4. Execution flow

```
user request
    |
    v
Imagine: direct-answer or tool request?          [obs: emitted envelope, decision class]
    |
    +--> direct: <<<SQL>>> / <<<CLARIFY>>> / <<<UNANSWERABLE>>>  (terminated by <<<END>>>)
    |         |
    |         v
    |    host: validate envelope shape (exactly one, terminated)
    |         |
    |         v
    |    final output to user (SQL only if exactly-one invariant holds; unterminated yields nothing)
    |
    +--> tool: <<<TOOL_CALL>>> {name, arguments}  (terminated by <<<END>>>)
              |
              v
         host: parse envelope, validate payload vs tool_call.schema.json   [obs: validation pass/fail]
              |
              v
         host: permission + budget check                                   [obs: allow / deny with reason]
              |
              +--> deny: tool_result {status: denied, error:{code,message}}
              |
              +--> allow: execute tool, enforce per-call timeout            [obs: raw result, duration]
                       |
                       +--> timeout: tool_result {status: timeout}
                       +--> success: tool_result {status: success, result, originated:"tool"}
                       +--> error:   tool_result {status: error, error:{code,message}}
              |
              v
         host appends tool_result (originated: tool) to model context        [obs: paired call/observation]
              |
              v
         Imagine: grounded final output
              (<<<SQL>>> grounded in observed schema/rows,
               <<<CLARIFY>>> on missing evidence,
               <<<UNANSWERABLE>>> when evidence contradicts or is absent)
              |
              v
         host: final envelope validation, audit log entry                    [obs: full turn trail]
```

**Per-transition observability.** Each bracketed `[obs: ...]` item is recorded in the host audit trail: the envelope text, the validation verdict, the authorization decision with reason, the tool's raw result and duration, the paired observation, and the final output. Any turn is replayable.

**Call budget.** A per-turn limit on tool calls (default proposal: 5) and a per-call timeout (default proposal: 30s). When the budget is exhausted, the host returns an explicit budget error and the model must produce its final output from what it has, clarifying or abstaining rather than hallucinating the rest.

## 5. Tool interface v1: registry

All v1 tools are read-only. Default permissions are advisory to the host implementer; the host enforces whatever permission set it is configured with, and every denial is explicit.

| Tool | Responsibility | Default permission |
|---|---|---|
| `schema.inspect` | Return the catalog (tables, columns, types, keys) for scope `public` or `private`. The model's source of truth for schema, replacing memorized schema. | allow, scoped by `scope` arg |
| `clock.now` | Return the host clock reading (timestamp + timezone) as of the call. The only legitimate source of "now" for relative time expressions. | allow |
| `sql.explain` | Analyze a SELECT statement (plan, estimated cost, errors) without executing it. Lets the model sanity-check generated SQL before running it. | allow, SELECT only |
| `sql.execute` | Run a single bounded SELECT with `max_rows` (1..1000). Returns rows plus a truncation flag. Non-SELECT statements rejected. | allow, SELECT only, bounded |
| `nedb.provenance` | Return the provenance record for one row (`table`, `key`): lineage, time-travel snapshots, write history. | allow, read-only |

## 6. Protocol definition

### 6.1 Envelopes

The model emits exactly one envelope per turn, chosen from:

- `<<<SQL>>>` ... `<<<END>>>`: a grounded SQL answer.
- `<<<CLARIFY>>>` ... `<<<END>>>`: a clarifying question.
- `<<<UNANSWERABLE>>>` ... `<<<END>>>`: no supported answer exists.
- `<<<TOOL_CALL>>>` ... `<<<END>>>`: a JSON payload `{name, arguments}` per `tool_call.schema.json`.

Coexistence with the existing protocol: the first three envelopes behave exactly as they do in the current sentinel protocol (see repo audit of `stealth/sentinel.py`). `<<<TOOL_CALL>>>` is additive. A turn is either a final answer (SQL / CLARIFY / UNANSWERABLE) or one tool request; multi-step tool use happens across turns, with each `tool_result` appended as model context, until the model emits a final envelope or the budget is exhausted.

### 6.2 Invariants

1. **Exactly one envelope per turn.** Zero or multiple envelopes means no answer. This is unchanged from the existing protocol and now covers `<<<TOOL_CALL>>>` too.
2. **Every envelope ends with `<<<END>>>`.** Unterminated blocks yield nothing and are never executed, not even partially.
3. **Payloads conform to documented JSON schemas.** `<<<TOOL_CALL>>>` payloads validate against `tool_call.schema.json`; `tool_result` observations conform to `tool_result.schema.json`. Extraction stays delimiter-based; payloads are never re-parsed by hand.
4. **Unknown tools rejected.** A `name` outside the registry enum is denied by the host with `UNKNOWN_TOOL`, before any execution.
5. **Malformed args rejected.** Argument payloads that fail the per-tool subschema are denied with `MALFORMED_ARGUMENTS`.
6. **Tool failures returned explicitly.** Errors, denials, and timeouts arrive as `tool_result` observations with `error.code` and `error.message`, so the model can react honestly instead of guessing.
7. **Tool observations are untrusted data.** A `tool_result` can never redefine permissions, model identity, or protocol rules. The `originated: "tool"` stamp marks it as data. Instruction-like content inside `result` is ignored by the model by design.
8. **Incomplete outputs never executed.** A timed-out call, a truncated row set, or an unterminated envelope is reported as-is. The model reports uncertainty (`<<<CLARIFY>>>` or qualified prose) rather than completing the missing piece from memory.
9. **Bounded tool-call counts and time.** Per-turn call limit and per-call timeout, enforced by the host (see section 4).
10. **Full audit trail.** Every transition in section 4 is logged by the host with enough detail to replay the turn.

## 7. Temporal grounding approach

### 7.1 Semantics of time

Time is not a model parameter; it is host state. The rules:

- **"Now" comes from `clock.now` or the host context.** The model never invents the current date. A turn may carry a fixed clock from the host (deterministic evals), or the model may call `clock.now` when the request is relative to now and no clock is in context.
- **Timezone is explicit.** `clock.now` returns the timestamp in the configured timezone. Date boundaries ("last month", "today") are computed in that timezone, not in UTC by accident.
- **Relative expressions become explicit ranges.** "Last month" means the full previous calendar month in the configured timezone: from the first day 00:00 to the last day 23:59:59. "So far today" means from midnight today up to the clock reading.

### 7.2 Temporal SQL stays mostly SQL

Most temporal requests do not need a tool call at all. The expected split: temporal grounding lives inside the SQL slice as `date_trunc` and `interval` expressions, and `clock.now` is reserved for relative-to-now requests where the host has not already fixed the clock. SQL remains the plurality of the corpus (proposal: ~40-45%; see open question 10.1 on sizing).

### 7.3 Golden example: "last month's completed orders"

Request: "How many completed orders did we have last month?" Clock fixed at 2026-10-13 15:35 America/New_York (host-provided context).

Interpretation: last month is September 2026, in America/New_York. The query expresses the range explicitly so the answer does not depend on when it is read:

```sql
<<<SQL>>>
SELECT count(*) AS completed_orders
FROM orders
WHERE status = 'completed'
  AND created_at >= date_trunc('month', (TIMESTAMP '2026-10-13 15:35:00 America/New_York') - INTERVAL '1 month')
  AND created_at <  date_trunc('month', TIMESTAMP '2026-10-13 15:35:00 America/New_York');
<<<END>>>
```

Equivalent idiom when the host supplies a `now()` bound to the configured clock:

```sql
WHERE status = 'completed'
  AND created_at >= date_trunc('month', now() - INTERVAL '1 month')
  AND created_at <  date_trunc('month', now())
```

Both forms pin "last month" to explicit calendar boundaries in the correct timezone. The eval fixtures (section 8) use fixed clocks so the same prompt always has one right answer.

### 7.4 Failure behavior

If the temporal reading is ambiguous ("recent orders" with no agreed window), the model asks (`<<<CLARIFY>>>`), it does not pick a window silently. If a `clock.now` call fails, the model reports that it cannot resolve relative time (`<<<CLARIFY>>>` or `<<<UNANSWERABLE>>>`), it does not fall back to a memorized date.

## 8. Training curriculum overview

Curriculum design follows one principle: **skew toward judgment, not syntax.** The hard part is not formatting `{"name": ...}`; it is deciding when to call, when to abstain, and which tool fits. The proposed slices (hypotheses, to be sized by the Phase 0/Phase 4 baseline of unmodified v12):

- **SQL plurality (~40-45%).** Direct grounded SQL, including the temporal SQL idioms (date_trunc, interval) inside this slice. Keeps the regression gate passing by construction.
- **Temporal grounding cases.** Relative-to-now requests with fixed-clock fixtures in the prompt, so the model learns explicit range SQL and timezone-aware boundaries. Only genuinely relative requests exercise `clock.now`.
- **Tool-use cases (~20% hypothesis).** Multi-turn episodes: schema unknown so call `schema.inspect`; ambiguous schema so inspect then clarify; rows needed so `sql.execute` with a sane `max_rows`; suspicious numbers so `nedb.provenance`; plan check via `sql.explain` before a heavy read.
- **Abstention cases.** Deliberate traps: unknown tool names, malformed arguments, out-of-scope schema, failed tool calls, contradictory observations. The correct behavior is `<<<CLARIFY>>>` or `<<<UNANSWERABLE>>>`, never a fabricated recovery.
- **Summary fidelity cases.** `sql.execute` returns rows, and the final answer must report exactly those rows: counts, values, and the truncation flag when `max_rows` cut the result. Fabrication concentrates here, so these cases are over-represented relative to their corpus share.

Every curriculum row uses the existing JSONL convention: `messages[]` (system/user/assistant) plus `meta{kind, ...}`, with `meta.kind` extended to cover the new episode types (e.g. `tool_call`, `tool_result`, `temporal_sql`, `abstain`, `summary_fidelity`).

## 9. Evaluation strategy summary

Three layers, each catching what the others miss:

1. **Regression gate (unchanged).** The 40/40 held-out SQL telemetry suite must pass in full. No exceptions, no re-weighting.
2. **Temporal eval with fixed-clock fixtures.** Prompts ship with a fixed clock in context (timestamp + timezone). Expected outputs use explicit `date_trunc`/`interval` ranges. The fixture makes the answer deterministic: one clock, one right SQL, one right row set. Cases cover month/quarter/year boundaries, timezone edges (a request evaluated near midnight), and ambiguous phrasings that must produce CLARIFY, not a guess.
3. **Tool-use eval with row-verified summaries.** Episodes run against a real NEDB backend through the host. Scoring checks: the right tool was requested with valid args (judgment), the host's validation/denial behavior on adversarial inputs (robustness), and, critically, the final answer against the rows actually observed. A summary that invents a value not present in the returned rows fails even if the SQL was perfect. This is the anti-fabrication layer.
4. **Refusal and clarification regression suite.** Dedicated out-of-schema and ambiguous probes (building on the known gap: the 40/40 telemetry set covers valid questions only, and a manual out-of-schema probe produced a hallucinated column instead of UNANSWERABLE). Unknown tools, malformed args, permission denials, timeouts, and contradictory observations must each produce CLARIFY or UNANSWERABLE, never a fabricated answer.

Determinism note: temporal and tool-use evals pin the clock and the database contents, so runs are reproducible without retraining anything.

## 10. Phased plan

- **Phase 0: Audit.** Inventory the current protocol (`stealth/sentinel.py`), corpus conventions, the 40/40 gate, and host-side capabilities. Deliverable: the audit note this RFC builds on. No model changes.
- **Phase 1: Architecture RFC.** This document plus the JSON schemas. Review gate: Mark approves the contract.
- **Phase 2: Protocol prototypes and tests.** Deterministic parser prototype for the new envelope (delimiter-based extraction, exactly-one invariant, `<<<END>>>` termination), JSON Schema validation harness for `tool_call`/`tool_result`, and unit tests. No training.
- **Phase 3: Temporal eval spec with fixed-clock fixtures.** Fixture format, expected-range SQL, ambiguity cases. No training.
- **Phase 4: Tool-use curriculum taxonomy and baseline test plan for unmodified v12.** Taxonomy of episode kinds; a test plan that runs the baseline eval of unmodified v12 against the temporal and tool-use probes to size the curriculum split (the ~20% tool-use figure is a hypothesis until this baseline lands). No training, no weight changes.
- **Phase 5: Hearth / llama.cpp / NEDB / AiAssist Secure compatibility analysis.** Map the protocol contract onto the parallel host-side build: where validation lives, where budgets are enforced, how observations are stamped and audited. The contract (this RFC) is what lets both tracks proceed independently.
- **Phase 6: Candidate comparison.** When a candidate model exists on a feature branch, compare it against the frozen v12 baseline on all four eval layers. Ship criteria: gate still 40/40, temporal and tool-use layers pass at target rates, refusal suite clean. v12 artifacts are never modified in the process.

## 11. Success definition

The walkthrough that proves the design works, end to end:

**"Who were our five biggest customers last month?"**

1. The model does not know the schema, so it requests `schema.inspect` with scope `public`. The host validates and returns the catalog. The model now knows the customers and orders tables.
2. "Last month" is relative to now. The host context carries a fixed clock (or the model calls `clock.now`); the model interprets the previous calendar month in the configured timezone.
3. The model generates grounded SQL: join customers to orders, filter `created_at` to the explicit month range, aggregate spend, order descending, limit 5. Optionally it runs `sql.explain` first.
4. The host validates the `sql.execute` call (SELECT only, `max_rows` within bounds) and runs it. NEDB returns five rows plus a no-truncation flag. The observation is stamped `originated: "tool"`.
5. The model returns a machine-readable result (JSON rows with customer id, name, spend) inside `<<<SQL>>>` or a structured final envelope, reporting exactly the observed rows.
6. On failure at any step (unknown table, denied call, timeout, empty month), the model reports uncertainty honestly: `<<<CLARIFY>>>` or `<<<UNANSWERABLE>>>`, never an invented top-five.

If that walkthrough runs clean on a frozen-clock fixture with an audit trail that replays, the design has succeeded.

## 12. Open questions for Mark

1. **Corpus split sizing.** The ~20% tool-use and ~40-45% SQL figures are hypotheses. The Phase 4 baseline eval of unmodified v12 should size them empirically. Agree to hold the numbers as hypotheses until then?
2. **Host-side build ownership.** This RFC defines the contract; the Hearth / AiAssist Secure orchestration build is a parallel track. Who owns the host implementation schedule, and where does the audit trail live (host-local log, shipped to NEDB, both)?
3. **Write-approval UX.** v1 is read-only by design, but the eventual write path needs a human-approval interaction. What should that approval look like (chat confirmation, signed token, separate console), and should the protocol reserve an envelope shape for it now?
4. **Budget defaults.** Proposed per-turn call limit of 5 and per-call timeout of 30s. Are these the right defaults, or should the host configure them per deployment?
5. **Final envelope shape.** Section 11 step 5 returns a machine-readable result. Should the final answer after tool use stay inside the existing three envelopes (e.g. `<<<SQL>>>` carrying the grounded query plus a result block), or does the protocol need a fourth final envelope such as `<<<RESULT>>>`?
6. **Provenance depth.** `nedb.provenance` returns a per-row record. Is per-row granularity right for v1, or do we need table-level lineage queries too?
