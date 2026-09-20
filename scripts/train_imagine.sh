#!/usr/bin/env bash
# Build or continue Imagine v1 from LOCAL checkpoints only.
set -euo pipefail

BASE="${BASE:-/sukuna-imagine/models/deepseek-coder-1.3b-instruct}"
CORPUS="${CORPUS:-corpus/imagine_train.jsonl}"
OUT="${OUT:-runs/imagine-deepseek13-v1}"
RESET="${RESET:-0}"

if [[ "$RESET" == "1" ]]; then
  STUDENT="$BASE"
  MODE="reset-from-base"
elif [[ -d "$OUT/final" ]]; then
  STUDENT="$OUT/final"
  MODE="continue-from-imagine"
else
  STUDENT="$BASE"
  MODE="first-run-from-base"
fi

if [[ ! -d "$STUDENT" ]]; then
  echo "ERROR: local student model not found: $STUDENT" >&2
  echo "Expected staged base at: $BASE" >&2
  exit 2
fi

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

echo "Imagine training mode: $MODE"
echo "Input checkpoint:      $STUDENT"
echo "Output checkpoint:     $OUT/final"
echo "Corpus:                $CORPUS"
echo "Learning rate:         ${LR:-5e-6}"
echo "Epochs:                ${EPOCHS:-1}"
echo "LoRA rank:             ${LORA:-16}"

python -m stealth.train \
  --model "$STUDENT" \
  --corpus "$CORPUS" \
  --out "$OUT" \
  --lr "${LR:-5e-6}" \
  --epochs "${EPOCHS:-1}" \
  --batch "${BATCH:-2}" \
  --accum "${ACCUM:-4}" \
  --seq-len "${SEQ_LEN:-1024}" \
  --max-len "${MAX_LEN:-1024}" \
  --save-every "${SAVE_EVERY:-100}" \
  --lora "${LORA:-16}" \
  --grad-ckpt

echo "Imagine checkpoint -> $OUT/final"
echo "Next run will continue from this checkpoint automatically."
echo "Use RESET=1 bash scripts/train_imagine.sh to restart from the staged base."
