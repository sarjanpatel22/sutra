"""sutra train — turn real engagement traces into a QLoRA fine-tune of the local worker.

The playbook ([[playbook]]) teaches Sutra HOW to work in-context. This is the other half:
once Sutra has run real engagements, the cheap local worker (qwen 7B) can be FINE-TUNED on what
actually worked, so the method is baked into its weights and it stops fumbling — letting the paid
orchestrator spend fewer calls re-steering it. The high tier learns by prompt (it is a cloud model
we cannot train); the low tier learns by training. This covers the low tier.

Three stages, decoupled so the cheap parts run anywhere and the GPU part stays optional:

  1. CAPTURE — `record_trace` appends each finished engagement's full message trace (tool calls
     AND results intact, unlike the lossy .md) to a bounded JSONL. Wired into both autonomous
     loops, so every live run from now on produces training-ready data for free.
  2. BUILD  — `build_dataset` turns those traces into supervised chat samples (one per successful
     engagement: the worker system prompt + the goal + the assistant/tool turns that reached a
     flag or finding). $0, deterministic, no GPU.
  3. TRAIN  — `run_finetune` runs a standard 4-bit QLoRA over qwen 7B. The heavy deps
     (torch/transformers/trl/peft/bitsandbytes) are an optional extra; without them it prints the
     exact install command and the plan, so the scaffold is safe to ship and call before the GPU
     stack is set up.

Nothing here is hands-free or paid — training is an explicit, multi-hour local GPU job you start.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from friday.core.turnlog import append_capped

_SHARE = Path.home() / ".local" / "share" / "friday"
_TRACES = _SHARE / "traces.jsonl"
# Bounded — one row per engagement; his run count is small, but cap so the file never runs away.
_MAX_TRACES = 200
_DATASET = _SHARE / "train" / "worker_sft.jsonl"
_ADAPTER_DIR = _SHARE / "train" / "worker-lora"
# The local worker we fine-tune. Ollama tag qwen2.5:7b <-> this HF instruct checkpoint.
DEFAULT_BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"
_TRAIN_DEPS = ("torch", "transformers", "trl", "peft", "bitsandbytes", "datasets")
_TRAIN_INSTALL = 'pip install -e ".[train]"'
_log = logging.getLogger(__name__)


# ── 1. capture ─────────────────────────────────────────────────────────────────────────
def _attr(obj: object, key: str, default: object = None) -> object:
    """Read `key` off an object attribute or a dict — tool calls come back in both shapes."""
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _serialize_tool_calls(tcs: Any) -> list[dict[str, Any]] | None:
    """Flatten LiteLLM/OpenAI tool-call objects (or dicts) to plain JSON dicts with names AND
    arguments intact — the .md transcript keeps only names, which is useless for training."""
    if not tcs:
        return None
    out: list[dict[str, Any]] = []
    for tc in tcs:
        fn = _attr(tc, "function")
        out.append({"id": _attr(tc, "id"), "type": "function",
                    "function": {"name": _attr(fn, "name") or "?",
                                 "arguments": _attr(fn, "arguments") or ""}})
    return out


def _normalize(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make a message list JSON-safe: keep role/content/name/tool_call_id, flatten tool_calls."""
    norm: list[dict[str, Any]] = []
    for m in messages:
        row: dict[str, Any] = {"role": m.get("role"), "content": m.get("content")}
        tcs = _serialize_tool_calls(m.get("tool_calls"))
        if tcs:
            row["tool_calls"] = tcs
        if m.get("name"):
            row["name"] = m["name"]
        if m.get("tool_call_id"):
            row["tool_call_id"] = m["tool_call_id"]
        norm.append(row)
    return norm


def record_trace(goal: str, messages: list[dict[str, Any]], report: str) -> None:
    """Append one finished engagement's trace (tool calls/results intact) to the training log.
    Best-effort — a capture failure must never break the engagement."""
    try:
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "goal": (goal or "").strip()[:300],
            "report": (report or "").strip()[:2000],
            "messages": _normalize(messages),
        }
        append_capped(_TRACES, json.dumps(rec), _MAX_TRACES)
    except Exception as e:  # noqa: BLE001 — capture must never break the turn
        _log.warning("could not record engagement trace: %s", e)


def load_traces() -> list[dict[str, Any]]:
    """Every captured engagement trace, oldest first. Empty on missing/corrupt — never raises."""
    try:
        return [json.loads(s) for s in _TRACES.read_text().splitlines() if s.strip()]
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001 — a corrupt log must not crash dataset building
        _log.warning("could not read traces: %s", e)
        return []


# ── 2. build dataset ───────────────────────────────────────────────────────────────────
def _succeeded(messages: list[dict[str, Any]], report: str) -> bool:
    """Did this engagement actually get somewhere? Reuse the loop's own flag/finding extractors so
    'success' means the same thing here as in the engagement file."""
    from friday.core import agent
    flags, findings = agent.engagement_highlights(messages, report)
    return bool(flags or findings)


def _to_sample(messages: list[dict[str, Any]], *, worker_system: str) -> dict[str, Any] | None:
    """One SFT chat sample: the worker system prompt + the engagement's non-meta turns. Drops the
    orchestrator's directives (meta-narration, not worker behaviour). Returns None if the trace has
    no actual tool action to learn from."""
    turns: list[dict[str, Any]] = [{"role": "system", "content": worker_system}]
    for m in messages:
        if m.get("role") == "system":
            continue
        content = m.get("content")
        if (m.get("role") == "assistant" and isinstance(content, str)
                and content.startswith("[orchestrator]")):
            continue  # orchestrator meta — not worker behaviour
        turns.append(m)
    if not any(t.get("role") == "assistant" and t.get("tool_calls") for t in turns):
        return None  # nothing was actually done — not worth training on
    return {"messages": turns}


def build_dataset(out_path: Path | None = None, *, include_failed: bool = False,
                  max_samples: int = 0) -> dict[str, Any]:
    """Turn captured traces into a worker SFT dataset (JSONL of {"messages": [...]}). By default
    keeps only engagements that reached a flag or finding — we train on what WORKED. Returns stats
    and writes the dataset file (creating its parent)."""
    from friday.core.agent import WORKER_SYSTEM_PROMPT

    out_path = out_path or _DATASET
    traces = load_traces()
    samples: list[dict[str, Any]] = []
    skipped_unsuccessful = skipped_no_action = 0
    for tr in traces:
        msgs = tr.get("messages") or []
        if not include_failed and not _succeeded(msgs, tr.get("report", "")):
            skipped_unsuccessful += 1
            continue
        sample = _to_sample(msgs, worker_system=WORKER_SYSTEM_PROMPT)
        if sample is None:
            skipped_no_action += 1
            continue
        samples.append(sample)
    if max_samples > 0:
        samples = samples[-max_samples:]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(s) for s in samples))
    return {"traces": len(traces), "samples": len(samples),
            "skipped_unsuccessful": skipped_unsuccessful,
            "skipped_no_action": skipped_no_action, "path": str(out_path)}


# ── 3. fine-tune ───────────────────────────────────────────────────────────────────────
def _missing_train_deps() -> list[str]:
    """Training extras that aren't importable (the common case until the GPU stack is set up)."""
    return [d for d in _TRAIN_DEPS if importlib.util.find_spec(d) is None]


def _finetune_plan(dataset: Path, *, base_model: str, out_dir: Path, epochs: int,
                   n_samples: int) -> str:
    return (
        "QLoRA fine-tune plan (local GPU):\n"
        f"  base model : {base_model}\n"
        f"  dataset    : {dataset} ({n_samples} samples)\n"
        "  method     : 4-bit NF4 + LoRA (r=16, alpha=32) via trl SFTTrainer\n"
        f"  epochs     : {epochs}\n"
        f"  output     : {out_dir} (LoRA adapter)\n"
        "  then serve : merge or load the adapter, then point the worker tier at it."
    )


def run_finetune(dataset: Path | None = None, *, base_model: str = DEFAULT_BASE_MODEL,
                 out_dir: Path | None = None, epochs: int = 2, dry_run: bool = False) -> str:
    """Run a 4-bit QLoRA over the worker model on the built dataset. If the training deps aren't
    installed, print the plan + the one install command and stop — so the scaffold is safe to call
    early. Returns a human-readable status string."""
    dataset = dataset or _DATASET
    out_dir = out_dir or _ADAPTER_DIR
    if not dataset.exists():
        return (f"No dataset at {dataset}. Build it first with: sutra train --dataset-only "
                "(after you have run some live engagements).")
    n = sum(1 for s in dataset.read_text().splitlines() if s.strip())
    plan = _finetune_plan(dataset, base_model=base_model, out_dir=out_dir, epochs=epochs,
                          n_samples=n)
    missing = _missing_train_deps()
    if missing:
        return (f"Training deps not installed: {', '.join(missing)}\n  {_TRAIN_INSTALL}\n\n{plan}")
    if dry_run:
        return f"(dry run — deps present, not training)\n\n{plan}"
    if n == 0:
        return f"Dataset {dataset} is empty — run live engagements first, then rebuild."
    return _do_finetune(dataset, base_model=base_model, out_dir=out_dir, epochs=epochs, plan=plan)


def _do_finetune(dataset: Path, *, base_model: str, out_dir: Path, epochs: int, plan: str) -> str:
    """The actual QLoRA run. Only reached when the training extra is installed, so these heavy
    imports never load in CI. Standard trl SFTTrainer + peft LoRA + bitsandbytes 4-bit; exact
    kwargs track the installed trl version (use tokenizer= on older trl)."""
    import torch
    from datasets import load_dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    _log.info("starting QLoRA:\n%s", plan)
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True)
    tok = AutoTokenizer.from_pretrained(base_model)
    model = AutoModelForCausalLM.from_pretrained(base_model, quantization_config=bnb,
                                                 device_map="auto")
    ds = load_dataset("json", data_files=str(dataset), split="train")  # 'messages' chat format
    lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = SFTConfig(output_dir=str(out_dir), num_train_epochs=epochs,
                    per_device_train_batch_size=1, gradient_accumulation_steps=8,
                    learning_rate=2e-4, logging_steps=5, save_strategy="epoch", bf16=True)
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=ds, peft_config=lora,
                         processing_class=tok)
    trainer.train()
    trainer.save_model(str(out_dir))
    return f"QLoRA complete — LoRA adapter saved to {out_dir}\nServe it on the worker tier next."
