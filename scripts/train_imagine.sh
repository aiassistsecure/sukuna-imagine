#!/usr/bin/env bash
# Build the canonical Imagine v1 student from DeepSeek-Coder-1.3B-Instruct.
set -euo pipefail

STUDENT="${STUDENT:-deepseek-ai/deepseek-coder-1.3b-instruct}"
CORPUS="${CORPUS:-corpus/imagine_train.jsonl}"
OUT="${OUT:-runs/imagine-deepseek13-v1}"

python -m stealth.train \
  --model "$STUDENT" \
  --corpus "$CORPUS" \
  --out "$OUT" \
  --epochs "${EPOCHS:-4}" \
  --batch "${BATCH:-8}" \
  --accum "${ACCUM:-1}" \
  --seq-len "${SEQ_LEN:-2048}" \
  --save-every "${SAVE_EVERY:-100}"

echo "Imagine checkpoint -> $OUT/final"
