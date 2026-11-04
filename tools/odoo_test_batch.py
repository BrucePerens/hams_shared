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

Runs that stay solo: --solo, a dirty tree, a class or method selector (`-u mod:Class`), --test-tags, a mode other
than standard, --mcp, --coverage, --pause-on-fail, --offline-isolation, --profile, --build-daemons, a run started
by a batch leader, and a working directory that is not a repository root.

Splitting the union across machines by recorded duration is planned (`plan_machine_split` computes the split) but
the leader still runs everything on this machine: `run_on_machine` is the stub for remote_odoo_test.py.
"""

import json
import os
import re
import subprocess
import sys
import time

import batch_queue
import lint_batch

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


# [@ANCHOR: odoo_test_batch:ineligible_reason]
def ineligible_reason(args, environ, cwd_is_root, dirty):
    """Why this test run must run alone (a short sentence), or None when it may join a batch. `args` is
    test.py's parsed namespace; `cwd_is_root`: the working directory is the repository's top level; `dirty`:
    uncommitted changes."""
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
    for flag in ("test_tags", "coverage", "mcp", "pause_on_fail", "offline_isolation", "profile", "build_daemons"):
        if getattr(args, flag, None):
            return "--%s" % flag.replace("_", "-")
    if module_scope(args.module) is None:
        return "no whole-module -u list"
    if not cwd_is_root:
        return "not run from a repository root"
    if dirty:
        return "uncommitted changes in the tree (only committed tips are batched)"
    return None


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
                break
    return found


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


# [@ANCHOR: odoo_test_batch:run_on_machine]
def run_on_machine(machine, tree, modules, log_directory):
    """STUB: run `test.py -u <modules>` for the combined `tree` on another test machine through
    hams_com's devbox_tools/remote_odoo_test.py (bruce-yoga, mac1). Not implemented: remote_odoo_test.py lives
    in hams_com and syncs a trimmed tree plus a hams_open checkout; wiring it needs a change there and a
    per-machine queue (to-do night_shift_todo/medium/split-test-batches-across-machines-*.md). Only "local" works."""
    raise NotImplementedError("running a test batch on %s is not implemented; see the runbook" % machine)


# [@ANCHOR: odoo_test_batch:estimate_seconds]
def estimate_seconds(modules, durations, default=DEFAULT_MODULE_SECONDS):
    """Estimated seconds to test `modules`: the sum of their recorded durations, `default` for an unrecorded one."""
    return float(sum(durations.get(m, default) for m in modules))


def _durations_path(sdir):
    return os.path.join(sdir, "durations.json")


# [@ANCHOR: odoo_test_batch:load_durations]
def load_durations(sdir):
    """{module: seconds} recorded for this repository's test queue; {} when none or unreadable."""
    try:
        with open(_durations_path(sdir), encoding="utf-8") as handle:
            data = json.load(handle)
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float)) and v == v and v < 1e9}
    except (OSError, ValueError, AttributeError):
        return {}


# [@ANCHOR: odoo_test_batch:record_durations]
def record_durations(sdir, modules, seconds):
    """Fold one run into the table: each module of a run that took `seconds` in all is credited an equal share
    (a crude figure: test.py reports no per-module time), averaged 50/50 with an earlier record."""
    if not modules or not seconds or seconds < 0:
        return
    share = float(seconds) / len(modules)
    table = load_durations(sdir)
    for module in modules:
        table[module] = share if module not in table else (table[module] + share) / 2.0
    try:
        os.makedirs(sdir, exist_ok=True)
        tmp = _durations_path(sdir) + ".tmp-%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(table, handle)
        os.replace(tmp, _durations_path(sdir))
    except OSError:
        pass


# [@ANCHOR: odoo_test_batch:write_pass_record]
def write_pass_record(tip, modules, batch_id, environ=None):
    """Record that the modules passed for commit `tip` in a batch: ``~/.cache/hams-test/passed/<tip>``, one line
    ``tests <time> modules=a,b batch=N``. Nothing is written when `modules` is empty. Returns the path or None."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    directory = os.path.join(base, "hams-test", "passed")
    if not modules:
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, tip)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("tests %s modules=%s batch=%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), ",".join(modules), batch_id))
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
    the modules test.py implicated, each attributed to the members whose scope names it; no implicated module
    (a crash, an Odoo error before the tests) is unattributable and splits the batch. Durations are recorded
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

    def collect(self, tree, results, output, code):
        union = sorted({m for member in self.current_members for m in member.get("scope", ())})
        if self.sdir:
            record_durations(self.sdir, union, time.monotonic() - self._started)
        text = ""
        try:
            with open(os.path.join(os.path.dirname(results), "logs", "filtered_test.txt"), encoding="utf-8",
                      errors="replace") as handle:
                text = handle.read()
        except OSError:
            pass
        if text and self._log:
            self._emit("\n===== filtered_test.txt =====\n" + text[-100000:] + "\n")
        if code == 0:
            return union, {}
        failures = {}
        for module in implicated_modules(text):
            lines = [line.strip() for line in text.splitlines() if module in line][:30]
            failures["module %s" % module] = "\n".join(lines) or module
        if not failures:
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
    reason = ineligible_reason(args, environ, cwd_is_root, dirty)
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
        lambda tip, modules, batch_id: write_pass_record(tip, modules, batch_id, environ),
        sdir=sdir, environ=environ)
    return lint_batch.run_member(
        cwd, os.path.basename(key), branch, head, "origin/main", scope, runner, timeout=args.wait_timeout, out=out,
        sdir=sdir, estimate=lambda modules: estimate_seconds(modules, durations), kind=KIND,
        solo_hint="test.py -u %s --solo" % args.module, environ=environ)
