#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benchmark: does a pre-built TEMPLATE database speed up an Odoo test run?  (Bruce, 2026-11-08)

The theory under test: every `test.py` run starts with `DROP/CREATE DATABASE` and `odoo -i <modules>`, installing the
modules under test and their dependencies from nothing; if one process installed them once into a template database
(`hams_tpl_<hash>`) and each run did `CREATE DATABASE hams_test_sK TEMPLATE hams_tpl_<hash>`, start-up would drop from
minutes to seconds. This tool MEASURES that instead of assuming it. It changes nothing in `test.py`: the runner's own
`main()` is imported and run unchanged, with exactly two things replaced for the clone arms (`rebuild_db`, and the `-i`
argument of the Odoo command line), which is also the whole of a minimal production implementation.

    python3 hams_shared/tools/benchmark_template_db.py run --repo-root ~/workspace/tmp/tplbench/hams_com --set small --reps 3 \
        --out results_small.json
    python3 hams_shared/tools/benchmark_template_db.py report results_small.json results_medium.json results_large.json
    python3 hams_shared/tools/benchmark_template_db.py key --repo-root . --modules parking

Arms (every one goes through `test.py`'s own `main()`, holding one test slot like any run):
    fresh_ssd     today's run: rebuild_db (DROP/CREATE + vector), `odoo -i`, tests.
    fresh_ram     the same with the database moved into a tmpfs tablespace right after CREATE (the RAM-tablespace idea alone).
    build         one-off template build: fresh install, no tests, database kept as hams_tpl_<hash> with its key as comment.
    clone_ssd     CREATE DATABASE ... TEMPLATE (template and clone on SSD) + filestore copy + secret re-key, plain `odoo`
                  load (no -i) and the tests.
    clone_ram     the same with template and clone in the tmpfs tablespace.
    clone_update  clone_ssd but `odoo -u` instead of plain load, so the at_install tests run again.
    clone_i       a clone with the command line UNCHANGED (`-i` kept): the trap. `-i` on installed modules updates nothing,
                  so Odoo runs no tests at all and exits 0; the harness records it to prove the danger.
Full documentation of every function and structure: tools/README.md, "Template-database benchmark"; the findings and the
design of a production implementation: docs/TEMPLATE_DATABASE_FOR_TEST_RUNS.md.
"""

import argparse
import ast
import calendar
import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time

# [@ANCHOR: tools:template_db_identifier]
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""Same rule as test.py's `_SAFE_DB_NAME_RE`: a database or tablespace name is interpolated into SQL inside double quotes."""

TEMPLATE_PREFIX = "hams_tpl_"
PSQL_RETRIES = 30
PSQL_RETRY_SECONDS = 2.0
PSQL_RETRY_MARKERS = ("recovery mode", "not yet accepting connections", "Connection refused", "starting up")
BENCH_DATABASE = "hams_bench"
BENCH_TABLESPACE = "hams_bench_ram_ts"
ARMS = ("fresh_ssd", "fresh_ram", "build", "clone_ssd", "clone_ram", "clone_update", "clone_i")
SECRET_PARAMETERS = ("database.secret", "database.uuid", "form_shield.signing_key")
SKIPPED_SOURCE_DIRS = frozenset({"__pycache__", ".git", ".pytest_cache", "node_modules"})
SKIPPED_SOURCE_SUFFIXES = (".pyc", ".pyo")
BENIGN_ERROR_PREFIXES = ("Importing test framework",)
"""ERROR-level lines that are not a failure and so are not counted in `LogMilestones.error_lines`: Odoo logs this one at ERROR when a business
module (`hams_helpdesk`, 2026-11-08) imports `odoo.tests.common` while tests are off, as every template build does."""


# [@ANCHOR: tools:template_db_check_identifier]
def check_identifier(name):
    """Returns `name` when it is a plain SQL identifier; raises ValueError otherwise (never builds SQL from a bad name)."""
    if not isinstance(name, str) or not SAFE_IDENTIFIER_RE.match(name) or len(name.encode()) > 63:
        raise ValueError(f"{name!r} is not a safe PostgreSQL identifier")
    return name


# [@ANCHOR: tools:template_db_parse_module_list]
def parse_module_list(text):
    """Module names from a comma, space or newline separated list (`modules.txt`), in order, without duplicates."""
    seen = []
    for token in re.split(r"[,\s]+", text or ""):
        if token and token not in seen:
            seen.append(token)
    return seen


# [@ANCHOR: tools:template_db_find_modules]
def find_module_dirs(addons_dirs):
    """{module name: directory} for every `<dir>/<name>/__manifest__.py` under the given addons directories (first wins)."""
    found = {}
    for base in addons_dirs:
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(base, entry)
            if entry not in found and os.path.isfile(os.path.join(path, "__manifest__.py")):
                found[entry] = os.path.realpath(path)
    return found


def _read_depends(module_dir):
    with open(os.path.join(module_dir, "__manifest__.py"), encoding="utf-8") as handle:
        manifest = ast.literal_eval(handle.read())
    return list(manifest.get("depends", []))


# [@ANCHOR: tools:template_db_dependency_closure]
def dependency_closure(names, module_dirs):
    """Sorted names of `names` and everything they depend on that lives in `module_dirs`.

    Dependencies not in `module_dirs` are Odoo's own addons; their content is covered by the Odoo version in the key. A
    manifest that cannot be parsed raises (a template must never be keyed from a guess)."""
    closure = set()
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in closure or name not in module_dirs:
            continue
        closure.add(name)
        pending.extend(_read_depends(module_dirs[name]))
    return sorted(closure)


# [@ANCHOR: tools:template_db_hash_sources]
def hash_module_sources(module_dirs, names, include_tests=True):
    """sha256 hex over the relative path and the bytes of every file of the named modules.

    Skips `__pycache__`, `.git`, `*.pyc`. With `include_tests=False` a `tests` directory is skipped too (a test edit then
    keeps a template valid: safe only while no test defines a model, which `tests_define_models` checks). Paths are sorted so
    the result does not depend on directory order. Symbolic links are hashed by their target text, never followed."""
    digest = hashlib.sha256()
    for name in sorted(names):
        root = module_dirs[name]
        digest.update(b"module:" + name.encode() + b"\0")
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(
                d for d in dirs if d not in SKIPPED_SOURCE_DIRS and not (not include_tests and d == "tests" and current == root)
            )
            for filename in sorted(files):
                if filename.endswith(SKIPPED_SOURCE_SUFFIXES):
                    continue
                path = os.path.join(current, filename)
                relative = os.path.relpath(path, root)
                digest.update(b"file:" + relative.encode() + b"\0")
                if os.path.islink(path):
                    digest.update(b"link:" + os.readlink(path).encode())
                    continue
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
    return digest.hexdigest()


# [@ANCHOR: tools:template_db_tests_define_models]
def tests_define_models(module_dirs, names):
    """Module names whose `tests` directory defines an ORM model (`_name = ` in a class deriving from `models.*Model`).

    Such a model's table is created only when the module is installed or updated with tests enabled, so a template built
    without tests would lack it and a plain-load clone could not run those tests. An empty result is what lets the key
    ignore test files."""
    offenders = []
    pattern = re.compile(r"^\s*_name\s*=", re.MULTILINE)
    for name in sorted(names):
        tests_dir = os.path.join(module_dirs[name], "tests")
        for current, _dirs, files in os.walk(tests_dir):
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                with open(os.path.join(current, filename), encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
                if "models.Model" in text or "models.TransientModel" in text or "models.AbstractModel" in text:
                    if pattern.search(text):
                        offenders.append(name)
                        break
            else:
                continue
            break
    return offenders


# [@ANCHOR: tools:template_db_key]
@dataclasses.dataclass(frozen=True)
class TemplateKey:
    """Everything a template database's content depends on; two runs may share a template only if every field is equal."""

    modules: tuple
    source_hash: str
    odoo_version: str
    postgres_major: str
    vector_version: str
    options: tuple = ()
    crypto_key_fingerprint: str = ""

    def canonical(self):
        """Canonical JSON text of the key (sorted keys, no spaces); the input of `digest`."""
        return json.dumps(dataclasses.asdict(self), sort_keys=True, separators=(",", ":"))

    def digest(self):
        """sha256 hex of `canonical()`."""
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def database_name(self):
        """`hams_tpl_` plus the first 16 hex digits of the digest: the name encodes the key, so a changed key can never
        find an old template; a stale one is simply not the one asked for."""
        return TEMPLATE_PREFIX + self.digest()[:16]

    def comment(self):
        """The text stored as the template's database comment: the full digest and key, which `verify_template` compares."""
        return json.dumps({"hams_template_db": 1, "digest": self.digest(), "key": json.loads(self.canonical())}, sort_keys=True)


# [@ANCHOR: tools:template_db_verify_template]
def verify_template(comment, key):
    """(True, "") when the database comment `comment` was written for exactly `key`, else (False, reason).

    The second line of defence behind the content-addressed name: it catches a hand-made database with the right name, a
    truncated name collision and a template whose build never finished (no comment is written until the build succeeds)."""
    if not comment:
        return False, "no comment: the build did not finish, or this is not a template"
    try:
        stored = json.loads(comment)
    except ValueError:
        return False, "comment is not JSON"
    if not isinstance(stored, dict) or stored.get("hams_template_db") != 1:
        return False, "comment is not a template comment"
    if stored.get("digest") != key.digest():
        return False, "digest differs: the modules, sources, Odoo, PostgreSQL or options changed"
    return True, ""


# [@ANCHOR: tools:template_db_summarize]
def summarize(values):
    """{"n", "median", "min", "max", "mean", "stdev", "cv_percent"} of a list of numbers; `None` fields for an empty list.

    `stdev` is the sample standard deviation (0 for one value); `cv_percent` = 100 * stdev / mean, the honest variance
    figure on this shared box."""
    values = [float(v) for v in values if v is not None]
    if not values:
        return {"n": 0, "median": None, "min": None, "max": None, "mean": None, "stdev": None, "cv_percent": None}
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "n": len(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "mean": mean,
        "stdev": stdev,
        "cv_percent": (100.0 * stdev / mean) if mean else 0.0,
    }


# [@ANCHOR: tools:template_db_parse_loadscreen]
def parse_loadscreen(text):
    """The box state from `/dev/shm/loadscreen/stats.txt` as a flat dict: `load1`, `cpu_busy_1min`, `cpu_busy_5min`,
    `io_wait_1min`, `postgres_1min`, `test_slots_busy`, `tests_waiting`, `mem_avail_gb`, `recommendation`; absent
    fields are left out. Never raises on unexpected text."""
    state = {}
    match = re.search(r"RECOMMENDATION:\s*(\w+)", text or "")
    if match:
        state["recommendation"] = match.group(1)
    rows = {
        "cpu busy %": "cpu_busy",
        "cpu iowait %": "io_wait",
        "load1": "load1",
        "postgres": "postgres",
        "test slots busy": "test_slots_busy",
        "tests waiting": "tests_waiting",
        "mem avail GB": "mem_avail_gb",
    }
    for line in (text or "").splitlines():
        for label, field in rows.items():
            if line.startswith(label):
                numbers = re.findall(r"-?\d+(?:\.\d+)?", line[len(label):])
                if not numbers:
                    continue
                if field in ("cpu_busy", "io_wait", "postgres"):
                    state[field + "_1min"] = float(numbers[0])
                    if len(numbers) > 1:
                        state[field + "_5min"] = float(numbers[1])
                else:
                    state[field] = float(numbers[0])
    return state


# [@ANCHOR: tools:template_db_read_loadscreen]
def read_loadscreen(path="/dev/shm/loadscreen/stats.txt"):
    """`parse_loadscreen` of the file, `{}` when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_loadscreen(handle.read())
    except OSError:
        return {}


_LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) (\d+) (\w+) (\S+) (\S+): (.*)$")
_RESULT_LINE = re.compile(r"(\d+) failed, (\d+) error\(s\) of (\d+) tests")
_FAILURE_LINE = re.compile(r"^(?:FAIL|ERROR): (setUpClass \([\w.]+\)|\w+\.\w+)")
_STARTING_LINE = re.compile(r"^Starting (\w+\.\w+) \.\.\.")


def _utc_epoch(date_text, millis):
    return calendar.timegm(time.strptime(date_text, "%Y-%m-%d %H:%M:%S")) + int(millis) / 1000.0


# [@ANCHOR: tools:template_db_log_milestones]
@dataclasses.dataclass
class LogMilestones:
    """What `parse_odoo_log` found. Times are seconds since the epoch (Odoo logs UTC); a missing event is `None`.

    first_line: the first Odoo log line. modules_loaded: the `Modules loaded.` line (install or load finished).
    post_tests_start: `Starting post tests`. first_test: the first `Starting Class.test ...` line of any kind.
    first_post_install_test: the first such line after post_tests_start. last_line: the last line.
    tests_started: ordered list of "Class.test" (each name once per occurrence). failed: set of "Class.test" with a FAIL/ERROR
    line. result: (failed, errors, tests) from the `odoo.tests.result` line, or None. install_seconds: the figure Odoo itself
    reports for the module load (`N modules loaded in X s`), the largest one. error_lines: how many lines were logged at ERROR or
    CRITICAL, except those starting with a `BENIGN_ERROR_PREFIXES` entry (a template build with a count above zero is not trusted)."""

    first_line: float = None
    modules_loaded: float = None
    post_tests_start: float = None
    first_test: float = None
    first_post_install_test: float = None
    last_line: float = None
    tests_started: list = dataclasses.field(default_factory=list)
    failed: set = dataclasses.field(default_factory=set)
    result: tuple = None
    install_seconds: float = None
    error_lines: int = 0


# [@ANCHOR: tools:template_db_parse_odoo_log]
def parse_odoo_log(lines):
    """Reads Odoo's log (an iterable of text lines, as `test.py` prints them) into a `LogMilestones`.

    Lines that are not Odoo log lines (the runner's own banners, continuation lines of a traceback) are ignored. The test
    outcome is every started test minus every test named in a `FAIL:`/`ERROR:` line; only `odoo.tests.result` is the
    authority on counts, per the project's rule, and `result` carries it."""
    m = LogMilestones()
    for raw in lines:
        match = _LOG_LINE.match(raw.rstrip("\n"))
        if not match:
            continue
        stamp = _utc_epoch(match.group(1), match.group(2))
        level, logger, message = match.group(4), match.group(6), match.group(7).strip()
        if m.first_line is None:
            m.first_line = stamp
        m.last_line = stamp
        if message == "Modules loaded." and m.modules_loaded is None:
            m.modules_loaded = stamp
        elif message == "Starting post tests":
            m.post_tests_start = stamp
        started = _STARTING_LINE.match(message)
        if started:
            m.tests_started.append(started.group(1))
            if m.first_test is None:
                m.first_test = stamp
            if m.post_tests_start is not None and m.first_post_install_test is None:
                m.first_post_install_test = stamp
        if level in ("ERROR", "CRITICAL"):
            if not message.startswith(BENIGN_ERROR_PREFIXES):
                m.error_lines += 1
            failure = _FAILURE_LINE.match(message)
            if failure:
                m.failed.add(failure.group(1))
        if logger == "odoo.tests.result":
            counts = _RESULT_LINE.search(message)
            if counts:
                m.result = tuple(int(g) for g in counts.groups())
        loaded = re.search(r"(\d+) modules loaded in ([\d.]+)s", message)
        if loaded and logger == "odoo.modules.loading":
            seconds = float(loaded.group(2))
            if m.install_seconds is None or seconds > m.install_seconds:
                m.install_seconds = seconds
    return m


# [@ANCHOR: tools:template_db_compare_outcomes]
def compare_outcomes(reference, other, only_post_install=True, post_install_names=None):
    """Compares two `LogMilestones`: the pass/fail set of a clone against a fresh install.

    Returns {"same": bool, "only_in_reference": [...], "only_in_other": [...], "failed_differ": [...]} with test names sorted.
    A test counts as run when it was started; "failed_differ" lists tests started in both whose FAIL status differs.
    With `only_post_install` the reference's at_install tests (started before its `post_tests_start`) are left out, since a
    plain-load clone runs the at_install suite too but at a different point; `post_install_names` instead gives the exact
    set to compare when the caller knows it."""
    ref_names = list(reference.tests_started)
    if post_install_names is not None:
        ref_set = set(ref_names) & set(post_install_names)
        other_set = set(other.tests_started) & set(post_install_names)
    else:
        ref_set, other_set = set(ref_names), set(other.tests_started)
    only_ref = sorted(ref_set - other_set)
    only_other = sorted(other_set - ref_set)
    differ = sorted(t for t in ref_set & other_set if (t in reference.failed) != (t in other.failed))
    return {"same": not (only_ref or only_other or differ), "only_in_reference": only_ref, "only_in_other": only_other, "failed_differ": differ}


# [@ANCHOR: tools:template_db_equality_lines]
def equality_lines(records):
    """Markdown bullet lines saying, per set and per clone arm repetition, whether its outcome equals the first `fresh_ssd` run's.

    `records`: list of dicts (`RunRecord` as dicts) with `set_name`, `arm`, `rep`, `outcome_names`, `failed_names`, `result`.
    Two runs are the same when `result` and the sets of started tests and of failed tests (`setUpClass (...)` errors included)
    are equal. A set with no `fresh_ssd` record gives no lines. `clone_i` is reported as what it is: the number of tests it ran."""
    lines = []
    records = valid_records(records)
    for set_name in sorted({r["set_name"] for r in records}):
        group = [r for r in records if r["set_name"] == set_name]
        reference = next((r for r in group if r["arm"] == "fresh_ssd"), None)
        if reference is None:
            continue
        for r in group:
            if r["arm"] in ("fresh_ssd", "build"):
                continue
            same = (
                r["result"] == reference["result"]
                and set(r["outcome_names"]) == set(reference["outcome_names"])
                and set(r["failed_names"]) == set(reference["failed_names"])
            )
            lines.append(
                f"- {set_name} {r['arm']} #{r['rep']} vs fresh_ssd #{reference['rep']}: "
                + ("SAME outcome" if same else f"DIFFERENT (result {r['result']} vs {reference['result']}, {len(r['outcome_names'])} vs {len(reference['outcome_names'])} tests started)")
            )
    return lines


# [@ANCHOR: tools:template_db_clone_sql]
def clone_database_sql(name, template, tablespace=None, strategy=None):
    """``CREATE DATABASE "name" TEMPLATE "template" [TABLESPACE "ts"] [STRATEGY = x];`` as one statement.

    `strategy` is None (the server default, WAL_LOG), "wal_log" or "file_copy". All three names are validated by
    `check_identifier` (strategy against its two values); raises ValueError. FILE_COPY is the only strategy the server
    can reflink (`file_copy_method = clone`) and it forces a checkpoint, which is itself measured."""
    check_identifier(name)
    check_identifier(template)
    parts = [f'CREATE DATABASE "{name}" TEMPLATE "{template}"']
    if tablespace is not None:
        parts.append(f'TABLESPACE "{check_identifier(tablespace)}"')
    if strategy is not None:
        if strategy.lower() not in ("wal_log", "file_copy"):
            raise ValueError(f"unknown CREATE DATABASE strategy {strategy!r}")
        parts.append(f"STRATEGY = {strategy.upper()}")
    return " ".join(parts) + ";"


# [@ANCHOR: tools:template_db_rekey_sql]
def rekey_sql():
    """SQL that gives a clone its own per-database secrets: new `database.uuid`, `database.secret`, `form_shield.signing_key`
    (the parameters in `SECRET_PARAMETERS` that an install writes from a random source) and `database.create_date`. Uses
    `gen_random_uuid()` and `md5(random())` only: these are test databases; a production clone would use pgcrypto."""
    quoted = ", ".join(f"'{p}'" for p in SECRET_PARAMETERS)
    return (
        "UPDATE ir_config_parameter SET value = CASE key "
        "WHEN 'database.secret' THEN gen_random_uuid()::text "
        "WHEN 'database.uuid' THEN gen_random_uuid()::text "
        "WHEN 'database.create_date' THEN now()::text "
        "ELSE md5(random()::text) || md5(random()::text) END "
        f"WHERE key IN ({quoted}, 'database.create_date');"
    )


# [@ANCHOR: tools:template_db_proc_cpu_seconds]
def proc_cpu_seconds(stat_text, clock_ticks=100):
    """User plus system CPU seconds from the text of `/proc/<pid>/stat` (fields 14 and 15), or None if it cannot be parsed.

    The command name in field 2 may contain spaces and parentheses, so fields are counted after the LAST `)`."""
    try:
        rest = stat_text[stat_text.rindex(")") + 2:].split()
        return (int(rest[11]) + int(rest[12])) / clock_ticks
    except (ValueError, IndexError, ZeroDivisionError):
        return None


# [@ANCHOR: tools:template_db_postgres_cpu_sampler]
class PostgresCpuSampler:
    """Samples the CPU time of the PostgreSQL backends serving one database while a measured run executes.

    PostgreSQL's CPU is not a child of the measured process, so `wait4` cannot see it. A thread polls `proc_root` every
    `interval` seconds, picks processes whose command line is `postgres: <user> <database> ...`, and keeps the largest
    CPU total seen per pid. The sum is a LOWER BOUND (a backend's last fraction of a second before it exits is missed),
    and does not include the checkpointer or WAL writer. Use as a context manager; `cpu_seconds` is valid after exit."""

    def __init__(self, database, interval=0.25, proc_root="/proc", clock_ticks=None):
        self.database = database
        self.interval = interval
        self.proc_root = proc_root
        self.clock_ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
        self.per_pid = {}
        self._stop = threading.Event()
        self._thread = None

    def sample_once(self):
        """One pass over `proc_root`; returns nothing, updates `per_pid`."""
        try:
            entries = os.listdir(self.proc_root)
        except OSError:
            return
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(os.path.join(self.proc_root, entry, "cmdline"), "rb") as handle:
                    words = handle.read().replace(b"\0", b" ").decode("utf-8", "replace").split()
                if len(words) < 3 or words[0] != "postgres:" or self.database not in words[1:4]:
                    continue
                with open(os.path.join(self.proc_root, entry, "stat"), encoding="utf-8", errors="replace") as handle:
                    seconds = proc_cpu_seconds(handle.read(), self.clock_ticks)
            except OSError:
                continue
            if seconds is not None:
                self.per_pid[entry] = max(seconds, self.per_pid.get(entry, 0.0))

    def _loop(self):
        while not self._stop.is_set():
            self.sample_once()
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="pg-cpu-sampler")
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join(timeout=5)
        self.sample_once()
        return False

    @property
    def cpu_seconds(self):
        """Sum over the pids seen of their largest CPU total."""
        return sum(self.per_pid.values())


# [@ANCHOR: tools:template_db_arm_plan]
@dataclasses.dataclass(frozen=True)
class ArmPlan:
    """How one arm differs from today's `test.py` run. Built by `arm_plan`; read by the `_stub` child.

    arm: one of `ARMS`. clone: replace rebuild_db with a clone of the template. drop_install: remove `-i <modules>` from the
    Odoo command line (plain load). update: use `-u` in place of `-i`. build: template build (database named after the key,
    no tests). ram: put the database in the tmpfs tablespace. tests: keep `--test-enable --test-tags` (False only for build)."""

    arm: str
    clone: bool = False
    drop_install: bool = False
    update: bool = False
    build: bool = False
    ram: bool = False
    tests: bool = True


# [@ANCHOR: tools:template_db_arm_plan_for]
def arm_plan(arm):
    """The `ArmPlan` of `arm`; raises ValueError for a name not in `ARMS`."""
    plans = {
        "fresh_ssd": ArmPlan("fresh_ssd"),
        "fresh_ram": ArmPlan("fresh_ram", ram=True),
        "build": ArmPlan("build", build=True, tests=False),
        "clone_ssd": ArmPlan("clone_ssd", clone=True, drop_install=True),
        "clone_ram": ArmPlan("clone_ram", clone=True, drop_install=True, ram=True),
        "clone_update": ArmPlan("clone_update", clone=True, update=True),
        "clone_i": ArmPlan("clone_i", clone=True),
    }
    if arm not in plans:
        raise ValueError(f"unknown arm {arm!r}; choose from {', '.join(ARMS)}")
    return plans[arm]


# [@ANCHOR: tools:template_db_transform_command]
def transform_odoo_command(cmd, plan, database=None):
    """The Odoo command line (a list) `test.py` built, changed for `plan`; the input list is not modified.

    Always: with `database` given, the value after `-d` is replaced (template build). `drop_install` removes `-i` and its
    module list. `update` turns `-i a,b` into `-u a,b` without `base` (updating base would reload Odoo's whole core, which
    no test needs). Without `plan.tests` the `--test-enable` flag and the `--test-tags` pair are removed. Every other argument
    is kept in order."""
    out = list(cmd)
    if database is not None and "-d" in out:
        out[out.index("-d") + 1] = database
    if plan.drop_install or plan.update:
        index = out.index("-i")
        modules = out[index + 1]
        del out[index:index + 2]
        if plan.update:
            names = [m for m in modules.split(",") if m != "base"]
            out[index:index] = ["-u", ",".join(names)]
    if not plan.tests:
        if "--test-enable" in out:
            out.remove("--test-enable")
        if "--test-tags" in out:
            index = out.index("--test-tags")
            del out[index:index + 2]
    return out


# [@ANCHOR: tools:template_db_run_record]
@dataclasses.dataclass
class RunRecord:
    """One measured run, serialised into the results file by `dataclasses.asdict`.

    set_name/arm/rep identify it. wall_total: rebuild start to Odoo exit (s). wall_prepare: rebuild_db or clone (s).
    wall_to_modules_loaded / wall_to_first_test / wall_to_first_post_install_test: from rebuild start (s), None when the
    event did not happen. odoo_seconds: Odoo's own `modules loaded in` figure. cpu_runner_seconds: user+system CPU of the
    runner process tree (Odoo, Chrome). cpu_postgres_seconds: sampled PostgreSQL backend CPU (lower bound). load_before /
    load_after: `parse_loadscreen` at both ends. tests_started / failed counts, `result` the odoo.tests.result tuple,
    `outcome_names`/`failed_names` the lists used for `compare_outcomes`. exit_code: the runner's exit status. notes: free text."""

    set_name: str
    arm: str
    rep: int
    wall_total: float = None
    wall_prepare: float = None
    wall_to_modules_loaded: float = None
    wall_to_first_test: float = None
    wall_to_first_post_install_test: float = None
    odoo_seconds: float = None
    cpu_runner_seconds: float = None
    cpu_postgres_seconds: float = None
    load_before: dict = dataclasses.field(default_factory=dict)
    load_after: dict = dataclasses.field(default_factory=dict)
    result: list = None
    outcome_names: list = dataclasses.field(default_factory=list)
    failed_names: list = dataclasses.field(default_factory=list)
    exit_code: int = None
    notes: str = ""
    error_lines: int = 0


# [@ANCHOR: tools:template_db_record_from_run]
def record_from_run(set_name, arm, rep, events, milestones, cpu_runner, cpu_postgres, load_before, load_after, exit_code):
    """Builds a `RunRecord` from the stub's event times and the parsed log.

    `events` is {"rebuild_start", "rebuild_end", "odoo_end"} as epoch seconds written by the stub (missing keys give None
    fields). Times derived from the Odoo log are measured from `rebuild_start`, so every arm is on the same clock."""
    start = events.get("rebuild_start")

    def since(stamp):
        return None if stamp is None or start is None else round(stamp - start, 3)

    return RunRecord(
        set_name=set_name,
        arm=arm,
        rep=rep,
        wall_total=since(events.get("odoo_end")),
        wall_prepare=since(events.get("rebuild_end")),
        wall_to_modules_loaded=since(milestones.modules_loaded),
        wall_to_first_test=since(milestones.first_test),
        wall_to_first_post_install_test=since(milestones.first_post_install_test),
        odoo_seconds=milestones.install_seconds,
        cpu_runner_seconds=cpu_runner,
        cpu_postgres_seconds=cpu_postgres,
        load_before=load_before,
        load_after=load_after,
        result=list(milestones.result) if milestones.result else None,
        outcome_names=list(milestones.tests_started),
        failed_names=sorted(milestones.failed),
        exit_code=exit_code,
        error_lines=milestones.error_lines,
    )


METRICS = (
    ("wall_prepare", "DB ready (s)"),
    ("wall_to_modules_loaded", "modules loaded (s)"),
    ("wall_to_first_test", "first test (s)"),
    ("wall_to_first_post_install_test", "first post_install test (s)"),
    ("wall_total", "total (s)"),
    ("cpu_runner_seconds", "runner CPU (s)"),
    ("cpu_postgres_seconds", "PG CPU (s, sampled)"),
)


# [@ANCHOR: tools:template_db_build_succeeded]
def build_succeeded(record):
    """True when a `build` arm run produced a trustworthy template.

    The runner calls a run with no `odoo.tests.result` line a failure (exit 1), and a build runs no tests on purpose, so the
    exit status cannot be the test. A build succeeded when Odoo logged `Modules loaded.` and not one ERROR/CRITICAL line, whatever
    the exit status. `record` is a `RunRecord`."""
    return record.wall_to_modules_loaded is not None and record.error_lines == 0


# [@ANCHOR: tools:template_db_valid_records]
def valid_records(records):
    """The records (dicts or `RunRecord`s) whose `notes` do not start with `INVALID`, as dicts.

    A run disturbed by something outside the benchmark (the shared PostgreSQL crash-restarting under it, 2026-11-08 20:18 UTC) is marked
    by editing its `notes` to `INVALID: <reason>`; it stays in the results file for the record and is left out of every table and comparison."""
    rows = [r if isinstance(r, dict) else dataclasses.asdict(r) for r in records]
    return [r for r in rows if not str(r.get("notes") or "").startswith("INVALID")]


# [@ANCHOR: tools:template_db_render_report]
def render_report(records):
    """Markdown tables (one per set) of the median [min-max] of every metric per arm, plus the box load, from RunRecords.

    `records` is a list of RunRecord or dicts. Rows are arms in `ARMS` order; a cell is `median [min-max]` over the repetitions
    (n shown in the first column), `-` when no run produced the metric. A final line per set gives the load1 range seen, since
    the box is shared and a timing without it cannot be judged."""
    rows = valid_records(records)
    out = []
    for set_name in sorted({r["set_name"] for r in rows}, key=lambda s: ("small", "medium", "large").index(s) if s in ("small", "medium", "large") else 9):
        out.append(f"### {set_name}\n")
        out.append("| arm (n) | " + " | ".join(label for _key, label in METRICS) + " |")
        out.append("|---|" + "---|" * len(METRICS))
        loads = []
        for arm in ARMS:
            group = [r for r in rows if r["set_name"] == set_name and r["arm"] == arm]
            if not group:
                continue
            cells = []
            for key, _label in METRICS:
                stats = summarize([r.get(key) for r in group])
                cells.append("-" if stats["n"] == 0 else f"{stats['median']:.1f} [{stats['min']:.1f}-{stats['max']:.1f}]")
            out.append(f"| {arm} ({len(group)}) | " + " | ".join(cells) + " |")
            loads += [r.get("load_before", {}).get("load1") for r in group] + [r.get("load_after", {}).get("load1") for r in group]
        loads = [v for v in loads if v is not None]
        if loads:
            out.append(f"\nload1 during these runs: {min(loads):.0f} to {max(loads):.0f} (16 cores / 32 threads).\n")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------------------------
# Orchestration: everything below touches the machine (PostgreSQL, mounts, processes). The pure logic above carries the tests.
# ---------------------------------------------------------------------------------------------------------------------


# [@ANCHOR: tools:template_db_load_runner]
def load_runner_module(repo_root):
    """Imports `<repo_root>/hams_shared/tools/test.py` (or `<repo_root>/tools/test.py`) under the name `hams_test_runner`.

    Not `import test`: that is the standard library's regression-test package. The runner's directory is put first on
    `sys.path` so its sibling modules import, and the current directory is the repository root, which `main()` requires.
    Raises FileNotFoundError when the runner is not there."""
    for candidate in (os.path.join(repo_root, "hams_shared", "tools"), os.path.join(repo_root, "tools")):
        path = os.path.join(candidate, "test.py")
        if os.path.isfile(path):
            sys.path.insert(0, candidate)
            os.chdir(repo_root)
            spec = importlib.util.spec_from_file_location("hams_test_runner", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["hams_test_runner"] = module
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError(f"no hams_shared/tools/test.py under {repo_root}")


def _psql(runner, sql, database="postgres", env=None):
    """Runs `sql` through the runner's own psql lookup; returns stdout text. Raises CalledProcessError on failure. A server that is crash-recovering or starting (psql exit 2 with a known message) is retried for up to a minute: the box's PostgreSQL was restarted under a measurement on 2026-11-08."""
    from_env = dict(os.environ if env is None else env)
    for name, value in (("PGHOST", "localhost"), ("PGUSER", "odoo"), ("PGPASSWORD", "odoo")):
        from_env.setdefault(name, value)
    psql_cmd = runner.infrastructure.get_pg_bin("psql")
    for attempt in range(PSQL_RETRIES):
        done = subprocess.run(
            [psql_cmd, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", database],
            input=sql.encode(),
            capture_output=True,
            check=False,
            env=from_env,
        )
        server_down = done.returncode == 2 and any(w in done.stderr.decode("utf-8", "replace") for w in PSQL_RETRY_MARKERS)
        if not server_down:
            break
        time.sleep(PSQL_RETRY_SECONDS)
    if done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, "psql", output=done.stdout, stderr=done.stderr)
    return done.stdout.decode()


def _event(name, **fields):
    path = os.environ.get("HAMS_BENCH_EVENTS")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": name, "time": time.time(), **fields}) + "\n")


FILESTORE_ROOT = "/var/lib/odoo/.local/share/Odoo/filestore"
"""Odoo's filestore of the `odoo` account, the same constant as test.py's `ODOO_FILESTORE_BASE` (the measured runs execute as `odoo`)."""


def _odoo_filestore_root():
    return FILESTORE_ROOT


# [@ANCHOR: tools:template_db_as_odoo]
def as_odoo(argv, env=None):
    """`argv` (a list) prefixed to run as the `odoo` account: `sudo -n -u odoo [env K=V ...] argv`.

    The measured runs must execute as `odoo` (the `daemon_key_manager` module chmods `/opt/hams/etc/keys`, which only its owner may do;
    run as another user the registry fails to load), exactly as the dev box's documented `sudo -n -u odoo env ...` invocation does. `env`: a
    dict of variables to set (sudo resets the rest). Never prompts (`-n`)."""
    prefix = ["sudo", "-n", "-u", "odoo"]
    if env is not None:
        prefix += ["env"] + [f"{k}={v}" for k, v in sorted(env.items())]
    return prefix + list(argv)


# [@ANCHOR: tools:template_db_filestore_bytes]
def filestore_bytes(name):
    """Apparent size in bytes of the filestore directory of database `name`, 0 if absent (read with `sudo -n du`)."""
    path = os.path.join(_odoo_filestore_root(), check_identifier(name))
    done = subprocess.run(as_odoo(["du", "-sb", "--apparent-size", path]), capture_output=True, text=True)
    return int(done.stdout.split()[0]) if done.returncode == 0 and done.stdout.split() else 0


# [@ANCHOR: tools:template_db_remove_filestore]
def remove_filestore(name):
    """Removes the filestore directory of a TEMPLATE-named database (`hams_tpl_...`, with or without a suffix); refuses any other name."""
    if not check_identifier(name).startswith(TEMPLATE_PREFIX):
        raise ValueError(f"refusing to remove the filestore of {name!r}")
    subprocess.run(as_odoo(["rm", "-rf", "--", os.path.join(_odoo_filestore_root(), name)]), capture_output=True)


# [@ANCHOR: tools:template_db_stub_main]
def stub_main(argv):
    """The child process of every measured run: `test.py`'s `main()` with the arm's two replacements installed.

    `argv` is [arm, repo_root, template_name_or_"-", *test.py arguments]. Writes `rebuild_start`, `rebuild_end` and `odoo_end`
    events to the file named by HAMS_BENCH_EVENTS. For arm `build` the database is `template_name` and its key comment is
    written when the install succeeded. Exits with `main()`'s exit status."""
    arm, repo_root, template = argv[0], argv[1], argv[2]
    plan = arm_plan(arm)
    runner = load_runner_module(repo_root)
    original_rebuild, original_run = runner.rebuild_db, runner.run_cmd
    seen = {}

    def rebuild(db_name, redis_flush_db=None):
        _event("rebuild_start", database=db_name)
        target = template if plan.build else db_name
        seen["database"] = target
        if plan.clone:
            clone_for_run(runner, db_name, template, plan, redis_flush_db)
        else:
            original_rebuild(target, redis_flush_db)
            if plan.ram:
                _psql(runner, f'ALTER DATABASE "{check_identifier(target)}" SET TABLESPACE "{BENCH_TABLESPACE}";')
        _event("rebuild_end", database=target)

    def run(cmd, *args, **kwargs):
        cmd = transform_odoo_command(cmd, plan, database=seen.get("database") if plan.build else None)
        _event("odoo_start", cmd=" ".join(cmd[:12]))
        code = original_run(cmd, *args, **kwargs)
        _event("odoo_end", code=code)
        return code

    runner.rebuild_db, runner.run_cmd = rebuild, run
    sys.argv = ["test.py"] + argv[3:]
    runner.main()
    return 0


# [@ANCHOR: tools:template_db_clone_for_run]
def clone_for_run(runner, db_name, template, plan, redis_flush_db):
    """`rebuild_db` for a clone arm: the same steps as the runner's (production-host refusal, Redis flush, drop, marker, stale
    filestore) with `CREATE DATABASE ... TEMPLATE` in place of `CREATE DATABASE` plus `CREATE EXTENSION vector`, then the
    template's filestore copied (reflink where the file system allows), then the per-database secrets re-keyed.
    Raises RuntimeError when the template is missing (a clone arm must never silently fall back to a fresh install)."""
    check_identifier(db_name)
    check_identifier(template)
    env = dict(os.environ)
    runner.refuse_to_run_on_production_host(database_names=runner.list_database_names(runner.infrastructure.get_pg_bin("psql"), env))
    redis_db = os.environ.get("REDIS_DB", "0") if redis_flush_db is None else str(redis_flush_db)
    subprocess.run(["redis-cli", "-n", redis_db, "flushdb"], check=False, env=env)
    if _psql(runner, f"SELECT 1 FROM pg_database WHERE datname = '{template}';").strip() != "1":
        raise RuntimeError(f"template database {template} does not exist; run the build arm first")
    marker = (
        runner.runner_cleanup.comment_database_sql(db_name, runner._RUN_CLEANUP["slot"], runner._RUN_CLEANUP["keep"])
        if runner._RUN_CLEANUP
        else ""
    )
    create = clone_database_sql(db_name, template, tablespace=BENCH_TABLESPACE if plan.ram else "pg_default", strategy=os.environ.get("HAMS_BENCH_STRATEGY"))
    _psql(
        runner,
        f"""SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}';
DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);
SET file_copy_method = {os.environ.get("HAMS_BENCH_COPY_METHOD", "copy")};
{create}
{marker}
""",
    )
    runner.remove_stale_filestore(db_name)
    source = os.path.join(_odoo_filestore_root(), template)
    if os.path.isdir(source):
        destination = os.path.join(_odoo_filestore_root(), db_name)
        subprocess.run(["cp", "-a", "--reflink=auto", source, destination], check=True)
    _psql(runner, rekey_sql(), database=db_name)




# [@ANCHOR: tools:template_db_ram_tablespace]
class RamTablespace:
    """A private tmpfs mounted for the experiment, with a PostgreSQL tablespace on it; a context manager.

    Enter: `sudo mount -t tmpfs -o size=<size> tmpfs <directory>` (refuses a directory that is already a mount point, so it can
    never shadow another session's), `chown postgres`, `CREATE TABLESPACE hams_bench_ram_ts`. Exit: drops every database
    still in the tablespace, drops the tablespace, unmounts. Touches neither /etc/fstab nor any PostgreSQL configuration.
    `sql(statement)` runs a statement as the server's superuser role and returns stdout; `shell(argv)` runs a command and
    returns the CompletedProcess. Both are injectable so the tests need no server."""

    def __init__(self, directory, size, sql, shell, name=BENCH_TABLESPACE):
        self.directory, self.size, self.sql, self.shell, self.name = directory, size, sql, shell, check_identifier(name)
        self.mounted = False
        self.created = False

    def __enter__(self):
        existing = self.sql(f"SELECT 1 FROM pg_tablespace WHERE spcname = '{self.name}';").strip()
        if existing:
            raise RuntimeError(f"tablespace {self.name} already exists (another run of this tool?); drop it first")
        if self.shell(["mountpoint", "-q", self.directory]).returncode == 0:
            raise RuntimeError(f"{self.directory} is already a mount point; refusing to use it")
        os.makedirs(self.directory, exist_ok=True)
        for argv in (
            ["sudo", "-n", "mount", "-t", "tmpfs", "-o", f"size={self.size}", "tmpfs", self.directory],
        ):
            done = self.shell(argv)
            if done.returncode != 0:
                raise RuntimeError(f"{' '.join(argv)} failed: {done.stderr}")
        self.mounted = True
        for argv in (["sudo", "-n", "chown", "postgres:postgres", self.directory], ["sudo", "-n", "chmod", "700", self.directory]):
            done = self.shell(argv)
            if done.returncode != 0:
                raise RuntimeError(f"{' '.join(argv)} failed: {done.stderr}")
        self.sql(f"CREATE TABLESPACE \"{self.name}\" LOCATION '{self.directory}';")
        self.created = True
        return self

    def __exit__(self, *_exc):
        if self.created:
            for line in self.sql(
                f"SELECT d.datname FROM pg_database d JOIN pg_tablespace t ON t.oid = d.dattablespace WHERE t.spcname = '{self.name}';"
            ).split():
                self.sql(f'DROP DATABASE IF EXISTS "{check_identifier(line)}" WITH (FORCE);')
            self.sql(f'DROP TABLESPACE IF EXISTS "{self.name}";')
        if self.mounted:
            self.shell(["sudo", "-n", "umount", self.directory])
        return False


def _shell(argv):
    return subprocess.run(argv, capture_output=True, text=True)


# [@ANCHOR: tools:template_db_timed_clone]
def timed_clone(connect, statement, drop_sql, backend_cpu):
    """Runs one CREATE DATABASE `statement`, returns {"wall": s, "cpu": s}, then runs `drop_sql` (not timed).

    `connect()` returns an autocommit DB-API connection to the `postgres` database (CREATE DATABASE cannot run in a
    transaction). `backend_cpu(pid)` is the CPU seconds of a backend process (`proc_cpu_seconds` of its /proc entry); CPU is
    the difference across the statement for the session's own backend only, so checkpointer and I/O workers are not in it."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]
        before = backend_cpu(pid)
        t0 = time.perf_counter()
        cur.execute(statement)
        wall = time.perf_counter() - t0
        after = backend_cpu(pid)
        cur.execute(drop_sql)
    finally:
        conn.close()
    return {"wall": wall, "cpu": None if before is None or after is None else after - before}


def _backend_cpu(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return proc_cpu_seconds(handle.read(), os.sysconf("SC_CLK_TCK"))
    except OSError:
        return None


# [@ANCHOR: tools:template_db_run_measured]
def run_measured(cmd, env, cwd, log_path, database, sampler_factory=PostgresCpuSampler):
    """Runs `cmd` to completion and returns (lines, returncode, cpu_runner_seconds, cpu_postgres_seconds).

    Output (stdout and stderr merged) is copied line by line to `log_path` and kept in `lines`. `cpu_runner_seconds` is the
    user plus system CPU of the child and every descendant it waited for (`os.wait4` rusage; processes that were killed
    and never reaped by their parent are not in it). `cpu_postgres_seconds` is the sampled backend CPU for `database`
    (`PostgresCpuSampler`, a lower bound). Blocks until the child exits; raises OSError when it cannot start."""
    lines = []
    with sampler_factory(database) as sampler, open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd, text=True, errors="replace")

        def pump():
            for line in proc.stdout:
                log.write(line)
                lines.append(line)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        _pid, status, usage = os.wait4(proc.pid, 0)
        returncode = os.waitstatus_to_exitcode(status)
        reader.join(timeout=30)
    return lines, returncode, round(usage.ru_utime + usage.ru_stime, 2), round(sampler.cpu_seconds, 2)


# [@ANCHOR: tools:template_db_bench]
class Bench:
    """Drives one benchmark session (a module set) against a checkout. See `run_command`."""

    def __init__(self, args):
        self.args = args
        self.repo_root = os.path.abspath(os.path.expanduser(args.repo_root))
        self.out_dir = os.path.abspath(os.path.expanduser(args.out_dir))
        os.makedirs(os.path.join(self.out_dir, "logs"), exist_ok=True)
        self.runner = load_runner_module(self.repo_root)
        self.records = []
        self.meta = {"set": args.set_name, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "clone": [], "build": []}

    # -- module set and key -------------------------------------------------------------------------------------------
    def module_list(self):
        if getattr(self.args, "modules", None):
            return parse_module_list(self.args.modules)
        if self.args.set_name == "small":
            return ["parking"]
        if self.args.set_name == "medium":
            return ["ham_onboarding"]
        with open(os.path.expanduser(self.args.modules_file), encoding="utf-8") as handle:
            return parse_module_list(handle.read())

    def build_key(self):
        runner = self.runner
        names = self.module_list()
        parsed = runner.parse_runner_args(["-u", ",".join(names)])
        base_dir = os.getcwd()
        ignore = runner.load_ignore_file(os.path.join(base_dir, parsed.config))
        targets, installs = runner.select_modules(parsed, base_dir, ignore)
        installs, _note = runner.add_test_companions(installs, targets, base_dir)
        addons = [p for p in runner.get_addons_path(base_dir).split(",") if p]
        module_dirs = find_module_dirs(addons)
        t0 = time.perf_counter()
        closure = dependency_closure(["base"] + list(installs), module_dirs)
        source_hash = hash_module_sources(module_dirs, closure, include_tests=self.args.key_includes_tests)
        self.meta["key_seconds"] = round(time.perf_counter() - t0, 3)
        self.meta["closure_modules"] = len(closure)
        self.meta["targets"] = list(targets)
        self.meta["tests_define_models"] = tests_define_models(module_dirs, closure)
        import odoo.release  # noqa: PLC0415

        version_text = _psql(runner, "SHOW server_version_num;").strip()
        vector = _psql(runner, "SELECT coalesce(max(version), '') FROM pg_available_extension_versions WHERE name = 'vector';").strip()
        key = TemplateKey(
            modules=tuple(["base"] + list(installs)),
            source_hash=source_hash,
            odoo_version=odoo.release.version,
            postgres_major=version_text[:2],
            vector_version=vector,
            options=("demo",),
            crypto_key_fingerprint=hashlib.sha256(hashlib.sha256(os.environ.get("HAMS_CRYPTO_KEY", "").encode()).digest()).hexdigest()[:16],
        )
        self.meta["template_key"] = json.loads(key.canonical())
        self.meta["template_digest"] = key.digest()
        self.install_list = list(installs)
        return key

    # -- ram tablespace -------------------------------------------------------------------------------------------------
    def sql(self, statement):
        return _psql(self.runner, statement)

    # -- one measured run -------------------------------------------------------------------------------------------------
    def run_arm(self, arm, rep, slot, slots, template, test_tags=None):
        plan = arm_plan(arm)
        events_path = os.path.join(self.out_dir, "logs", f"{self.args.set_name}_{arm}_{rep}.events")
        log_path = os.path.join(self.out_dir, "logs", f"{self.args.set_name}_{arm}_{rep}.log")
        for stale in (events_path, log_path):
            if os.path.exists(stale):
                os.remove(stale)
        test_args = ["--solo", "--preflight-done", "-u", ",".join(self.module_list()), "--db", BENCH_DATABASE, "--slots", str(slots), "--slot-held", str(slot)]
        if test_tags:
            test_args += ["--test-tags", test_tags]
        env = dict(os.environ)
        env.update(
            {
                "HAMS_ISOLATED_NS": "1",
                "HAMS_TEST_LOCK_HELD": "1",
                "HAMS_BENCH_EVENTS": events_path,
                "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
                "HAMS_BENCH_STRATEGY": self.args.strategy or "",
                "HAMS_BENCH_COPY_METHOD": self.args.copy_method,
            }
        )
        if not env["HAMS_BENCH_STRATEGY"]:
            del env["HAMS_BENCH_STRATEGY"]
        database = template if plan.build else self.runner.runner_slots.slot_database_name(BENCH_DATABASE, slot)
        keep = {k: v for k, v in env.items() if k.startswith(("HAMS_", "PG", "GIT_DISCOVERY")) or k in ("LANG", "LC_ALL")}
        keep["HOME"] = "/var/lib/odoo"
        with open(events_path, "w", encoding="utf-8"):
            pass
        os.chmod(events_path, 0o666)
        cmd = as_odoo([sys.executable, os.path.abspath(__file__), "_stub", arm, self.repo_root, template] + test_args, env=keep)
        load_before = read_loadscreen()
        lines, returncode, cpu_runner, cpu_postgres = run_measured(cmd, env, self.repo_root, log_path, database)
        events = {}
        if os.path.exists(events_path):
            with open(events_path, encoding="utf-8") as handle:
                for line in handle:
                    item = json.loads(line)
                    events[item["event"]] = item["time"]
        milestones = parse_odoo_log(lines)
        record = record_from_run(
            self.args.set_name, arm, rep, events, milestones, cpu_runner, cpu_postgres, load_before, read_loadscreen(), returncode,
        )
        if plan.tests and not milestones.result:
            record.notes = "NO odoo.tests.result LINE: the run proves nothing"
        self.records.append(record)
        self.save()
        print(f"[bench] {self.args.set_name} {arm} #{rep}: exit {record.exit_code}, total {record.wall_total}s, to first test {record.wall_to_first_test}s, result {record.result}", flush=True)
        return record

    def save(self):
        with open(os.path.join(self.out_dir, f"results_{self.args.set_name}.json"), "w", encoding="utf-8") as handle:
            json.dump({"meta": self.meta, "records": [dataclasses.asdict(r) for r in self.records]}, handle, indent=1)

    # -- template build and clone micro-benchmark ---------------------------------------------------------------------------
    def build_template(self, key, slot, slots, rep):
        record = self.run_arm("build", rep, slot, slots, key.database_name())
        if not build_succeeded(record):
            raise RuntimeError(f"template build failed (exit {record.exit_code}, {record.error_lines} error lines); see {self.out_dir}/logs")
        name = key.database_name()
        self.sql(f"COMMENT ON DATABASE \"{check_identifier(name)}\" IS '{key.comment().replace(chr(39), chr(39) * 2)}';")
        size = int(self.sql(f"SELECT pg_database_size('{name}');").strip())
        filestore = filestore_bytes(name)
        self.meta["build"].append({"rep": rep, "database_bytes": size, "filestore_bytes": filestore, "wall": record.wall_total, "cpu_runner": record.cpu_runner_seconds, "cpu_postgres": record.cpu_postgres_seconds})
        return name

    def clone_microbenchmark(self, template, ram_template, reps):
        import psycopg2  # noqa: PLC0415

        def connect():
            conn = psycopg2.connect(host="localhost", user="odoo", password=os.environ.get("PGPASSWORD", "odoo"), dbname="postgres")
            conn.autocommit = True
            return conn

        combos = [
            ("ssd->ssd wal_log", template, "pg_default", "wal_log", "copy"),
            ("ssd->ssd file_copy", template, "pg_default", "file_copy", "copy"),
            ("ssd->ssd file_copy+reflink", template, "pg_default", "file_copy", "clone"),
        ]
        if ram_template:
            combos += [
                ("ram->ram wal_log", ram_template, BENCH_TABLESPACE, "wal_log", "copy"),
                ("ram->ram file_copy", ram_template, BENCH_TABLESPACE, "file_copy", "copy"),
                ("ram->ssd file_copy", ram_template, "pg_default", "file_copy", "copy"),
                ("ssd->ram file_copy", template, BENCH_TABLESPACE, "file_copy", "copy"),
            ]
        scratch = "hams_bench_clone_probe"
        for label, source, tablespace, strategy, method in combos:
            for rep in range(1, reps + 1):
                statement = clone_database_sql(scratch, source, tablespace=tablespace, strategy=strategy)

                def connect_with_method(method=method):
                    c = connect()
                    c.cursor().execute(f"SET file_copy_method = {method}")  # a session setting, not a server change
                    return c

                result = timed_clone(connect_with_method, statement, f'DROP DATABASE "{scratch}" WITH (FORCE);', _backend_cpu)
                result.update({"combo": label, "rep": rep, "load1": read_loadscreen().get("load1")})
                self.meta["clone"].append(result)
                print(f"[bench] clone {label} #{rep}: {result['wall']:.2f}s cpu {result['cpu']}", flush=True)
        # filestore copy and re-key, timed on a real clone of the template
        name = "hams_bench_rekey_probe"
        self.sql(clone_database_sql(name, template))
        t0 = time.perf_counter()
        self.sql_in(name, rekey_sql())
        self.meta["rekey_wall"] = round(time.perf_counter() - t0, 3)
        self.sql(f'DROP DATABASE "{name}" WITH (FORCE);')
        source = os.path.join(_odoo_filestore_root(), template)
        destination = source + "_probe"
        for rep in range(1, reps + 1):
            t0 = time.perf_counter()
            subprocess.run(as_odoo(["cp", "-a", "--reflink=auto", source, destination]), check=True)
            elapsed = time.perf_counter() - t0
            subprocess.run(as_odoo(["rm", "-rf", "--", destination]), check=False)
            self.meta.setdefault("filestore_copy_wall", []).append(round(elapsed, 3))

    def sql_in(self, database, statement):
        return _psql(self.runner, statement, database=database)


# [@ANCHOR: tools:template_db_run_command]
def run_command(args):
    """`run`: one module set, every arm, `--reps` repetitions interleaved, results written to `--out-dir`."""
    os.environ.setdefault("HAMS_CI_LOAD_GATE_RATIO", "3")
    bench = Bench(args)
    runner = bench.runner
    key = bench.build_key()
    print(f"[bench] set {args.set_name}: {len(key.modules)} modules in the install list, template {key.database_name()}, key hashed in {bench.meta['key_seconds']}s", flush=True)
    if bench.meta["tests_define_models"]:
        print(f"[bench] WARNING: tests define models in {bench.meta['tests_define_models']}", flush=True)
    sys.argv = ["test.py", "benchmark_template_db", "--set", args.set_name]
    slots = runner.runner_slots.configured_slot_count(None)
    slot = runner._acquire_lock_via_queue(runner._test_runner_lock_path(), None, slots, None)
    print(f"[bench] holding test slot {slot} of {slots}", flush=True)
    bench.meta["slot"] = slot
    bench.meta["load_start"] = read_loadscreen()
    arms = [a for a in args.arms.split(",") if a]
    need_ram = any(a in ("fresh_ram", "clone_ram") for a in arms) or args.clone_micro
    ram = RamTablespace(os.path.expanduser(args.tmpfs_dir), args.tmpfs_size, bench.sql, _shell) if need_ram else None
    try:
        if ram:
            ram.__enter__()
        template = key.database_name()
        for rep in range(1, args.build_reps + 1):
            bench.build_template(key, slot, slots, rep)
        verdict, why = verify_template(bench.sql("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = '%s';" % template).strip(), key)
        bench.meta["template_verified"] = [verdict, why]
        ram_template = None
        if ram:
            ram_template = template + "_ram"
            t0 = time.perf_counter()
            bench.sql(clone_database_sql(ram_template, template, tablespace=BENCH_TABLESPACE))
            bench.meta["template_to_ram_wall"] = round(time.perf_counter() - t0, 3)
            bench.meta["ram_template_bytes"] = int(bench.sql(f"SELECT pg_database_size('{ram_template}');").strip())
            source = os.path.join(_odoo_filestore_root(), template)
            subprocess.run(as_odoo(["cp", "-a", "--reflink=auto", source, os.path.join(_odoo_filestore_root(), ram_template)]), check=True)
        if args.clone_micro:
            bench.clone_microbenchmark(template, ram_template, args.reps)
        for rep in range(1, args.reps + 1):
            for arm in arms:
                use_template = ram_template if arm == "clone_ram" else template
                bench.run_arm(arm, rep, slot, slots, use_template, test_tags=args.test_tags)
        bench.meta["load_end"] = read_loadscreen()
        bench.meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        bench.save()
    finally:
        for name in (key.database_name() + "_ram", key.database_name()):
            try:
                if not args.keep_template:
                    bench.sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);')
            except subprocess.CalledProcessError:
                pass
        if not args.keep_template:
            for name in (key.database_name(), key.database_name() + "_ram"):
                remove_filestore(name)
        if ram:
            ram.__exit__(None, None, None)
    return 0


# [@ANCHOR: tools:template_db_key_command]
def key_command(args):
    """`key`: prints the template key and name for a module list (no server work except two version queries)."""
    bench = Bench(args)
    key = bench.build_key()
    print(json.dumps({"database": key.database_name(), "digest": key.digest(), "seconds": bench.meta["key_seconds"], "modules": len(key.modules), "closure": bench.meta["closure_modules"], "tests_define_models": bench.meta["tests_define_models"]}, indent=1))
    return 0


# [@ANCHOR: tools:template_db_report_command]
def report_command(args):
    """`report`: renders the Markdown tables of one or more results files, plus build sizes and clone timings."""
    records, metas = [], []
    for path in args.files:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        for record in data["records"]:
            log = os.path.join(os.path.dirname(os.path.abspath(path)), "logs", f"{record['set_name']}_{record['arm']}_{record['rep']}.log")
            if os.path.exists(log):  # parse again with the current parser, which names setUpClass errors
                with open(log, encoding="utf-8", errors="replace") as handle:
                    milestones = parse_odoo_log(handle)
                record["outcome_names"], record["failed_names"] = list(milestones.tests_started), sorted(milestones.failed)
        records += data["records"]
        metas.append(data["meta"])
    print(render_report(records))
    print("#### outcomes\n")
    print("\n".join(equality_lines(records)))
    print()
    for meta in metas:
        print(f"#### {meta['set']}: build and clone\n")
        for item in meta.get("build", []):
            print(f"- build #{item['rep']}: {item['wall']}s wall, runner CPU {item['cpu_runner']}s, PG CPU {item['cpu_postgres']}s, database {item['database_bytes'] / 1e6:.1f} MB, filestore {item['filestore_bytes'] / 1e6:.1f} MB")
        by_combo = {}
        for item in meta.get("clone", []):
            by_combo.setdefault(item["combo"], []).append(item)
        for combo, items in by_combo.items():
            wall, cpu = summarize([i["wall"] for i in items]), summarize([i["cpu"] for i in items])
            print(f"- CREATE DATABASE {combo}: {wall['median']:.2f}s [{wall['min']:.2f}-{wall['max']:.2f}] wall, backend CPU median {cpu['median']}")
        print(f"- filestore copy: {meta.get('filestore_copy_wall')}  re-key: {meta.get('rekey_wall')}s  template to RAM: {meta.get('template_to_ram_wall')}s  key hash: {meta.get('key_seconds')}s ({meta.get('closure_modules')} modules)")
    return 0


# [@ANCHOR: tools:template_db_build_parser]
def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="measure one module set")
    run.add_argument("--repo-root", required=True, help="a hams_com checkout (or worktree) to test; its hams_shared/tools/test.py is the runner")
    run.add_argument("--set", dest="set_name", choices=("small", "medium", "large"), required=True)
    run.add_argument("--modules-file", default="~/workspace/tmp/wholesys/modules.txt", help="the large set's module list")
    run.add_argument("--reps", type=int, default=3)
    run.add_argument("--build-reps", type=int, default=1, help="template builds to time (the last one is kept)")
    run.add_argument("--arms", default="fresh_ssd,clone_ssd,clone_ram,fresh_ram,clone_update")
    run.add_argument("--test-tags", default=None, help="restrict the tests (start-up measurement for the large set)")
    run.add_argument("--tmpfs-dir", default="/tmp/hams_bench_ramts")
    run.add_argument("--tmpfs-size", default="8G")
    run.add_argument("--out-dir", default="~/workspace/tmp/tplbench/results")
    run.add_argument("--strategy", default=None, choices=(None, "wal_log", "file_copy"), help="CREATE DATABASE strategy in the clone arms")
    run.add_argument("--copy-method", default="copy", choices=("copy", "clone"), help="file_copy_method for the clone arms (session setting)")
    run.add_argument("--no-clone-micro", dest="clone_micro", action="store_false")
    run.add_argument("--key-includes-tests", dest="key_includes_tests", action="store_true", default=True)
    run.add_argument("--keep-template", action="store_true")
    key = sub.add_parser("key", help="print the template key for a module list")
    key.add_argument("--repo-root", required=True)
    key.add_argument("--modules", required=True)
    key.add_argument("--out-dir", default="/tmp")
    key.add_argument("--key-includes-tests", dest="key_includes_tests", action="store_true", default=True)
    key.add_argument("--modules-file", default=None)
    report = sub.add_parser("report", help="render results files as Markdown")
    report.add_argument("files", nargs="+")
    return parser


# [@ANCHOR: tools:template_db_main]
def main(argv=None):
    """Command line entry; see the module docstring. `_stub` is the internal child of a measured run."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_stub":
        return stub_main(argv[1:])
    args = build_parser().parse_args(argv)
    if args.command == "key":
        args.set_name = "large"
    return {"run": run_command, "key": key_command, "report": report_command}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
