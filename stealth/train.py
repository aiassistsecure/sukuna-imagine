#!/usr/bin/env python3
"""stealth :: hardware-aware single-GPU training harness

Designed for one CUDA GPU or CPU fallback. Runtime defaults adapt to visible
VRAM and device capabilities while explicit CLI flags always win. Full fine-
tuning remains the default; LoRA is an explicit choice.

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
import sys
import time

import torch
from torch.utils.data import Dataset, DataLoader
from transformers import (AutoModelForCausalLM,
                          get_cosine_schedule_with_warmup,
                          get_constant_schedule_with_warmup)

from .tokenizer import load_tokenizer

IGNORE = -100

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def _ansi(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _COLOR else text

def _green(s: str) -> str: return _ansi("32;1", s)
def _red(s: str) -> str: return _ansi("31;1", s)
def _yellow(s: str) -> str: return _ansi("33;1", s)
def _cyan(s: str) -> str: return _ansi("36;1", s)
def _magenta(s: str) -> str: return _ansi("35;1", s)
def _dim(s: str) -> str: return _ansi("2", s)


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

    POISON_MARKERS = ("Ġ", "Ċ", "▁")

    def __init__(self, path: str, tok, max_len: int = 2048):
        self.rows = []
        skipped_long = skipped_bad = 0
        poisoned = []
        for lineno, line in enumerate(open(path), 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            msgs = rec["messages"]
            assistant_text = msgs[-1].get("content", "") if msgs else ""
            hits = [m for m in self.POISON_MARKERS if m in assistant_text]
            if hits:
                poisoned.append((lineno, hits, assistant_text[:160]))
                continue
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
        if poisoned:
            print(_red("FATAL: tokenizer-artifact contamination found in training targets"))
            for lineno, hits, preview in poisoned[:10]:
                print(f"  line {lineno}: markers={hits}  {preview!r}")
            print(_red("Refusing to train. Rebuild or clean the corpus first."))
            raise SystemExit(4)
        self.skipped_long = skipped_long
        self.skipped_bad = skipped_bad
        if skipped_long or skipped_bad:
            print(f"  {_yellow('!')} skipped {skipped_long} over-length, {skipped_bad} unusable")

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


class Collator:
    """Picklable DataLoader collator for spawn-based platforms such as Windows."""

    def __init__(self, pad_id: int, packed: bool):
        self.pad_id = pad_id
        self.packed = packed

    def __call__(self, batch):
        if self.packed:
            rows = batch
        else:
            rows = [(ids, labels) for ids, labels, _ in batch]
        return collate(rows, self.pad_id)


# -------------------------------------------------------------------- train --

def main() -> int:
    ap = argparse.ArgumentParser(description="stealth trainer (hardware-aware single GPU)")
    ap.add_argument("--model", required=True, help="HF id or local path of the base")
    ap.add_argument("--corpus", default="corpus/train.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--batch", type=int, default=None, help="packed rows per step (default: auto from VRAM)")
    ap.add_argument("--accum", type=int, default=None, help="gradient accumulation (default: auto for effective batch ~16)")
    ap.add_argument("--lr", type=float, default=1e-5, help="full FT wants a small LR")
    ap.add_argument("--seq-len", type=int, default=2048)
    ap.add_argument("--max-len", type=int, default=2048, help="drop samples longer than this")
    ap.add_argument("--warmup", type=float, default=0.03)
    ap.add_argument("--scheduler", choices=["cosine", "constant"], default="cosine")
    ap.add_argument("--save-every", type=int, default=100,
                    help="steps. An OOM with epoch-only checkpoints once cost a whole run.")
    ap.add_argument("--workers", type=int, default=None, help="DataLoader workers (default: auto from CPU count)")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--no-pack", action="store_true")
    ap.add_argument("--grad-ckpt", action=argparse.BooleanOptionalAction, default=None,
                    help="gradient checkpointing (default: auto on lower-VRAM GPUs)")
    ap.add_argument("--lora", type=int, default=0,
                    help="rank>0 switches to LoRA. Default 0 = FULL fine-tune.")
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
        print(_cyan("══════════════════ TRAINING ENVIRONMENT ══════════════════"))
        print(f"  {_green('GPU')}       {name}")
        print(f"  {_green('compute')}   sm_{cap[0]}{cap[1]}")
        print(f"  {_green('VRAM')}      {vram:.1f} GB")
        bf16_ok = torch.cuda.is_bf16_supported()
    else:
        print("* NO GPU VISIBLE — running on CPU fallback; training will be slow.")
        bf16_ok = False
    dtype = torch.bfloat16 if bf16_ok else torch.float32
    print(f"  {_green('dtype')}     {dtype}")

    # Hardware-aware runtime defaults. Explicit CLI values always win.
    if a.workers is None:
        a.workers = min(8, max(0, (os.cpu_count() or 2) // 2))
    if a.batch is None:
        if dev != "cuda":
            a.batch = 1
        elif vram >= 120:
            a.batch = 16
        elif vram >= 70:
            a.batch = 8
        elif vram >= 40:
            a.batch = 4
        elif vram >= 20:
            a.batch = 2
        else:
            a.batch = 1
    if a.accum is None:
        a.accum = max(1, math.ceil(16 / a.batch))
    if a.grad_ckpt is None:
        a.grad_ckpt = dev == "cuda" and vram < 40
    print(f"  {_green('auto batch')} {a.batch} × accum {a.accum}")
    print(f"  {_green('workers')}   {a.workers}")
    print(f"  {_green('grad ckpt')} {'ON' if a.grad_ckpt else 'OFF'}")

    attn = a.attn
    if attn == "auto":
        attn = "sdpa"
        try:
            import flash_attn  # noqa: F401
            if dev == "cuda" and torch.cuda.get_device_capability(0)[0] >= 8:
                attn = "flash_attention_2"
        except Exception:
            pass
    print(f"  {_green('attention')} {attn}")

    try:
        tok = load_tokenizer(a.model)
    except RuntimeError as exc:
        print(_red(f"FATAL: {exc}"))
        return 5
    print(f"  {_green('tokenizer')} {tok.__class__.__name__}")
    print(f"  {_green('roundtrip')} OK")

    print(f"  {_green('model')}     {a.model}")
    print(_dim("  loading weights locally..."))
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
        print(f"  {_magenta('mode')}      FULL fine-tune")
        print(f"  {_magenta('trainable')} {n/1e9:.2f}B parameters")

    if a.grad_ckpt:
        model.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False})
        print(f"  {_green('grad ckpt')} ON")

    print("\n" + _cyan("════════════════════ DATASET ════════════════════"))
    print(f"  {_green('corpus')}    {a.corpus}")
    base = SQLCorpus(a.corpus, tok, max_len=a.max_len)
    print(f"  {_green('samples')}   {len(base)}")
    print(f"  {_green('max len')}   {a.max_len}")
    if getattr(base, "skipped_long", 0):
        frac = base.skipped_long / max(len(base) + base.skipped_long, 1)
        warn = _red if frac > 0.10 else _yellow
        print(f"  {warn('over-length')} {base.skipped_long} skipped ({frac:.1%})")
    if not len(base):
        print("FATAL: empty dataset — check the corpus and the chat template")
        return 2

    if a.no_pack:
        ds = base
        coll = Collator(tok.pad_token_id, packed=False)
    else:
        ds = Packed(base, a.seq_len, tok.pad_token_id, seed=a.seed)
        coll = Collator(tok.pad_token_id, packed=True)
        eff = f"{ds.efficiency:.1%}"
        eff_c = _green(eff) if ds.efficiency >= 0.75 else _yellow(eff)
        print(f"  {_green('packing')}   {len(ds)} rows × {a.seq_len} tokens")
        print(f"  {_green('efficiency')} {eff_c}")

    dl = DataLoader(ds, batch_size=a.batch, shuffle=True, collate_fn=coll,
                    num_workers=a.workers, pin_memory=(dev == "cuda"),
                    drop_last=False, persistent_workers=a.workers > 0)

    steps_per_epoch = math.ceil(len(dl) / a.accum)
    total = max(1, int(steps_per_epoch * a.epochs))
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=a.lr, weight_decay=0.01, betas=(0.9, 0.95))
    warmup_steps = int(a.warmup * total)
    sched = (get_constant_schedule_with_warmup(opt, warmup_steps)
             if a.scheduler == "constant"
             else get_cosine_schedule_with_warmup(opt, warmup_steps, total))
    print("\n" + _cyan("══════════════════ TRAINING PLAN ══════════════════"))
    print(f"  {_green('epochs')}          {a.epochs}")
    print(f"  {_green('optimizer steps')} {total}")
    print(f"  {_green('micro batch')}     {a.batch}")
    print(f"  {_green('grad accum')}      {a.accum}")
    print(f"  {_green('effective batch')} {a.batch * a.accum} packed rows")
    print(f"  {_green('sequence len')}    {a.seq_len}")
    print(f"  {_green('learning rate')}   {a.lr:.2e}")
    print(f"  {_green('warmup')}          {a.warmup:.1%}")
    print(f"  {_green('scheduler')}       {a.scheduler}")
    print(f"  {_green('output')}          {a.out}")

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
        print("\n" + _magenta(f"════════════════════ EPOCH {ep + 1}/{math.ceil(a.epochs)} ════════════════════"))
        run, nb = 0.0, 0
        ep_loss_sum, ep_steps = 0.0, 0
        for i, (ids, lab, att) in enumerate(dl):
            ids, lab, att = ids.to(dev, non_blocking=True), lab.to(dev, non_blocking=True), att.to(dev, non_blocking=True)
            out = model(input_ids=ids, attention_mask=att, labels=lab)
            (out.loss / a.accum).backward()
            run += out.loss.item()
            nb += 1
            tok_seen += int(att.sum().item())

            is_accum_boundary = (i + 1) % a.accum == 0
            is_epoch_tail = (i + 1) == len(dl)
            if is_accum_boundary or is_epoch_tail:
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                opt.step()
                sched.step()
                opt.zero_grad(set_to_none=True)
                step += 1
                el = time.perf_counter() - t0
                loss = run / max(nb, 1)
                ep_loss_sum += loss
                ep_steps += 1
                mem = (torch.cuda.max_memory_allocated() / 1e9) if dev == "cuda" else 0
                lr = sched.get_last_lr()[0] if sched.get_last_lr() else a.lr
                toks = tok_seen / max(el, 1e-9)

                if not math.isfinite(loss):
                    print(_red(f"\nFATAL: non-finite loss at step {step}: {loss}"), flush=True)
                    return 3

                loss_c = _green(f"{loss:.4f}") if loss < 2.0 else (_yellow(f"{loss:.4f}") if loss < 4.0 else _red(f"{loss:.4f}"))
                mem_ratio = mem / max(vram, 1e-9) if dev == "cuda" else 0.0
                mem_c = _red(f"{mem:.1f}GB") if mem_ratio >= 0.92 else (_yellow(f"{mem:.1f}GB") if mem_ratio >= 0.80 else _green(f"{mem:.1f}GB"))
                print(
                    f"  {_cyan(f'step {step:>4}/{total:<4}')}  "
                    f"loss {loss_c}  "
                    f"lr {_dim(f'{lr:.2e}')}  "
                    f"{_green(f'{toks:,.0f} tok/s')}  "
                    f"peak {mem_c}  "
                    f"{_dim(f'{el:.0f}s')}",
                    flush=True,
                )
                if mem_ratio >= 0.92:
                    print(f"    {_red('⚠ VRAM')} peak usage is above 92% of the device", flush=True)

                log.append({"step": step, "epoch": ep + 1, "loss": loss,
                            "lr": lr, "peak_vram_gb": round(mem, 2),
                            "tokens": tok_seen, "tok_s": round(toks, 1),
                            "seconds": round(el, 1)})
                run, nb = 0.0, 0

                if a.save_every and step % a.save_every == 0:
                    ck = os.path.join(a.out, f"step{step}")
                    model.save_pretrained(ck)
                    tok.save_pretrained(ck)
                    json.dump(log, open(os.path.join(a.out, "log.json"), "w"), indent=2)
                    print(f"    {_green('✓ checkpoint')} -> {ck}", flush=True)

                if step >= total:
                    done = True
                    break

        if ep_steps:
            print(f"  {_magenta('epoch mean loss')} {ep_loss_sum / ep_steps:.4f}")

    final = os.path.join(a.out, "final")
    if a.lora > 0:
        print(f"  {_cyan('merge')}      folding LoRA adapter into full local checkpoint")
        model = model.merge_and_unload()
    model.save_pretrained(final)
    tok.save_pretrained(final)
    json.dump(log, open(os.path.join(a.out, "log.json"), "w"), indent=2)
    el = time.perf_counter() - t0
    print("\n" + _green("══════════════════ TRAINING COMPLETE ══════════════════"))
    print(f"  {_green('time')}       {el/60:.1f} min")
    print(f"  {_green('tokens')}     {tok_seen:,}")
    print(f"  {_green('throughput')} {tok_seen/el:,.0f} tok/s")
    if log:
        print(f"  {_green('final loss')} {log[-1]['loss']:.4f}")
    print(f"  {_green('checkpoint')} {final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
