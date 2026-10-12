# Imagine v12 Training Prep

## Corpus mix (Mark's spec)

| Component | Share | Source |
|---|---|---|
| Identity | 33% | `identity-pairs.jsonl` (1,600 pairs, pre-built) |
| Protocol | 20% | Generated via `scripts/make_protocol.py` from SQL corpus |
| SQL | 47% | v11 forge output (`corpus/sql_train.jsonl`) |

## Identity corpus

External input specified as **1,600 pairs**. Validate the transferred file before training.
The planned category counts below total **1,700**, so they are a planning
breakdown rather than verified counts of that file:
- Paraphrases (who are you / who made you / what are you): 400
- Indirect identity questions: 200
- Lineage traps (DeepSeek/GPT/Claude/Llama confusion): 200
- Jailbreak/roleplay resistance: 200
- Conflicting context: 150
- Capability questions: 150
- Version questions: 100
- Multilingual/noisy: 100
- SQL regression safety: 50
- Boundary ("I don't know"): 50

All responses follow the identity spec: short, Interchained attribution,
honest DeepSeek-Coder lineage, no invented details, firm on jailbreaks.

## Training box steps

```bash
# 1. Clone and checkout
git clone https://github.com/aiassistsecure/sukuna-imagine.git
cd sukuna-imagine
git checkout main

# 2. Bootstrap (H200/B200)
bash scripts/setup_h200_or_b200.sh

# 3. Generate SQL corpus (v11 forge)
python3 scripts/forge_teacher.py --out corpus/sql_train.jsonl

# 4. Generate protocol corpus (20%)
python3 scripts/make_protocol.py \
    --source corpus/sql_train.jsonl \
    --out corpus/protocol_train.jsonl

# 5. Copy in identity corpus (33%)
# (transfer identity-pairs.jsonl to the box first)
cp /path/to/identity-pairs.jsonl corpus/identity_train.jsonl

# 6. Merge at 33/20/47
python3 scripts/merge_v12_corpus.py \
    --identity corpus/identity_train.jsonl \
    --protocol corpus/protocol_train.jsonl \
    --sql corpus/sql_train.jsonl \
    --out corpus/v12_train.jsonl

# 7. Train (two-stage, same as v11)
# Stage 1: full fine-tune smoke
BASE=models/imagine-v11/final OUT=models/imagine-v12-smoke RESET=1 \
    CORPUS=corpus/v12_train.jsonl LORA=0 bash scripts/train_imagine.sh
# Stage 2: continue from smoke into a separate final output; trainer folds LoRA
BASE=models/imagine-v12-smoke/final OUT=models/imagine-v12 RESET=1 \
    CORPUS=corpus/v12_train.jsonl LORA=16 bash scripts/train_imagine.sh

# 8. Evaluate
# Identity regression
python3 scripts/evaluate_identity.py --model models/imagine-v12/final \
    --no-system --out eval_identity_v12_unprompted.json
python3 scripts/evaluate_identity.py --model models/imagine-v12/final \
    --out eval_identity_v12_prompted.json
# SQL held-out (must stay at 100%)
python3 -m stealth.evaluate --model models/imagine-v12/final --materialise
```

## Notes

- Base model: `imagine-v11/final` (NOT deepseek-coder — v12 builds on v11)
- Identity answers must work with NO system prompt (baked into weights)
- SQL eval must stay at 100% (40/40) — identity training must not regress the core skill
- Publish path: same as v11 (GGUF quants via Hearth, push to HF)

## Mix enforcement and preflight

The default merger now enforces **row shares** of 33/20/47 (not target-token
shares). It chooses the smallest total divisible by 100 that retains every
input row in its component quota. Smaller pools repeat in shuffled complete
cycles. The seed is reproducible; `--total` explicitly sets a different positive
multiple of 100 and may omit rows. Review the `.mix.json` report for repetitions,
omissions, per-kind counts, and identity rows without a system prompt.

Input records must contain nonempty chat messages ending in an assistant target.
The merger validates structure, not SQL execution correctness or identity facts.
The externally supplied identity corpus must include system-free examples if
unprompted identity is a goal; do not infer learned identity from prompted scores.
Review imported targets for stale read-only instructions. The current protocol
generator and identity evaluator support requested reads and writes; older
identity generators remain legacy sources and require review before reuse.

Protocol generation reuses SQL targets rather than inventing statements. It
accepts exactly one bare SQL sentinel block and excludes fenced, nested, empty,
or trailing-prose targets. It does not parse or execute SQL; retain the existing
execution gate when admitting source SQL.

The training wrapper resumes from OUT/final if it exists. The commands above
use RESET=1 to explicitly select BASE for each stage and distinct output paths;
RESET=1 selects a checkpoint, it does not delete files. Use fresh output paths
for another attempt if existing checkpoints need to be preserved.

Before publishing, run the zero-match LEFT JOIN regression, schema-missing
refusal cases, sentinel compliance, and requested INSERT/UPDATE/DELETE cases.
Compare the same held-out prompts across the source HF checkpoint, merged HF
checkpoint, and final GGUF using the checkpoint's actual training template and
system contract. Keep held-out questions out of all three corpus pools. A
successful merge alone is not model validation; the 40/40 SQL score is an
acceptance target that must be measured again, not assumed.
