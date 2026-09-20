#!/usr/bin/env bash
# Build the canonical Imagine v1 student from Qwen2.5-Coder-0.5B-Instruct.
set -euo pipefail

STUDENT="${STUDENT:-Qwen/Qwen2.5-Coder-0.5B-Instruct}"
CORPUS="${CORPUS:-corpus/imagine_train.jsonl}"
OUT="${OUT:-runs/imagine-qwen05-v1}"

python -m stealth.train \
  --model "$STUDENT" \
  --corpus "$CORPUS" \
  --out "$OUT" \
  --epochs "${EPOCHS:-3}" \
  --batch "${BATCH:-16}" \
  --accum "${ACCUM:-1}" \
  --seq-len "${SEQ_LEN:-2048}" \
  --save-every "${SAVE_EVERY:-100}"

echo "Imagine checkpoint -> $OUT/final"
