# Imagine v12 Training Prep

## Corpus mix (Mark's spec)

| Component | Share | Source |
|---|---|---|
| Identity | 33% | `identity-pairs.jsonl` (1,600 pairs, pre-built) |
| Protocol | 20% | Generated via `scripts/make_protocol.py` from SQL corpus |
| SQL | 47% | v11 forge output (`corpus/sql_train.jsonl`) |

## Identity corpus

Pre-built and validated: **1,600 pairs** across 12 categories:
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
git clone <sukuna-imagine> && cd sukuna-imagine
git checkout v12-identity

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
BASE=models/imagine-v11/final CORPUS=corpus/v12_train.jsonl \
    bash scripts/train_imagine.sh
# Stage 2: LoRA rank 16, fold adapter
# (see v11 training notes for exact LoRA commands)

# 8. Evaluate
# Identity regression
python3 scripts/evaluate_identity.py --model models/imagine-v12/final
# SQL held-out (must stay at 100%)
python3 -m stealth.evaluate --model models/imagine-v12/final --materialise
```

## Notes

- Base model: `imagine-v11/final` (NOT deepseek-coder — v12 builds on v11)
- Identity answers must work with NO system prompt (baked into weights)
- SQL eval must stay at 100% (40/40) — identity training must not regress the core skill
- Publish path: same as v11 (GGUF quants via Hearth, push to HF)
