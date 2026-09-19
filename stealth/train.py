#!/usr/bin/env python3
"""stealth :: training harness (single A6000, 48 GB)

Designed for one Ampere card. That is not a limitation to work around -- an
A6000 comfortably FULL fine-tunes a ~1B model, which removes the constraint
that caused both regressions on the previous model. LoRA was forced on us by
2 CPU cores; here it is a choice, and the default is off.

    bf16 weights  (1B)          ~2 GB
    AdamW states + fp32 master  ~12 GB
    activations, packed @2048   ~4-8 GB
    ------------------------------------
    comfortably inside 48 GB, so batch size is a throughput decision

THROUGHPUT, in order of how much each one buys:

  1. SEQUENCE PACKING. Text-to-SQL samples are short and wildly uneven -- a
     compact schema with a one-line answer next to a five-table DDL. Padding
     to the longest in a batch wastes most of the tensor. Packing concatenates
     samples up to `--seq-len` with document boundaries respected, so almost
     every token in the batch is a real token.
  2. bf16 + TF32 matmuls. Ampere native; no loss scaling needed, unlike the
     fp16 you are forced into on Volta.
  3. FlashAttention-2 when installed. Ampere-supported, and it is the
     difference between attention being free and attention being the bill.
  4. Multi-worker dataloader with pinned memory, so the GPU never waits on
     tokenisation.

WHAT IS DELIBERATELY NOT AUTOMATIC: the loss is computed on the ASSISTANT
SPAN ONLY. The prompt contains the whole schema, which is often longer than
the answer; training on it teaches the model to regurgitate DDL instead of
writing SQL. Getting this wrong silently produces a model that looks like it
trained fine and cannot answer a question.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import time

import torch
from torch.utils.data import Dataset, DataLoader
from transformers import (AutoTokenizer, AutoModelForCausalLM,
                          get_cosine_schedule_with_warmup)

IGNORE = -100


# ------------------------------------------------------------------ dataset --

def _ids(x):
    """transformers>=5 returns BatchEncoding from apply_chat_template.

    len() of that object is 2, which silently destroys every label and leaves
    an empty dataset. This bug cost a full run on the previous model, so the
    normalisation is explicit and centralised rather than assumed.
    """
    if hasattr(x, "keys"):
        x = x["input_ids"]
    if x and isinstance(x[0], list):
        x = x[0]
    return list(x)


class SQLCorpus(Dataset):
    """Loss on the assistant span only; prompt tokens are masked to -100."""

    def __init__(self, path: str, tok, max_len: int = 2048):
        self.rows = []
        skipped_long = skipped_bad = 0
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            msgs = rec["messages"]
            if msgs[-1]["role"] != "assistant":
                skipped_bad += 1
                continue
            prompt_ids = _ids(tok.apply_chat_template(
                msgs[:-1], tokenize=True, add_generation_prompt=True))
            full_ids = _ids(tok.apply_chat_template(
                msgs, tokenize=True, add_generation_prompt=False))
            if len(full_ids) > max_len:
                skipped_long += 1
                continue
            labels = list(full_ids)
            for i in range(min(len(prompt_ids), len(labels))):
                labels[i] = IGNORE
            if all(l == IGNORE for l in labels):
                skipped_bad += 1
                continue
            self.rows.append((full_ids, labels, rec.get("meta", {})))
        if skipped_long or skipped_bad:
            print(f"  ! skipped {skipped_long} over-length, {skipped_bad} unusable")

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        return self.rows[i]


class Packed(Dataset):
    """Concatenate samples up to seq_len. Near-zero padding waste.

    Document boundaries are NOT crossed by the loss: each sample keeps its own
    -100 mask, so a packed row is several independent supervised spans sharing
    one forward pass. Attention does bleed across documents in this simple
    form, which for short SQL samples is an acceptable trade for the
    throughput -- and it is the standard packing used by most SFT stacks.
    """

    def __init__(self, base: SQLCorpus, seq_len: int, pad_id: int, seed: int = 0):
        self.pad_id = pad_id
        order = list(range(len(base)))
        random.Random(seed).shuffle(order)
        self.packs: list[tuple[list[int], list[int]]] = []
        cur_i: list[int] = []
        cur_l: list[int] = []
        for idx in order:
            ids, labels, _ = base[idx]
            if len(cur_i) + len(ids) > seq_len and cur_i:
                self.packs.append((cur_i, cur_l))
                cur_i, cur_l = [], []
            cur_i.extend(ids)
            cur_l.extend(labels)
        if cur_i:
            self.packs.append((cur_i, cur_l))
        tot = sum(len(p[0]) for p in self.packs)
        cap = len(self.packs) * seq_len
        self.efficiency = tot / max(cap, 1)

    def __len__(self):
        return len(self.packs)

    def __getitem__(self, i):
        return self.packs[i]


def collate(batch, pad_id: int):
    mx = max(len(b[0]) for b in batch)
    ids, lab, att = [], [], []
    for i, l in batch:
        p = mx - len(i)
        ids.append(i + [pad_id] * p)
        lab.append(l + [IGNORE] * p)
        att.append([1] * len(i) + [0] * p)
    return (torch.tensor(ids, dtype=torch.long),
            torch.tensor(lab, dtype=torch.long),
            torch.tensor(att, dtype=torch.long))


# -------------------------------------------------------------------- train --

def main() -> int:
    ap = argparse.ArgumentParser(description="stealth trainer (A6000)")
    ap.add_argument("--model", required=True, help="HF id or local path of the base")
    ap.add_argument("--corpus", default="corpus/train.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--batch", type=int, default=8, help="packed rows per step")
    ap.add_argument("--accum", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-5, help="full FT wants a small LR")
    ap.add_argument("--seq-len", type=int, default=2048)
    ap.add_argument("--max-len", type=int, default=2048, help="drop samples longer than this")
    ap.add_argument("--warmup", type=float, default=0.03)
    ap.add_argument("--save-every", type=int, default=100,
                    help="steps. An OOM with epoch-only checkpoints once cost a whole run.")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--no-pack", action="store_true")
    ap.add_argument("--grad-ckpt", action="store_true",
                    help="not needed at 1B on 48GB; here for bigger students")
    ap.add_argument("--lora", type=int, default=0,
                    help="rank>0 switches to LoRA. Default 0 = FULL fine-tune, "
                         "because on 48GB you can afford it and LoRA blast "
                         "radius caused real regressions on the previous model.")
    ap.add_argument("--attn", default="auto",
                    choices=["auto", "flash_attention_2", "sdpa", "eager"])
    a = ap.parse_args()

    torch.manual_seed(a.seed)
    random.seed(a.seed)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if dev == "cuda":
        name = torch.cuda.get_device_name(0)
        cap = torch.cuda.get_device_capability(0)
        vram = torch.cuda.get_device_properties(0).total_memory / 1e9
        print(f"* {name} | sm_{cap[0]}{cap[1]} | {vram:.1f} GB")
        bf16_ok = torch.cuda.is_bf16_supported()
    else:
        print("* NO GPU VISIBLE — this harness is written for an A6000. "
              "Running on CPU will work but will be painfully slow.")
        bf16_ok = False
    dtype = torch.bfloat16 if bf16_ok else torch.float32
    print(f"* dtype {dtype}")

    attn = a.attn
    if attn == "auto":
        attn = "sdpa"
        try:
            import flash_attn  # noqa: F401
            if dev == "cuda" and torch.cuda.get_device_capability(0)[0] >= 8:
                attn = "flash_attention_2"
        except Exception:
            pass
    print(f"* attention: {attn}")

    tok = AutoTokenizer.from_pretrained(a.model)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token

    print(f"* loading {a.model}")
    model = AutoModelForCausalLM.from_pretrained(
        a.model, dtype=dtype, attn_implementation=attn, low_cpu_mem_usage=True)
    model.config.use_cache = False
    model.to(dev)

    if a.lora > 0:
        from peft import LoraConfig, get_peft_model
        model = get_peft_model(model, LoraConfig(
            r=a.lora, lora_alpha=a.lora * 2, lora_dropout=0.05, bias="none",
            task_type="CAUSAL_LM",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                            "gate_proj", "up_proj", "down_proj"]))
        model.print_trainable_parameters()
    else:
        n = sum(p.numel() for p in model.parameters())
        print(f"* FULL fine-tune, {n/1e9:.2f}B parameters trainable")

    if a.grad_ckpt:
        model.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False})
        print("* gradient checkpointing ON")

    print(f"* corpus {a.corpus}")
    base = SQLCorpus(a.corpus, tok, max_len=a.max_len)
    print(f"  {len(base)} samples")
    if not len(base):
        print("FATAL: empty dataset — check the corpus and the chat template")
        return 2

    if a.no_pack:
        ds = base
        coll = lambda b: collate([(i, l) for i, l, _ in b], tok.pad_token_id)  # noqa: E731
    else:
        ds = Packed(base, a.seq_len, tok.pad_token_id, seed=a.seed)
        coll = lambda b: collate(b, tok.pad_token_id)                          # noqa: E731
        print(f"  packed into {len(ds)} rows of {a.seq_len} "
              f"({ds.efficiency:.1%} token efficiency)")

    dl = DataLoader(ds, batch_size=a.batch, shuffle=True, collate_fn=coll,
                    num_workers=a.workers, pin_memory=(dev == "cuda"),
                    drop_last=False, persistent_workers=a.workers > 0)

    steps_per_epoch = math.ceil(len(dl) / a.accum)
    total = max(1, int(steps_per_epoch * a.epochs))
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=a.lr, weight_decay=0.01, betas=(0.9, 0.95))
    sched = get_cosine_schedule_with_warmup(opt, int(a.warmup * total), total)
    print(f"* {total} optimizer steps "
          f"(effective batch {a.batch * a.accum} packed rows)")

    os.makedirs(a.out, exist_ok=True)
    json.dump(vars(a), open(os.path.join(a.out, "args.json"), "w"), indent=2)

    model.train()
    step = 0
    log: list[dict] = []
    t0 = time.perf_counter()
    tok_seen = 0
    done = False

    for ep in range(math.ceil(a.epochs)):
        if done:
            break
        run, nb = 0.0, 0
        for i, (ids, lab, att) in enumerate(dl):
            ids, lab, att = ids.to(dev, non_blocking=True), lab.to(dev, non_blocking=True), att.to(dev, non_blocking=True)
            out = model(input_ids=ids, attention_mask=att, labels=lab)
            (out.loss / a.accum).backward()
            run += out.loss.item()
            nb += 1
            tok_seen += int(att.sum().item())

            if (i + 1) % a.accum == 0:
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                opt.step()
                sched.step()
                opt.zero_grad(set_to_none=True)
                step += 1
                el = time.perf_counter() - t0
                loss = run / max(nb, 1)
                if step % 5 == 0 or step == 1:
                    mem = (torch.cuda.max_memory_allocated() / 1e9) if dev == "cuda" else 0
                    print(f"  step {step}/{total}  loss {loss:.4f}  "
                          f"{tok_seen/el:,.0f} tok/s  {el:.0f}s  peak {mem:.1f}GB",
                          flush=True)
                log.append({"step": step, "epoch": ep + 1, "loss": loss,
                            "tokens": tok_seen, "seconds": round(el, 1)})
                run, nb = 0.0, 0

                if a.save_every and step % a.save_every == 0:
                    ck = os.path.join(a.out, f"step{step}")
                    model.save_pretrained(ck)
                    tok.save_pretrained(ck)
                    json.dump(log, open(os.path.join(a.out, "log.json"), "w"), indent=2)
                    print(f"    checkpoint -> {ck}", flush=True)

                if step >= total:
                    done = True
                    break

    final = os.path.join(a.out, "final")
    model.save_pretrained(final)
    tok.save_pretrained(final)
    json.dump(log, open(os.path.join(a.out, "log.json"), "w"), indent=2)
    el = time.perf_counter() - t0
    print(f"* done in {el/60:.1f} min · {tok_seen:,} tokens · "
          f"{tok_seen/el:,.0f} tok/s -> {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
