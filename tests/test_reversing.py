"""Tests for the binary-triage skill (friday/skills/reversing.py).

A minimal but real 64-bit ELF is synthesized byte-by-byte so header + mitigation parsing is checked
deterministically with no compiler/binaries. A second test runs against the live python interpreter
(a real ELF on the platforms this runs on) to prove end-to-end reading.
"""

from __future__ import annotations

import struct
import sys

from friday.skills import reversing


def _make_elf() -> bytes:
    """64-bit LE PIE ELF with one non-exec GNU_STACK phdr (NX on) + telltale strings appended."""
    eh = bytearray(64)
    eh[0:4] = b"\x7fELF"
    eh[4] = 2  # 64-bit
    eh[5] = 1  # little-endian
    eh[6] = 1  # version
    struct.pack_into("<H", eh, 16, 3)  # e_type = DYN (PIE)
    struct.pack_into("<H", eh, 18, 0x3E)  # e_machine = x86-64
    struct.pack_into("<I", eh, 20, 1)  # e_version
    struct.pack_into("<Q", eh, 24, 0x1040)  # e_entry
    struct.pack_into("<Q", eh, 32, 64)  # e_phoff — phdr right after ehdr
    struct.pack_into("<H", eh, 52, 64)  # e_ehsize
    struct.pack_into("<H", eh, 54, 56)  # e_phentsize
    struct.pack_into("<H", eh, 56, 1)  # e_phnum

    ph = bytearray(56)
    struct.pack_into("<I", ph, 0, reversing._GNU_STACK)  # p_type = GNU_STACK
    struct.pack_into("<I", ph, 4, 0x6)  # p_flags = R|W (no X) → NX on

    tail = (
        b"__stack_chk_fail\x00"  # canary symbol
        b"http://evil.example/c2\x00"
        b"flag{unit_test_flag}\x00"
        b"/bin/sh\x00"
        b"%n%n%n\x00"
        b"password=hunter2\x00"
    )
    return bytes(eh) + bytes(ph) + tail


async def _analyze(tmp_path, data: bytes, name="sample.bin"):
    p = tmp_path / name
    p.write_bytes(data)
    return await reversing.analyze_binary(str(p))


async def test_elf_header_and_mitigations(tmp_path):
    out = await _analyze(tmp_path, _make_elf())
    assert out["format"] == "ELF"
    elf = out["elf"]
    assert elf["bits"] == 64 and elf["endian"] == "little"
    assert elf["machine"] == "x86-64"
    assert elf["pie"] is True  # e_type DYN
    assert elf["nx"] is True  # GNU_STACK without X
    assert elf["relro"] == "none"
    assert elf["canary"] is True  # __stack_chk_fail present


async def test_interesting_strings_classified(tmp_path):
    out = await _analyze(tmp_path, _make_elf())
    i = out["interesting"]
    assert any("evil.example" in u for u in i["urls"])
    assert any("flag{" in f for f in i["flags"])
    assert any("/bin/sh" in e for e in i["exec_hints"])
    assert i["format_string"]  # %n picked up
    assert any("password" in s for s in i["secrets"])


async def test_notes_flag_format_string(tmp_path):
    out = await _analyze(tmp_path, _make_elf())
    joined = " ".join(out["notes"]).lower()
    assert "format-string" in joined
    assert "canary" not in joined or "no stack canary" not in joined  # canary IS present here


async def test_non_elf_script(tmp_path):
    out = await _analyze(tmp_path, b"#!/bin/bash\necho hi http://x.test/y\n", name="s.sh")
    assert out["format"] == "script" and "elf" not in out
    assert any("x.test" in u for u in out["interesting"].get("urls", []))


async def test_real_elf_interpreter():
    # sys.executable is a real ELF on the platforms this suite runs on
    out = await reversing.analyze_binary(sys.executable)
    assert out["format"] in ("ELF", "Mach-O", "PE/DOS")
    assert out["string_count"] > 0


def test_has_bind_now_detects_full_relro():
    pad = b"\x00" * 16  # off==0 means "no PT_DYNAMIC"; real offsets are nonzero
    # 64-bit dynamic array: DT_FLAGS(30) with DF_BIND_NOW(0x8), then DT_NULL
    dyn = pad + struct.pack("<QQ", 30, 0x8) + struct.pack("<QQ", 0, 0)
    assert reversing._has_bind_now(dyn, 16, len(dyn) - 16, 64, "<") is True
    # DT_BIND_NOW(24) tag on its own also counts
    dyn2 = pad + struct.pack("<QQ", 24, 0) + struct.pack("<QQ", 0, 0)
    assert reversing._has_bind_now(dyn2, 16, len(dyn2) - 16, 64, "<") is True
    # nothing eager → partial only
    dyn3 = pad + struct.pack("<QQ", 1, 0) + struct.pack("<QQ", 0, 0)  # DT_NEEDED then DT_NULL
    assert reversing._has_bind_now(dyn3, 16, len(dyn3) - 16, 64, "<") is False


async def test_missing_file():
    assert "error" in await reversing.analyze_binary("/no/such/file/here.bin")


async def test_no_path():
    assert "error" in await reversing.analyze_binary("")


def test_analyze_binary_is_passive():
    from friday.skills import registry

    registry.autodiscover()
    assert registry.get("analyze_binary").auto_confirm is True  # read-only → auto
