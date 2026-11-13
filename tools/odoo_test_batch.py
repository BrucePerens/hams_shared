#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Odoo test side of the coalescing batch queue (ADR 0115, docs/runbooks/LINT_TEST_BATCHING.md).

`test.py -u <modules>` calls `maybe_run_batched` before it takes any lock. A plain run of whole modules from a
committed repository root joins the batch for this repository (batch_queue.py); the leader builds one combined
tree (origin/main plus every member's branch), runs ONE `test.py -u <union of the members' modules> --solo` in it,
reads the modules Odoo's extractor implicated (the list test.py writes into filtered_test.txt), attributes each to
the branches that asked for that module, and writes a pass record (`~/.cache/hams-test/passed/<tip>`) for every
branch that passed. Every caller prints its own verdict and exits with its own code.

Runs that stay solo: --solo, a dirty tree, a tree whose `hams_shared` is linked by hand to something other than the main
checkout's (the combined tree links the main one; `lint_batch.foreign_shared_checkout`), a class or method selector (`-u mod:Class`), --test-tags, a mode other
than standard, --mcp, --coverage, --pause-on-fail, --offline-isolation, --profile, --build-daemons, a run started
by a batch leader, and a working directory that is not a repository root.

Remote test hosts (bruce-yoga, the mac1 VM): hams_com's devbox_tools/remote_odoo_test.py is the front end. Its
callers join a queue of their own kind ("rtest", `run_remote_member`); the leader (`RemoteTestBatch`) splits the
union of the members' modules over the idle hosts by recorded duration (`plan_machine_split`), calls
`run_on_machine` once per host in parallel, and each host builds origin/main + the members' commits from its own
git clone and runs test.py there. Failures are attributed per module exactly as for local batches.
"""

import fcntl
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import batch_queue
import lint_batch
import runner_slots
import atomic_io

KIND = "test"
RUNNER_ENV = "HAMS_TEST_BATCH_RUNNER"
DEFAULT_MODULE_SECONDS = 300.0


# [@ANCHOR: odoo_test_batch:module_scope]
def module_scope(module_arg):
    """Module names of a `-u` argument as a sorted list, or None when it cannot be batched: empty, or any
    entry carries a `:Class` selector. A `hams_open/` prefix is dropped as test.py does."""
    if not module_arg:
        return None
    modules = []
    for item in module_arg.split(","):
        item = item.strip().replace("hams_open/", "")
        if not item or ":" in item:
            return None
        modules.append(item)
    return sorted(set(modules))


# [@ANCHOR: odoo_test_batch:union_of]
def union_of(members, field):
    """The set of module names in the `field` ("scope" or "install") of every member; a member without the key (written
    before the field existed) contributes nothing."""
    return {m for member in members for m in member.get(field, ())}


# [@ANCHOR: odoo_test_batch:ineligible_reason]
def ineligible_reason(args, environ, cwd_is_root, dirty, foreign_shared=False):
    """Why this test run must run alone (a short sentence), or None when it may join a batch. `args` is
    test.py's parsed namespace; `cwd_is_root`: the working directory is the repository's top level; `dirty`:
    uncommitted changes; `foreign_shared`: the tree's `hams_shared` is not the main checkout's
    (`lint_batch.foreign_shared_checkout`): the combined tree links the main one, so a branch under test in a
    hand-linked hams_shared would be tested with main's helpers and pass."""
    if getattr(args, "solo", False):
        return "--solo"
    if environ.get(RUNNER_ENV):
        return "this is the combined run of a batch"
    if environ.get("HAMS_TEST_BATCH") == "off":
        return "HAMS_TEST_BATCH=off"
    if environ.get("HAMS_TEST_LOCK_HELD") == "1":
        return "started inside a test run"
    if args.internal_ns_init or args.preflight_done or args.queue_status:
        return "an internal phase of a run"
    if args.mode != "standard":
        return "mode %s" % args.mode
    for flag in ("test_tags", "coverage", "mcp", "pause_on_fail", "offline_isolation", "profile", "build_daemons", "keep_db", "raw_failures"):
        if getattr(args, flag, None):
            return "--%s" % flag.replace("_", "-")
    if module_scope(args.module) is None:
        return "no whole-module -u list"
    if not cwd_is_root:
        return "not run from a repository root"
    if dirty:
        return "uncommitted changes in the tree (only committed tips are batched)"
    if foreign_shared:
        return "this tree's hams_shared is not the main checkout's (the combined tree would test with the main one)"
    return None


# [@ANCHOR: odoo_test_batch:main_checkout_root]
def main_checkout_root(repo):
    """The directory of the main checkout of the git repository `repo` belongs to (the parent of its common git
    directory, so a worktree nested anywhere resolves to the real checkout); `repo` itself when git cannot say."""
    result = subprocess.run(["git", "-C", repo, "rev-parse", "--git-common-dir"], capture_output=True, text=True)
    common = result.stdout.strip()
    if result.returncode != 0 or not common:
        return repo
    if not os.path.isabs(common):
        common = os.path.join(repo, common)
    return os.path.dirname(os.path.normpath(common))


# [@ANCHOR: odoo_test_batch:implicated_modules]
def implicated_modules(text):
    """Modules test.py's extractor named in filtered_test.txt: the ``  - <module>`` lines after "these modules
    are implicated in the failure(s):". Empty when it named none (a crash, or a failure with no traceback)."""
    found, active = [], False
    for line in str(text).splitlines():
        if "modules are implicated in the failure" in line:
            active = True
            continue
        if active:
            match = re.match(r"^\s+-\s+([A-Za-z0-9_]+)\s*$", line)
            if match:
                found.append(match.group(1))
            elif line.strip():
                active = False  # another section follows; a later header (a second host's text) starts a new list
    return found


# [@ANCHOR: odoo_test_batch:failing_tests]
def failing_tests(text):
    """The set of failing test ids in a ``filtered_test.txt``, each ``"<module>:<Class>.<method>"``.

    Two line forms are read: Odoo's logger form ``odoo.addons.<module>.tests.<file>: ERROR: Class.method`` (also
    ``FAIL:``) and the unittest form ``ERROR: setUpClass (odoo.addons.<module>.tests.<file>.Class)`` (also
    ``test_x (...)``, ``tearDownClass``). A failure appears twice in the file (once in the extracted blocks, once
    in the context); the set holds it once. Empty when no failing test is named (a crash, a module-load error)."""
    found = set()
    for line in str(text).splitlines():
        match = re.search(r"odoo\.addons\.([A-Za-z0-9_]+)\.tests[\w.]*: (?:ERROR|FAIL): ([A-Za-z_]\w*\.\w+)\s*$", line)
        if match:
            found.add("%s:%s" % (match.group(1), match.group(2)))
            continue
        match = re.search(r"(?:ERROR|FAIL): (\w+) \(odoo\.addons\.([A-Za-z0-9_]+)\.tests\.(?:[\w.]*\.)?(\w+)\)\s*$", line)
        if match:
            found.add("%s:%s.%s" % (match.group(2), match.group(3), match.group(1)))
    return found


# [@ANCHOR: odoo_test_batch:odoo_failure_count]
def odoo_failure_count(text):
    """The number of failed tests and errors Odoo itself counted: the sum over every ``odoo.tests.result: N failed, M error(s) of T tests``
    line of a ``filtered_test.txt``, or None when the text holds no such line (the extractor keeps it only sometimes), so no count is known."""
    found = re.findall(r"odoo\.tests\.result:\s*(\d+) failed, (\d+) error\(s\) of \d+ tests", str(text))
    return sum(int(failed) + int(errors) for failed, errors in found) if found else None


# What a nested test.py prints at exit 0 when no test ran (it normally exits non-zero then; this is the second line of defence).
VACUOUS_PASS_RE = re.compile(r"TEST RUN FAILED: no tests ran|printed no odoo\.tests\.result line")


# [@ANCHOR: odoo_test_batch:vacuous_reason]
def vacuous_reason(union, output):
    """Why an exit status 0 of the nested test.py proves nothing, or None: no module was tested (an empty `-u` list), or its output says no test ran
    (`TEST RUN FAILED: no tests ran`, `printed no odoo.tests.result line`). Pure."""
    if not union:
        return "test.py exited 0 but no module was selected for testing: nothing was tested"
    if VACUOUS_PASS_RE.search(str(output)):
        return "test.py exited 0 but its output says no test ran (no odoo.tests.result verdict)"
    return None


# [@ANCHOR: odoo_test_batch:module_finder]
def module_finder(modules):
    """`find_tokens(line)` for `blame_by_tokens`: the entries of `modules` named as whole words in a line."""
    pattern = re.compile(r"(?<![\w])(%s)(?![\w])" % "|".join(re.escape(m) for m in sorted(modules, key=len, reverse=True))) \
        if modules else None

    def find(line):
        return sorted(set(pattern.findall(line))) if pattern else []

    return find


# [@ANCHOR: odoo_test_batch:plan_machine_split]
def plan_machine_split(modules, durations, machines, default=DEFAULT_MODULE_SECONDS):
    """Split `modules` over `machines` by recorded duration (longest first onto the least loaded machine, the
    LPT rule). `durations` is {module: seconds}; a module with no record counts `default`. Returns
    {machine: [modules]} (machines with nothing are omitted). One machine, or none given, returns everything
    on the first machine (or "local")."""
    machines = list(machines) or ["local"]
    if len(machines) == 1:
        return {machines[0]: sorted(modules)}
    load = {m: 0.0 for m in machines}
    plan = {m: [] for m in machines}
    for module in sorted(modules, key=lambda m: (-durations.get(m, default), m)):
        target = min(machines, key=lambda m: (load[m], machines.index(m)))
        plan[target].append(module)
        load[target] += durations.get(module, default)
    return {m: sorted(mods) for m, mods in plan.items() if mods}


REMOTE_KIND = "rtest"
REMOTE_LOCK_DIR = os.path.join("~", ".cache", "hams-remote-test")


# [@ANCHOR: odoo_test_batch:idle_machines]
def idle_machines(machines, lock_dir=None):
    """The entries of `machines` whose run lock is free right now, in order. The lock is the one
    remote_odoo_test.py takes (``<lock_dir>/<machine>.lock``, default ~/.cache/hams-remote-test); it is probed
    with a non-blocking flock and released at once, so the answer can be stale by the time a run starts (the
    driver then waits for the lock)."""
    directory = os.path.expanduser(lock_dir or REMOTE_LOCK_DIR)
    os.makedirs(directory, exist_ok=True)
    free = []
    for machine in machines:
        with open(os.path.join(directory, machine + ".lock"), "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            fcntl.flock(handle, fcntl.LOCK_UN)
        free.append(machine)
    return free


# [@ANCHOR: odoo_test_batch:run_on_machine]
def run_on_machine(machine, tree, repo_name, members, modules, driver, log_path, work_dir, union=None):
    """Run test.py on the test host `machine` for the members' commits, and return the host's result. The host
    INSTALLS/UPDATES `union` (default: `modules`) and TESTS only `modules`, its share, so splitting a batch never
    changes the installed set (a test of one module may need another module of the batch to be installed).

    Calls the driver (hams_com's remote_odoo_test.py, `driver` is its argv prefix) as ``driver machine tree
    union --repo R --members-file F --result-json J --log L --wait [--share modules]``. The host builds its tree from its own
    origin/main with every member tip merged and runs test.py there. Blocks until the host is done (minutes to an
    hour). Returns the driver's result dict ``{"host", "exit_code", "merged": [sha], "conflicted": [sha],
    "filtered": text, "log", "seconds", "error"}``; a driver that crashed or wrote nothing yields exit_code 75 with
    "error" set (the run could not start: this is not a test failure)."""
    members_file = os.path.join(work_dir, machine + "-members.json")
    result_file = os.path.join(work_dir, machine + "-result.json")
    seen, unique = set(), []
    for member in members:
        if member["tip"] not in seen:
            seen.add(member["tip"])
            unique.append({"branch": member["branch"], "tip": member["tip"]})
    with open(members_file, "w", encoding="utf-8") as handle:
        json.dump(unique, handle)
    for stale in (result_file,):
        if os.path.exists(stale):
            os.unlink(stale)  # a result from an earlier call must never be read as this one's
    union = sorted(union) if union else sorted(modules)
    argv = [*driver, machine, tree, ",".join(union), "--repo", repo_name, "--members-file", members_file,
            "--result-json", result_file, "--log", log_path, "--wait"]
    if sorted(modules) != union:
        argv += ["--share", ",".join(sorted(modules))]
    proc = subprocess.run(argv, capture_output=True, text=True, errors="replace")
    try:
        with open(result_file, encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, ValueError):
        return {"host": machine, "exit_code": 75, "merged": [], "conflicted": [], "filtered": "", "log": log_path,
                "seconds": 0.0, "error": "the driver wrote no result (exit %s): %s" % (
                    proc.returncode, (proc.stdout + proc.stderr)[-400:].strip())}
    result.setdefault("filtered", "")
    return result


# [@ANCHOR: odoo_test_batch:estimate_seconds]
def estimate_seconds(modules, durations, default=DEFAULT_MODULE_SECONDS):
    """Estimated seconds to test `modules`: the sum of their recorded durations, `default` for an unrecorded one."""
    return float(sum(durations.get(m, default) for m in modules))


def _durations_path(sdir):
    return os.path.join(sdir, "durations.json")


def _module_durations_path(sdir):
    return os.path.join(sdir, "module_durations.json")


def _read_seconds_table(path):
    """{name: seconds} from the JSON file at `path`; entries that are not finite numbers in [0, 1e9) are dropped; {} when
    the file is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        table = {}
        for key, value in data.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(float(value)) and 0 <= value < 1e9:
                table[key] = float(value)
        return table
    except (OSError, ValueError, AttributeError):
        return {}


def _write_seconds_table(path, table):
    """Write `table` as JSON at `path` atomically (temporary file, then rename); a failed write is ignored."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_io.atomic_write(path, json.dumps(table), mode=0o644)
    except OSError:
        pass


# [@ANCHOR: odoo_test_batch:load_durations]
def load_durations(sdir):
    """{module: seconds} recorded for this repository's test queue; {} when none or unreadable. A module with a
    measured time (`record_module_durations`, from the hosts' own per-module test times) uses it; any other module
    uses the crude equal-share figure of `record_durations`."""
    table = _read_seconds_table(_durations_path(sdir))
    table.update(_read_seconds_table(_module_durations_path(sdir)))
    return table


# [@ANCHOR: odoo_test_batch:record_durations]
def record_durations(sdir, modules, seconds):
    """Fold one run into the crude table: each module of a run that took `seconds` in all is credited an equal share
    (used only when the host reported no per-module times), averaged 50/50 with an earlier record. Measured times
    (`record_module_durations`) take precedence over this table in `load_durations`."""
    if not modules or not seconds or seconds < 0:
        return
    share = float(seconds) / len(modules)
    table = _read_seconds_table(_durations_path(sdir))
    for module in modules:
        table[module] = share if module not in table else (table[module] + share) / 2.0
    _write_seconds_table(_durations_path(sdir), table)


# [@ANCHOR: odoo_test_batch:record_module_durations]
def record_module_durations(sdir, durations):
    """Fold the measured per-module test times of one host's run into `<sdir>/module_durations.json`:
    `durations` is {module: seconds} as the host's ``odoo.tests.stats`` lines report them
    (`remote_odoo_test.module_test_seconds`). A module's first measurement is stored as it is, a later one is averaged
    50/50 with the stored figure (the hosts differ in speed). Entries that are not finite numbers in [0, 1e9) are
    ignored; written atomically; a failed write is ignored."""
    if not durations:
        return
    table = _read_seconds_table(_module_durations_path(sdir))
    changed = False
    for module, seconds in durations.items():
        if not isinstance(module, str) or not module or isinstance(seconds, bool) \
                or not isinstance(seconds, (int, float)) or not math.isfinite(float(seconds)) \
                or not 0 <= seconds < 1e9:
            continue
        table[module] = float(seconds) if module not in table else (table[module] + float(seconds)) / 2.0
        changed = True
    if changed:
        _write_seconds_table(_module_durations_path(sdir), table)


# [@ANCHOR: odoo_test_batch:write_pass_record]
def write_pass_record(tip, modules, batch_id, environ=None, with_tips=()):
    """Record that the modules passed for commit `tip` in a batch: ``~/.cache/hams-test/passed/<tip>``, one line
    ``tests <time> modules=a,b batch=N`` and, when other branches were in the run, `` with=<12 hex>,..`` (what the tip was
    checked with, `batch_queue.with_field`). Nothing is written when `modules` is empty. Returns the path or None."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    directory = os.path.join(base, "hams-test", "passed")
    if not modules:
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, tip)
        atomic_io.atomic_write(path, "tests %s modules=%s batch=%s%s\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%S"), ",".join(modules), batch_id, batch_queue.with_field(with_tips)), mode=0o644)
        return path
    except OSError:
        return None


# [@ANCHOR: odoo_test_batch:OdooTestBatch]
class OdooTestBatch(lint_batch.LintBatch):
    """The leader's runner for tests: a `lint_batch.LintBatch` whose run is `test.py -u <union>` and whose
    owners are modules.

    Constructor: as LintBatch, plus `passthrough` carrying `-d`, `-c`, `--wait-timeout` of the caller's command.
    Per run: `-u` is the union of the scopes of the members merged into that run's tree, `-l` a log directory
    next to the tree (`filtered_test.txt` is read from it, appended to the batch log and deleted with the
    tree), `--solo --wait` so the nested test.py neither batches again nor fails fast on the box lock. Failures:
    the modules test.py implicated plus the module of every failing test the report names, each attributed to the members whose scope
    names it; no named module (a crash, an Odoo error before the tests), or more failures counted by Odoo than named, is unattributable and splits the batch. Durations are recorded
    after each run (`record_durations`)."""

    kind = KIND
    runner_env = RUNNER_ENV

    def __init__(self, *args, sdir=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sdir = sdir
        self.command = self._test_command
        self._started = time.monotonic()

    def _test_command(self, tree, results):
        union = sorted({m for member in self.current_members for m in member.get("scope", ())})
        self._started = time.monotonic()
        logs = os.path.join(os.path.dirname(results), "logs")
        os.makedirs(logs, exist_ok=True)
        os.chmod(logs, 0o777)  # the nested test.py may run as the odoo user and writes here
        return [sys.executable, os.path.join(tree, "tools", "test.py"), "-u", ",".join(union), "--solo", "--wait",
                "-l", os.path.join(os.path.dirname(results), "logs"), *self.passthrough]

    def verdict_code(self, code, selected, output):
        if code == 0 and vacuous_reason(selected, output):
            return 1
        return code

    # [@ANCHOR: odoo_test_batch:OdooTestBatch_collect]
    def collect(self, tree, results, output, code):
        union = sorted({m for member in self.current_members for m in member.get("scope", ())})
        if self.sdir:
            record_durations(self.sdir, union, time.monotonic() - self._started)
        text = ""
        # The nested test.py may have run in a slot above 1 (ADR 0116), which names its report
        # filtered_test.slot-K.txt; this log directory is the batch's own, so one report is in it.
        reports = sorted(glob.glob(os.path.join(os.path.dirname(results), "logs", "filtered_test*.txt")))
        try:
            with open(reports[0], encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except (IndexError, OSError):
            pass
        if text and self._log:
            self._emit("\n===== filtered_test.txt =====\n" + text[-100000:] + "\n")
        if code == 0:
            reason = vacuous_reason(union, output)
            return union, ({"test.py": "%s\n%s" % (reason, output[-5000:])} if reason else {})
        failures = {}
        # The modules of every named failing test count too: the extractor implicates only a module whose failure printed a traceback, so a module
        # whose failing test printed none would otherwise pass its branch.
        named = failing_tests(text)
        for module in dict.fromkeys(implicated_modules(text) + sorted({test_id.split(":", 1)[0] for test_id in named})):
            lines = [line.strip() for line in text.splitlines() if module in line][:30]
            failures["module %s" % module] = "\n".join(lines) or module
        counted = odoo_failure_count(text)
        if not failures or (counted is not None and counted > len(named)):
            # No module named, or Odoo counted more failures than the report names: some failure belongs to nobody we can name, so the batch splits.
            failures["test.py"] = (text or output)[-20000:]
        return union, failures

    def tokens(self, members):
        owners = {}
        for member in members:
            for module in member.get("scope", ()):
                owners.setdefault(module, [])
                if member["tip"] not in owners[module]:
                    owners[module].append(member["tip"])
        return owners, module_finder(sorted(owners))


# [@ANCHOR: odoo_test_batch:batches_that_may_run]
def batches_that_may_run(args, environ):
    """How many local test batches may run at once: the machine's test slot count (ADR 0116), because each batch's nested
    `test.py` takes a slot like any run; 1 when the count cannot be read. `args.slots` (if given) can only raise it."""
    try:
        return runner_slots.configured_slot_count(getattr(args, "slots", None), environ)
    except (runner_slots.SlotConfigError, OSError):
        return 1


# [@ANCHOR: odoo_test_batch:maybe_run_batched]
def maybe_run_batched(args, environ=None, cwd=None, out=print):
    """Called by test.py's main() before any lock. Returns the exit code of this caller's batched run, or
    None when the run goes on alone (ineligible, a merge conflict with another branch, a dead batch runner).

    `args`: test.py's parsed namespace (module, db, config, wait_timeout, ...). Reads git for the repository
    root, dirty state, tip and branch name. Blocks until the caller's verdict is known (minutes to an hour)."""
    environ = os.environ if environ is None else environ
    cwd = os.getcwd() if cwd is None else cwd
    top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    root = top.stdout.strip() if top.returncode == 0 else ""
    cwd_is_root = bool(root) and os.path.realpath(root) == os.path.realpath(cwd)
    status = subprocess.run(["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True)
    dirty = status.returncode != 0 or bool(status.stdout.strip())
    foreign = bool(root) and lint_batch.foreign_shared_checkout(root, main_checkout_root(root))
    reason = ineligible_reason(args, environ, cwd_is_root, dirty, foreign_shared=foreign)
    if reason is not None:
        if args.module and reason not in ("--solo", "an internal phase of a run"):
            out("[*] test batch: running alone (%s)" % reason)
        return None
    head = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    if not head:
        return None
    if not branch or branch == "HEAD":
        branch = head[:10]
    key = batch_queue.repo_key(cwd)
    sdir = batch_queue.state_dir(KIND, key, environ)
    scope = module_scope(args.module)
    durations = load_durations(sdir)
    passthrough = ["-d", args.db, "-c", args.config]
    if args.wait_timeout is not None:
        passthrough += ["--wait-timeout", str(args.wait_timeout)]
    runner = OdooTestBatch(
        cwd, os.path.basename(key), "origin/main", passthrough,
        lambda tip, modules, batch_id, with_tips: write_pass_record(tip, modules, batch_id, environ, with_tips),
        sdir=sdir, environ=environ)
    return lint_batch.run_member(
        cwd, os.path.basename(key), branch, head, "origin/main", scope, runner, timeout=args.wait_timeout, out=out,
        sdir=sdir, estimate=lambda modules: estimate_seconds(modules, durations), kind=KIND,
        solo_hint="test.py -u %s --solo" % args.module, environ=environ, max_running=batches_that_may_run(args, environ))


# [@ANCHOR: odoo_test_batch:RemoteTestBatch]
class RemoteTestBatch(OdooTestBatch):
    """The leader's runner for batches tested on the remote test hosts: an `OdooTestBatch` whose run goes to
    machines instead of a local combined tree.

    Constructor: ``RemoteTestBatch(repo, repo_name, base, passthrough, stamp_writer, sdir, machines, driver,
    work_base=None, probe=idle_machines, runner=run_on_machine, out=print, environ=None)``. `machines` is the
    ordered list of host names it may use, `driver` the argv prefix of remote_odoo_test.py, `probe`/`runner` are
    replaceable (tests use fakes). Per run (`_run_once`): the union of the group's modules is split over ALL the
    machines (`plan_machine_split`, recorded durations; a busy host's share waits for its lock, `probe` only labels it busy), one thread per machine calls `run_on_machine`, and the
    results are merged: a conflicted merge (found by the hosts, who all build the same tree) becomes a "conflicted"
    member, a host that could not start makes the run fatal (exit 75), a failure is the modules each host's
    ``filtered_test.txt`` implicates (none implicated: unattributable, the batch splits). Durations are recorded per
    module from the host's own per-module test times (`outcome["durations"]`, `record_module_durations`); a host that
    reported none is credited an equal share of its wall time (`record_durations`). Each host's log is appended to the batch log."""

    kind = REMOTE_KIND

    def __init__(self, repo, repo_name, base, passthrough, stamp_writer, sdir, machines, driver, work_base=None,
                 probe=idle_machines, runner=run_on_machine, out=print, environ=None):
        super().__init__(repo, repo_name, base, passthrough, stamp_writer, sdir=sdir, out=out, environ=environ)
        self.machines = list(machines)
        self.environmental = []
        self.driver = list(driver)
        self.work_base = work_base
        self.probe = probe
        self.runner = runner

    def __call__(self, batch_id, members, log_path):
        self.environmental = []
        doc = super().__call__(batch_id, members, log_path)
        if self.environmental:
            note = "%d test(s) fail identically on origin/main (environmental) and were not counted against any branch: %s" % (
                len(self.environmental), ", ".join(self.environmental[:5]) + (" ..." if len(self.environmental) > 5 else ""))
            for verdict in doc["verdicts"].values():
                if verdict.get("status") == batch_queue.PASSED:
                    verdict["note"] = note
        return doc

    def _run_once(self, batch_id, group):
        self._run_no += 1
        union = sorted({m for member in group for m in member.get("scope", ())})
        installed = sorted(union_of(group, "install") | set(union))  # companions are installed, never tested
        # Every host gets a share, busy or not: each host's driver waits for that host's lock (--wait). Splitting over only
        # the hosts idle at this instant put a whole 24-module batch on bruce-yoga while mac1 and bruce17 freed up minutes
        # later (2026-11-05, batch 31: Bruce, "Combine tests").
        idle = self.probe(self.machines)
        busy = [m for m in self.machines if m not in idle]
        plan = plan_machine_split(union, load_durations(self.sdir), list(self.machines))
        self.current_members = group
        self.remember([m["tip"] for m in group], union)
        self._emit("\n===== remote run %d.%d: %d branch(es): %s =====\n" % (
            batch_id, self._run_no, len({m["tip"] for m in group}), ", ".join(m["branch"] for m in group)))
        if set(installed) - set(union):
            self._emit("[*] installed on every host, not tested: %s\n" % ",".join(sorted(set(installed) - set(union))))
        for machine, mods in plan.items():
            self._emit("[*] %s: %s%s\n" % (machine, ",".join(mods), " (busy now: its share starts when the host is free)"
                                            if machine in busy else ""))
        work = tempfile.mkdtemp(prefix="rtest_b%d_r%d_" % (batch_id, self._run_no), dir=self.work_base)
        results, lock = {}, threading.Lock()

        def work_one(machine, mods):
            log_path = os.path.join(work, machine + ".log")
            outcome = self.runner(machine, self.repo, self.repo_name, group, mods, self.driver, log_path, work, installed)
            with lock:
                results[machine] = (mods, log_path, outcome)

        threads = [threading.Thread(target=work_one, args=item) for item in plan.items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        failures, exit_code, conflicted_tips, errors, environmental = {}, 0, set(), [], set()
        if not results:
            # No host was given anything to test (no module in the union): an empty set of passing hosts is not a pass.
            shutil.rmtree(work, ignore_errors=True)
            return {"exit_code": 75, "fatal": True, "failures": {}, "conflicted": [],
                    "detail": "no host was given a module to test: nothing was tested"}
        for machine in sorted(results):
            mods, log_path, outcome = results[machine]
            text = outcome.get("filtered") or ""
            try:
                with open(log_path, encoding="utf-8", errors="replace") as handle:
                    tail = handle.read()[-100000:]
            except OSError:
                tail = ""
            self._emit("\n===== %s: exit %s, %.0f s, log %s =====\n%s\n" % (
                machine, outcome.get("exit_code"), outcome.get("seconds") or 0, log_path, tail))
            if outcome.get("error"):
                errors.append("run on %s could not start: %s" % (machine, outcome["error"]))
                continue
            conflicted_tips |= set(outcome.get("conflicted") or ())
            if outcome.get("durations"):
                record_module_durations(self.sdir, outcome["durations"])
            else:
                record_durations(self.sdir, mods, outcome.get("seconds") or 0)
            code = outcome.get("exit_code", 1)
            if code == 0:
                continue
            ids = failing_tests(text)
            if ids:
                same_on_main = ids & failing_tests(outcome.get("baseline_filtered") or "") \
                    if outcome.get("baseline_exit") not in (None, 0) else set()
                environmental |= same_on_main
                for test_id in sorted(ids - same_on_main):
                    module, name = test_id.split(":", 1)
                    method = name.split(".")[-1]
                    lines = [line.strip() for line in text.splitlines() if method in line][:10]
                    failures["module %s" % module] = failures.get("module %s" % module, "") + "\n".join(
                        ["failed: " + test_id] + lines) + "\n"
                    exit_code = exit_code or code
                continue
            exit_code = exit_code or code
            implicated = implicated_modules(text)
            for module in implicated:
                lines = [line.strip() for line in text.splitlines() if module in line][:30]
                failures["module %s" % module] = "\n".join(lines) or module
            if not implicated:
                failures["%s test.py" % machine] = (text or tail)[-20000:]
        self.environmental = sorted(environmental)
        if environmental:
            self._emit("\n[*] %d failing test(s) fail identically on origin/main (environmental, not charged to any "
                       "branch):\n    %s\n" % (len(environmental), "\n    ".join(sorted(environmental)[:40])))
        shutil.rmtree(work, ignore_errors=True)
        if errors:
            return {"exit_code": 75, "fatal": True, "failures": {}, "conflicted": [], "detail": "; ".join(errors)}
        if not failures:
            exit_code = 0  # nothing but environmental failures: the branches are not at fault
        conflicted = [(m, "merge conflict with origin/main or another branch on the test host")
                      for m in group if m["tip"] in conflicted_tips]
        return {"exit_code": exit_code, "failures": failures, "conflicted": conflicted, "selected": union}


# [@ANCHOR: odoo_test_batch:run_remote_member]
def run_remote_member(tree, repo_name, branch, tip, modules_arg, machines, driver, wait=False, environ=None,
                      out=print, timeout=None, install=()):
    """The caller's side of a remote test run (remote_odoo_test.py): join the "rtest" queue of the repository of
    `tree`, block until this branch's verdict, print it, and return the exit code (0 passed, 1 failed, 75 could
    not run or gave up waiting), or None when the branch must run alone (its merge conflicts with another
    branch of the batch, or the batch runner kept dying). `modules_arg` is the comma-separated whole-module -u
    list (a ``:Class`` selector raises ValueError: such a run cannot be batched). `machines` are host names,
    `driver` the argv prefix of remote_odoo_test.py. `wait` is accepted for symmetry (the queue always waits);
    `timeout` seconds bound the wait for a verdict. `install` are modules the hosts must INSTALL for this branch's tests
    without testing them (test companions: a service account, an imported helper); they join the member as ``install``
    and `RemoteTestBatch._run_once` adds them to every host's installed set but not to the tested split."""
    environ = os.environ if environ is None else environ
    scope = module_scope(modules_arg)
    if scope is None:
        raise ValueError("a run with a class selector or no module list cannot be batched")
    key = batch_queue.repo_key(tree)
    sdir = batch_queue.state_dir(REMOTE_KIND, key, environ)
    durations = load_durations(sdir)
    runner = RemoteTestBatch(
        tree, os.path.basename(key), "origin/main", [],
        lambda sha, modules, batch_id, with_tips: write_pass_record(sha, modules, batch_id, environ, with_tips),
        sdir, machines, driver, out=out, environ=environ)
    return lint_batch.run_member(
        tree, os.path.basename(key), branch, tip, "origin/main", scope, runner, timeout=timeout, out=out,
        sdir=sdir, estimate=lambda modules: estimate_seconds(modules, durations) / max(1, len(machines)),
        kind=REMOTE_KIND, solo_hint="remote_odoo_test.py <host> <tree> %s --solo" % modules_arg, environ=environ,
        install=install)
