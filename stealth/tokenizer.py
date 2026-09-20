"""Tokenizer loader helpers for Imagine.

DeepSeek-Coder v1 ships a ByteLevel-BPE tokenizer.json but declares a
Llama tokenizer class. Transformers v5 can route that through LlamaTokenizer
and replace the ByteLevel pre-tokenizer with Metaspace, which strips
whitespace. Load tokenizer.json through PreTrainedTokenizerFast directly.
"""
from __future__ import annotations

from transformers import PreTrainedTokenizerFast


def load_tokenizer(path: str):
    tok = PreTrainedTokenizerFast.from_pretrained(path)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token

    probe = "SELECT count(*) FROM samples;\n"
    ids = tok.encode(probe, add_special_tokens=False)
    decoded = tok.decode(ids, skip_special_tokens=True)
    if decoded != probe:
        raise RuntimeError(
            "Tokenizer round-trip failed: "
            f"expected {probe!r}, decoded {decoded!r}"
        )
    return tok
