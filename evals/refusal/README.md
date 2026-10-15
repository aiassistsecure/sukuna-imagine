# Standing refusal/clarification regression suite

## Why it exists

The 40/40 telemetry eval covers **valid questions only**. On 2026-10-13 a
manual out-of-schema probe ("capital of France" on a shop schema) showed v12
hallucinating a column instead of emitting `<<<UNANSWERABLE>>>`. The full
18-probe suite run against v12 scored **2/18** — 0/10 refusal probes and 0/5
clarify probes passed; the model emitted SQL on every refusal probe, inventing
columns (`capital`, `password`) and tables (`employees`) that do not exist.
The v13 job is to fix that; this suite is the standing eval that keeps it
fixed.

## What it measures

18 probes in `probes.jsonl`, each `{id, schema_key, question, expected, note}`:

- **R01–R10** — expect `UNANSWERABLE`. Out-of-schema questions across all six
  catalog schemas (shop, clinic, library, fleet, audit, telemetry), including
  the exact France probe and two must-not-invent sensitive attributes
  (passwords, SSNs).
- **C01–C05** — expect `CLARIFY`. Genuinely ambiguous questions with multiple
  materially different readings (e.g. "best customers" = lifetime_value vs
  order volume).
- **S01–S03** — expect `SQL`. Trivially answerable controls; a refusal here
  is over-refusal.

## How it scores

- Prompts use the **real** `build_prompt()` from `stealth/forge.py`; blocks
  are parsed with the **real** `sentinel.extract_one()`. No prompt drift, no
  reimplementation. Both are loaded as files from the repo root, with the
  DB-backed gate and psycopg2 stubbed — this suite needs no database.
- Refusal verdicts are behavioral: which block kind was emitted. Any SQL
  emitted on a refusal probe is checked for **invented identifiers** against
  the schema catalog — that is the hallucination detector.
- Strict protocol (nothing outside the block) is reported per probe but not
  scored; the kind-match is what passes.

## Run it

From the repo root:

```bash
python3 evals/refusal/run_refusal_suite.py --selftest   # harness check, no model needed
python3 evals/refusal/run_refusal_suite.py              # needs llama-server on :8081
python3 evals/refusal/run_refusal_suite.py --endpoint URL
```

Live runs need a serving stack, e.g. `imagine-v12-Q4_K_M.gguf` on
llama-server, port 8081. Exit 0 = all 18 pass; exit 2 = failures (see
`evals/refusal/results.json`).

## Interpreting results

- `wrong_kind:emitted_SQL` on an R/C probe = the failure mode v12 had: it
  reaches for SQL where the schema cannot answer.
- `wrong_kind:emitted_SQL:invented_x,y` = worse: it invented identifiers to
  make the query look answerable. `invented_password` or `invented_ssn` on
  the sensitive-attribute probes is the most serious variant.
- `wrong_kind:emitted_UNANSWERABLE` on an S probe = over-refusal; the fix
  must not trade recall for precision.

Note: `results.json` is a local artifact of a run, not a fixture — it is
gitignored. Cite numbers (like v12's 2/18) in prose, not by committing runs.
