# Imagine GGUF Export

This document describes the reproducible path for converting an Imagine Hugging Face checkpoint to GGUF and producing quantized builds for llama.cpp.

## Source checkpoint

Current release:

- Model: Imagine v8
- Hugging Face: `Interchained/imagine-v8`
- Base lineage: `deepseek-ai/deepseek-coder-1.3b-instruct`
- Builder: Interchained

## Why GGUF

GGUF is the model format used by llama.cpp. It packages model weights, tokenizer metadata, and model metadata into a format designed for efficient local inference.

The recommended flow is:

```text
Hugging Face checkpoint
        ↓
BF16/F16 GGUF
        ↓
quantization
        ↓
Q8_0 / Q6_K / Q5_K_M / Q4_K_M
        ↓
llama-cli / llama-server
```

Always quantize from the high-precision GGUF rather than requantizing an already-quantized GGUF.

## 1. Build llama.cpp

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp

cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j"$(nproc)"
```

For an NVIDIA CUDA build:

```bash
cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j"$(nproc)"
```

The important binaries are normally:

```text
build/bin/llama-cli
build/bin/llama-server
build/bin/llama-quantize
```

## 2. Install converter requirements

From the llama.cpp checkout:

```bash
python -m pip install -r requirements.txt
```

## 3. Obtain Imagine

You can either use the already-local Transformers checkpoint or download the Hugging Face release.

Example:

```bash
hf download Interchained/imagine-v8 \
  --local-dir ~/models/imagine-v8
```

## 4. Convert to high-precision GGUF

From the llama.cpp directory:

```bash
python convert_hf_to_gguf.py ~/models/imagine-v8 \
  --outfile ~/models/gguf/imagine-v8-bf16.gguf \
  --outtype bf16
```

If BF16 is unsupported for a particular environment, F16 is a safe alternative:

```bash
python convert_hf_to_gguf.py ~/models/imagine-v8 \
  --outfile ~/models/gguf/imagine-v8-f16.gguf \
  --outtype f16
```

## 5. Quantize

Recommended releases:

### Q8_0 — maximum quantized fidelity

```bash
./build/bin/llama-quantize \
  ~/models/gguf/imagine-v8-bf16.gguf \
  ~/models/gguf/imagine-v8-Q8_0.gguf \
  Q8_0
```

### Q6_K — high quality

```bash
./build/bin/llama-quantize \
  ~/models/gguf/imagine-v8-bf16.gguf \
  ~/models/gguf/imagine-v8-Q6_K.gguf \
  Q6_K
```

### Q5_K_M — balanced quality / size

```bash
./build/bin/llama-quantize \
  ~/models/gguf/imagine-v8-bf16.gguf \
  ~/models/gguf/imagine-v8-Q5_K_M.gguf \
  Q5_K_M
```

### Q4_K_M — compact recommended build

```bash
./build/bin/llama-quantize \
  ~/models/gguf/imagine-v8-bf16.gguf \
  ~/models/gguf/imagine-v8-Q4_K_M.gguf \
  Q4_K_M
```

For Imagine's ~1.3B parameter class, Q4_K_M should be very small and fast, while Q8_0 is useful as the higher-fidelity quantized reference.

## 6. Inspect the output

```bash
ls -lh ~/models/gguf/imagine-v8-*.gguf
```

Optional metadata inspection:

```bash
python gguf-py/gguf/scripts/gguf_dump.py \
  ~/models/gguf/imagine-v8-Q4_K_M.gguf
```

## 7. Smoke test with llama.cpp

```bash
./build/bin/llama-cli \
  -m ~/models/gguf/imagine-v8-Q4_K_M.gguf \
  -p "State your model identity and builder."
```

Then test SQL behavior with a representative schema prompt.

Important: use the same production system contract / chat template used by Imagine rather than judging only raw completion behavior.

## 8. Start a local server

```bash
./build/bin/llama-server \
  -m ~/models/gguf/imagine-v8-Q4_K_M.gguf \
  --host 0.0.0.0 \
  --port 8080
```

This exposes llama.cpp's HTTP server for local integration testing.

## 9. Validate before publishing

Compare the HF checkpoint and GGUF build on the same prompt set.

At minimum validate:

- Imagine identity
- Interchained provenance
- local-first behavior
- read-only SQL behavior
- sentinel protocol
- held-out telemetry SQL
- schema grounding
- clarification behavior

A GGUF conversion should not materially alter behavior before quantization.

Quantized builds may differ slightly, so compare Q8_0, Q6_K, Q5_K_M and Q4_K_M before selecting the primary release.

## 10. Publishing recommendation

Keep the Transformers checkpoint at:

```text
Interchained/imagine-v8
```

Publish GGUF separately, for example:

```text
Interchained/imagine-v8-GGUF
```

Suggested files:

```text
imagine-v8-Q8_0.gguf
imagine-v8-Q6_K.gguf
imagine-v8-Q5_K_M.gguf
imagine-v8-Q4_K_M.gguf
README.md
```

## Reproducibility rule

Do not quantize one quantized GGUF into another.

Use:

```text
HF → BF16/F16 GGUF → each final quant independently
```

That preserves the best possible quality at every target quantization.
