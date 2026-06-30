"""Binary triage / static reverse-engineering — the first look at an unknown executable.

analyze_binary identifies a file, parses its ELF header (class, type, machine, entry), runs a
checksec-style mitigation report (NX, PIE, RELRO, stack canary, stripped), and extracts + classifies
the interesting strings (URLs, IPs, exec hints, format-string sinks, embedded secrets, CTF flags).
The mitigation report tells you which memory-corruption primitives are in play before you ever open
a disassembler. Pure-stdlib (struct parsing, no deps) so it works offline/in CI with zero binaries;
enriched by `file` when it is installed. Read-only static analysis — never executes the target. $0.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import struct
from pathlib import Path
from typing import Any

from friday.skills.recon import _run  # shared async subprocess helper (returncode, stdout, stderr)
from friday.skills.registry import skill

_MAX_BYTES = 50_000_000  # don't slurp a multi-GB file into memory

_E_TYPE = {1: "REL (object)", 2: "EXEC", 3: "DYN (PIE/shared)", 4: "CORE"}
_E_MACHINE = {
    0x03: "x86",
    0x3E: "x86-64",
    0x28: "ARM",
    0xB7: "AArch64",
    0x08: "MIPS",
    0xF3: "RISC-V",
    0x14: "PowerPC",
}
_PT_DYNAMIC = 2
_GNU_STACK = 0x6474E551
_GNU_RELRO = 0x6474E552
_PF_X = 0x1
# Dynamic-section tags that mark full RELRO (eager binding → read-only GOT).
_DT_BIND_NOW = 24
_DT_FLAGS = 30
_DT_FLAGS_1 = 0x6FFFFFFB
_DF_BIND_NOW = 0x8
_DF_1_NOW = 0x1

# (bucket, compiled pattern) — what makes a string worth surfacing during triage.
_STRING_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("urls", re.compile(r"https?://[^\s'\"<>]{4,}")),
    ("ips", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("flags", re.compile(r"(?:flag|ctf|key)\{[^}]{0,80}\}", re.I)),
    ("exec_hints", re.compile(r"/bin/sh|/bin/bash|cmd\.exe|powershell|system|execve|popen|setuid")),
    ("format_string", re.compile(r"%n|%\d*\$?[ds]")),
    ("secrets", re.compile(r"password|passwd|secret|api[_-]?key|token|BEGIN [A-Z ]*PRIVATE", re.I)),
]


def _identify(data: bytes) -> str:
    """Coarse format from magic bytes."""
    if data[:4] == b"\x7fELF":
        return "ELF"
    if data[:2] == b"MZ":
        return "PE/DOS"
    if data[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
        return "Mach-O"
    if data[:2] == b"#!":
        return "script"
    if data[:4] == b"\x50\x4b\x03\x04":
        return "ZIP/JAR/APK"
    return "unknown"


def _has_bind_now(data: bytes, off: int, size: int, bits: int, endian: str) -> bool:
    """Scan the PT_DYNAMIC entries for eager binding (DT_BIND_NOW / BIND_NOW in DT_FLAGS[_1])."""
    if not off:
        return False
    word = "Q" if bits == 64 else "I"
    step = 16 if bits == 64 else 8
    val_off = 8 if bits == 64 else 4
    end = min(off + size, len(data))
    pos = off
    while pos + step <= end:
        tag = struct.unpack_from(endian + word, data, pos)[0]
        val = struct.unpack_from(endian + word, data, pos + val_off)[0]
        if tag == 0:  # DT_NULL — end of the dynamic array
            break
        if tag == _DT_BIND_NOW:
            return True
        if tag == _DT_FLAGS and val & _DF_BIND_NOW:
            return True
        if tag == _DT_FLAGS_1 and val & _DF_1_NOW:
            return True
        pos += step
    return False


def _parse_elf(data: bytes) -> dict[str, Any]:
    """ELF header + program-header mitigation parse (pure-stdlib, both 32- and 64-bit)."""
    if len(data) < 64:
        return {"error": "file too short to be a valid ELF"}
    bits = 64 if data[4] == 2 else 32
    endian = "<" if data[5] == 1 else ">"
    half = endian + "H"
    e_type = struct.unpack_from(half, data, 16)[0]
    e_machine = struct.unpack_from(half, data, 18)[0]
    if bits == 64:
        e_entry = struct.unpack_from(endian + "Q", data, 24)[0]
        e_phoff = struct.unpack_from(endian + "Q", data, 32)[0]
        e_phentsize = struct.unpack_from(half, data, 54)[0]
        e_phnum = struct.unpack_from(half, data, 56)[0]
        flags_off, ph_min = 4, 56
    else:
        e_entry = struct.unpack_from(endian + "I", data, 24)[0]
        e_phoff = struct.unpack_from(endian + "I", data, 28)[0]
        e_phentsize = struct.unpack_from(half, data, 42)[0]
        e_phnum = struct.unpack_from(half, data, 44)[0]
        flags_off, ph_min = 24, 32

    word = "Q" if bits == 64 else "I"
    off_field = 8 if bits == 64 else 4
    fsz_field = 32 if bits == 64 else 16

    nx: bool | None = None  # None = no GNU_STACK header (assume executable stack on old toolchains)
    relro = False
    dyn_off = dyn_sz = 0
    for i in range(min(e_phnum, 128)):
        base = e_phoff + i * e_phentsize
        if base + max(ph_min, flags_off + 4) > len(data):
            break
        p_type = struct.unpack_from(endian + "I", data, base)[0]
        p_flags = struct.unpack_from(endian + "I", data, base + flags_off)[0]
        if p_type == _GNU_STACK:
            nx = not (p_flags & _PF_X)
        elif p_type == _GNU_RELRO:
            relro = True
        elif p_type == _PT_DYNAMIC:
            dyn_off = struct.unpack_from(endian + word, data, base + off_field)[0]
            dyn_sz = struct.unpack_from(endian + word, data, base + fsz_field)[0]

    bind_now = relro and _has_bind_now(data, dyn_off, dyn_sz, bits, endian)
    relro_str = "full" if relro and bind_now else "partial" if relro else "none"

    return {
        "bits": bits,
        "endian": "little" if endian == "<" else "big",
        "type": _E_TYPE.get(e_type, f"0x{e_type:x}"),
        "machine": _E_MACHINE.get(e_machine, f"0x{e_machine:x}"),
        "entry": hex(e_entry),
        "pie": e_type == 3,
        "nx": nx,
        "relro": relro_str,
        "canary": b"__stack_chk_fail" in data,
    }


def _extract_strings(data: bytes, min_len: int = 4) -> list[str]:
    """ASCII + UTF-16LE printable runs, de-duplicated, order preserved."""
    found: list[str] = []
    seen: set[str] = set()
    for pat in (rb"[\x20-\x7e]{%d,}" % min_len, rb"(?:[\x20-\x7e]\x00){%d,}" % min_len):
        for m in re.finditer(pat, data):
            s = m.group().replace(b"\x00", b"").decode("ascii", "replace")
            if s not in seen:
                seen.add(s)
                found.append(s)
    return found


def _classify(strings: list[str]) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {b: [] for b, _ in _STRING_RULES}
    for s in strings:
        for bucket, pat in _STRING_RULES:
            if pat.search(s) and s not in buckets[bucket]:
                buckets[bucket].append(s)
    return {k: v[:15] for k, v in buckets.items() if v}  # cap each bucket


@skill(
    name="analyze_binary",
    description=(
        "Static reverse-engineering triage of an executable/library — the first look before a "
        "disassembler. Identifies the file, parses the ELF header (class/type/machine/entry), and "
        "runs a checksec-style mitigation report (NX, PIE, RELRO, stack canary, stripped) so you "
        "know which memory-corruption primitives apply. Also extracts and classifies the "
        "interesting strings: URLs, IPs, exec hints (/bin/sh, system), format-string sinks (%n), "
        "embedded secrets, and CTF flags. Pure-stdlib, works offline; read-only (never executes)."
    ),
    auto_confirm=True,  # read-only static analysis — never runs the target
    timeout_s=60,
)
async def analyze_binary(path: str, max_strings: int = 40) -> dict[str, Any]:
    """Triage a local binary statically.

    path: filesystem path to the binary/library/firmware blob.
    max_strings: how many raw strings to include in the sample (classification is always full).
    """
    path = (path or "").strip()
    if not path:
        return {"error": "no path given"}
    p = Path(path).expanduser()
    if not p.is_file():
        return {"error": f"not a file: {path}"}

    def _read() -> bytes:
        with p.open("rb") as fh:
            return fh.read(_MAX_BYTES)

    data = await asyncio.to_thread(_read)
    fmt = _identify(data)

    out: dict[str, Any] = {
        "path": str(p),
        "size": p.stat().st_size,
        "format": fmt,
    }

    if fmt == "ELF":
        out["elf"] = _parse_elf(data)

    # Enrich with `file` when present (human-readable type + reliable stripped detection).
    if shutil.which("file"):
        rc, fout, _ = await _run(["file", "-b", str(p)], timeout=15)
        if rc == 0 and fout.strip():
            desc = fout.strip()
            out["file_desc"] = desc[:300]
            if isinstance(out.get("elf"), dict):
                low = desc.lower()
                out["elf"]["stripped"] = (
                    True if "not stripped" not in low and "stripped" in low else False
                )

    strings = await asyncio.to_thread(_extract_strings, data)
    out["string_count"] = len(strings)
    out["interesting"] = _classify(strings)
    out["strings_sample"] = strings[: max(0, max_strings)]

    notes: list[str] = []
    elf = out.get("elf")
    if isinstance(elf, dict) and "error" not in elf:
        if elf.get("nx") is False:
            notes.append("Executable stack (NX off) — shellcode on the stack is viable.")
        if not elf.get("pie"):
            notes.append("No PIE — fixed load address, simplifies ROP/ret2.")
        if elf.get("relro") in ("none", "partial"):
            rel = elf["relro"]
            notes.append(f"{rel.title()} RELRO — GOT (.got.plt) is writable (GOT-overwrite).")
        if not elf.get("canary"):
            notes.append("No stack canary symbol — straight stack-buffer overflow may work.")
        if elf.get("stripped"):
            notes.append("Stripped — no symbols; lean on strings/xrefs.")
    if out["interesting"].get("format_string"):
        notes.append("Format-string specifiers present — check for a user-controlled printf.")
    out["notes"] = notes or ["No obvious quick wins from headers; dig into the disassembly."]
    return out
