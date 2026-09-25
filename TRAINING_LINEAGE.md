# Imagine Training Lineage

Current training plan:

- **Teacher:** `deepseek-ai/deepseek-coder-6.7b-instruct`
- **Truth authority:** PostgreSQL L0-L4 execution gate
- **Student/base:** `deepseek-ai/deepseek-coder-1.3b-instruct`
- **Final model:** Imagine

## Teacher forge

```bash
mkdir -p corpus

python scripts/forge_teacher.py \
  --teacher deepseek-ai/deepseek-coder-6.7b-instruct \
  --out corpus/sql_train.jsonl \
  --rejects corpus/sql_rejects.jsonl
```

## Identity curriculum

```bash
python scripts/make_identity.py \
  --out corpus/identity.jsonl
```

## Merge corpus

```bash
python scripts/merge_corpus.py \
  --out corpus/imagine_train.jsonl \
  corpus/sql_train.jsonl \
  corpus/identity.jsonl
```

## Train Imagine

```bash
bash scripts/train_imagine.sh
```

The teacher proposes candidate SQL, but only examples accepted by the PostgreSQL execution gate are admitted to the training corpus.
