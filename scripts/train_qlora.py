"""QLoRA fine-tune of Qwen2.5-7B-Instruct into a sharper Sutra offline tool-caller.

Runs in the isolated .venv-train (torch/transformers/peft/bitsandbytes). Reads the tools
schema (/tmp/sutra_tools.json) + the synthesized dataset (/tmp/sft_toolcalling.jsonl),
renders each example through Qwen2.5's chat template, and trains a 4-bit LoRA. Loss is
masked to the assistant completion only (manual -100 labels on the prompt), so it learns
the tool_call, not to echo the schema. Deliberately TRL-free — masking is hand-rolled to
dodge the churning trl/transformers SFT API.

The full 43-tool schema is ~5.8k tokens, so each example offers the correct tool + a
random sample of distractors (~23 tools, ~3k tokens) — the standard tool-calling trick:
short sequences, teaches discrimination, generalizes to the full set at inference.

    .venv-train/bin/python scripts/train_qlora.py        # adapter -> /tmp/qwen7b-sutra-lora
"""

from __future__ import annotations

import json
import random

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)

random.seed(0)
BASE = "Qwen/Qwen2.5-7B-Instruct"
OUT = "/tmp/qwen7b-sutra-lora"
MAXLEN = 4096
DISTRACTORS = 8  # correct tool + 8 distractors ≈ 1.5k tokens — fits VRAM with room for attention
SYS = (
    "You are Sutra. You have tools that DO things. When the user asks you to fetch, "
    "open, run, search, compute, or recall something real, CALL the single matching "
    "tool. If it's just chitchat or general knowledge, answer normally with NO tool."
)


def _subset(tools: list[dict], correct: str | None) -> list[dict]:
    """Correct tool (if any) + a random sample of distractors, shuffled."""
    others = [t for t in tools if t["function"]["name"] != correct]
    keep = [t for t in tools if t["function"]["name"] == correct]
    keep += random.sample(others, min(DISTRACTORS, len(others)))
    random.shuffle(keep)
    return keep


def _featurize(tok, tools: list[dict], rows: list[dict]) -> list[dict]:
    """Render each example and build input_ids + labels (prompt masked to -100)."""
    feats, lens = [], []
    for ex in rows:
        sub = _subset(tools, ex["tool"])
        msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": ex["user"]}]
        if ex["tool"]:
            call = {"name": ex["tool"], "arguments": ex["args"]}
            asst = {"role": "assistant", "content": "",
                    "tool_calls": [{"type": "function", "function": call}]}
        else:
            asst = {"role": "assistant", "content": ex["args"].get("_answer", "Sure.")}
        full = tok.apply_chat_template(msgs + [asst], tools=sub, tokenize=False)
        prompt = tok.apply_chat_template(
            msgs, tools=sub, tokenize=False, add_generation_prompt=True)
        f = tok(full, truncation=True, max_length=MAXLEN, add_special_tokens=False)["input_ids"]
        p = tok(prompt, truncation=True, max_length=MAXLEN, add_special_tokens=False)["input_ids"]
        n = min(len(p), len(f))  # mask the shared prompt prefix; learn only the completion
        feats.append({"input_ids": f, "labels": [-100] * n + f[n:], "attention_mask": [1] * len(f)})
        lens.append(len(f))
    lens.sort()
    print(f"featurized {len(feats)}; tokens min={lens[0]} p50={lens[len(lens)//2]} max={lens[-1]}")
    return feats


class _Collator:
    def __init__(self, pad_id: int) -> None:
        self.pad_id = pad_id

    def __call__(self, batch: list[dict]) -> dict:
        m = max(len(b["input_ids"]) for b in batch)
        ii, ll, am = [], [], []
        for b in batch:
            pad = m - len(b["input_ids"])
            ii.append(b["input_ids"] + [self.pad_id] * pad)
            ll.append(b["labels"] + [-100] * pad)
            am.append(b["attention_mask"] + [0] * pad)
        return {"input_ids": torch.tensor(ii), "labels": torch.tensor(ll),
                "attention_mask": torch.tensor(am)}


def main() -> None:
    tools = json.load(open("/tmp/sutra_tools.json"))
    rows = [json.loads(line) for line in open("/tmp/sft_toolcalling.jsonl")]
    tok = AutoTokenizer.from_pretrained(BASE)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    ds = Dataset.from_list(_featurize(tok, tools, rows))

    bnb = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(
        BASE, quantization_config=bnb, dtype=torch.bfloat16, device_map={"": 0},
        attn_implementation="sdpa")  # memory-efficient attention — avoids the long-seq OOM
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(
        r=16, lora_alpha=32, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"]))
    model.print_trainable_parameters()

    args = TrainingArguments(
        output_dir=OUT, per_device_train_batch_size=1, gradient_accumulation_steps=8,
        num_train_epochs=3, learning_rate=2e-4, lr_scheduler_type="cosine", warmup_ratio=0.05,
        logging_steps=5, save_strategy="no", bf16=True, optim="paged_adamw_8bit",
        gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[])
    Trainer(model=model, args=args, train_dataset=ds,
            data_collator=_Collator(tok.pad_token_id)).train()
    model.save_pretrained(OUT)
    tok.save_pretrained(OUT)
    print("SAVED_ADAPTER", OUT)


if __name__ == "__main__":
    main()
