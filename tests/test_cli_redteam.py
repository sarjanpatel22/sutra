"""Tests for the `sutra redteam` CLI argument guards — especially objective/paste mode.

These cover the pure argument-validation paths (no network): they assert the command rejects
malformed invocations with a clean exit(1) instead of a traceback, and that paste mode wires the
pasted replies through to the skill without ever calling a target endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import friday.cli as cli

runner = CliRunner()


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_objective_requires_attempts_file(tmp_path: Path) -> None:
    r = runner.invoke(cli.app, ["redteam", "http://x/api/chat", "--objective", "produce X"])
    assert r.exit_code == 1
    assert "--attempts-file" in r.output


def test_replies_file_without_objective_is_rejected(tmp_path: Path) -> None:
    reps = _write(tmp_path, "r.json", '["a reply"]')
    r = runner.invoke(cli.app, ["redteam", "http://x/api/chat", "--replies-file", reps])
    assert r.exit_code == 1
    assert "paste mode" in r.output and "--objective" in r.output


def test_paste_mode_length_mismatch_is_clean_error(tmp_path: Path) -> None:
    att = _write(tmp_path, "a.txt", "attempt one\nattempt two\n")
    reps = _write(tmp_path, "r.json", '["only one reply"]')
    r = runner.invoke(cli.app, ["redteam", "http://x/api/chat", "--objective", "produce X",
                                "--attempts-file", att, "--replies-file", reps,
                                "--judge-model", "judge"])
    assert r.exit_code == 1
    assert "pair" in r.output and "1:1" in r.output


def test_paste_mode_requires_judge(tmp_path: Path) -> None:
    att = _write(tmp_path, "a.txt", "attempt one\n")
    reps = _write(tmp_path, "r.json", '["a reply"]')
    r = runner.invoke(cli.app, ["redteam", "http://x/api/chat", "--objective", "produce X",
                                "--attempts-file", att, "--replies-file", reps])
    assert r.exit_code == 1
    assert "--judge-model" in r.output


def test_paste_mode_bad_json_is_clean_error(tmp_path: Path) -> None:
    """A malformed --replies-file gives a clean message, not a JSONDecodeError traceback."""
    att = _write(tmp_path, "a.txt", "attempt one\n")
    reps = _write(tmp_path, "r.json", "{not valid json")
    r = runner.invoke(cli.app, ["redteam", "http://x/api/chat", "--objective", "produce X",
                                "--attempts-file", att, "--replies-file", reps,
                                "--judge-model", "judge"])
    assert r.exit_code == 1
    assert "JSON array" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_paste_mode_missing_replies_file_is_clean_error(tmp_path: Path) -> None:
    att = _write(tmp_path, "a.txt", "attempt one\n")
    missing = str(tmp_path / "nope.json")
    r = runner.invoke(cli.app, ["redteam", "http://x/api/chat", "--objective", "produce X",
                                "--attempts-file", att, "--replies-file", missing,
                                "--judge-model", "judge"])
    assert r.exit_code == 1
    assert "not found" in r.output


def test_paste_mode_scores_without_calling_target(tmp_path: Path, monkeypatch) -> None:
    """End-to-end via the CLI: pasted replies are judged; the target endpoint is never called."""
    from friday.skills import llm_redteam as rt

    target_calls = {"n": 0}

    def fake(e, m, system, user, api, key):
        if "strict evaluator" in system.lower():        # the objective judge
            return '{"complied": true, "reason": "the pasted reply does the thing"}'
        target_calls["n"] += 1                # a target hit (system == "") — must not happen
        return "SHOULD NOT BE CALLED"

    monkeypatch.setattr(rt, "_chat", fake)
    att = _write(tmp_path, "a.txt", "attempt one\nattempt two\n")
    reps = _write(tmp_path, "r.json", json.dumps(["complying reply", "another one"]))
    out_dir = tmp_path / "reports"
    r = runner.invoke(cli.app, ["redteam", "http://judge/api/chat", "--objective", "produce X",
                                "--attempts-file", att, "--replies-file", reps,
                                "--judge-model", "judge", "--out", str(out_dir / "rep.md")])
    assert r.exit_code == 0, r.output
    assert target_calls["n"] == 0                       # nothing sent to a target
    assert "Paste mode" in r.output
