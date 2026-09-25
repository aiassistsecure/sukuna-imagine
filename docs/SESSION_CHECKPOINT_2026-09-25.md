# Imagine session checkpoint — 2026-09-25

## Current release state

- Imagine v8 has been trained and published to Hugging Face.
- Source/training/evaluation/GGUF work has been merged to `main`.
- Merge commit: `1ae7dbfcdded557f22fbceaa5803a47ab63d06a9`
- Hugging Face release: `Interchained/imagine-v8`
- GGUF workflow is documented in:
  - `docs/GGUF_EXPORT.md`
  - `scripts/export_gguf.sh`

## v8 training state

Final checkpoint:

`runs/imagine-deepseek13-v8-system-identity/final`

Identity corpus used:

- 832 rows
- min length: 788 tokens
- avg length: 807 tokens
- max length: 843 tokens

Training completed:

- 208 steps
- 2,685,704 tokens
- ~28,867 tok/s
- peak VRAM 28.6 GB
- epoch mean loss 1.8431
- final loss 1.7930

## Identity behavior observations

v8 showed meaningful improvement in:

- Imagine identity
- Interchained provenance
- identity resistance against DeepSeek/ChatGPT relabeling
- local-first behavior
- schema grounding

The external 6.7B judge remained unreliable and sometimes marked plainly correct answers as wrong or incorrect answers as correct. Actual model responses should remain the primary diagnostic evidence.

## SQL evaluation observations

Prior strict held-out telemetry result:

- parse: 100%
- execute: 100%
- strict reference-result agreement: 15/21 = 71.4%
- invented relation: 0
- invented column: 0
- refusals: 0

Several strict mismatches were caused by reference-shape or projection differences rather than necessarily incorrect SQL semantics.

## Important correction for next development cycle

The long-term Imagine database contract is **NOT read-only**.

The read-only rule was an intermediate design assumption and became overrepresented in the system prompt and curriculum.

The intended target is a model that can **read and write databases**, including appropriate:

- SELECT
- INSERT
- UPDATE
- DELETE
- schema changes / DDL where explicitly requested and appropriate

Next work should redesign the SQL contract around **safe, explicit read/write behavior**, not blanket mutation prohibition.

Potential direction:

- distinguish read vs write intent
- require explicit user intent for destructive changes
- clarify materially ambiguous mutations
- keep schema grounding
- avoid invented tables/columns
- preserve execution validation
- add transactional / preview / dry-run patterns where useful
- build read/write curricula and held-out evals
- update SYSTEM prompt accordingly

## Infrastructure

The expensive training instance was terminated intentionally.

Future work can continue from a smaller/cheaper instance because:

- v8 weights are on Hugging Face
- source is on GitHub main
- GGUF workflow is committed
- training/eval lineage is preserved

## Next task

Start from the current main branch and redesign Imagine's database behavior from:

`read-only SQL assistant`

to:

`schema-grounded read/write PostgreSQL model with explicit safety and intent handling`.
