#!/usr/bin/env bash
# Build Imagine v1 from a LOCAL DeepSeek-Coder-1.3B-Instruct checkpoint.
set -euo pipefail

STUDENT="${STUDENT:-/sukuna-imagine/models/deepseek-coder-1.3b-instruct}"
CORPUS="${CORPUS:-corpus/imagine_train.jsonl}"
OUT="${OUT:-runs/imagine-deepseek13-v1}"

if [[ ! -d "$STUDENT" ]]; then
  echo "ERROR: local student model not found: $STUDENT" >&2
  echo "Stage deepseek-ai/deepseek-coder-1.3b-instruct there first, then rerun." >&2
  exit 2
fi

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

python -m stealth.train \
  --model "$STUDENT" \
  --corpus "$CORPUS" \
  --out "$OUT" \
  --epochs "${EPOCHS:-4}" \
  --batch "${BATCH:-2}" \
  --accum "${ACCUM:-4}" \
  --seq-len "${SEQ_LEN:-1024}" \
  --max-len "${MAX_LEN:-1024}" \
  --save-every "${SAVE_EVERY:-100}" \
  --grad-ckpt

echo "Imagine checkpoint -> $OUT/final"
