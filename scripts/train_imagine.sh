#!/usr/bin/env bash
# Build or continue Imagine using hardware-aware trainer defaults.
set -euo pipefail

BASE="${BASE:-deepseek-ai/deepseek-coder-1.3b-instruct}"
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

echo "Imagine training mode: $MODE"
echo "Input checkpoint:      $STUDENT"
echo "Output checkpoint:     $OUT/final"
echo "Corpus:                $CORPUS"
echo "Learning rate:         ${LR:-5e-6}"
echo "Epochs:                ${EPOCHS:-1}"
echo "LoRA rank:             ${LORA:-0}"
echo "Hardware tuning:       auto (override with BATCH/ACCUM/WORKERS/GRAD_CKPT)"

ARGS=(
  --model "$STUDENT"
  --corpus "$CORPUS"
  --out "$OUT"
  --lr "${LR:-5e-6}"
  --epochs "${EPOCHS:-1}"
  --seq-len "${SEQ_LEN:-2048}"
  --max-len "${MAX_LEN:-2048}"
  --save-every "${SAVE_EVERY:-100}"
  --lora "${LORA:-0}"
  --attn "${ATTN:-auto}"
)

[[ -n "${BATCH:-}" ]] && ARGS+=(--batch "$BATCH")
[[ -n "${ACCUM:-}" ]] && ARGS+=(--accum "$ACCUM")
[[ -n "${WORKERS:-}" ]] && ARGS+=(--workers "$WORKERS")
case "${GRAD_CKPT:-auto}" in
  1|true|on) ARGS+=(--grad-ckpt) ;;
  0|false|off) ARGS+=(--no-grad-ckpt) ;;
  auto) ;;
  *) echo "ERROR: GRAD_CKPT must be auto, on/off, true/false, or 1/0" >&2; exit 2 ;;
esac

python -m stealth.train "${ARGS[@]}"

echo "Imagine checkpoint -> $OUT/final"
echo "Next run continues from this checkpoint automatically."
echo "Use RESET=1 bash scripts/train_imagine.sh to restart from BASE."
