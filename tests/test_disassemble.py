"""Tests for the disassemble skill (friday/skills/disassemble.py).

The objdump-output parsers (dangerous-call detection, function scoping) are exercised on canned
objdump text so they're deterministic with no binutils/compiler. The skill's graceful paths
(missing binary, objdump absent) are covered too; a live objdump run is guarded by availability.
"""

from __future__ import annotations

import shutil

import pytest

from friday.skills import disassemble

_OBJDUMP = """
0000000000001149 <main>:
    1149:\t55                   \tpush   rbp
    1162:\te8 d9 fe ff ff       \tcall   1040 <gets@plt>
    1170:\te8 cb fe ff ff       \tcall   1042 <strcpy@plt>
    1180:\te8 aa bb cc dd       \tcall   1050 <printf@plt>
    1190:\te8 11 22 33 44       \tcall   1060 <system@plt>

0000000000002000 <helper>:
    2000:\t55                   \tpush   rbp
    2010:\te8 99 88 77 66       \tcall   1040 <gets@plt>
""".strip("\n")


def test_dangerous_calls_detected_and_sorted():
    f = disassemble._dangerous_calls(_OBJDUMP)
    names = [x["function"] for x in f]
    assert "gets" in names and "strcpy" in names and "system" in names
    assert "printf" not in names  # not a sink
    assert f[0]["severity"] == "high"  # sorted worst-first
    addrs = {(x["function"], x["address"]) for x in f}
    assert ("gets", "1162") in addrs and ("gets", "2010") in addrs  # both call sites


def test_filter_function_scopes_to_one_block():
    block = disassemble._filter_function(_OBJDUMP, "main")
    assert "<main>:" in block and "gets@plt" in block
    assert "<helper>:" not in block  # stops at the blank line before helper


def test_filter_function_unknown_returns_empty():
    assert disassemble._filter_function(_OBJDUMP, "nope") == ""


async def test_no_path():
    assert "error" in await disassemble.disassemble("")


async def test_missing_file():
    assert "error" in await disassemble.disassemble("/no/such/binary")


async def test_objdump_absent_is_graceful(tmp_path, monkeypatch):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 100)
    monkeypatch.setattr(disassemble.shutil, "which", lambda _: None)
    out = await disassemble.disassemble(str(p))
    assert "error" in out and "objdump" in out["error"]


@pytest.mark.skipif(not shutil.which("objdump"), reason="binutils/objdump not installed")
async def test_live_objdump_on_real_binary():
    import sys

    out = await disassemble.disassemble(sys.executable, max_lines=50)
    assert "error" not in out
    assert out["disassembly"]  # got some assembly text
    assert isinstance(out["dangerous_calls"], list)


def test_disassemble_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("disassemble").auto_confirm is True  # read-only → auto
