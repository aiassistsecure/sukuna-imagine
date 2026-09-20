<div align="center">

# 🥷 sukuna-imagine

### A model small enough to run on your laptop. Strict enough to trust with your database.

**Natural language in. Correct PostgreSQL out. ~1B parameters. No GPU. No API key. No metered inference.**

[![gate](https://img.shields.io/badge/execution_gate-39%2F39_live_PostgreSQL-3ecf8e?style=flat-square)](tests/test_gate.py)
[![verifier](https://img.shields.io/badge/verifier-PostgreSQL_16-336791?style=flat-square)](stealth/gate.py)
[![parser](https://img.shields.io/badge/parser-libpg__query-336791?style=flat-square)](https://github.com/pganalyze/libpg_query)
[![runtime](https://img.shields.io/badge/runtime-CPU_only-f5a524?style=flat-square)](#)
[![licence](https://img.shields.io/badge/code-BUSL--1.1_→_Apache--2.0-8957e5?style=flat-square)](LICENSE)

*Part of project `imagine` — Interchained*

</div>

---

## The uncomfortable question nobody asks about text-to-SQL

> **How do you know the training data is right?**

Almost everyone answers: *a big model wrote it, and big models are pretty good.*

That is a vibe, not a verification. And it is why small text-to-SQL models
confidently return a number that is simply **the wrong number** — syntactically
perfect, schema-valid, executed without error, and answering a question nobody asked.

We answer differently. **We run it.**

```
                          ┌─────────────────────────────┐
  candidate SQL  ───────► │  L0  real PostgreSQL parser │  not a regex
                          ├─────────────────────────────┤
                          │  L1  read-only + bounded    │  no writes, no sleeps
                          ├─────────────────────────────┤
                          │  L2  EXPLAIN on live schema │  hallucinations die HERE
                          ├─────────────────────────────┤
                          │  L3  execute, timed, capped │  real rows
                          ├─────────────────────────────┤
                          │  L4  SAME ANSWER as ref?    │  ◄── the one that matters
                          └──────────────┬──────────────┘
                                         ▼
                                  admitted to corpus
```

**Nothing enters the training set that a live database has not agreed with.**

---

## 🎯 The one-character problem

This is the failure mode that makes text-to-SQL quietly dangerous, and the reason L4 exists:

| candidate | reference | text diff | **answer** |
|---|---|---|---|
| `count(*)` | `count(id)` | big | ✅ **same** |
| `JOIN ... WHERE total > 400` | `WHERE id IN (SELECT ...)` | huge | ✅ **same** |
| `WHERE price >= 249` | `WHERE price > 249` | **one character** | ❌ **different** |
| `WHERE status = 'Paid'` | `WHERE status = 'paid'` | **one letter** | ❌ **different** |

Every string-similarity metric gets this exactly backwards. It rewards the queries that
*look* alike and punishes the ones that *are* alike.

**Execution doesn't care what your query looks like.**

---

## 💡 Why a 1B model can actually win here

Because text-to-SQL is one of the rare tasks with a **perfect verifier** sitting right there,
already installed, already correct: the database.

You can't machine-check *"is this a good conversational answer?"*
You absolutely can machine-check *"does this return the same rows?"*

We have run this play before. [**nedb-cast-slm**](https://github.com/aiassistsecure/nedb-cast-slm)
hit **92% exact-plan match at 3.3 million parameters** — trained on 2 vCPUs in 41 minutes —
because the query parser was simultaneously the corpus generator, the grader, *and* the
entry gate. No example got in unless it round-tripped.

> Same play. Bigger grammar. A real database instead of a toy one.

---

## 🧨 The bug that became the feature

Our previous model's headline failure was this: asked to query NEDB, it emitted

```sql
SELECT COUNT(*) FROM orders WHERE status = 'paid';
```

...when the spec demanded our in-house query language. We logged it as a defect. We wrote
tooling to prevent it. We nearly fine-tuned it away.

Then NEDB v5 dropped the in-house language and vendored PostgreSQL's grammar instead —
*"nobody wants to learn a new query language."*

**The model had been right the whole time.** This repo is what happens when you stop
correcting it and start training it.

---

## 📦 The output envelope

The model wraps SQL in [**sentinel blocks**](https://github.com/Eth-Interchained/sentinel-blocks):

```
<<<SQL>>>
SELECT c.city, count(*)
  FROM orders o JOIN customers c ON c.id = o.customer_id
 WHERE o.status = 'paid'
 GROUP BY c.city;
<<<END>>>
```

SQL is *the* payload that justifies the technique — single quotes, double quotes, semicolons,
newlines, dollar-quoting, all at once. Anything that **re-parses** the payload (JSON escaping,
markdown fences, a regex hunting for `SELECT.*`) will eventually mangle a legitimate query.

Sentinels are extracted **by delimiter and never interpreted.** Proven on the nastiest fixture
we could write:

```sql
SELECT 'a;b', "weird col" FROM t WHERE x = 'it''s'   -- round-trips byte-exact
```

**And it can refuse:**

| block | meaning |
|---|---|
| `<<<SQL>>>` | here is your query |
| `<<<UNANSWERABLE>>>` | this schema cannot answer that |
| `<<<CLARIFY>>>` | ambiguous — here's what's missing |

Guessing is the enemy. A model that says *"there is no revenue column"* beats one that
invents `SUM(revenue)` every single time.

> ⚠️ **A truncated generation is not an answer.** An unterminated block — or two blocks —
> extracts to **nothing**. Silently accepting half a query is how you execute half a query.

---

## 🔬 What's proven, right now

```
== sentinel envelope ==                    9/9   ✅
== gate levels (live PostgreSQL 16) ==    24/24  ✅
== L4 result agreement ==                  6/6   ✅
                                          ─────
                                          39/39
```

Real errors from the real planner, not simulated:

```
hallucinated table       → ERROR: relation "invoices" does not exist
hallucinated column      → ERROR: column "customer_name" does not exist
ambiguous reference      → ERROR: column reference "id" is ambiguous
the old DSL sneaking in  → ParseError: syntax error at or near "FROM"
SELECT 1; DROP TABLE ... → rejected: expected exactly 1 statement, got 2
```

**Half the fixtures must REJECT. Half must ACCEPT. The accept half matters more** — a gate
that quietly refuses *correct* SQL starves the corpus of exactly the examples worth learning
from, and nothing downstream would ever tell you.

That discipline is not theoretical. The previous model's harness produced **seven distinct
false-positive classes**, each one flattering us until it was caught. Here it caught a broken
fixture of mine on the very first run — the gate was right and my test was wrong.

---

## 🧬 Imagine v1: teacher → verifier → student

The model that ships is **not** the teacher.

```text
mistralai/Devstral-Small-2507        teacher; proposes harder SQL
                 │
                 ▼
        PostgreSQL L0-L4 gate        truth authority
                 │
                 ▼
   verified SQL + Imagine identity
                 │
                 ▼
deepseek-ai/deepseek-coder-1.3b-instruct    canonical student base
                 │
                 ▼
              Imagine                compact local model
```

The canonical v1 student is **DeepSeek-Coder-1.3B-Instruct**. Devstral is an
optional corpus teacher only: its SQL never enters training unless the live
database agrees with the deterministic reference at L4.

### Build the v1 training corpus

Template-only forging remains the zero-teacher baseline. To add Devstral
proposals:

```bash
python scripts/forge_teacher.py \
  --teacher mistralai/Devstral-Small-2507 \
  --out corpus/sql_train.jsonl \
  --rejects corpus/sql_rejects.jsonl

python scripts/make_identity.py --out corpus/identity.jsonl

python scripts/merge_corpus.py \
  --out corpus/imagine_train.jsonl \
  corpus/sql_train.jsonl corpus/identity.jsonl
```

Identity is deliberately small. It teaches the name **Imagine**, Interchained
provenance, local-first purpose, schema-grounding, read-only behavior, and the
refuse/clarify contract. SQL capability still comes overwhelmingly from the
execution-gated corpus.

### Train the student

```bash
bash scripts/train_imagine.sh
```

Override any component without editing the recipe:

```bash
STUDENT=deepseek-ai/deepseek-coder-1.3b-instruct \
CORPUS=corpus/imagine_train.jsonl \
OUT=runs/imagine-deepseek13-v1 \
bash scripts/train_imagine.sh
```

The first checkpoint is still judged on the held-out schema with
`stealth.evaluate`; training loss is not the product metric.

---

## 🗺️ Status

| component | state |
|---|---|
| 🟢 Execution gate (L0–L4) | **built · 39/39 on live PostgreSQL 16** |
| 🟢 Sentinel envelope | **built · fixture-proven** |
| 🟢 Schema catalog | **5 schemas, adversarial by design** |
| 🟢 Parallel corpus forge | **built · 2,027 candidates/sec on 2 cores · 19/19 poison fixtures** |
| 🟢 Execution-accuracy eval | **built · 4/4 directions verified (oracle 100%, saboteur 0%)** |
| 🟢 Training harness (A6000) | **built · bf16 · packed · FlashAttention-2 · full FT default** |
| 🟢 Canonical student base | **DeepSeek-Coder-1.3B-Instruct** |\n| 🟢 Optional teacher path | **Devstral Small 2507 → L4 execution gate** |\n| 🟡 Imagine v1 training | identity + verified SQL corpus ready to build |

### One command

```bash
BASE=<hf-id-or-path> ./scripts/run_a6000.sh
```

Gate self-test → forge corpus → build held-out eval → **baseline the base model
before training it** → train → re-evaluate. Step 3 is not optional: without a
before-number, an after-number means nothing.

---

## ⚡ Run it yourself

```bash
pip install psycopg2-binary pglast
./scripts/bootstrap_pg.sh          # throwaway PostgreSQL, durability off
python tests/test_gate.py          # expect 39/39
```

No API keys. No cloud. The verifier runs on your machine, because that is the entire point.

---

## 📐 Four rules, each one paid for

**1 · Don't write a verifier — the engine already shipped one.**
Real parser. Real planner. Real rows. Every hand-rolled SQL validator is a bug farm that
disagrees with the database in ways you discover in production.

**2 · Assert the property, not a proxy.**
Every check self-tests in *both* directions before it is trusted. Seven harness lies taught
us this, and all seven were the same mistake: measuring the easy thing instead of the true thing.

**3 · Exhaust the free fix first.**
On the previous model, two fine-tune runs moved the target metric by **zero**. One schema
redesign moved it by **four**. Training is the last resort, not the first instinct.

**4 · Schema goes in the prompt, not in the weights.**
The model must learn *"read the schema you were handed"* — not memorise ours. The catalog is
adversarial on purpose: the same concept under different names (`orders` / `appointments` /
`routes`), the same name meaning different things (`total` is money in one schema and a
package **count** in another), camelCase, quoted `"Mixed Case"`, reserved words like `order`
that must be quoted, nullable columns so `count(col) != count(*)` is real, and a table with
zero rows so "no results" is a trained-for answer.

`telemetry` is **held out entirely.** The gap between trained-schema accuracy and held-out
accuracy *is* the memorisation gap, and it is printed on every eval run.

---

<div align="center">

**Built by [Interchained](https://github.com/Eth-Interchained)** · ownership at every layer, including the model

Code **BUSL-1.1** → Apache-2.0 on 2030-09-19 · open source on a timer, not open source withheld

`3 > 1`

</div>
