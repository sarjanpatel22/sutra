"""Accuracy benchmark for the MCP vuln scanner — a CI-enforced quality gate.

Recall on a corpus of known-vulnerable sink snippets (all five classes, both languages) + a
false-positive rate on benign snippets, including HARD negatives that contain sink-ish words but are
safe (evaluate() vs eval(), safe_load, list-arg subprocess, literal paths, comments). Thresholds
leave headroom below the current results so it isn't brittle, while still catching a regression.
"""

from __future__ import annotations

from friday.core.mcp_audit import scan_source

# Vulnerable — each MUST produce at least one finding.
POSITIVES = [
    ("s.js", "const out = exec(userInput);"),
    ("s.js", "execSync(`git clone ${repo}`);"),
    ("s.js", "spawn(userCmd, [], { shell: true });"),
    ("s.js", "child_process.exec(payload);"),
    ("s.py", "os.system(cmd)"),
    ("s.py", "subprocess.Popen(command, shell=True)"),
    ("s.py", 'os.popen(f"nslookup {host}")'),
    ("s.js", "fs.readFileSync(req.params.file)"),
    ("s.js", "fs.writeFile(userPath, data, cb)"),
    ("s.py", "open(user_supplied_path)"),
    ("s.py", "Path(base_dir + name).read_text()"),
    ("s.py", "shutil.copy(src, dest)"),
    ("s.py", "r = requests.get(target_url)"),
    ("s.js", "await axios.get(userUrl)"),
    ("s.py", "urllib.request.urlopen(url)"),
    ("s.py", "obj = pickle.loads(blob)"),
    ("s.py", "cfg = yaml.load(content)"),
    ("s.py", "model = torch.load(model_path)"),
    ("s.py", "d = marshal.loads(data)"),
    ("s.js", "const o = unserialize(input)"),
    ("s.py", "value = eval(user_expr)"),
    ("s.py", "exec(snippet)"),
    ("s.js", "const fn = new Function(userBody)"),
    ("s.js", "vm.runInNewContext(code)"),
    ("s.py", 'cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")'),
    ("s.py", 'cur.execute("DELETE FROM t WHERE name = " + name)'),
    ("s.js", "db.query(`SELECT * FROM t WHERE email = ${email}`)"),
    ("s.js", "knex.raw(`UPDATE t SET x = ${v}`)"),
]

# Benign — each must produce NO findings. Several are HARD (contain trigger words but are safe).
NEGATIVES = [
    ("s.py", 'subprocess.run(["ls", "-la"])'),
    ("s.js", 'execFileSync("git", ["log", "-1"])'),
    ("s.py", "cfg = yaml.safe_load(text)"),
    ("s.py", "cfg = yaml.load(text, Loader=yaml.SafeLoader)"),
    ("s.py", "data = json.loads(payload)"),
    ("s.py", 'open("config.json")'),
    ("s.js", 'fs.readFileSync("./package.json", "utf8")'),
    ("s.js", "const x = evaluate(input)"),
    ("s.py", "def open_file(): pass"),
    ("s.py", "system_status = check_health()"),
    ("s.py", "result = calculate(a, b)"),
    ("s.py", 'logger.info(f"user {name} logged in")'),
    ("s.py", "total = price * qty"),
    ("s.py", "requests_made += 1"),
    ("s.py", "self.command = command"),
    ("s.py", "# eval(x) is dangerous — never do this"),
    ("s.py", "loads = pickle_safe(data)"),
    ("s.py", "return template.format(name=name)"),
    ("s.js", "const u = new URL(base)"),
    ("s.py", 'path = os.path.join(BASE, "static", fname)'),
    ("s.py", 'cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))'),
    ("s.py", 'cur.execute("INSERT INTO t VALUES (%s, %s)", (a, b))'),
    ("s.js", "db.query('SELECT * FROM t WHERE id = $1', [id])"),
]


def _recall() -> float:
    return sum(1 for fn, c in POSITIVES if scan_source(fn, c)) / len(POSITIVES)


def _fp_rate() -> float:
    return sum(1 for fn, c in NEGATIVES if scan_source(fn, c)) / len(NEGATIVES)


def test_recall_above_threshold() -> None:
    missed = [c for fn, c in POSITIVES if not scan_source(fn, c)]
    assert _recall() >= 0.92, f"recall regressed to {_recall():.0%}; missed: {missed}"


def test_false_positive_rate_below_threshold() -> None:
    flagged = [(c, [f.category for f in scan_source(fn, c)]) for fn, c in NEGATIVES
               if scan_source(fn, c)]
    assert _fp_rate() <= 0.08, f"false positives regressed: {flagged}"


def test_all_classes_exercised() -> None:
    hit: set[str] = set()
    for fn, c in POSITIVES:
        hit.update(f.category for f in scan_source(fn, c))
    assert {"command_injection", "path_traversal", "ssrf", "unsafe_deserialization",
            "code_eval", "sql_injection"} <= hit
