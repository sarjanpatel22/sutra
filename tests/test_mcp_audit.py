"""Tests for friday.core.mcp_audit — the MCP / agent-tool vulnerability scanner.

Beyond per-detector unit checks, two tests are REGRESSION ANCHORS: they encode the exact patterns
of the two real bugs we confirmed live (the mcp-terminal command-injection allowlist bypass and the
CrewAI restricted-`exec` sandbox escape). If a detector change stops flagging either, CI fails.
"""

from __future__ import annotations

from friday.core import mcp_audit as M


def _cats(fn: str, code: str) -> dict[str, str]:
    """{category: severity} for a one/few-line snippet."""
    return {f.category: f.severity for f in M.scan_source(fn, code)}


# ── command injection (language-aware) ─────────────────────────────────────────
def test_command_injection_js_dynamic_is_high() -> None:
    assert _cats("s.js", "const out = execSync(userCmd);")["command_injection"] == "high"
    assert _cats("s.js", "spawn(cmd, [], { shell: true })")["command_injection"] == "high"


def test_command_injection_py_dynamic_is_high() -> None:
    assert _cats("s.py", "subprocess.run(cmd, shell=True)")["command_injection"] == "high"
    assert _cats("s.py", 'os.popen(f"ping {host}")')["command_injection"] == "high"


def test_command_literal_args_not_high() -> None:
    # a hardcoded command is at most medium (still a sink), never high
    assert _cats("s.py", 'subprocess.run(["ls", "-la"])') == {}          # no shell=True → no sink
    assert _cats("s.js", 'execSync("git --version")').get("command_injection") == "medium"


def test_language_awareness_exec() -> None:
    # Python exec is CODE EVAL; JS exec is a child_process COMMAND — never conflated
    assert "code_eval" in _cats("s.py", "exec(code)")
    assert "command_injection" in _cats("s.js", "exec(userCmd)")
    assert "command_injection" not in _cats("s.py", "exec(code)")


# ── path traversal ─────────────────────────────────────────────────────────────
def test_path_traversal_flagged_high_without_guard() -> None:
    assert _cats("s.py", "open(user_path).read()")["path_traversal"] == "high"
    assert _cats("s.js", "fs.readFileSync(req.query.file)")["path_traversal"] == "high"


def test_path_traversal_guard_downgrades_to_medium() -> None:
    code = "p = (root / name).resolve()\np.relative_to(root)\nopen(p).read()"
    assert _cats("s.py", code)["path_traversal"] == "medium"


def test_path_literal_not_flagged() -> None:
    assert "path_traversal" not in _cats("s.js", 'fs.readFileSync("./package.json")')


# ── ssrf ───────────────────────────────────────────────────────────────────────
def test_ssrf_flagged_medium() -> None:
    assert _cats("s.py", "r = requests.get(user_url)")["ssrf"] == "medium"
    assert _cats("s.js", "await fetch(userUrl)")["ssrf"] == "medium"


def test_ssrf_guard_downgrades_to_low() -> None:
    code = "if is_private(ip): raise\nr = requests.get(user_url)"
    assert _cats("s.py", code)["ssrf"] == "low"


# ── unsafe deserialization ─────────────────────────────────────────────────────
def test_deserialization_pickle_and_yaml_high() -> None:
    assert _cats("s.py", "obj = pickle.loads(data)")["unsafe_deserialization"] == "high"
    assert _cats("s.py", "cfg = yaml.load(text)")["unsafe_deserialization"] == "high"
    assert _cats("s.py", "m = torch.load(path)")["unsafe_deserialization"] == "high"


def test_yaml_safe_loader_not_flagged() -> None:
    assert "unsafe_deserialization" not in _cats("s.py", "yaml.load(t, Loader=yaml.SafeLoader)")
    assert "unsafe_deserialization" not in _cats("s.py", "yaml.safe_load(t)")


def test_json_loads_not_deserialization() -> None:
    assert "unsafe_deserialization" not in _cats("s.py", "obj = json.loads(data)")


# ── code eval ──────────────────────────────────────────────────────────────────
def test_code_eval_flagged() -> None:
    assert _cats("s.py", "result = eval(expr)")["code_eval"] == "high"
    assert _cats("s.js", "const r = new Function(body)()")["code_eval"] == "high"


def test_evaluate_is_not_eval() -> None:
    # a function called `evaluate(` must not trip the `eval(` sink
    assert "code_eval" not in _cats("s.js", "const x = evaluate(data)")


# ── hygiene ────────────────────────────────────────────────────────────────────
def test_clean_code_no_findings() -> None:
    for code in ["total = a + b", "return sum(rows)", 'name = "espresso"',
                 "def open_connection(): pass", "system_info = {}"]:
        assert M.scan_source("s.py", code) == []


def test_comment_lines_skipped() -> None:
    assert M.scan_source("s.py", "# os.system(user_cmd) example in a comment") == []
    assert M.scan_source("s.js", "// exec(userCmd) — documented, not called") == []


def test_cwe_tags_present() -> None:
    f = M.scan_source("s.py", "os.system(cmd)")[0]
    assert f.cwe == "CWE-78" and f.category == "command_injection"


# ── repo walk + report ─────────────────────────────────────────────────────────
def test_scan_repo_walks_and_skips_deps(tmp_path) -> None:
    (tmp_path / "server.py").write_text("os.system(user_cmd)\n")
    dep = tmp_path / "node_modules" / "x"
    dep.mkdir(parents=True)
    (dep / "evil.js").write_text("exec(userInput)\n")   # in node_modules → must be skipped
    findings = M.scan_repo(tmp_path)
    files = {f.file for f in findings}
    assert "server.py" in files
    assert not any("node_modules" in f for f in files)


def test_audit_report_structure(tmp_path) -> None:
    (tmp_path / "a.py").write_text("os.system(cmd)\npickle.loads(data)\n")
    rep = M.audit_report(tmp_path)
    assert rep["total"] == 2 and rep["high"] == 2
    assert rep["by_category"]["command_injection"] == 1
    assert all({"file", "line", "category", "cwe", "severity"} <= set(f) for f in rep["findings"])


# ── REGRESSION ANCHORS: the two real bugs we confirmed live ─────────────────────
_MCP_TERMINAL = """
function validate(command, allowedCommands) {
  const parts = command.split(/\\s+/);
  const baseCmd = parts[0];
  const shellOperators = ['&&', '||', '|', ';', '`'];
  if (shellOperators.some((op) => command.includes(op))) throw new Error('no');
  if (!allowedCommands.includes(baseCmd)) throw new Error('no');
}
const proc = spawn(command, [], { shell: true });
"""


def test_refinds_mcp_terminal_command_injection() -> None:
    """The real @rinardnick/mcp-terminal bug: first-token allowlist + incomplete shell-operator
    blocklist + spawn(shell:true). Must surface as a HIGH command injection."""
    cats = _cats("index.ts", _MCP_TERMINAL)
    assert cats.get("command_injection") == "high"


_CREWAI_SANDBOX = """
def run(code, locals):
    exec(code, {"__builtins__": safe_builtins()}, locals)
    return locals.get("result")
"""


def test_refinds_crewai_sandbox_escape() -> None:
    """The real CrewAI CodeInterpreterTool bug: a restricted-builtins exec() run on agent-supplied
    code. Must surface as a HIGH code_eval."""
    cats = _cats("tool.py", _CREWAI_SANDBOX)
    assert cats.get("code_eval") == "high"


# ── guard-bypass: incomplete shell-metacharacter blocklist ─────────────────────
_CODE_MCP_RUN = """
@mcp.tool()
def run_command(ctx, command: str) -> str:
    dangerous_operators = [';', '&&', '||', '>', '>>', '|', '<']
    if any(op in command for op in dangerous_operators):
        return "Error: dangerous operator"
    process = subprocess.Popen(command, shell=True, cwd=PROJECT_ROOT)
"""


def test_guard_bypass_incomplete_blocklist_is_high_and_names_gaps() -> None:
    """A hand-rolled blocklist that omits command substitution / backticks / newline must be flagged
    HIGH with the omitted characters spelled out — the real MCP command-injection bypass shape."""
    f = next(x for x in M.scan_source("server.py", _CODE_MCP_RUN)
             if x.category == "command_injection")
    assert f.severity == "high"
    assert "OMITS" in f.note
    assert "$(" in f.note and "backtick" in f.note and "newline" in f.note


def test_multiline_subprocess_shell_true_is_detected() -> None:
    """Real MCP servers format subprocess calls across lines; a same-line-only matcher misses the
    injection sink. shell=True on a later line of the same call must still flag HIGH."""
    code = ("@mcp.tool()\n"
            "def run(command: str):\n"
            "    process = subprocess.Popen(\n"
            "        command,\n"
            "        shell=True,\n"
            "        cwd=ROOT,\n"
            "    )\n")
    f = next(x for x in M.scan_source("s.py", code) if x.category == "command_injection")
    assert f.severity == "high" and f.reachable is True


def test_multiline_subprocess_list_args_not_flagged() -> None:
    # a multi-line call WITHOUT shell=True (safe list argv) must stay clean
    code = ("subprocess.run(\n"
            '    ["ls", "-la"],\n'
            "    capture_output=True,\n"
            ")\n")
    assert not [x for x in M.scan_source("s.py", code) if x.category == "command_injection"]


def test_complete_blocklist_has_no_omit_note() -> None:
    """A blocklist that DOES cover $(, backtick, newline and & must not claim a bypass gap."""
    code = (
        "def run(command):\n"
        "    bad = ['$(', '`', '\\n', '&', ';', '|']\n"
        "    if any(b in command for b in bad): return 'no'\n"
        "    subprocess.Popen(command, shell=True)\n"
    )
    f = next(x for x in M.scan_source("s.py", code) if x.category == "command_injection")
    assert "OMITS" not in f.note


# ── reachability: sink fed by an MCP tool-handler parameter ─────────────────────
def test_reachability_python_tool_param() -> None:
    code = ("@mcp.tool()\n"
            "async def read_file(path: str) -> str:\n"
            "    with open(path) as f:\n"
            "        return f.read()\n")
    f = next(x for x in M.scan_source("s.py", code) if x.category == "path_traversal")
    assert f.reachable is True


def test_reachability_python_non_handler_not_reachable() -> None:
    # same sink, but the enclosing function is a plain helper (no @tool) → not attacker-reachable
    code = ("def _helper(internal):\n"
            "    with open(internal) as f:\n"
            "        return f.read()\n")
    f = next(x for x in M.scan_source("s.py", code) if x.category == "path_traversal")
    assert f.reachable is False


def test_reachability_js_destructured_tool_arg() -> None:
    code = ('server.tool("run", schema, async ({ target }) => {\n'
            "  const out = execSync(target);\n"
            "  return out;\n"
            "});\n")
    f = next(x for x in M.scan_source("s.js", code) if x.category == "command_injection")
    assert f.reachable is True and f.severity == "high"


def test_audit_report_exposes_reachable_count(tmp_path) -> None:
    (tmp_path / "srv.py").write_text(
        "@mcp.tool()\ndef t(path: str):\n    return open(path).read()\n")
    rep = M.audit_report(tmp_path)
    assert rep["reachable"] >= 1
    assert all("reachable" in f for f in rep["findings"])
