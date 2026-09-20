#!/usr/bin/env python3
"""stealth :: evaluation — execution accuracy, not string similarity

The only number that matters: given a schema and a question, does the model's
SQL return THE SAME ROWS as the reference, on a real database?

Everything else is a proxy. Exact-match on SQL text punishes correct answers
written differently and rewards wrong answers written similarly, which is
precisely backwards.

Reported metrics, weakest to strongest:

    parseable        L0 — it is PostgreSQL at all
    executable       L3 — it binds to the schema and runs
    EXECUTION ACC    L4 — it returns the right answer          <-- the metric
    heldout EX ACC   L4 on schemas never seen in training      <-- the real one

The last line is what separates a model that learned to READ a schema from one
that memorised ours. The previous model (cast) scored 92% on its own domains
and fell apart the moment a column was renamed; `--heldout` exists so that
cannot happen quietly again.

Also tracked, because they are failure modes with teeth:

    invented_relation   named a table that does not exist
    invented_column     named a column that does not exist
    refusal             emitted UNANSWERABLE/CLARIFY
    unparseable_block   no single well-formed sentinel block
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stealth.forge import build_prompt, materialise, _swap_db      # noqa: E402
from stealth.gate import Gate, Level                               # noqa: E402
from stealth.schemas import CATALOG, HELDOUT_KEYS, TRAIN_KEYS, get  # noqa: E402
from stealth.sentinel import extract_one                            # noqa: E402

_NO_REL = re.compile(r'relation "([^"]+)" does not exist', re.I)
_NO_COL = re.compile(r'column "?([^"\s]+)"? does not exist', re.I)


def load_eval_set(path: str) -> list[dict]:
    """Eval rows: {schema_key, question, reference_sql}."""
    return [json.loads(l) for l in open(path) if l.strip()]


def eval_rows(rows, generate, admin_dsn: str, style: str = "ddl",
              verbose: bool = True) -> dict:
    """`generate(messages) -> str` is the only model-shaped thing here.

    Deliberately a callable, so the same evaluator works against a local
    transformers model, a llama.cpp server, or a hosted endpoint without
    the metric changing underneath us.
    """
    gates: dict[str, Gate] = {}
    tally = {"n": 0, "parseable": 0, "executable": 0, "correct": 0,
             "refusal": 0, "unparseable_block": 0,
             "invented_relation": 0, "invented_column": 0, "wrong_answer": 0}
    per_schema: dict[str, dict[str, int]] = {}
    details = []

    for row in rows:
        key = row["schema_key"]
        if key not in gates:
            g = Gate(_swap_db(admin_dsn, f"stealth_{key}"))
            g.connect()
            gates[key] = g
        gate = gates[key]
        ps = per_schema.setdefault(key, {"n": 0, "correct": 0})

        tally["n"] += 1
        ps["n"] += 1

        t0 = time.perf_counter()
        text = generate(build_prompt(get(key), row["question"], style))
        gen_ms = (time.perf_counter() - t0) * 1000

        blk = extract_one(text)
        if blk is None:
            tally["unparseable_block"] += 1
            details.append({**row, "verdict": "unparseable_block",
                            "output": text[:300], "gen_ms": round(gen_ms)})
            continue
        if blk.kind != "SQL":
            tally["refusal"] += 1
            details.append({**row, "verdict": f"refusal:{blk.kind}",
                            "output": blk.payload[:200], "gen_ms": round(gen_ms)})
            continue

        r = gate.run(blk.payload, reference_sql=row["reference_sql"])
        if r.level >= Level.PARSE:
            tally["parseable"] += 1
        if r.level >= Level.EXECUTE:
            tally["executable"] += 1

        if r.ok and r.level == Level.AGREE:
            tally["correct"] += 1
            ps["correct"] += 1
            verdict = "correct"
        else:
            if _NO_REL.search(r.reason):
                tally["invented_relation"] += 1
                verdict = "invented_relation"
            elif _NO_COL.search(r.reason):
                tally["invented_column"] += 1
                verdict = "invented_column"
            elif r.level >= Level.EXECUTE:
                tally["wrong_answer"] += 1
                verdict = "wrong_answer"
            else:
                verdict = f"stop@{r.level.name}"
        details.append({**row, "verdict": verdict, "sql": blk.payload,
                        "reason": r.reason, "gen_ms": round(gen_ms)})
        if verbose:
            mark = "ok  " if verdict == "correct" else "FAIL"
            print(f"  [{mark}] {key:10} {verdict:20} {row['question'][:52]}")

    for g in gates.values():
        g.close()

    n = max(tally["n"], 1)
    tally["execution_accuracy"] = round(tally["correct"] / n, 4)
    tally["parse_rate"] = round(tally["parseable"] / n, 4)
    tally["execute_rate"] = round(tally["executable"] / n, 4)
    tally["per_schema"] = {
        k: {**v, "acc": round(v["correct"] / max(v["n"], 1), 4)}
        for k, v in per_schema.items()
    }
    return {"tally": tally, "details": details}


# ------------------------------------------------------------------ runners --

def hf_generator(model_path: str, max_new: int = 256, device: str = "auto"):
    """Load a Hugging Face causal LM and return generate(messages) -> text.

    Ordinary HF instruct models use their tokenizer chat template. Devstral
    checkpoints intentionally use Mistral's Tekken tokenizer through
    mistral-common instead, so they get a dedicated, upstream-compatible path.

    On CUDA, device_map="auto" lets Accelerate shard large checkpoints across
    all visible GPUs instead of forcing the whole model onto cuda:0.
    """
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM

    want_cuda = torch.cuda.is_available() and device in ("auto", "cuda")
    dtype = torch.bfloat16 if (want_cuda and torch.cuda.is_bf16_supported()) else torch.float32
    load_kwargs = {"dtype": dtype, "low_cpu_mem_usage": True}
    if device == "auto" and torch.cuda.is_available():
        load_kwargs["device_map"] = "auto"

    m = AutoModelForCausalLM.from_pretrained(model_path, **load_kwargs).eval()
    if not (device == "auto" and torch.cuda.is_available()):
        dev = ("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device
        m = m.to(dev)

    # With a sharded model, inputs belong on the device holding the embedding
    # layer. For ordinary single-device models this resolves to that same device.
    input_device = m.get_input_embeddings().weight.device

    if "devstral" in model_path.lower():
        try:
            from mistral_common.protocol.instruct.messages import SystemMessage, UserMessage
            from mistral_common.protocol.instruct.request import ChatCompletionRequest
            from mistral_common.tokens.tokenizers.mistral import MistralTokenizer
        except ImportError as e:
            raise RuntimeError(
                "Devstral requires mistral-common>=1.7.0. "
                "Install/update requirements.txt before evaluating."
            ) from e

        tok = MistralTokenizer.from_hf_hub(model_path)

        def gen(messages):
            converted = []
            for msg in messages:
                role = msg["role"]
                if role == "system":
                    converted.append(SystemMessage(content=msg["content"]))
                elif role == "user":
                    converted.append(UserMessage(content=msg["content"]))
                else:
                    raise ValueError(f"unsupported Devstral prompt role: {role!r}")

            encoded = tok.encode_chat_completion(
                ChatCompletionRequest(messages=converted)
            )
            input_ids = torch.tensor(
                [encoded.tokens], dtype=torch.long, device=input_device
            )
            with torch.no_grad():
                out = m.generate(
                    input_ids=input_ids,
                    max_new_tokens=max_new,
                    do_sample=False,
                )
            return tok.decode(out[0][input_ids.shape[1]:].tolist())

        return gen

    tok = AutoTokenizer.from_pretrained(model_path, fix_mistral_regex=True)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token

    def gen(messages):
        if not tok.chat_template:
            raise ValueError(
                f"{model_path!r} does not expose tokenizer.chat_template. "
                "Use a model-specific tokenizer path or --url with an "
                "OpenAI-compatible server."
            )
        enc = tok.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        ).to(input_device)
        with torch.no_grad():
            out = m.generate(
                **enc,
                max_new_tokens=max_new,
                do_sample=False,
                pad_token_id=tok.pad_token_id or tok.eos_token_id,
            )
        return tok.decode(
            out[0][enc["input_ids"].shape[1]:],
            skip_special_tokens=True,
        )

    return gen


def openai_generator(url: str, model: str = "local", max_new: int = 256,
                     api_key: str | None = None):
    """Works against llama.cpp's server, vLLM, or anything OpenAI-shaped."""
    import urllib.request

    def gen(messages):
        payload = {"model": model, "messages": messages, "temperature": 0,
                   "max_tokens": max_new, "stream": False}
        hdr = {"Content-Type": "application/json"}
        if api_key:
            hdr["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                     data=json.dumps(payload).encode(), headers=hdr)
        with urllib.request.urlopen(req, timeout=600) as r:
            d = json.loads(r.read().decode())
        return d["choices"][0]["message"].get("content") or ""
    return gen


def main() -> int:
    ap = argparse.ArgumentParser(description="stealth execution-accuracy eval")
    ap.add_argument("--eval-set", default="corpus/eval.jsonl")
    ap.add_argument("--dsn", default=os.environ.get(
        "STEALTH_ADMIN_DSN",
        "host=/agent/workspace/pgrun user=stealth dbname=postgres"))
    ap.add_argument("--model", help="local HF path")
    ap.add_argument("--url", help="OpenAI-compatible endpoint instead of --model")
    ap.add_argument("--served-model", default="local")
    ap.add_argument("--out", default="eval_results.json")
    ap.add_argument("--style", default="ddl", choices=["ddl", "compact"])
    ap.add_argument("--materialise", action="store_true",
                    help="(re)create the databases the eval set needs")
    a = ap.parse_args()

    rows = load_eval_set(a.eval_set)
    keys = sorted({r["schema_key"] for r in rows})
    print(f"* {len(rows)} eval rows across {keys}")
    heldout = [k for k in keys if k in HELDOUT_KEYS]
    if heldout:
        print(f"* HELD-OUT schemas present: {heldout} — this is the number that matters")

    if a.materialise:
        for k in keys:
            materialise(CATALOG[k], a.dsn)
            print(f"  materialised {k}")

    if a.url:
        gen = openai_generator(a.url, a.served_model)
    elif a.model:
        gen = hf_generator(a.model)
    else:
        print("need --model or --url")
        return 2

    res = eval_rows(rows, gen, a.dsn, style=a.style)
    t = res["tally"]
    json.dump(res, open(a.out, "w"), indent=2)

    print(f"\n  parse rate          {t['parse_rate']:.1%}")
    print(f"  execute rate        {t['execute_rate']:.1%}")
    print(f"  EXECUTION ACCURACY  {t['execution_accuracy']:.1%}   <-- the metric")
    print(f"  wrong answer        {t['wrong_answer']}")
    print(f"  invented relation   {t['invented_relation']}")
    print(f"  invented column     {t['invented_column']}")
    print(f"  refusals            {t['refusal']}")
    print(f"  unparseable block   {t['unparseable_block']}")
    print("\n  per schema:")
    for k, v in sorted(t["per_schema"].items()):
        flag = "  <- HELD OUT" if k in HELDOUT_KEYS else ""
        print(f"    {k:12} {v['correct']}/{v['n']}  {v['acc']:.1%}{flag}")
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
