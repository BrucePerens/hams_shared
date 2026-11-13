#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Execution engine for run_linters.py: steps, parallel scheduling, timing, input-hash cache and
the `--changed` selection.

run_linters.py describes WHAT to run (a list of `Step`); this module decides HOW: which steps
run (all, `--only`, `--changed`), on how many workers, in what printed order, and whether a
step can be skipped because its inputs are byte-for-byte what they were when it last passed.

Semantics kept from the old sequential orchestrator, step for step:
  * every step runs the same argv, cwd and env as before;
  * a non-zero exit code prints the step's stdout then stderr (after the step's optional
    header); a zero exit prints stdout only when non-empty (unless `print_ok` is False);
  * the run fails (exit 1) when ANY step failed; a failure never stops the other steps, so a
    single run reports everything;
  * output is buffered per step and printed in the order the steps were declared, however
    they were scheduled.
"""

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import hang_guard

# Directories no scanner descends into (heavy, generated, or other sessions' worktrees). Used
# only to fingerprint step inputs and to enumerate changed files; the checkers keep their own
# exclusion lists.
PRUNE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "target",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".mypy_cache",
        ".pytest_cache",
        ".claude",
        ".cargo",
    }
)

CACHE_VERSION = 1


# pytest-xdist (a test-only dependency, tools/requirements-test.txt): a pytest step runs as
# `pytest -n N --dist loadfile` when the step's interpreter can import xdist and the step is not
# listed in XDIST_SERIAL. `loadfile` keeps every test file on one worker, so a module's fixtures
# and the failure report per module stay what they were; only different files overlap. The cache
# key of a step is computed from its declared argv, so a pass recorded serially still counts.
# XDIST_SERIAL: step-name prefixes whose files share ports, files or global state across modules
# (found by running them under -n; see AGENTS.md "Linters"). HAMS_LINT_XDIST=0 turns xdist off,
# HAMS_LINT_XDIST=N forces N workers.
# `pytest devbox_tools/` left the list 2026-11-08: it was the longest step of 56 of 142 measured runs (102 s on average, 294 s at load 150, one process
# using about 40 CPU seconds), and the same 1024 tests (12 skipped, as before) pass under `-n 4 --dist loadfile` in 51 s at the same load.
XDIST_SERIAL = (
    "pytest tools/[host-bound]",  # copies the whole tree into scratch directories and drives the real checkout
    "pytest daemons/hams_simulated_bots",  # idle-CPU and timing tests: another worker's load would fail them
    "pytest ingest/",  # not validated under -n yet
    "pytest scripts/",  # not validated under -n yet
)
XDIST_MAX_WORKERS = 4
_xdist_ok = {}
_xdist_workers = [0]


def xdist_available(interpreter):
    """True when `interpreter` (a path) can import xdist; asked once per interpreter."""
    if interpreter not in _xdist_ok:
        try:
            _xdist_ok[interpreter] = (
                subprocess.run([interpreter, "-c", "import xdist"], capture_output=True, timeout=60).returncode == 0
            )
        except (OSError, subprocess.SubprocessError):
            _xdist_ok[interpreter] = False
    return _xdist_ok[interpreter]


def xdist_workers_for(jobs, cores=None, environ=None):
    """Workers per pytest step when `jobs` steps run side by side: the cores divided among them,
    at least 2 (a single worker is a slower serial run), at most XDIST_MAX_WORKERS; 0 = off."""
    environ = os.environ if environ is None else environ
    forced = environ.get("HAMS_LINT_XDIST", "")
    if forced.isdigit():
        return 0 if int(forced) < 2 else int(forced)
    cores = cores or os.cpu_count() or 1
    return max(2, min(XDIST_MAX_WORKERS, cores // max(1, jobs))) if cores >= 4 else 0


def with_xdist(step, workers):
    """The argv to run for `step`: its own, with `-n workers --dist loadfile` after `pytest` when
    that is allowed (see XDIST_SERIAL); otherwise unchanged."""
    argv = step.argv
    if (
        workers < 2
        or not argv
        or len(argv) < 3
        or argv[1:3] != ["-m", "pytest"]
        or not step.name.startswith("pytest ")
        or step.name.startswith(XDIST_SERIAL)
        or any(a == "-n" or (a.startswith("-n") and a[2:].isdigit()) for a in argv[3:])
        or not xdist_available(argv[0])
    ):
        return argv
    return argv[:3] + ["-n", str(workers), "--dist", "loadfile"] + argv[3:]


# [@ANCHOR: lint_exec:lint_exec_step_class]
@dataclasses.dataclass
class Step:
    """One independently runnable check.

    argv        command line; None for a step whose outcome is known at build time
                (`static_output` is printed and the step counts as failed).
    lock        name of a shared resource the step must hold while it runs (steps with the
                same lock never overlap: one cargo build directory, one Chrome profile).
    fail_header printed before the output of a failing run.
    print_ok    print stdout of a passing run when non-empty.
    missing_msg printed (and the step fails) when argv[0] does not exist.
    roots       directories whose files are the step's inputs (cache key, `--changed`).
    suffixes    file suffixes (lower case, with dot) that are inputs; None means every file.
    uses_tools  the checker script's own code is an input (always true in practice).
    cacheable   False for steps that read the network or the toolchain's state.
    always      run in `--changed` mode regardless of which files changed.
    aliases     names this step answered to before steps were merged; `--only` accepts them.
    gated       a step with no argv (`static_output`, always a failure) that `--changed` runs only
                when one of its inputs changed, instead of always: a missing toolchain fails the
                step only when there is something for it to check.
    needs       names of steps that must have ENDED (passed or failed: a failure never stops
                other steps) before this one starts; a name not in the run is ignored.
    """

    name: str
    argv: list = None
    cwd: str = None
    env: dict = None
    lock: str = None
    fail_header: str = None
    print_ok: bool = True
    missing_msg: str = None
    static_output: str = None
    roots: tuple = ()
    suffixes: frozenset = None
    cacheable: bool = True
    always: bool = False
    gated: bool = False
    aliases: tuple = ()
    needs: tuple = ()  # names of steps that must have ended before this one starts (ignored when not in the run); see `execute`


# [@ANCHOR: lint_exec:lint_exec_result_class]
@dataclasses.dataclass
class Result:
    name: str
    returncode: int
    output: str
    seconds: float
    cached: bool = False
    skipped: str = None
    saturated: str = ""  # why the machine was saturated when a FAILED step ended ("" when it was not, or the step passed)
    timed_out: bool = False  # stopped by its wall-clock limit (hang_guard): a failure that is never rerun; the output names the hang report


def step_to_dict(step):
    """JSON-friendly description of a step (the environment is reduced to the keys that differ
    from the parent's, so secrets in the ambient environment are never printed)."""
    env_delta = {}
    for key, value in (step.env or {}).items():
        if os.environ.get(key) != value:
            env_delta[key] = value
    return {
        "name": step.name,
        "argv": step.argv,
        "cwd": step.cwd,
        "env_delta": env_delta,
        "lock": step.lock,
        "roots": list(step.roots),
        "suffixes": None if step.suffixes is None else sorted(step.suffixes),
    }


# [@ANCHOR: lint_exec:_main_submodule_gitdir]
def _main_submodule_gitdir(common):
    """The git directory of the MAIN checkout's submodule when `common` is a submodule's git directory
    inside a linked worktree of the parent, else `common` unchanged.

    A submodule initialised inside a worktree of its parent keeps its git directory at
    `<parent>/.git/worktrees/<worktree>/modules/<name>`, whose `core.worktree` names the worktree's own
    submodule directory, so `main_worktree_of` answered the worktree itself and the run found no main
    checkout (ESLint "not installed" from a hams_open worktree with hams_shared checked out inside it,
    2026-11-06). The main checkout's submodule is the same name under `<parent>/.git/modules/`."""
    match = re.match(r"^(.*[/\\]\.git)[/\\]worktrees[/\\][^/\\]+[/\\]modules[/\\](.+)$", common)
    if match:
        main = os.path.join(match.group(1), "modules", match.group(2))
        if os.path.isdir(main):
            return main
    return common


def main_worktree_of(path):
    """Working-tree directory of the MAIN checkout of the git repository that contains `path`, or
    None. For a worktree of hams_shared that is the checkout holding node_modules; hams_shared is a
    submodule of hams_open, so its git directory sits under hams_open/.git/modules and
    `git worktree list` names that directory, not the working tree: the working tree is the
    `core.worktree` setting of the common git directory."""
    def git(*args):
        try:
            res = subprocess.run(["git", *args], capture_output=True, text=True)
        except OSError:
            return None
        return res.stdout.strip() if res.returncode == 0 else None

    common = git("-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    common = _main_submodule_gitdir(common)
    configured = git("--git-dir", common, "config", "core.worktree")
    if configured:
        return os.path.realpath(os.path.join(common, configured))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return None


REPO_PAIR = ("hams_com", "hams_open")

# Bound at import: the checkers' tests replace `subprocess.run` to script the checker's own commands (cargo), and
# these git probes of the repository layout must not consume those scripted answers.
_run_git = subprocess.run


def _git_out(path, *args):
    """stdout of `git -C path ARGS`, stripped, or None when git fails or is missing."""
    try:
        res = _run_git(["git", "-C", path, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def repo_name(path):
    """hams_com or hams_open for a checkout or worktree, whatever its directory is called; None when it
    cannot be told. The directory's own name when it is one of the two, else the basename of its
    `origin` URL (a worktree under ~/workspace/tmp/<topic> is named after the topic)."""
    path = os.path.abspath(path)
    if os.path.basename(path) in REPO_PAIR:
        return os.path.basename(path)
    if not _checkout_common_dir(path):
        return None
    url = _git_out(path, "remote", "get-url", "origin") or ""
    name = os.path.basename(url.rstrip("/"))
    name = name[: -len(".git")] if name.endswith(".git") else name
    return name if name in REPO_PAIR else None


def _checkout_common_dir(path):
    """Real path of the git common directory of the checkout whose top level is `path`, or None when `path` is
    not the top of a checkout (a plain directory inside some other repository answers None, not that one's)."""
    top = _git_out(path, "rev-parse", "--show-toplevel")
    if not top or os.path.realpath(top) != os.path.realpath(path):
        return None
    common = _git_out(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.realpath(common) if common else None


def _same_repository(a, b):
    """True when `a` and `b` are the same directory or two checkouts of one git repository (a worktree and
    its main checkout share the git common directory)."""
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    common_a, common_b = _checkout_common_dir(a), _checkout_common_dir(b)
    return bool(common_a and common_a == common_b)


def sibling_repo_root(repo_root, accept=os.path.isdir):
    """The OTHER repository of the pair (hams_open for a hams_com checkout and the reverse), or None.

    The repository is told by identity (`repo_name`), not by its directory name. A hams_open worktree at
    ~/workspace/tmp/<topic> used to take `../hams_open` as its "sibling": the symlink
    ~/workspace/tmp/hams_open -> the main hams_open checkout, so the cargo gate linted hams_open twice,
    worktree and main, in one target directory, and reported main's crates against the worktree's
    metadata (false E0004, 2026-11-05). Candidates, first accepted wins: HAMS_COM_TREE / HAMS_OPEN_TREE
    (as verify_anchors.py), the directory beside `repo_root`, the directory beside its main checkout. A
    candidate must be a directory that `accept` takes and must not be `repo_root`'s own repository, by
    `repo_name` or by sharing its git common directory (a worktree and its main checkout do)."""
    repo_root = os.path.abspath(repo_root)
    own = repo_name(repo_root)
    if own:
        other = REPO_PAIR[1 - REPO_PAIR.index(own)]
    else:
        other = "hams_open" if os.path.basename(repo_root) != "hams_open" else "hams_com"
    candidates = []
    override = os.environ.get(other.upper() + "_TREE", "").strip()
    if override:
        candidates.append(os.path.abspath(override))
    candidates.append(os.path.join(os.path.dirname(repo_root), other))
    main = main_worktree_of(repo_root) if _checkout_common_dir(repo_root) else None
    if main:
        candidates.append(os.path.join(os.path.dirname(main), other))
    for candidate in candidates:
        if not os.path.isdir(candidate) or not accept(candidate):
            continue
        name = repo_name(candidate)
        if (own and name == own) or _same_repository(candidate, repo_root):
            continue
        return candidate
    return None


# [@ANCHOR: lint_exec:has_odoo_module]
def has_odoo_module(path):
    """True when a directory directly under `path` holds an Odoo `__manifest__.py`: the `accept` test of
    `sibling_repo_root` for the checkers that scan modules (hams_open and hams_com both have some)."""
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return any(os.path.isfile(os.path.join(path, name, "__manifest__.py")) for name in names)


# [@ANCHOR: lint_exec:has_agents_md]
def has_agents_md(path):
    """True when `path` holds an `AGENTS.md`: the `accept` test of `sibling_repo_root` for the documentation
    checkers (`check_doc_images`), which scan manuals rather than modules."""
    return os.path.isfile(os.path.join(path, "AGENTS.md"))


def render_output(step, returncode, stdout, stderr):
    """The text the old orchestrator printed for one finished command."""
    parts = []
    if returncode != 0:
        if step.fail_header:
            parts.append(step.fail_header + "\n")
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(stderr)
    elif step.print_ok and stdout and stdout.strip():
        parts.append(stdout)
    return "".join(parts)


# Steps whose tests look at machine-wide state (every entry of /dev/shm, every process called `odoo shell`) and so fail when another lint run
# executes the same tests at the same moment: with lint slots (2026-11-08) two runs did, 3 of 3 runs side by side. They run one at a time per
# machine, across runs, under a flock in ~/.cache/hams-lint/steplocks/ (released when the step ends or the process dies).
MACHINE_WIDE_STEPS = frozenset({"pytest devbox_tools/"})


# [@ANCHOR: lint_exec:machine_wide_lock]
@contextlib.contextmanager
def machine_wide_lock(step_name, directory=None):
    """Hold the machine-wide lock of `step_name` for the block when it is in MACHINE_WIDE_STEPS (waits for another run's step to end);
    other steps pass straight through. `directory` replaces ~/.cache/hams-lint/steplocks (tests). Full entry: tools/README.md."""
    if step_name.split("[")[0] not in MACHINE_WIDE_STEPS:
        yield
        return
    directory = directory or os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "hams-lint", "steplocks")
    handle = None
    try:
        os.makedirs(directory, exist_ok=True)
        handle = open(os.path.join(directory, re.sub(r"[^A-Za-z0-9_.-]+", "_", step_name.split("[")[0]) + ".lock"), "a+", encoding="utf-8")  # noqa: SIM115
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError:
        if handle is not None:
            handle.close()
        handle = None  # no lock directory: run unserialized, as before
    try:
        yield
    finally:
        if handle is not None:
            handle.close()


# ---- timeouts under load (ADR 0117) ------------------------------------------------------------------

PRESSURE_PATH = "/proc/pressure/cpu"
TIMEOUT_TEXT = re.compile(
    r"timed out|time out|timeout|did not (?:complete|respond) within|deadline exceeded|TimeoutExpired", re.IGNORECASE)
DEFAULT_PRESSURE_PERCENT = 25.0
DEFAULT_LOAD_RATIO = 1.25
MAX_TIMEOUT_SCALE = 4.0


# [@ANCHOR: lint_exec:finite]
def _finite(text, default):
    """`text` as a finite float, else `default` (empty, non-numeric, nan and inf all give the default)."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


# [@ANCHOR: lint_exec:cpu_pressure]
def cpu_pressure(path=PRESSURE_PATH):
    """The 10-second average percentage of time some task waited for a processor (`some avg10` in /proc/pressure/cpu), or None
    when the file is missing or unreadable (not Linux, no PSI). Reads one small file; never raises."""
    try:
        with open(path, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("some"):
                    for field in line.split():
                        if field.startswith("avg10="):
                            return _finite(field[len("avg10="):], None)
    except (OSError, ValueError):
        pass
    return None


# [@ANCHOR: lint_exec:saturation]
def saturation(environ=None, loadavg=os.getloadavg, cpus=None, pressure=cpu_pressure):
    """(saturated, reason): is this machine too busy for a time-limited step to be judged by its wall clock?

    True when CPU pressure `some avg10` is at least `HAMS_LINT_TIMEOUT_PSI_PERCENT` (default 25, the load gate's own limit) or the
    one-minute load average is at least `HAMS_LINT_TIMEOUT_LOAD_RATIO` (default 1.25) times the processor count. `reason` names the
    signal that tripped, with its numbers, for the report and the stamp's log; "" when not saturated. Reads /proc and the load
    average; no locks."""
    environ = os.environ if environ is None else environ
    cpus = cpus or os.cpu_count() or 1
    psi_limit = _finite(environ.get("HAMS_LINT_TIMEOUT_PSI_PERCENT") or DEFAULT_PRESSURE_PERCENT, DEFAULT_PRESSURE_PERCENT)
    ratio = _finite(environ.get("HAMS_LINT_TIMEOUT_LOAD_RATIO") or DEFAULT_LOAD_RATIO, DEFAULT_LOAD_RATIO)
    psi = pressure()
    if psi is not None and psi >= psi_limit:
        return True, "CPU pressure %.0f%% (limit %.0f%%)" % (psi, psi_limit)
    try:
        load = loadavg()[0]
    except OSError:
        return False, ""
    if load >= ratio * cpus:
        return True, "load %.1f on %d processors (limit %.2f x)" % (load, cpus, ratio)
    return False, ""


# [@ANCHOR: lint_exec:timeout_scale]
def timeout_scale(environ=None, loadavg=os.getloadavg, cpus=None, pressure=cpu_pressure):
    """Factor by which a step's own time limits are stretched right now: 1.0 on a quiet machine, otherwise the load average per
    processor (at least 1, at most 4). Exported to every step as `HAMS_LINT_TIMEOUT_SCALE` when it starts; a checker that has a
    time limit multiplies it by this (`check_pip_audit.py` does). `HAMS_LINT_TIMEOUT_SCALE_FIXED=1` in the caller's environment pins it to 1."""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_LINT_TIMEOUT_SCALE_FIXED"):
        return 1.0
    sat, _why = saturation(environ, loadavg, cpus, pressure)
    if not sat:
        return 1.0
    cpus = cpus or os.cpu_count() or 1
    try:
        load = loadavg()[0]
    except OSError:
        return 1.0
    return round(min(MAX_TIMEOUT_SCALE, max(1.0, load / cpus)), 2)


# [@ANCHOR: lint_exec:timeout_suspect]
def timeout_suspect(result):
    """True for a result that may have failed only because the machine was starved: it failed, ran (was not a cache hit), the
    machine was saturated when it ended (`Result.saturated`) and its output speaks of a timeout (`TIMEOUT_TEXT`). Such a step is rerun
    once alone after the batch (run_linters.py); a real failure that merely mentions a timeout reruns uselessly but cheaply and is
    still a failure when it fails again."""
    return bool(result.returncode != 0 and not result.cached and not result.timed_out and result.saturated and TIMEOUT_TEXT.search(result.output or ""))


# [@ANCHOR: lint_exec:run_step]
def run_step(step, locks, recent=(), deadline=None, wrap=None):
    """Run one step; never raises for a checker failure. It runs at most `hang_guard.step_limit` seconds (from its `recent` durations; also
    at most until `deadline`, a `time.monotonic()` value, the whole run's cap): past that a hang report is written, the step's own process
    group is stopped and the result is a TIMED OUT failure naming the report. `wrap`, when given, maps the command line to the one run
    (the result cache's trace, `lint_trace_cache.trace_argv`)."""
    started = time.monotonic()
    if step.argv is None:
        elapsed = time.monotonic() - started
        return Result(step.name, 1, step.static_output or "", elapsed)
    lock = locks.get(step.lock) if step.lock else None
    if lock:
        lock.acquire()
    started = time.monotonic()
    try:
        try:
            with machine_wide_lock(step.name):
                started = time.monotonic()
                environ = step.env if step.env is not None else os.environ
                scale = timeout_scale(environ)
                limit = hang_guard.step_limit(step.name, recent, environ, scale)
                capped = deadline is not None and deadline - started < limit
                if capped:
                    limit = max(deadline - started, 1.0)
                env = dict(environ, HAMS_LINT_TIMEOUT_SCALE=str(scale))
                if "faulthandler_timeout" not in env.get("PYTEST_ADDOPTS", ""):  # a hung pytest test dumps every thread's stack by itself
                    env["PYTEST_ADDOPTS"] = (env.get("PYTEST_ADDOPTS", "") + f" -o faulthandler_timeout={int(min(limit, 600))}").strip()
                argv = with_xdist(step, _xdist_workers[0])
                res, report, timed_out = hang_guard.run_limited(
                    wrap(argv) if wrap else argv, limit, step.name, hang_guard.report_dir(environ), cwd=step.cwd, env=env)
        except FileNotFoundError:
            if step.missing_msg is None:
                raise
            elapsed = time.monotonic() - started
            return Result(step.name, 1, step.missing_msg + "\n", elapsed)
    finally:
        if lock:
            lock.release()
    elapsed = time.monotonic() - started
    output = render_output(step, res.returncode, res.stdout, res.stderr)
    if timed_out:
        why = "the whole-run cap" if capped else "its limit"
        output += (f"\n[!] {step.name}: TIMED OUT after {elapsed:.0f} s ({why}); the step's process group was stopped. "
                   f"Hang report: {report or '(not written)'}\n")
        return Result(step.name, hang_guard.TIMED_OUT_CODE, output, elapsed, timed_out=True)
    why = saturation()[1] if res.returncode != 0 else ""
    return Result(step.name, res.returncode, output, elapsed, saturated=why)


def walk_inputs(root, suffixes):
    """Yield (path, size, mtime_ns) of every input file below `root`, pruned, sorted."""
    found = []
    if os.path.isfile(root):
        info = os.stat(root)
        return [(root, info.st_size, info.st_mtime_ns)]
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name in PRUNE_DIRS:
                    continue
                stack.append(entry.path)
                continue
            if suffixes is not None:
                if os.path.splitext(entry.name)[1].lower() not in suffixes:
                    continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            found.append((entry.path, info.st_size, info.st_mtime_ns))
    found.sort()
    return found


def tool_versions():
    """Versions of the external tools a step may depend on; part of every cache key."""
    versions = [sys.version]
    for cmd in (["flake8", "--version"], ["node", "--version"], ["cargo", "--version"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            versions.append(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            versions.append("missing:" + cmd[0])
    return "|".join(versions)


# [@ANCHOR: lint_exec:lint_exec_input_fingerprints_class]
class InputFingerprints:
    """Memoised fingerprint of (root, suffix-set) pairs, shared by all steps of one run."""

    def __init__(self, tools_dir):
        self.tools_dir = tools_dir
        self._memo = {}
        self._lock = threading.Lock()
        self._versions = None

    def of_root(self, root, suffixes):
        key = (root, None if suffixes is None else tuple(sorted(suffixes)))
        with self._lock:
            if key not in self._memo:
                digest = hashlib.sha256()
                for path, size, mtime in walk_inputs(root, suffixes):
                    digest.update(f"{path}\0{size}\0{mtime}\n".encode())
                self._memo[key] = digest.hexdigest()
            return self._memo[key]

    def versions(self):
        with self._lock:
            if self._versions is None:
                self._versions = tool_versions()
            return self._versions

    def step_key(self, step):
        """Safe invalidation rule: the argv/cwd/env-diff, the contents-by-stat of every input
        file, every file of the checker tooling (tools/*.py, *.cjs, *.json) and the external
        tool versions. Anything else changing is, by construction, not an input."""
        digest = hashlib.sha256()
        digest.update(f"v{CACHE_VERSION}\0{step.name}\0".encode())
        digest.update(json.dumps(step.argv).encode())
        digest.update(str(step.cwd).encode())
        for root in step.roots:
            digest.update(self.of_root(root, step.suffixes).encode())
        # .rs/.toml/.lock: the Rust tools (hams_lint and its rule file, rust_function_scan) decide results too.
        tool_suffixes = frozenset({".py", ".cjs", ".js", ".json", ".sh", ".rs", ".toml", ".lock"})
        digest.update(self.of_root(self.tools_dir, tool_suffixes).encode())
        digest.update(self.versions().encode())
        return digest.hexdigest()


# [@ANCHOR: lint_exec:lint_exec_cache_class]
class Cache:
    """JSON file of step name -> key + stored output of the last PASSING run."""

    def __init__(self, path):
        self.path = path
        self.data = {"version": CACHE_VERSION, "ok": {}, "seconds": {}}
        self._lock = threading.Lock()
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if loaded.get("version") == CACHE_VERSION:
                self.data = loaded
        except (OSError, ValueError):
            pass

    def hit(self, name, key):
        with self._lock:
            entry = self.data["ok"].get(name)
        if entry and entry.get("key") == key:
            return entry.get("output", "")
        return None

    def record(self, result, key):
        with self._lock:
            if result.skipped or result.cached:
                return
            self.data["seconds"][result.name] = round(result.seconds, 2)
            if result.returncode == 0 or result.timed_out:  # a quick failure says nothing about how long the step usually takes
                hang_guard.remember(self.data.setdefault("recent", {}), result.name, result.seconds)
            if result.returncode == 0 and key:
                self.data["ok"][result.name] = {
                    "key": key,
                    "output": result.output,
                }
            else:
                self.data["ok"].pop(result.name, None)

    def recent_seconds(self, name):
        with self._lock:
            return list(self.data.get("recent", {}).get(name, ()))

    def known_seconds(self, name):
        return self.data["seconds"].get(name, 0.0)

    def save(self):
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, exist_ok=True)
            tmp_path = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle)
            os.replace(tmp_path, self.path)
        except OSError as error:
            print(f"(lint cache not saved: {error})", file=sys.stderr)


def select_for_changed(steps, changed_paths, run_all):
    """Split `steps` into (to_run, skipped:[(step, reason)]) for `--changed`.

    A step runs when it is `always`, when the checker tooling itself changed (`run_all`), or
    when at least one changed file lies under one of its roots with one of its suffixes. Sound
    under the premise the ratchet establishes: the base commit passes every step, so a step
    whose inputs are untouched still passes."""
    to_run = []
    skipped = []
    for step in steps:
        if run_all or step.always or (step.argv is None and not step.gated):
            to_run.append(step)
            continue
        if _touches(step, changed_paths):
            to_run.append(step)
        else:
            skipped.append((step, "no input changed"))
    return to_run, skipped


def _touches(step, changed_paths):
    for path in changed_paths:
        if step.suffixes is not None:
            if os.path.splitext(path)[1].lower() not in step.suffixes:
                continue
        for root in step.roots:
            prefix = root.rstrip(os.sep) + os.sep
            if path == root or path.startswith(prefix):
                return True
    return False


UNKNOWN_STEP_SECONDS = 30.0  # estimate for a step never measured: early enough that a new long step does not end the run alone


def step_estimates(steps, durations=None, cache=None):
    """{index: estimated seconds} of each step: `durations` (step name -> median seconds over every cache file of the user,
    `lint_lock.known_durations`), else the run's own cache, else UNKNOWN_STEP_SECONDS; a step with no argv costs nothing. Pure
    apart from reading `cache`."""
    out = {}
    for index, step in enumerate(steps):
        if step.argv is None:
            out[index] = 0.0
            continue
        seconds = (durations or {}).get(step.name)
        if seconds is None and cache is not None:
            seconds = cache.known_seconds(step.name) or None
        out[index] = float(seconds) if seconds is not None and math.isfinite(seconds) and seconds >= 0 else UNKNOWN_STEP_SECONDS
    return out


# [@ANCHOR: lint_exec:critical_paths]
def critical_paths(steps, estimates):
    """({index: critical-path seconds}, {index: [indexes it needs]}): a step's own estimate plus the longest chain of steps that
    need it (transitively), so a step that others wait for starts before an equally long step nobody waits for. `needs` names that
    are not in `steps` are dropped. Raises ValueError on a dependency cycle (named). Pure."""
    by_name = {}
    for index, step in enumerate(steps):
        by_name.setdefault(step.name, index)
    needs = {index: sorted({by_name[name] for name in step.needs if name in by_name and by_name[name] != index})
             for index, step in enumerate(steps)}
    dependents = {index: [] for index in range(len(steps))}
    for index, wanted in needs.items():
        for other in wanted:
            dependents[other].append(index)
    paths = {}
    state = {}

    def visit(index, trail):
        if state.get(index) == "done":
            return paths[index]
        if state.get(index) == "open":
            cycle = trail[trail.index(index):] + [index]
            raise ValueError("lint steps need each other in a cycle: " + " -> ".join(steps[i].name for i in cycle))
        state[index] = "open"
        longest = max((visit(other, trail + [index]) for other in dependents[index]), default=0.0)
        paths[index] = estimates[index] + longest
        state[index] = "done"
        return paths[index]

    for index in range(len(steps)):
        visit(index, [])
    return paths, needs


# [@ANCHOR: lint_exec:lint_exec__scheduler_class]
class _Scheduler:
    """Hands steps to workers as a dependency graph: a step is READY when every step it `needs` has ended and its `lock` is free; of the
    ready steps the one with the longest remaining critical path goes first (for a locked step, at least the estimated work still waiting
    on that lock, since those steps cannot overlap). A worker never sits blocked on a lock another step holds: it takes the next ready
    step instead. Thread-safe."""

    def __init__(self, steps, estimates):
        self.steps = steps
        self.estimates = estimates
        self.paths, self.needs = critical_paths(steps, estimates)
        self.waiting = set(range(len(steps)))
        self.ended = set()
        self.held = set()
        self.lock_left = {}
        for index, step in enumerate(steps):
            if step.lock:
                self.lock_left[step.lock] = self.lock_left.get(step.lock, 0.0) + estimates[index]
        self.cond = threading.Condition()

    def _priority(self, index):
        step = self.steps[index]
        rank = self.paths[index]
        if step.lock:
            rank = max(rank, self.lock_left.get(step.lock, 0.0))
        return (-rank, index)

    def _ready(self, index):
        step = self.steps[index]
        if step.lock and step.lock in self.held:
            return False
        return all(other in self.ended for other in self.needs[index])

    def take(self, block=True):
        """The next step index to run, or None when every step has been handed out. Blocks while steps wait only on running ones;
        with `block` False returns None at once instead (`has_waiting` tells the two Nones apart)."""
        with self.cond:
            while True:
                if not self.waiting:
                    return None
                ready = [index for index in self.waiting if self._ready(index)]
                if ready:
                    index = min(ready, key=self._priority)
                    self.waiting.discard(index)
                    step = self.steps[index]
                    if step.lock:
                        self.held.add(step.lock)
                        self.lock_left[step.lock] -= self.estimates[index]
                    return index
                if not block:
                    return None
                self.cond.wait()

    def has_waiting(self):
        with self.cond:
            return bool(self.waiting)

    def done(self, index):
        with self.cond:
            self.ended.add(index)
            lock = self.steps[index].lock
            if lock:
                self.held.discard(lock)
            self.cond.notify_all()

    def pause(self, seconds):
        """Sleep up to `seconds`, waking early when a step ends."""
        with self.cond:
            if self.waiting:
                self.cond.wait(seconds)


ELASTIC_POLL_SECONDS = 10.0


# [@ANCHOR: lint_exec:elastic_jobs]
def elastic_jobs(jobs, environ=None, cpus=None):
    """How many EXTRA workers a run may add while the machine is mostly idle: HAMS_LINT_ELASTIC_JOBS when it is a whole number (0 turns it
    off), else `jobs` (a run may double), never more than leaves the run at half the processors. Pure. Full entry: tools/README.md."""
    environ = os.environ if environ is None else environ
    cpus = cpus or os.cpu_count() or 2
    try:
        wanted = int(environ.get("HAMS_LINT_ELASTIC_JOBS", jobs))
    except ValueError:
        wanted = jobs
    return max(0, min(wanted, cpus // 2 - jobs))


# [@ANCHOR: lint_exec:lint_exec__admission_class]
class _Admission:
    """Admits one extra worker at a time: at least `spacing` seconds after the previous admission (a step that has just started has not
    loaded the processors yet) and only while `headroom()` holds (lint_lock's burst test: processors under 60 % busy, CPU stall under 10 %,
    24 GiB free, the same numbers that open a burst slot)."""

    def __init__(self, headroom, spacing, clock=time.monotonic):
        self.headroom = headroom
        self.spacing = spacing
        self.clock = clock
        self.last = None
        self.lock = threading.Lock()

    def admit(self):
        with self.lock:
            now = self.clock()
            if self.last is not None and now - self.last < self.spacing:
                return False
            if not self.headroom():
                return False
            self.last = now
            return True


def machine_headroom(environ=None):
    """(headroom function, spacing seconds) from lint_lock's burst rule; a function that always refuses when lint_lock cannot be imported."""
    try:
        import lint_lock
    except ImportError:
        return (lambda: False), 60.0
    environ = os.environ if environ is None else environ
    busy_limit, psi_limit, min_mem, spacing = lint_lock.burst_limits(environ)

    def headroom():
        ok, _why = lint_lock.burst_headroom(lint_lock.cpu_busy_percent(), lint_lock.cpu_stall(), lint_lock.mem_available_gb(),
                                            busy_limit, psi_limit, min_mem)
        return ok
    return headroom, spacing


# [@ANCHOR: lint_exec:traced_work]
def traced_work(step, trace_cache, run):
    """The Result of a test step through the result cache: a stored pass whose every input is unchanged is returned as a cache hit (its
    original output plus a note); otherwise the step runs (`run(wrap)`) under the trace when strace can trace here, and a pass with no
    veto (network, host services, hardware, environment, randomness; inputs changed while it ran) is stored. A failure is never stored and
    never replaced by a stored pass. The trace never decides a verdict on its own: a run that executed a privileged program or a tracer
    (`lint_trace_cache.trace_blockers`), or that failed with a timeout under the trace, is rerun untraced at once and that result counts;
    such a step then runs untraced (`TraceCache.untraceable`)."""
    import lint_trace_cache
    key = trace_cache.step_key(step)
    entry = trace_cache.lookup(step, key)
    if entry is not None:
        return Result(step.name, 0, (entry.get("output") or "") + lint_trace_cache.cache_hit_note(entry), 0.0, cached=True)
    if not trace_cache.usable or trace_cache.untraceable(key):
        return run(None)
    handle, trace_file = tempfile.mkstemp(prefix="lint-trace-", suffix=".txt")
    os.close(handle)
    try:
        listening = lint_trace_cache.listening_ports()
        started, started_ns = time.time(), time.time_ns()
        result = run(lambda argv: lint_trace_cache.trace_argv(argv, trace_file))
        stopped = lint_trace_cache.aborted(trace_file)
        if stopped:  # a program that behaves differently under the trace started: the traced run was ended, the untraced run decides
            trace_cache.mark_untraceable(key, step, [stopped])
            return run(None)
        trace = trace_cache.read_trace(trace_file)
        blockers = lint_trace_cache.trace_blockers(trace) if trace is not None else []
        if not blockers and result.returncode != 0:  # the trace never decides a failure: the untraced run does
            again = run(None)
            if again.returncode == 0:
                trace_cache.mark_untraceable(key, step, ["failed under the trace and passed untraced"])
            return again
        if blockers:
            trace_cache.mark_untraceable(key, step, blockers)
            return run(None)
        if result.returncode == 0 and not result.timed_out:
            trace_cache.record(step, key, result, trace, listening, started, started_ns)
        return result
    finally:
        for path in (trace_file, trace_file + ".abort"):
            with contextlib.suppress(OSError):
                os.unlink(path)


# [@ANCHOR: lint_exec:execute]
def execute(steps, jobs, cache=None, fingerprints=None, out=None, durations=None, extra=0, admission=None, trace_cache=None):
    """Run `steps` on `jobs` workers, scheduled as a dependency graph longest-critical-path first (`_Scheduler`; `durations` is step
    name -> measured median seconds, see `step_estimates`); print each step's output in declaration order as soon as every earlier
    step has been printed. Up to `extra` more workers join while the machine is mostly idle (`admission`, an `_Admission`; by default
    lint_lock's burst rule): each takes one step per admission. Test steps are looked up in and recorded to `trace_cache` (a
    `lint_trace_cache.TraceCache`; see `traced_work`) instead of `cache`. Returns the list of Results (declaration order).
    """
    out = out or sys.stdout
    # side by side: no more steps than there are to run (one pytest step alone may use the whole machine)
    _xdist_workers[0] = xdist_workers_for(min(jobs, max(1, sum(1 for step in steps if step.argv is not None))))
    locks = {}
    for step in steps:
        if step.lock and step.lock not in locks:
            locks[step.lock] = threading.Lock()
    results = [None] * len(steps)
    keys = [None] * len(steps)
    deadline = time.monotonic() + hang_guard.run_cap()

    def work(index):
        step = steps[index]
        key = None
        if trace_cache is not None and step.argv is not None and trace_cache.traced(step.name):
            recent = cache.recent_seconds(step.name) if cache is not None else ()
            return index, traced_work(step, trace_cache, lambda wrap: run_step(step, locks, recent, deadline, wrap=wrap)), None
        if cache is not None and fingerprints is not None and step.cacheable:
            if step.argv is not None:
                key = fingerprints.step_key(step)
                stored = cache.hit(step.name, key)
                if stored is not None:
                    return index, Result(step.name, 0, stored, 0.0, cached=True), key
        result = run_step(step, locks, cache.recent_seconds(step.name) if cache is not None else (), deadline)
        return index, result, key

    scheduler = _Scheduler(steps, step_estimates(steps, durations, cache))
    finished = queue.Queue()

    def guarded(body):
        """A worker that dies of a bug in the runner hands the error to the main thread (which would otherwise wait for ever)."""
        def run():
            try:
                body()
            except BaseException as error:
                finished.put((None, error, None))
        return run

    @guarded
    def worker():
        while True:
            index = scheduler.take()
            if index is None:
                return
            run_one(index)

    def run_one(index):
        try:
            finished.put(work(index))
        except BaseException as error:  # a bug in the runner itself: hand it to the main thread, never hang it
            finished.put((index, error, None))
        finally:
            scheduler.done(index)

    if extra and admission is None:
        admission = _Admission(*machine_headroom())

    @guarded
    def extra_worker():
        """Runs a step only when admitted, one admission per step: when the machine fills up the extra workers fall idle again."""
        while scheduler.has_waiting():
            if not admission.admit():
                scheduler.pause(ELASTIC_POLL_SECONDS)
                continue
            index = scheduler.take(block=False)
            if index is None:
                scheduler.pause(ELASTIC_POLL_SECONDS)
                continue
            run_one(index)

    printed = 0
    pending = {}
    base = max(1, min(jobs, len(steps)))
    extra = max(0, min(extra, len(steps) - base))
    with ThreadPoolExecutor(max_workers=base + extra) as pool:
        for _ in range(base):
            pool.submit(worker)
        for _ in range(extra):
            pool.submit(extra_worker)
        for _ in range(len(steps)):
            index, result, key = finished.get()
            if isinstance(result, BaseException):
                raise result
            results[index] = result
            keys[index] = key
            pending[index] = result
            while printed in pending:
                text = pending.pop(printed).output
                if text:
                    out.write(text if text.endswith("\n") else text + "\n")
                    out.flush()
                printed += 1
    if cache is not None:
        for index, result in enumerate(results):
            cache.record(result, keys[index])
        cache.save()
    return results


def timing_table(results, skipped=(), wall=None, top=None):
    """Human-readable per-step wall-clock table, slowest first."""
    lines = []
    ranked = sorted(results, key=lambda r: -r.seconds)
    if top:
        ranked = ranked[:top]
    lines.append(f"{'step':<58} {'sec':>8}  status")
    for res in ranked:
        if res.cached:
            status = "cached"
        else:
            status = "ok" if res.returncode == 0 else "FAILED"
        lines.append(f"{res.name:<58} {res.seconds:8.1f}  {status}")
    total_cpu = sum(r.seconds for r in results)
    lines.append(
        f"{len(results)} steps run, {len(skipped)} skipped; "
        f"sum of step times {total_cpu:.1f}s"
        + (f", wall clock {wall:.1f}s" if wall is not None else "")
    )
    return "\n".join(lines)
