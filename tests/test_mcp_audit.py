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


def test_db_exec_is_not_command_injection() -> None:
    # `.exec()` on a DB/connection receiver is SQL execution, not child_process — must NOT be CWE-78
    for code in ["db.exec(query)", "this.db!.exec(query, cb)", "return dbAdapter.exec(query);",
                 "await pool.exec(sql)", "connection.exec(q)"]:
        assert "command_injection" not in _cats("s.ts", code), code
    # but a real child_process exec (bare or child_process.) is still flagged
    assert "command_injection" in _cats("s.js", "child_process.exec(cmd)")
    assert "command_injection" in _cats("s.js", "const out = exec(userCmd)")


def test_ts_method_signature_not_command_injection() -> None:
    # a typed-parameter `exec(query: string)` is an interface/method DEFINITION, not a call
    assert "command_injection" not in _cats("s.ts", "exec(query: string): Promise<void>;")
    assert "command_injection" not in _cats("s.ts", "  async exec(sql: string): Promise<void> {")


def test_d_ts_declaration_files_skipped(tmp_path) -> None:
    (tmp_path / "server.ts").write_text("exec(userCmd)\n")     # real sink
    (tmp_path / "global.d.ts").write_text("exec(sql: string): this;\n")  # declaration — skip
    files = {f.file for f in M.scan_repo(tmp_path)}
    assert "server.ts" in files
    assert "global.d.ts" not in files


def test_test_files_and_minified_bundles_skipped(tmp_path) -> None:
    (tmp_path / "server.py").write_text("os.system(user_cmd)\n")       # real
    (tmp_path / "layer_test.py").write_text("pickle.loads(blob)\n")    # test file — skip
    (tmp_path / "app.spec.ts").write_text("exec(userInput)\n")         # spec — skip
    (tmp_path / "yarn.cjs").write_text("var x=" + "a;" * 2000 + "\n")  # minified (huge line) — skip
    files = {f.file for f in M.scan_repo(tmp_path)}
    assert "server.py" in files
    assert files == {"server.py"}


# ── path traversal ─────────────────────────────────────────────────────────────
def test_path_traversal_flagged_high_without_guard() -> None:
    assert _cats("s.py", "open(user_path).read()")["path_traversal"] == "high"
    assert _cats("s.js", "fs.readFileSync(req.query.file)")["path_traversal"] == "high"


def test_path_traversal_guard_downgrades_to_medium() -> None:
    code = "p = (root / name).resolve()\np.relative_to(root)\nopen(p).read()"
    assert _cats("s.py", code)["path_traversal"] == "medium"


def test_path_literal_not_flagged() -> None:
    assert "path_traversal" not in _cats("s.js", 'fs.readFileSync("./package.json")')


def test_path_dotdot_string_check_is_high_bypassable() -> None:
    # a `..` substring check is NOT real confinement — must stay HIGH and say why
    code = "if '..' in name: raise ValueError\nopen(name).read()"
    f = next(x for x in M.scan_source("s.py", code) if x.category == "path_traversal")
    assert f.severity == "high" and "bypassable" in f.note.lower()


def test_path_strong_guard_beats_dotdot_check() -> None:
    # a `..` check alongside a real resolve()+relative_to is confinement → medium, not high
    code = ("if '..' in name: raise\n"
            "p = (root / name).resolve()\n"
            "p.relative_to(root)\n"
            "open(p).read()")
    assert _cats("s.py", code)["path_traversal"] == "medium"


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


def test_method_eval_is_not_code_eval() -> None:
    # PyTorch `model.eval()` (eval-mode) and other `.eval()`/`.exec()` methods are not the builtin
    for code in ["model.eval()", "self.model.eval()", "net.eval()", "stmt.exec()"]:
        assert "code_eval" not in _cats("s.py", code), code


def test_def_eval_is_not_code_eval() -> None:
    # `def eval(x):` defines a same-named function; it is not a call to the eval builtin
    assert "code_eval" not in _cats("s.py", "def eval(x): return x")
    assert "code_eval" not in _cats("s.js", "function exec(cmd) { return cmd }")


def test_torch_load_weights_only_is_safe() -> None:
    assert "unsafe_deserialization" not in _cats(
        "s.py", "loaded = torch.load(path, map_location='cpu', weights_only=True)")
    # without the guard it is still flagged
    assert _cats("s.py", "loaded = torch.load(path)")["unsafe_deserialization"] == "high"


# ── sql injection ──────────────────────────────────────────────────────────────
def test_sql_injection_fstring_and_concat_high() -> None:
    assert _cats("s.py", 'cursor.execute(f"SELECT * FROM t WHERE id = {uid}")')["sql_injection"] \
        == "high"
    assert _cats("s.js", "db.query(`SELECT * FROM users WHERE n = ${name}`)")["sql_injection"] \
        == "high"
    assert _cats("s.py", 'cur.execute("SELECT * FROM t WHERE id = " + user_id)')["sql_injection"] \
        == "high"
    assert _cats("s.js", "knex.raw(`DROP TABLE ${t}`)")["sql_injection"] == "high"


def test_sql_parameterized_not_flagged() -> None:
    # placeholders + bound params (no interpolation into the string) = safe
    for code in ['cursor.execute("SELECT * FROM t WHERE id = ?", (uid,))',
                 'cur.execute("SELECT * FROM t WHERE id = %s", (uid,))',
                 'db.query("SELECT * FROM t WHERE id = $1", [id])']:
        assert "sql_injection" not in _cats("s.py", code), code


def test_sql_db_exec_dynamic_is_sqli_not_command() -> None:
    cats = _cats("s.ts", "db.exec(`INSERT INTO t VALUES (${v})`)")
    assert cats.get("sql_injection") == "high"
    assert "command_injection" not in cats


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


def test_reachability_multi_hop_python() -> None:
    # param → derived local → sink: reachability must follow the taint, not just direct params
    code = ("@mcp.tool()\n"
            "def convert(name: str):\n"
            "    tex_path = OUT_DIR + '/' + name.replace('.pdf', '.tex')\n"
            "    command = 'latexmk ' + tex_path\n"
            "    os.system(command)\n")
    f = next(x for x in M.scan_source("s.py", code) if x.category == "command_injection")
    assert f.reachable is True


def test_reachability_multi_hop_js_latexpdf_shape() -> None:
    # the real LateXPDF-MCP shape: file_name (tool arg) → latexFilePath → command → exec
    code = ("server.setRequestHandler(CallToolRequestSchema, async (req) => {\n"
            "  const { file_name } = req.params.arguments;\n"
            "  const latexFilePath = `${OUT}/${file_name.replace(/\\.pdf$/, '.tex')}`;\n"
            '  const command = `latexmk --outdir="${OUT}" "${latexFilePath}"`;\n'
            "  exec(command, (e, out) => {});\n"
            "});\n")
    f = next(x for x in M.scan_source("index.ts", code) if x.category == "command_injection")
    assert f.reachable is True


def test_taint_does_not_leak_to_unrelated_local() -> None:
    code = ("@mcp.tool()\n"
            "def t(name: str):\n"
            "    safe = 'constant string'\n"
            "    open(safe)\n")
    f = next((x for x in M.scan_source("s.py", code) if x.category == "path_traversal"), None)
    assert f is None or f.reachable is False


def test_audit_report_exposes_reachable_count(tmp_path) -> None:
    (tmp_path / "srv.py").write_text(
        "@mcp.tool()\ndef t(path: str):\n    return open(path).read()\n")
    rep = M.audit_report(tmp_path)
    assert rep["reachable"] >= 1
    assert all("reachable" in f for f in rep["findings"])


# ── batch funnel: dedup + ranking ───────────────────────────────────────────────
def test_is_known_vuln_flags_dups() -> None:
    assert M.is_known_vuln("https://github.com/zcaceres/markdownify-mcp")
    assert M.is_known_vuln("mcpvault")
    assert M.is_known_vuln("https://github.com/RinardNick/mcp-terminal.git")
    assert not M.is_known_vuln("https://github.com/some/totally-fresh-server")


def test_rank_batch_promotes_fresh_reachable_high() -> None:
    fresh = {"high": 1, "reachable": 1, "by_category": {"command_injection": 1},
             "findings": [{"severity": "high", "reachable": True}]}
    noisy = {"high": 9, "reachable": 0, "by_category": {"path_traversal": 9},
             "findings": [{"severity": "high", "reachable": False} for _ in range(9)]}
    dup = {"high": 5, "reachable": 5, "by_category": {"command_injection": 5},
           "findings": [{"severity": "high", "reachable": True} for _ in range(5)]}
    rows = M.rank_batch([("noisy-lib", noisy), ("markdownify-mcp", dup), ("fresh-srv", fresh)])
    # fresh repo with a reachable HIGH ranks first; the known-vuln dup sinks to last despite 5 hits
    assert rows[0]["repo"] == "fresh-srv"
    assert rows[-1]["repo"] == "markdownify-mcp"
    assert rows[0]["reachable_high"] == 1 and rows[-1]["known_vuln"] is True
