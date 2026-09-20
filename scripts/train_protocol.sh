#!/usr/bin/env bash
# Conservative protocol-only adaptation from the clean local base.
set -euo pipefail

BASE="${BASE:-/sukuna-imagine/models/deepseek-coder-1.3b-instruct}"
CORPUS="${CORPUS:-corpus/protocol_train.jsonl}"
OUT="${OUT:-runs/imagine-protocol-v1}"

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

[[ -d "$BASE" ]] || { echo "missing local base: $BASE" >&2; exit 2; }
[[ -f "$CORPUS" ]] || { echo "missing protocol corpus: $CORPUS" >&2; exit 2; }

python -m stealth.train \
  --model "$BASE" \
  --corpus "$CORPUS" \
  --out "$OUT" \
  --lr "${LR:-2e-5}" \
  --epochs "${EPOCHS:-1}" \
  --batch "${BATCH:-8}" \
  --accum "${ACCUM:-2}" \
  --seq-len "${SEQ_LEN:-1024}" \
  --max-len "${MAX_LEN:-1024}" \
  --lora "${LORA:-16}" \
  --no-pack

echo "Protocol checkpoint -> $OUT/final"
