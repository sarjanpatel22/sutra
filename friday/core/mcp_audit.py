"""MCP / agent-tool vulnerability scanner — static triage for the vuln classes that dominate the
MCP CVE wave (command injection, path traversal, SSRF, unsafe deserialization, code eval).

Heuristic + fast (no model call): walk a checkout, flag the dangerous SINKS where untrusted tool
input can reach a shell, the filesystem, the network, a deserializer, or an evaluator; rank by
severity; return structured findings (file, line, category, CWE, why, excerpt). Like the injection
detector, this NEVER claims a confirmed vuln — it surfaces CANDIDATES a human verifies against the
real tool wiring, then discloses responsibly. The point is BREADTH: point it at many in-scope
servers and let the paying bugs float up (the "ahead of everyone" play for the 5,000+ MCP servers,
most unaudited).

Detectors (language-aware where it matters, so a JS `exec` isn't confused with Python `exec`):
  - command_injection (CWE-78): shell exec reached by a DYNAMIC argument — the incomplete-blocklist
    allowlist bypass we proved live in a real MCP terminal server.
  - path_traversal (CWE-22): a file sink fed a DYNAMIC path with no confinement guard nearby.
  - ssrf (CWE-918): a server-side fetch of a DYNAMIC url with no scheme/host allowlist nearby.
  - unsafe_deserialization (CWE-502): pickle / yaml.load / torch.load / marshal / node-serialize.
  - code_eval (CWE-95): eval / exec / new Function / vm.run — arbitrary code from input.

Two ranking signals lift the paying bugs above the noise:
  - REACHABILITY: a finding is `reachable` when the sink sits inside an MCP tool handler and its
    dynamic argument is one of that handler's parameters — i.e. attacker-influenced input actually
    flows to the sink. This is what separates a real tool-exposed RCE from an `open(path)` deep in
    a library. `--reachable` filters to only these.
  - GUARD-BYPASS: a command sink protected by a hand-rolled blocklist of shell metacharacters is
    flagged HIGH with the specific characters the blocklist OMITS (command substitution `$(...)`,
    backticks, newline, `&`) — the exact bypass shape behind the real MCP command-injection CVEs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SRC_EXT = {".py", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx"}
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", "venv", "vendor",
              "site-packages", ".mypy_cache", ".pytest_cache", "test", "tests", "__tests__"}
_SEV = {"high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    category: str
    cwe: str
    severity: str
    sink: str
    excerpt: str
    note: str
    reachable: bool = False  # sink fed by an MCP tool-handler parameter (attacker-influenced input)


# ── sinks ────────────────────────────────────────────────────────────────────
# Shell execution — split by language so JS `exec` (child_process) and Python `exec` (code eval)
# aren't conflated.
_CMD_JS = re.compile(
    r"\b(?:child_process\.)?exec(?:Sync)?\s*\("
    r"|\b(?:spawn|spawnSync|execFile|execFileSync)\s*\([^)]*shell\s*:\s*true", re.I)
_CMD_PY = re.compile(
    r"\bos\.system\s*\(|\bos\.popen\s*\(|\bos\.exec\w*\s*\("
    r"|\bsubprocess\.(?:run|call|check_output|check_call|Popen)\s*\([^)]*shell\s*=\s*True"
    r"|\bcommands\.get(?:status)?output\s*\(|\bpty\.spawn\s*\(", re.I)
# A subprocess call OPENED on this line (its shell=True may live on a later line — real MCP servers
# format these across multiple lines, and same-line-only matching misses the injection sink).
_CMD_PY_OPEN = re.compile(r"\bsubprocess\.(?:run|call|check_output|check_call|Popen)\s*\(")
_SHELL_TRUE = re.compile(r"shell\s*=\s*True", re.I)
# Code evaluation.
_EVAL_JS = re.compile(
    r"\beval\s*\(|\bnew\s+Function\s*\(|\bvm\.(?:runIn\w+|compileFunction)\s*\(", re.I)
_EVAL_PY = re.compile(r"\beval\s*\(|\bexec\s*\(", re.I)
# File read/write by path (both languages).
_FILE = re.compile(
    r"\bfs\.(?:readFile|writeFile|readFileSync|writeFileSync|appendFile\w*|createReadStream"
    r"|createWriteStream|unlink\w*|readdir\w*|open\w*)\s*\("
    r"|\bopen\s*\(|\bPath\s*\([^)]*\)\.(?:read_text|read_bytes|write_text|write_bytes|open)"
    r"|\bsend_file\s*\(|\bshutil\.(?:copy\w*|move)\s*\(", re.I)
# Server-side fetch (SSRF).
_SSRF = re.compile(
    r"\brequests\.(?:get|post|put|patch|delete|head|request)\s*\("
    r"|\bhttpx\.(?:get|post|put|patch|delete|head|request|stream)\s*\("
    r"|\burllib\.request\.urlopen\s*\(|\burlopen\s*\(|\baiohttp\b[^;\n]*\.(?:get|post)\s*\("
    r"|\bfetch\s*\(|\baxios\.(?:get|post|put|delete|patch|request)\s*\(|\baxios\s*\("
    r"|\bhttp\.get\s*\(|\bhttps\.get\s*\(|\bgot\s*\(|\brequest\s*\(", re.I)
# Unsafe deserialization.
_DESERIAL = re.compile(
    r"\bc?[Pp]ickle\.loads?\s*\(|\b_pickle\.loads?\s*\(|\bmarshal\.loads?\s*\("
    r"|\bdill\.loads?\s*\(|\btorch\.load\s*\(|\bjoblib\.load\s*\(|\bjsonpickle\.decode\s*\("
    r"|\byaml\.load\s*\(|\bunserialize\s*\(|\bnode-serialize\b", re.I)

# A DYNAMIC argument anywhere on the line — variable / template literal / f-string / concat /
# a suggestive input name. Distinguishes exec(userInput) from exec("git --version").
_DYNAMIC = re.compile(
    r"\$\{|f['\"]|\.format\s*\(|%\s*[a-z(]|\+\s*[a-z_$\"'`]"
    r"|[a-z_$][\w.$\[\]]*\s*[,)]"
    r"|\b(?:command|cmd|input|payload|path|file|filename|filepath|dir|arg|args|user|param|params"
    r"|query|url|uri|name|data|body|req|request|target|host)\b", re.I)
# Confinement guard for a path sink (nearby → likely bounded, so lower severity — but verify it
# isn't bypassable; an incomplete guard is the payday).
_PATH_GUARD = re.compile(
    r"realpath|\.resolve\s*\(|resolve_path|relative_to|commonpath|commonprefix|is_relative_to"
    r"|normalize\s*\([^)]*\)[^;\n]{0,80}(?:startswith|startsWith)"
    r"|basename|secure_filename|safe_join|allow(?:ed)?[_ ]?list|whitelist", re.I)
# Guard for SSRF (allowlist / private-range / scheme check nearby).
_URL_GUARD = re.compile(
    r"allow(?:ed)?[_ ]?list|whitelist|is_private|ip_address|ipaddress|urlparse|urlsplit"
    r"|\.scheme\b|block(?:ed)?[_ ]?list|deny|SSRF|metadata|169\.254|localhost|127\.0", re.I)
# yaml.load is safe when a Safe loader is used.
_YAML_SAFE = re.compile(r"SafeLoader|safe_load|Loader\s*=\s*\w*(?:Safe|C?Safe)", re.I)

# A hand-rolled shell-metacharacter blocklist near a command sink (the `dangerous_operators = [...]`
# / `shellOperators = [...]` pattern). Its mere presence proves the argument is attacker-controlled;
# whether it's COMPLETE decides exploitability — an omission is the payday.
_BLOCKLIST_HINT = re.compile(
    r"(?:danger|black|deny|forbid|disallow|unsafe|blocked?|shell|meta|special|illegal|invalid)"
    r"\w*\s*[=:]\s*[\[(]|[\[(][^\]\n)]*(?:'&&'|\"&&\"|';'|\";\"|'\|\|'|'\|'|\"\|\")", re.I)
# The shell metacharacters that matter, and how each is written in a typical blocklist literal.
# If a blocklist exists but omits one of these, that character is a live injection vector.
_METACHARS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("command substitution `$(...)`", ("$(", "${", "$)")),
    ("backtick command substitution", ("`",)),
    ("newline", ("\\n", "\\r", "\\x0a")),
    ("background/chain `&`", ("&",)),
)


def _blocklist_gaps(window: str) -> list[str]:
    """If `window` contains a shell-metachar blocklist, return the high-impact characters it does
    NOT cover (each = a bypass). Empty list if there's no blocklist to reason about."""
    if not _BLOCKLIST_HINT.search(window):
        return []
    return [name for name, tokens in _METACHARS if not any(t in window for t in tokens)]


# ── tool-handler reachability ─────────────────────────────────────────────────
# Decorators / registrations that mark a function as an MCP tool handler (attacker-reachable).
_PY_TOOL_DECO = re.compile(r"@\s*[\w.]*\.?(?:tool|call_tool|list_tools|resource)\b", re.I)
_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+\w+\s*\((.*?)\)\s*(?:->.*)?:", re.S)
_PY_DEF_OPEN = re.compile(r"^(\s*)(?:async\s+)?def\s+\w+\s*\(")
# JS/TS tool registration or CallTool handler — reachability signal for those files.
_JS_TOOL = re.compile(
    r"\.(?:tool|registerTool)\s*\(|setRequestHandler\s*\(\s*CallToolRequest"
    r"|CallToolRequestSchema|name\s*===?\s*['\"]", re.I)


def _py_params(param_src: str) -> set[str]:
    """Parameter names from a Python def's parameter source (types/defaults stripped)."""
    names: set[str] = set()
    depth = 0
    cur = ""
    for ch in param_src:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            names.add(cur)
            cur = ""
        else:
            cur += ch
    names.add(cur)
    out: set[str] = set()
    for raw in names:
        n = raw.split(":")[0].split("=")[0].strip().lstrip("*").strip()
        if n and n.isidentifier() and n not in {"self", "cls", "ctx", "context"}:
            out.add(n)
    return out


def _is_py(path: str) -> bool:
    return path.endswith(".py")


def _excerpt(line: str) -> str:
    return line.strip()[:160]


def _dynamic(line: str) -> bool:
    return bool(_DYNAMIC.search(line))


def _paren_content(text: str) -> str:
    """The balanced content of the first (...) group in `text` (handles nested parens)."""
    start = text.find("(")
    if start < 0:
        return ""
    depth = 0
    for idx in range(start, len(text)):
        c = text[idx]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1: idx]
    return text[start + 1:]


def _py_tool_scopes(lines: list[str]) -> list[tuple[int, int, set[str]]]:
    """(first_body_line, last_body_line, param_names) for each @tool-decorated Python function —
    the scopes where a sink's argument being a parameter name means attacker-reachable."""
    scopes: list[tuple[int, int, set[str]]] = []
    n, i, pending = len(lines), 0, False
    while i < n:
        line = lines[i]
        if _PY_TOOL_DECO.search(line):
            pending, i = True, i + 1
            continue
        if _PY_DEF_OPEN.match(line):
            indent = len(line) - len(line.lstrip())
            sig, j = line, i
            while sig.count("(") > sig.count(")") and j < n - 1:
                j += 1
                sig += "\n" + lines[j]
            params = _py_params(_paren_content(sig))
            end, k = n, j + 1
            while k < n:
                t = lines[k]
                t_indent = len(t) - len(t.lstrip())
                if t.strip() and not t.lstrip().startswith("#") and t_indent <= indent:
                    end = k
                    break
                k += 1
            if pending and params:
                scopes.append((j + 2, end, params))  # body starts after the signature
            pending, i = False, j + 1
            continue
        if line.strip() and not line.lstrip().startswith("@"):
            pending = False
        i += 1
    return scopes


def _js_tool_params(source: str) -> set[str]:
    """Names an MCP client controls in a JS/TS tool file: args destructured from the request plus
    tool inputSchema property names. Empty if the file isn't a tool handler."""
    if not _JS_TOOL.search(source):
        return set()
    params: set[str] = set()
    for m in re.finditer(r"(?:const|let|var)\s*\{([^}]*)\}\s*=\s*[^;\n]*"
                         r"(?:args|arguments|params|request\.params)", source):
        params.update(re.findall(r"[A-Za-z_]\w*", m.group(1)))
    for m in re.finditer(r"async\s*\(\s*\{([^}]*)\}\s*\)\s*=>", source):
        params.update(re.findall(r"[A-Za-z_]\w*", m.group(1)))
    # inputSchema property names (properties: { file_name: {...}, latex_source: {...} })
    props = re.search(r"properties\s*:\s*\{(.*?)\n\s*\}", source, re.S)
    if props:
        params.update(re.findall(r"([A-Za-z_]\w*)\s*:\s*\{", props.group(1)))
    return params - {"type", "properties", "description", "required", "enum", "items"}


def scan_source(rel_path: str, source: str) -> list[Finding]:
    """Findings in one file's `source`. rel_path is only used for reporting."""
    lines = source.splitlines()
    py = _is_py(rel_path)
    scopes = _py_tool_scopes(lines) if py else []
    js_params = set() if py else _js_tool_params(source)
    out: list[Finding] = []

    def reach(i: int, line: str) -> bool:
        """True if a sink on line `i` is fed by a tool-handler parameter (attacker-reachable)."""
        params = js_params
        if py:
            params = set()
            for start, end, ps in scopes:
                if start <= i <= end:
                    params = ps
                    break
        return any(re.search(rf"\b{re.escape(p)}\b", line) for p in params)

    for i, line in enumerate(lines, 1):
        s = line.lstrip()
        if s.startswith(("#", "//", "*", "/*")):
            continue  # comment — skip to keep signal high
        win = "\n".join(lines[max(0, i - 3): i + 3])  # ±3 lines for guard proximity
        dyn = _dynamic(line)
        rch = reach(i, line)

        def add(cat: str, cwe: str, sev: str, sink: str, note: str, rb: bool = False) -> None:
            out.append(
                Finding(rel_path, i, cat, cwe, sev, sink[:44], _excerpt(line), note, rb))

        cmd = (_CMD_PY if py else _CMD_JS).search(line)
        cmd_win = line
        if not cmd and py and _CMD_PY_OPEN.search(line) and "shell" not in line:
            # multi-line subprocess call: shell=True on a later line within the same call
            fwd = "\n".join(lines[i - 1: i + 6])
            if _SHELL_TRUE.search(fwd):
                cmd, cmd_win = _CMD_PY_OPEN.search(line), fwd
        if cmd:
            dyn_cmd = dyn or (cmd_win is not line and _dynamic(cmd_win))
            rch = rch or (cmd_win is not line and reach(i, cmd_win))
            # A nearby hand-rolled shell-metachar blocklist proves the arg is attacker-controlled
            # and, if it has gaps, is bypassable — force HIGH and name the exact bypass characters.
            gaps = _blocklist_gaps("\n".join(lines[max(0, i - 9): i + 6]))
            sev = "high" if (dyn_cmd or gaps) else "medium"
            if gaps:
                note = ("shell metacharacter blocklist near this sink OMITS " + ", ".join(gaps) +
                        " — bypassable command injection (satisfy the blocklist, inject via the "
                        "omitted character)")
            elif dyn_cmd:
                note = ("dynamic argument reaches a shell — command injection if the input is "
                        "attacker-influenced; check any allowlist/blocklist for bypasses")
            else:
                note = "shell-execution sink — confirm the argument can't be attacker-controlled"
            add("command_injection", "CWE-78", sev, cmd.group(0), note, rch)

        ev = (_EVAL_PY if py else _EVAL_JS).search(line)
        if ev:
            add("code_eval", "CWE-95", "high" if dyn else "medium", ev.group(0),
                "code evaluated from a dynamic value — arbitrary code execution if it derives from "
                "input; a restricted-builtins sandbox here is usually escapable" if dyn else
                "code-evaluation sink — verify the evaluated string is a trusted constant", rch)

        de = _DESERIAL.search(line)
        if de and not (de.group(0).lower().startswith("yaml") and _YAML_SAFE.search(win)):
            add("unsafe_deserialization", "CWE-502", "high", de.group(0),
                "untrusted deserialization — loading attacker-controlled serialized data is "
                "remote code execution; confirm the source is trusted or use a safe loader", rch)

        fm = _FILE.search(line)
        if fm and dyn:
            guarded = bool(_PATH_GUARD.search(win))
            add("path_traversal", "CWE-22", "medium" if guarded else "high", fm.group(0),
                "file path from input WITH a confinement guard — verify it can't be bypassed "
                "(`..`, symlink, absolute path)" if guarded else
                "file path from input with NO confinement guard — path traversal / arbitrary "
                "file read-write candidate", rch)

        sm = _SSRF.search(line)
        if sm and dyn and not (_EVAL_JS.search(line) or cmd):
            guarded = bool(_URL_GUARD.search(win))
            add("ssrf", "CWE-918", "low" if guarded else "medium", sm.group(0),
                "server-side fetch of a URL — often intended; a finding only if it reaches "
                "internal hosts / cloud metadata. " + ("An allowlist is present — check it covers "
                "redirects + DNS rebinding." if guarded else "No host allowlist / private-range "
                "check nearby."), rch)
    return out


def scan_repo(root: str | Path) -> list[Finding]:
    """Scan every source file under `root` (skipping deps/build/vcs/test dirs). Findings sorted
    most-severe first, then by file/line."""
    root = Path(root)
    findings: list[Finding] = []
    for p in root.rglob("*"):
        if p.is_dir() or p.suffix.lower() not in _SRC_EXT:
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            src = p.read_text("utf-8", "replace")
        except OSError:
            continue
        findings.extend(scan_source(str(p.relative_to(root)), src))
    # most-severe first; within a severity, tool-reachable (attacker-influenced) findings float up.
    findings.sort(key=lambda f: (-_SEV.get(f.severity, 0), not f.reachable, f.file, f.line))
    return findings


def audit_report(root: str | Path) -> dict[str, object]:
    """Structured audit summary for the CLI / skill: counts + the ranked findings."""
    findings = scan_repo(root)
    by_cat: dict[str, int] = {}
    for f in findings:
        by_cat[f.category] = by_cat.get(f.category, 0) + 1
    return {
        "root": str(root),
        "total": len(findings),
        "high": sum(1 for f in findings if f.severity == "high"),
        "reachable": sum(1 for f in findings if f.reachable),
        "by_category": by_cat,
        "findings": [
            {"file": f.file, "line": f.line, "category": f.category, "cwe": f.cwe,
             "severity": f.severity, "reachable": f.reachable, "sink": f.sink,
             "excerpt": f.excerpt, "note": f.note}
            for f in findings
        ],
    }
