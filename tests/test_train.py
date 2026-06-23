"""Tests for `sutra train` — capture engagement traces, build an SFT dataset, drive a QLoRA.

The GPU training itself never runs here (deps absent in CI); we test the capture, the dataset
extraction, and that the driver degrades to a plan + install hint when the extras are missing.
"""

import json
import types

import pytest
from typer.testing import CliRunner

import friday.cli as cli
from friday.core import train

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Keep every test off the real traces log and dataset path."""
    monkeypatch.setattr(train, "_TRACES", tmp_path / "traces.jsonl")
    monkeypatch.setattr(train, "_DATASET", tmp_path / "train" / "worker_sft.jsonl")


def _tc_obj(name, args):
    """An object-style tool call, the way LiteLLM hands them back."""
    fn = types.SimpleNamespace(name=name, arguments=json.dumps(args))
    return types.SimpleNamespace(id="call_1", function=fn)


def _port_scan_msg(host="10.10.10.5"):
    return {"role": "tool", "name": "port_scan",
            "content": json.dumps({"host": host, "open": [{"port": 22, "banner": "ssh"}]})}


# ── capture ──────────────────────────────────────────────────────────────────────────
def test_record_trace_flattens_tool_calls_object_and_dict():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "scan it"},
        {"role": "assistant", "content": None,
         "tool_calls": [_tc_obj("port_scan", {"host": "10.10.10.5"})]},
        _port_scan_msg(),
        # a dict-style tool call must serialize the same way as the object one
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "function": {"name": "web_enum",
                                                  "arguments": '{"url": "http://x"}'}}]},
    ]
    train.record_trace("scan it", messages, "found ssh")
    rows = [json.loads(s) for s in train._TRACES.read_text().splitlines() if s.strip()]
    assert len(rows) == 1
    msgs = rows[0]["messages"]
    first_tc = msgs[2]["tool_calls"][0]
    assert first_tc["function"]["name"] == "port_scan"
    assert json.loads(first_tc["function"]["arguments"])["host"] == "10.10.10.5"
    assert msgs[4]["tool_calls"][0]["function"]["name"] == "web_enum"  # dict form preserved


def test_record_trace_never_raises_on_garbage(monkeypatch):
    # an un-serializable object in a message must be swallowed, not crash the engagement
    train.record_trace("g", [{"role": "user", "content": object()}], "r")  # no exception


# ── build dataset ────────────────────────────────────────────────────────────────────
def _write_traces(rows):
    train._TRACES.parent.mkdir(parents=True, exist_ok=True)
    train._TRACES.write_text("\n".join(json.dumps(r) for r in rows))


def test_build_dataset_keeps_successful_drops_failed():
    success = {"goal": "pwn", "report": "rooted", "messages": [
        {"role": "user", "content": "pwn 10.10.10.5"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "function": {"name": "port_scan", "arguments": "{}"}}]},
        _port_scan_msg(),  # a real finding → counts as success
    ]}
    failed = {"goal": "nope", "report": "nothing", "messages": [
        {"role": "user", "content": "look around"},
        {"role": "assistant", "content": "I could not find anything", "tool_calls": None},
    ]}
    _write_traces([success, failed])

    stats = train.build_dataset()
    assert stats["traces"] == 2 and stats["samples"] == 1
    assert stats["skipped_unsuccessful"] == 1
    out = [json.loads(s) for s in train._DATASET.read_text().splitlines() if s.strip()]
    assert len(out) == 1
    assert out[0]["messages"][0]["role"] == "system"  # worker system prompt prepended
    assert any(t.get("tool_calls") for t in out[0]["messages"])  # the action survived


def test_build_dataset_include_failed_and_drops_no_action():
    # an action that yields no recognised finding: successful=False, but it DID call a tool
    action_no_finding = {"goal": "g", "report": "", "messages": [
        {"role": "user", "content": "run id"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "function": {"name": "run_command",
                                                 "arguments": '{"command": "id"}'}}]},
        {"role": "tool", "name": "run_command", "content": json.dumps({"stdout": "uid=0"})},
    ]}
    no_action = {"goal": "g2", "report": "", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "let me think about it", "tool_calls": None},
    ]}
    _write_traces([action_no_finding, no_action])

    default = train.build_dataset()
    assert default["samples"] == 0 and default["skipped_unsuccessful"] == 2

    incl = train.build_dataset(include_failed=True)
    assert incl["samples"] == 1            # the action trace is now kept
    assert incl["skipped_no_action"] == 1  # the talk-only trace is still dropped


def test_build_dataset_drops_orchestrator_meta():
    tr = {"goal": "g", "report": "rooted", "messages": [
        {"role": "user", "content": "pwn it"},
        {"role": "assistant", "content": "[orchestrator] port_scan the box"},  # meta, must drop
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "function": {"name": "port_scan", "arguments": "{}"}}]},
        _port_scan_msg(),
    ]}
    _write_traces([tr])
    train.build_dataset()
    out = json.loads(train._DATASET.read_text().splitlines()[0])
    assert not any("[orchestrator]" in (m.get("content") or "") for m in out["messages"])


# ── fine-tune driver ─────────────────────────────────────────────────────────────────
def test_run_finetune_without_deps_prints_plan_and_install(tmp_path):
    ds = tmp_path / "ds.jsonl"
    ds.write_text(json.dumps({"messages": []}) + "\n")
    out = train.run_finetune(dataset=ds, base_model="Qwen/Qwen2.5-7B-Instruct")
    # CI has none of the training extras, so this is the graceful path
    assert "pip install" in out and ".[train]" in out
    assert "QLoRA fine-tune plan" in out and "Qwen/Qwen2.5-7B-Instruct" in out


def test_run_finetune_without_dataset_says_so(tmp_path):
    out = train.run_finetune(dataset=tmp_path / "missing.jsonl")
    assert "No dataset" in out and "--dataset-only" in out


def test_finetune_plan_mentions_samples_and_output(tmp_path):
    plan = train._finetune_plan(tmp_path / "d.jsonl", base_model="m", out_dir=tmp_path / "o",
                                epochs=3, n_samples=7)
    assert "7 samples" in plan and "epochs     : 3" in plan and "LoRA" in plan


# ── wiring + CLI ─────────────────────────────────────────────────────────────────────
async def test_run_autonomous_records_a_trace(monkeypatch, tmp_path):
    from friday.core import agent, dreaming

    monkeypatch.setenv("SUTRA_AUTONOMOUS", "1")
    monkeypatch.setattr(agent, "_ENGAGE_DIR", tmp_path / "eng")
    monkeypatch.setattr(dreaming, "recall_lessons", lambda *a, **k: [])
    monkeypatch.setattr(dreaming, "record_lesson", lambda *a, **k: None)

    async def _complete(messages, tier=None, tools=None, **kw):
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                content="nothing to do", tool_calls=None))])

    monkeypatch.setattr(agent, "complete", _complete)
    await agent.run_autonomous("scan it", max_rounds=2)
    rows = [json.loads(s) for s in train._TRACES.read_text().splitlines() if s.strip()]
    assert len(rows) == 1 and rows[0]["goal"] == "scan it"  # the loop captured a trace


def test_cli_train_dataset_only(monkeypatch):
    monkeypatch.setattr(train, "build_dataset", lambda **k: {
        "traces": 3, "samples": 2, "skipped_unsuccessful": 1, "skipped_no_action": 0,
        "path": "/x/worker_sft.jsonl"})

    def _boom(*a, **k):
        raise AssertionError("run_finetune must not run under --dataset-only")

    monkeypatch.setattr(train, "run_finetune", _boom)
    r = runner.invoke(cli.app, ["train", "--dataset-only"])
    assert r.exit_code == 0, r.output
    assert "samples: 2" in r.output and "worker_sft.jsonl" in r.output
