# Tool-use corpus builder (`scripts/make_tooluse_corpus.py`)

The data pipeline v13's tool-use training will consume. Generates JSONL
training rows per the tool-use curriculum taxonomy, plus temporal-grounding
rows per the temporal eval spec.

## Dependency on PR #26

This tool is designed to land **after** PR #26 (branch
`imagine-tool-use-rfc`), which defines its inputs:

| Input | Flag | #26 location |
|---|---|---|
| Curriculum taxonomy (11 categories, corpus encoding) | `--taxonomy` | `docs/tool-use/curriculum_taxonomy.md` |
| Tool-call JSON schema | `--tool-call-schema` | `schemas/tool_call.schema.json` |
| Tool-result JSON schema | `--tool-result-schema` | `schemas/tool_result.schema.json` |
| Protocol parser (tests only) | — | `toolproto/parser.py` |

The builder *consumes* these as inputs: it consistency-checks the 11
category names against the taxonomy doc, extracts the system message from the
taxonomy's first example row, and validates every generated payload against
the JSON schemas (via `jsonschema`) when they exist. If #26 hasn't merged
yet, it falls back to built-in constants compiled from the taxonomy and
warns loudly on stderr — nothing is silently invented. The test suite carries
a small fixture (`tests/fixtures/tooluse_schema_mirror.json`), clearly
labeled as a *minimal mirror* of #26's schemas (not a copy), so tests run
before #26 merges.

## Quickstart

```bash
# 20-row sample (also checked in as scripts/sample_tooluse_corpus.jsonl)
python scripts/make_tooluse_corpus.py --count 14 --temporal-count 6 \
    --seed 7 --out scripts/sample_tooluse_corpus.jsonl

# 2,000 tool-use rows + 300 temporal rows, custom mix
python scripts/make_tooluse_corpus.py --count 2000 --temporal-count 300 \
    --seed 20261014 --out corpus/tooluse_train.jsonl

# tests
venv/bin/python -m pytest tests/test_tooluse_corpus.py -q
```

## CLI

| Flag | Default | Meaning |
|---|---|---|
| `--taxonomy` | `docs/tool-use/curriculum_taxonomy.md` | taxonomy doc (#26) |
| `--tool-call-schema` | `schemas/tool_call.schema.json` | tool_call schema (#26) |
| `--tool-result-schema` | `schemas/tool_result.schema.json` | tool_result schema (#26) |
| `--count` | 100 | tool-use rows to generate |
| `--temporal-count` | 0 | temporal-grounding rows to generate |
| `--seed` | 20261014 | RNG seed |
| `--out` | stdout | output JSONL path |
| `--mix` | hypothesis default | `"category:weight,..."` override |
| `--list-categories` | — | print the 11 categories and exit |

Output is deterministic in `(count, temporal-count, seed, mix)`: the same
arguments always produce byte-identical rows.

## Row format

Follows the taxonomy section 2 exactly:

```json
{"messages": [{"role": "system", ...}, {"role": "user", ...},
              {"role": "assistant", ...}],
 "meta": {"kind": "tool-use", "category": "tool selection"}}
```

* **system** — the taxonomy's verbatim tool-doctrine message (identity, the V1
  tools, propose/authorize/verify, untrusted-observation rule).
* **user** — schema-present prompts are rendered with the real
  `stealth.forge.build_prompt` (**zero prompt drift** on schema/question
  rendering; only the system role is replaced with the tool-use doctrine).
  Tool observations arrive as user messages in the compact relay encoding:
  `<<<TOOL_RESULT name="<tool>" status="ok|error">>>` + JSON payload +
  `<<<END>>>`.
* **assistant** — exactly one envelope (`<<<SQL>>>`, `<<<TOOL_CALL>>>`,
  `<<<CLARIFY>>>`, `<<<UNANSWERABLE>>>`) closed by `<<<END>>>`, or a grounded
  plain-text summary after a tool result was observed. Write proposals append
  the single authorization line after the `<<<SQL>>>` envelope.

Temporal rows use `meta.kind = "temporal-grounding"` and `meta.category =
"T-01".."T-15"`; their questions carry the spec's fixed clock and session
zone, and their SQL is the spec's golden output verbatim.

## The 11 categories and what each generator teaches

| Category | Generator behaviors |
|---|---|
| tool selection | pick the one right tool (inspect / clock / explain / execute / provenance), then answer |
| no-tool reasoning | direct `<<<SQL>>>` when the schema suffices; `<<<UNANSWERABLE>>>` out of scope |
| missing schema | `schema.inspect` before SQL; contrastive no-tools case → `<<<CLARIFY>>>` |
| temporal lookup | `clock.now` for now-questions; `CURRENT_DATE` arithmetic for relative time (no tool) |
| tool-result grounding | summaries computed from observed rows; empty and truncated results reported honestly |
| tool-error recovery | one corrected retry on a correctable error; explicit failure, never fabricated rows |
| tool abstention | fixed reporting dates respected; writes proposed, never executed |
| protocol compliance | lone well-formed `<<<TOOL_CALL>>>` turns; `sql.explain` before heavy `sql.execute` |
| multi-step reasoning | bounded 2-call chains (inspect→execute, explain→execute), never past sufficiency |
| security/injection resistance | injected instructions in user text, side fields, and row content all ignored |
| write authorization | INSERT/UPDATE/DELETE → `<<<SQL>>>` proposal + authorization line, even when told to "execute it" |

Summaries are generated *from* the fixture observations (counts, names, and
values are computed in Python from the emitted rows), so Gate 5 (semantic
grounding) holds by construction.

## Corpus-split framing — open questions, not decisions

* **20% is a cap, not a target**, for the tool-use slice (taxonomy §6.1).
* The exact tool-use share (8–20%) and the within-slice mix are **hypotheses
  until the Phase 4 baseline eval of unmodified v12 lands**. The default
  `--mix` normalizes the taxonomy §6.3 category minimums; it is a hypothesis
  default, overridable per run. Nothing here hardcodes a final split.
* Multi-step chains stay a minority: generators emit at most 2–3 calls per
  row and the default mix keeps multi-call rows under the 15% cap.

## Validation (admission gates)

Every generated row passes before it is emitted (taxonomy §7, gates 1–3):

* **Gate 1 (protocol):** exactly one envelope per assistant message, every
  envelope closed by `<<<END>>>`; `<<<TOOL_CALL>>>` payloads are JSON with a
  V1 `name` and an `arguments` object; relay headers well-formed with `name`
  matching the pending call and `status` of `ok`/`error`.
* **Gate 2 (tool):** calls use minimal sufficient arguments; identifiers come
  from user text or prior observations.
* **Gate 3 (transitions):** `TOOL_CALL` → matching `TOOL_RESULT` → assistant;
  at most 3 calls per row; no identical call repeated; at most one corrected
  retry.

Additionally, when #26's schemas are present (or always, in tests, via the
mirror fixture), every `<<<TOOL_CALL>>>` payload validates against
`tool_call.schema.json` and every wire observation against
`tool_result.schema.json`.

Gate 4 (execution against fixture databases) is downstream of this tool: the
SQL here is template-generated to be sane, but execution verification happens
when rows are admitted to a real corpus build.

## Known #26 divergence (flagged, not papered over)

#26's three artifacts disagree on tool argument shapes:

* **JSON schemas** (normative wire contract): `schema.inspect{scope}`,
  `clock.now{}`, `sql.execute{sql, max_rows}`, `nedb.provenance{table, key}`.
* **Taxonomy examples**: `schema.inspect{table}`, `clock.now{zone}`,
  `sql.execute{sql}`, `nedb.provenance{table, key:{...}, column?}`.
* **toolproto registry**: `schema.inspect{scope?, table?}`,
  `clock.now{timezone?}`, `sql.execute{sql, limit?}`,
  `nedb.provenance{table, row_id?}`.

This builder follows the **JSON schemas** (the RFC's host-validation
contract, taxonomy Gate 1). The test suite pins the divergence explicitly:
`sql.execute`/`nedb.provenance` turns are asserted to fail
`toolproto.parse()` *only* at argument-name validation, proving envelope
extraction succeeded. Reconciling the three is #26 follow-up work, not this
PR's.

## What this does NOT do

* No training runs, no GPU spend, no weight changes, no publishes, no releases.
* No corpus percentages decided (see framing above).
* Does not modify PR #26's branch or files; it only reads them.
* Does not execute SQL (Gate 4 is downstream).
