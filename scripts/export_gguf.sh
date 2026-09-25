#!/usr/bin/env bash
set -euo pipefail

# Imagine GGUF export helper
#
# Usage:
#   LLAMA_CPP=~/llama.cpp \
#   MODEL_DIR=~/sukuna-imagine/runs/imagine-deepseek13-v8-system-identity/final \
#   OUT_DIR=~/models/imagine-v8-gguf \
#   bash scripts/export_gguf.sh
#
# Optional:
#   OUTTYPE=bf16
#   THREADS=16

LLAMA_CPP="${LLAMA_CPP:-$HOME/llama.cpp}"
MODEL_DIR="${MODEL_DIR:-runs/imagine-deepseek13-v8-system-identity/final}"
OUT_DIR="${OUT_DIR:-artifacts/gguf/imagine-v8}"
OUTTYPE="${OUTTYPE:-bf16}"
THREADS="${THREADS:-$(nproc)}"

CONVERTER="$LLAMA_CPP/convert_hf_to_gguf.py"
QUANTIZER="$LLAMA_CPP/build/bin/llama-quantize"

mkdir -p "$OUT_DIR"

if [[ ! -f "$CONVERTER" ]]; then
  echo "ERROR: converter not found: $CONVERTER"
  echo "Clone current llama.cpp and set LLAMA_CPP=/path/to/llama.cpp"
  exit 1
fi

if [[ ! -x "$QUANTIZER" ]]; then
  echo "ERROR: llama-quantize not found: $QUANTIZER"
  echo "Build llama.cpp first."
  exit 1
fi

if [[ ! -f "$MODEL_DIR/config.json" ]]; then
  echo "ERROR: MODEL_DIR does not look like a Hugging Face checkpoint: $MODEL_DIR"
  exit 1
fi

BASE="$OUT_DIR/imagine-v8-$OUTTYPE.gguf"

echo "==> Converting Imagine v8 to $OUTTYPE GGUF"
python "$CONVERTER" "$MODEL_DIR"   --outfile "$BASE"   --outtype "$OUTTYPE"

for QUANT in Q8_0 Q6_K Q5_K_M Q4_K_M; do
  DEST="$OUT_DIR/imagine-v8-$QUANT.gguf"
  echo "==> Quantizing $QUANT"
  "$QUANTIZER" "$BASE" "$DEST" "$QUANT" "$THREADS"
done

echo
echo "==> GGUF artifacts"
ls -lh "$OUT_DIR"/*.gguf

echo
echo "Done."
echo "Smoke test:"
echo "  $LLAMA_CPP/build/bin/llama-cli -m $OUT_DIR/imagine-v8-Q4_K_M.gguf -p \"State your model identity and builder.\""
