"""Stage-4 honest A/B: base Qwen2.5-7B vs the QLoRA-tuned adapter on tool-calling.

Both are scored in the SAME HF-generate harness (not Ollama), over the held-out
eval_toolcalling cases, so the comparison is apples-to-apples. The tuned model must
beat base or we don't ship it. Runs in .venv-train.

    .venv-train/bin/python scripts/eval_adapter.py
"""

from __future__ import annotations

import json
import re

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

BASE = "Qwen/Qwen2.5-7B-Instruct"
ADAPTER = "/tmp/qwen7b-sutra-lora"
SYS = (
    "You are Sutra. You have tools that DO things. When the user asks you to fetch, "
    "open, run, search, compute, or recall something real, CALL the single matching "
    "tool. If it's just chitchat or general knowledge, answer normally with NO tool."
)


def _called_tool(text: str) -> str | None:
    if "tool_call" not in text:
        return None
    m = re.search(r'"name":\s*"([^"]+)"', text)
    return m.group(1) if m else None


def run_eval(model, tok, cases, tools, label: str) -> None:
    correct, fails = 0, []
    for utt, expected in cases:
        msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": utt}]
        prompt = tok.apply_chat_template(
            msgs, tools=tools, tokenize=False, add_generation_prompt=True)
        ids = tok(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(**ids, max_new_tokens=96, do_sample=False,
                                  pad_token_id=tok.eos_token_id)
        text = tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=False)
        name = _called_tool(text)
        ok = (expected is None and name is None) or bool(expected and name in expected)
        correct += ok
        if not ok:
            fails.append(f"{utt[:34]!r}->{name}")
    print(f"\n{label}: {correct}/{len(cases)} = {100 * correct / len(cases):.0f}%")
    print("  fails:", "; ".join(fails) if fails else "none")


def main() -> None:
    cases = json.load(open("/tmp/eval_cases.json"))
    tools = json.load(open("/tmp/sutra_tools.json"))
    tok = AutoTokenizer.from_pretrained(BASE)
    bnb = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(
        BASE, quantization_config=bnb, dtype=torch.bfloat16, device_map={"": 0},
        attn_implementation="sdpa")
    model.eval()
    run_eval(model, tok, cases, tools, "BASE qwen2.5-7b (hf)")
    tuned = PeftModel.from_pretrained(model, ADAPTER)
    tuned.eval()
    run_eval(tuned, tok, cases, tools, "TUNED qwen2.5-7b + Sutra LoRA")


if __name__ == "__main__":
    main()
