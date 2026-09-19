#!/usr/bin/env bash
# stealth :: end-to-end on one A6000.
#
# Assumes PostgreSQL is reachable (scripts/bootstrap_pg.sh) and that
# STEALTH_ADMIN_DSN points at it.
set -euo pipefail

BASE="${BASE:?set BASE to the HF id or local path of the base model}"
OUT="${OUT:-runs/stealth-v1}"
export STEALTH_ADMIN_DSN="${STEALTH_ADMIN_DSN:-host=$PWD/pgrun user=stealth dbname=postgres}"

echo "=== 0 · prove the gate before trusting anything it produces ==="
python tests/test_gate.py
python tests/test_forge.py

echo "=== 1 · forge the corpus (execution-gated, all cores) ==="
python -m stealth.forge --out corpus/train.jsonl --rejects corpus/rejects.jsonl

echo "=== 2 · build the HELD-OUT eval set ==="
python scripts/make_eval.py --out corpus/eval.jsonl

echo "=== 3 · baseline the BASE model before training it ==="
python -m stealth.evaluate --eval-set corpus/eval.jsonl --model "$BASE" \
    --materialise --out eval_base.json || true

echo "=== 4 · train ==="
python -m stealth.train --model "$BASE" --corpus corpus/train.jsonl --out "$OUT" \
    --epochs 3 --batch 8 --accum 2 --seq-len 2048 --save-every 100

echo "=== 5 · evaluate the result on HELD-OUT schemas ==="
python -m stealth.evaluate --eval-set corpus/eval.jsonl --model "$OUT/final" \
    --out eval_trained.json

echo
echo "baseline  -> eval_base.json"
echo "trained   -> eval_trained.json"
echo "The number that matters is execution accuracy on the HELD-OUT schema."
