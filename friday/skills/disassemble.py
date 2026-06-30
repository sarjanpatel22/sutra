"""Disassembly + call-site vuln triage — the step after analyze_binary, before the debugger.

disassemble wraps objdump to dump a binary's disassembly (optionally scoped to one function) and,
in the same pass, flags call sites to dangerous libc functions — the memory-corruption and
command-injection sinks (gets/strcpy/sprintf/system/exec*/scanf …). That turns a wall of assembly
into a short list of "look here" addresses: each flagged call is a candidate overflow or injection
to trace back to attacker-controlled input. Read-only (objdump never executes the target). Degrades
gracefully when binutils is absent. $0.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from friday.skills.recon import _run  # shared async subprocess helper (returncode, stdout, stderr)
from friday.skills.registry import skill

# function name -> (severity, why). Matched against call targets in the disassembly.
_DANGEROUS: dict[str, tuple[str, str]] = {
    "gets": ("high", "no bounds check at all — classic stack overflow"),
    "strcpy": ("high", "unbounded copy — overflow if source > dest"),
    "strcat": ("high", "unbounded concat — overflow"),
    "sprintf": ("high", "unbounded format into a buffer — overflow"),
    "vsprintf": ("high", "unbounded format into a buffer — overflow"),
    "scanf": ("medium", "%s with no width → overflow"),
    "sscanf": ("medium", "%s with no width → overflow"),
    "fscanf": ("medium", "%s with no width → overflow"),
    "system": ("high", "shell exec — command injection if the arg is tainted"),
    "popen": ("high", "shell exec — command injection if the arg is tainted"),
    "execve": ("high", "process exec — argument injection if tainted"),
    "execlp": ("high", "PATH-resolved exec — hijack/injection if tainted"),
    "execvp": ("high", "PATH-resolved exec — hijack/injection if tainted"),
    "memcpy": ("medium", "overflow if the length is attacker-controlled"),
    "alloca": ("medium", "stack allocation from a tainted size — stack clash"),
    "strncpy": ("low", "no NUL-termination guarantee — off-by-one / missing terminator"),
}

# objdump call line, e.g. "  1162: e8 d9 fe ff ff  call  1040 <gets@plt>"
_CALL_RE = re.compile(r"\bcall\w*\s+[0-9a-fA-F]+\s+<([^>@\s]+)(@plt)?>")
# block header, e.g. "0000000000001149 <main>:"
_FUNC_HDR_RE = re.compile(r"^[0-9a-fA-F]+\s+<([^>]+)>:")


def _filter_function(text: str, fn: str) -> str:
    """Return only the disassembly block for function `fn` (header line to the next blank line)."""
    lines = text.splitlines()
    out: list[str] = []
    capturing = False
    for ln in lines:
        m = _FUNC_HDR_RE.match(ln)
        if m:
            capturing = m.group(1) == fn or m.group(1) == f"{fn}@plt"
            if capturing:
                out.append(ln)
            continue
        if capturing:
            if ln.strip() == "":  # blank line ends the function block
                break
            out.append(ln)
    return "\n".join(out)


def _dangerous_calls(text: str) -> list[dict[str, Any]]:
    """Scan disassembly for call sites to dangerous functions, with the call address."""
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ln in text.splitlines():
        m = _CALL_RE.search(ln)
        if not m:
            continue
        name = m.group(1)
        if name not in _DANGEROUS:
            continue
        addr = ln.strip().split(":", 1)[0].strip()
        key = (name, addr)
        if key in seen:
            continue
        seen.add(key)
        sev, why = _DANGEROUS[name]
        findings.append({"function": name, "severity": sev, "address": addr, "why": why})
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return findings


@skill(
    name="disassemble",
    description=(
        "Disassemble a binary with objdump and flag dangerous call sites in one pass — the triage "
        "step after analyze_binary. Dumps the disassembly (optionally just one function via "
        "function=) and reports every call to a memory-corruption or command-injection sink "
        "(gets/strcpy/sprintf/system/exec*/scanf/memcpy …) with its address, so a wall of assembly "
        "becomes a short list of overflow/injection candidates to trace to attacker input. "
        "Read-only (never executes the target); needs binutils (objdump). $0."
    ),
    auto_confirm=True,  # read-only static disassembly — never runs the target
    timeout_s=90,
)
async def disassemble(path: str, function: str = "", max_lines: int = 250) -> dict[str, Any]:
    """Disassemble a local binary and flag dangerous call sites.

    path: filesystem path to the binary/object/library.
    function: optional symbol name to scope the disassembly to (e.g. "main").
    max_lines: cap on disassembly lines returned (the vuln scan always runs over the full output).
    """
    path = (path or "").strip()
    if not path:
        return {"error": "no path given"}
    p = Path(path).expanduser()
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    if not shutil.which("objdump"):
        return {"error": "objdump not found — install binutils (e.g. apt install binutils)"}

    rc, out, err = await _run(["objdump", "-d", "-M", "intel", str(p)], timeout=80)
    if rc != 0 or not out.strip():
        msg = (err or "").strip() or "objdump produced no output"
        return {"error": f"objdump failed: {msg[:200]}", "path": str(p)}

    # vuln scan runs on the FULL disassembly; the returned text may be scoped/capped.
    findings = _dangerous_calls(out)

    scoped = out
    not_found = False
    if function.strip():
        block = _filter_function(out, function.strip())
        if block:
            scoped = block
        else:
            not_found = True

    lines = scoped.splitlines()
    truncated = len(lines) > max_lines
    disasm = "\n".join(lines[: max(0, max_lines)])

    note = (
        "Each flagged call is a candidate — trace its argument back to attacker-controlled input "
        "(argv, env, file, network) to confirm exploitability before reporting."
    )
    if not findings:
        note = "No dangerous-call sites found (still review custom parsing/memory logic by hand)."
    if not_found:
        note = f"Function '{function}' not found; showing full disassembly. " + note

    return {
        "path": str(p),
        "scoped_to": function.strip() or "(whole binary)",
        "function_found": not not_found,
        "dangerous_calls": findings,
        "vulnerable_surface": bool(findings),
        "disassembly": disasm,
        "truncated": truncated,
        "note": note,
    }
