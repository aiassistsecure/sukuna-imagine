# Phase 4 Baseline Testing Plan: Unmodified v12 on Tool-Use and Temporal Scenarios

Project Imagine, temporal grounding + native tool use. Phase 4 of the engineering
directive. Author: systems analysis track. Date: 2026-10-13.

## 1. Objective

Measure what unmodified Imagine v12 can and cannot do on the new scenarios
*before* any fine-tuning. v12 is frozen: no weight changes, no corpus changes,
no training runs in this phase. The baseline exists to size the tool-use corpus
hypothesis (currently floated at ~20% of corpus) with evidence instead of
instinct.

The decision the baseline feeds: how much of the new training slice must teach
*syntax* (emitting a well-formed `<<<TOOL_CALL>>>` envelope) versus *judgment*
(when to call, which tool to call, when to answer directly). If v12 already
emits clean envelopes under light prompting, the slice skews toward judgment
examples and shrinks. If it cannot reliably emit the envelope, a format-teaching
sub-slice is required first. Section 7 makes this rule explicit.

## 2. Non-goals

- No training, no fine-tuning, no LoRA adapters, no corpus edits.
- No changes to the host orchestration layer (that is the parallel build; the
  baseline simulates the host side with fixtures).
- No redefinition of the tool JSON schemas or envelope grammar. Those belong to
  the protocol RFC and deterministic parser tracks. The baseline consumes their
  contract as a fixed input and reports compliance against it.

## 3. Harness reuse

The baseline reuses the repo's existing eval conventions rather than inventing
new ones.

- **Row format.** `stealth/evaluate.py:load_eval_set` reads JSONL rows of
  `{schema_key, question, reference_sql}`. Baseline sets keep this shape and add
  optional fields: `reference_tool` (the labeled correct tool choice, or
  `"direct"` for answer-without-tools), `fixture_clock` (ISO timestamp pinning
  "now" for temporal probes), and `expected_behavior` (e.g. `"call"`,
  `"abstain"`, `"clarify"`, `"unanswerable"`). Rows without the new fields
  behave exactly like current eval rows.
- **Model seam.** The evaluator's `generate(messages) -> str` callable stays the
  only model-shaped thing in the harness, so baseline runs hit the same
  llama-server (`imagine-v12.service`, Q4_K_M GGUF, ctx 8192) with no harness
  fork.
- **Metric philosophy.** Execution accuracy over string similarity, per the
  module docstring in `stealth/evaluate.py`. Temporal probes are graded by
  running the model's SQL against a real database under a fixed clock and
  comparing result sets (L4-style), not by comparing date strings.
- **Fixed-clock fixtures.** Temporal probes run under the fixed-clock fixtures
  defined by the temporal eval spec track (directive deliverable 4). The
  baseline does not define its own clocks; it consumes that track's fixtures so
  both tracks grade against the same "now". Open question: whether the fixture
  set ships in time for the first baseline run; if not, the baseline starts
  with Tier 0-2 and adds Tier 3 when fixtures land.

## 4. Scenario sets

### Tier 0: Regression gate (must pass first)

The 40/40 held-out telemetry suite, run with inference config identical to the
2026-10-12 v12 eval. Target: 40/40, unchanged. If Tier 0 fails, the serving
setup drifted (wrong GGUF, changed sampling, context truncation), not the
model. Stop and fix the setup before reading any other number.

### Tier 1: Envelope emission (protocol compliance, no execution)

Unmodified v12 has never seen `<<<TOOL_CALL>>>`. These probes test whether
light prompting alone produces a parseable envelope.

- Prompt: the v12 system prompt plus a short tool preamble describing the five
  v1 tools (`schema.inspect`, `clock.now`, `sql.explain`, `sql.execute`,
  `nedb.provenance`) and the `<<<TOOL_CALL>>>` JSON envelope terminated by
  `<<<END>>>`, with the standing exactly-one-block rule from `sentinel.py`.
- Probes (~40): half where a tool call is the labeled correct move, half where
  answering directly is correct. The model is graded only on envelope shape:
  exactly one block, valid JSON per the RFC schema, terminated by `<<<END>>>`.
- Deliberate stress cases: questions that tempt prose around the block,
  questions with code fences in the wording, and multi-part questions that tempt
  two blocks (the exactly-one invariant must hold).

### Tier 2: Tool-use judgment probes

Given a question, the schema (as v12 normally receives it), and the tool
preamble, the model must decide: answer directly, call a tool, or decline.

- **When to call vs abstain (~30 probes).** Questions answerable from the
  supplied schema alone (label: `direct`) versus questions that genuinely need a
  tool: unknown schema details (`schema.inspect`), "as of today" or relative
  dates (`clock.now`), "why is this slow / is this valid" (`sql.explain`),
  "what does the data actually contain" (`sql.execute`, bounded SELECT),
  "when was this row written vs when was it true" (`nedb.provenance`).
- **Which tool (~20 probes).** Each probe admits exactly one correct tool; the
  distractors are plausible (e.g. `sql.execute` vs `sql.explain` for a
  performance question; `clock.now` vs guessing the date for "this quarter").
- **Missing-schema behavior (~15 probes).** Questions referencing tables or
  columns absent from the supplied schema. Labels distinguish: inspect first
  when the schema might be partial, versus `<<<UNANSWERABLE>>>` when the schema
  is declared complete. This tier doubles as a control for the known v12 eval
  gap (2026-10-13: an out-of-schema probe produced a hallucinated column instead
  of UNANSWERABLE), so tool-use work neither masks nor worsens it.
- **Grounded follow-through (~20 probes).** A fixture host returns a canned
  observation for the model's tool call (fixed JSON, marked tool-originated).
  The model then produces its final answer. Graded on: does the final answer
  stay within the observed rows, and does it cite the observation rather than
  inventing beyond it. This is the baseline for the fabrication risk.

### Tier 3: Temporal probes under fixed clocks

- **Calendar periods (~15):** "last quarter", "Q3", "this fiscal year" style
  questions resolved against the fixture clock.
- **Rolling windows (~15):** "past 30 days", "last 7 days", trailing windows
  anchored at the fixture "now".
- **Boundaries (~15):** month-end, quarter-end, year-end edge cases; inclusive
  vs exclusive bounds; "as of" a specific date.
- **Time zones (~10):** the same logical question asked with the fixture clock
  in different zones; the model must produce the same correct window, not shift
  it. Baseline prompt states the zone explicitly (simulating what `clock.now`
  would return); a sub-probe withholds the zone to test whether the model asks
  for the clock instead of guessing.
- Grading is execution accuracy against reference SQL run on the same fixture
  data, plus a dedicated date-hallucination count (Section 5).

### Tier 4: Refusal and clarification controls

Port the existing UNANSWERABLE/CLARIFY probe style into tool-adjacent cases:
ambiguous time references ("recent orders" with no window), ambiguous tool
need, and questions no tool can rescue. These keep the "don't call a tool"
judgment (the case the sentinel docstring calls out) under measurement.

## 5. Metrics

| Metric | Definition | Why it matters |
|---|---|---|
| Regression EX ACC | 40/40 held-out telemetry suite | The crown jewel; any regression blocks corpus work |
| Envelope compliance | Fraction of Tier 1 emissions that are exactly one well-formed `<<<TOOL_CALL>>>` JSON block terminated by `<<<END>>>` | Decides the syntax-vs-judgment split |
| Malformed rate | Bad JSON, unterminated blocks, extra prose, nested markers | Sizes the format-teaching sub-slice |
| Tool-selection accuracy | Fraction of Tier 2 probes where the chosen tool (or `direct`) matches the label | Core judgment measure |
| Unnecessary-call rate | Fraction of `direct`-labeled probes where the model calls a tool anyway | Over-reliance signal; drives abstention negatives |
| Missed-call rate | Fraction of tool-labeled probes where the model answers directly | Under-reliance signal |
| Temporal EX ACC | Execution accuracy on Tier 3 under fixed clocks | The temporal gap, in the repo's own currency |
| Date-hallucination rate | Fraction of temporal answers with date literals not derivable from the fixture clock and question | Silent-wrong-answer detector |
| Time-zone consistency | Fraction of zone-varied probe pairs producing the same correct window | Catches zone-shifted windows |
| Grounded-summary fidelity | Fraction of Tier 2 follow-throughs whose final answer stays within observed rows | Baseline for the fabrication risk |
| Refusal precision | UNANSWERABLE/CLARIFY emitted only where labeled | Protects the known v12 gap from regressing |

All rates are reported with raw counts (e.g. 34/40), never percentages alone.

## 6. Run discipline

- **Identical inference config.** Same GGUF file, same context length (8192),
  same sampling parameters, same system prompt base as the 2026-10-12 v12 eval.
  The full config is recorded in the run log; any deviation invalidates
  comparability and is declared up front.
- **Deterministic where possible.** Fixed seeds for probe ordering; fixture
  clocks pinned, never wall-clock time. A rerun of the baseline must reproduce
  its own numbers.
- **No prompt tuning between tiers.** The tool preamble is fixed before Tier 1
  runs. Tuning the preamble to chase Tier 1 numbers then reporting Tier 2 on
  the tuned preamble is contamination of the judgment read. If the preamble
  changes, Tiers 1-2 rerun from scratch and both versions are reported.
- **Held-out quarantine.** Baseline probe templates, including any new
  held-out temporal/tool templates, never enter training data. Template-level
  dedup against the v12 corpus runs before the baseline is considered final.

## 7. Decision rule for corpus sizing

The ~20% tool-use slice is a hypothesis. These rules convert baseline numbers
into a sizing recommendation for the curriculum track:

1. **Envelope compliance >= 90% with light prompting:** skew the slice toward
   judgment examples (when/which/abstain) over syntax. Format teaching is a
   thin preamble plus a small set of negative format examples.
2. **Envelope compliance < 70% or malformed rate > 20%:** the slice needs a
   dedicated syntax/format sub-slice first; judgment examples cannot land on a
   model that cannot emit the envelope.
3. **Tool-selection accuracy >= 80% with unnecessary-call rate <= 15%:** the
   model already judges well; keep the slice at the low end (~10-15%) and spend
   it on hard judgment cases (ambiguous tool choice, abstention).
4. **Unnecessary-call rate > 25%:** add abstention-focused negatives at roughly
   a 1:1 ratio with positive call examples until the rate drops in a pilot.
5. **Temporal EX ACC within 10 points of v12's SQL EX ACC:** temporal grounding
   is mostly a fixtures-and-prompting matter; the temporal slice stays small.
   **Gap > 15 points:** grow the temporal slice; the model is not resolving
   relative dates reliably even with the reference date supplied.
6. **Any Tier 0 regression:** corpus work pauses until the serving setup is
   root-caused. No new training on a drifting harness.

The final percentage is set jointly with the curriculum taxonomy track once
both the baseline numbers and the taxonomy exist. The baseline does not set it
alone.

## 8. Deliverables of the baseline run

- Run log: inference config, prompt preamble text, fixture clock values, seed.
- Per-tier results table with raw counts for every metric in Section 5.
- Failure corpus: every malformed envelope and every misjudged probe, verbatim,
  for the curriculum track to mine.
- Sizing recommendation memo applying the Section 7 rules, with the numbers
  attached.
