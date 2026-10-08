"""Verify-before-claim: a skill that can't confirm its postcondition is downgraded
to a visible error, so the agent can never report a phantom success. A verified
success is tagged `verified: True`.
"""
import json
from types import SimpleNamespace

from friday.core import agent, postcheck
from friday.skills import registry


def _tc(name, args, tid="t1"):
    return SimpleNamespace(id=tid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _payload(messages):
    return json.loads(messages[0]["content"])


# --- integration: a framework verifier changes the loop's reported outcome ---
async def test_unverified_success_is_downgraded_and_verified_is_tagged():
    @registry.skill(name="_pc_fake", description="pretends to act")
    async def _fake(ok: bool = True) -> dict:
        return {"status": "ok"}  # the skill *claims* success regardless

    postcheck.register("_pc_fake", lambda args, result: (bool(args.get("ok")), "checked"))
    try:
        # verifier says NOT ok -> the loop must surface an error, not a success
        bad = _payload(await agent._run_tool_calls([_tc("_pc_fake", {"ok": False})]))
        assert bad["error"] == "action not verified"
        assert bad["skill"] == "_pc_fake"
        assert bad["raw_result"] == {"status": "ok"}  # original output still visible

        # verifier says ok -> tagged verified
        good = _payload(await agent._run_tool_calls([_tc("_pc_fake", {"ok": True})]))
        assert good.get("verified") is True
    finally:
        registry.unregister("_pc_fake")
        postcheck._VERIFIERS.pop("_pc_fake", None)


# --- built-in verifiers -----------------------------------------------------
def test_run_command_verifier_catches_nonzero_exit():
    assert postcheck.verify("run_command", {}, {"returncode": 0})[0] is True
    ok, note = postcheck.verify("run_command", {}, {"returncode": 7, "stderr": "boom"})
    assert ok is False and "7" in note  # the phantom-success case run_command creates


def test_write_file_verifier(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hello")
    ok, _ = postcheck.verify("write_file", {}, {"path": str(f), "bytes_written": "5"})
    assert ok is True
    missing = str(tmp_path / "nope.txt")
    gone = postcheck.verify("write_file", {}, {"path": missing, "bytes_written": "5"})
    assert gone[0] is False
    wrong_size = postcheck.verify("write_file", {}, {"path": str(f), "bytes_written": "999"})
    assert wrong_size[0] is False


def test_edit_file_verifier(tmp_path):
    f = tmp_path / "y.txt"
    f.write_text("alpha beta")
    ok, _ = postcheck.verify("edit_file", {"new_text": "beta"}, {"path": str(f)})
    assert ok is True
    missing_text = postcheck.verify("edit_file", {"new_text": "gamma"}, {"path": str(f)})
    assert missing_text[0] is False


def test_unknown_skill_is_unverifiable():
    assert postcheck.verify("some_random_skill", {}, {"status": "ok"}) is None


def test_buggy_verifier_never_raises():
    postcheck.register("_pc_boom", lambda a, r: 1 / 0)  # raises
    try:
        assert postcheck.verify("_pc_boom", {}, {}) is None  # swallowed -> unverifiable
    finally:
        postcheck._VERIFIERS.pop("_pc_boom", None)
