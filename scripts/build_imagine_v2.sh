#!/usr/bin/env bash
# Rebuild Imagine from a clean local base in two isolated stages.
set -euo pipefail

BASE="${BASE:-/sukuna-imagine/models/deepseek-coder-1.3b-instruct}"
PROTOCOL_OUT="${PROTOCOL_OUT:-runs/imagine-protocol-v1}"
FINAL_OUT="${FINAL_OUT:-runs/imagine-v2}"

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

echo "== 1/4 audit verified corpus =="
grep -nE 'Ġ|Ċ|▁' corpus/sql_train.jsonl && {
  echo "tokenizer artifacts found in sql_train.jsonl; aborting" >&2
  exit 4
} || true

echo "== 2/4 build protocol curriculum =="
python scripts/make_protocol.py \
  --source corpus/sql_train.jsonl \
  --out corpus/protocol_train.jsonl \
  --copies "${PROTOCOL_COPIES:-8}"

echo "== 3/4 teach exact Imagine protocol =="
BASE="$BASE" \
OUT="$PROTOCOL_OUT" \
LR="${PROTOCOL_LR:-2e-5}" \
EPOCHS="${PROTOCOL_EPOCHS:-1}" \
bash scripts/train_protocol.sh

echo "== 4/4 teach verified SQL + identity conservatively =="
python scripts/merge_corpus.py \
  --out corpus/imagine_v2_train.jsonl \
  corpus/sql_train.jsonl corpus/identity.jsonl

python -m stealth.train \
  --model "$PROTOCOL_OUT/final" \
  --corpus corpus/imagine_v2_train.jsonl \
  --out "$FINAL_OUT" \
  --lr "${SQL_LR:-5e-6}" \
  --epochs "${SQL_EPOCHS:-1}" \
  --batch "${SQL_BATCH:-4}" \
  --accum "${SQL_ACCUM:-2}" \
  --seq-len "${SEQ_LEN:-1024}" \
  --max-len "${MAX_LEN:-1024}" \
  --lora "${LORA:-16}" \
  --no-pack

echo "Imagine v2 checkpoint -> $FINAL_OUT/final"
