#!/usr/bin/env bash
# sukuna-imagine one-shot setup for a GPU training box (H200, B200, ...).
# Run:  chmod +x setup_h200.sh && ./setup_h200.sh
# Everything the read/write project needs: repo, venv, deps, NEDB, gate proof.
# The torch wheel is picked dynamically from the box's GPU + CUDA version.
set -euo pipefail
exec > >(tee "$HOME/setup_h200.log") 2>&1

echo "=== 0 · GPU check ==="
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
python3 --version
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo unknown)"
echo "detected GPU: $GPU_NAME"

echo "=== 1 · repo ==="
WORK="$HOME/sukuna-imagine"
if [ ! -d "$WORK/.git" ]; then
  git clone https://github.com/aiassistsecure/sukuna-imagine "$WORK"
else
  git -C "$WORK" pull --ff-only || true
fi
cd "$WORK"

echo "=== 2 · venv (always) ==="
[ -d venv ] || python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip

echo "=== 3 · torch matched to this box (auto-detected) ==="
# Pick the torch CUDA wheel from the driver-reported CUDA version.
# e.g. driver CUDA 12.8 -> cu128, 12.6 -> cu126, 12.4 -> cu124, 12.1 -> cu121.
# Falls back to cu128 (covers Hopper H200 and Blackwell B200) when detection fails.
CUDA_VER="$(nvidia-smi 2>/dev/null | grep -oP 'CUDA Version:\s*\K[0-9]+\.[0-9]+' | head -1 || true)"
case "${CUDA_VER:-}" in
  12.8*) TORCH_CU="cu128" ;;
  12.6*) TORCH_CU="cu126" ;;
  12.4*) TORCH_CU="cu124" ;;
  12.1*) TORCH_CU="cu121" ;;
  11.8*) TORCH_CU="cu118" ;;
  *)
    echo "could not map CUDA version '${CUDA_VER:-unknown}' to a wheel - defaulting to cu128"
    TORCH_CU="cu128" ;;
esac
echo "CUDA ${CUDA_VER:-unknown} -> torch wheel ${TORCH_CU}"
pip install --index-url "https://download.pytorch.org/whl/${TORCH_CU}" torch
echo "=== 3b · remaining deps (torch already satisfied, kept as-is) ==="
pip install -r requirements.txt
pip install "nedb-engine>=11.12.14"   # bundles nedbd-v2, the pgwire endpoint

echo "=== 4 · optional flash-attn (needs nvcc; skipped if no CUDA toolkit) ==="
if command -v nvcc >/dev/null 2>&1; then
  pip install flash-attn --no-build-isolation || echo "flash-attn build failed, continuing without it"
else
  echo "no nvcc on this box - skipping flash-attn (optional; training works fine without it)"
fi

echo "=== 5 · torch/CUDA sanity ==="
python -c "import torch; print('torch', torch.__version__, '| cuda:', torch.cuda.is_available(), '|', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"

echo "=== 6 · NEDB as the database (no postgres) ==="
bash scripts/bootstrap_nedb.sh

echo "=== 7 · prove the gate (expect 68/68) ==="
export STEALTH_DSN="host=127.0.0.1 port=5433 user=stealth dbname=shop"
python tests/test_gate.py || true

echo
echo "=== DONE ==="
echo "workdir : $WORK"
echo "venv    : source $WORK/venv/bin/activate"
echo "nedb    : pgwire on 127.0.0.1:5433 (log: $WORK/nedb.log)"
echo "gate DSN: \$STEALTH_DSN (export it in every new shell)"
echo
echo "Next up for v11:"
echo "  1. Forge v11 corpus (analytical + predicate-placement templates)"
echo "     see docs/v10-analytical-templates.md, docs/v11-predicate-templates.md"
echo "  2. Validate through gate"
echo "  3. Train v11 from Interchained/imagine-v10 (two-stage: full FT smoke, then LoRA r16)"
echo "Base checkpoint: https://huggingface.co/Interchained/imagine-v10"
