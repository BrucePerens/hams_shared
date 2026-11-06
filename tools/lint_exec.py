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

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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


@dataclasses.dataclass
class Result:
    name: str
    returncode: int
    output: str
    seconds: float
    cached: bool = False
    skipped: str = None


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


def run_step(step, locks):
    """Run one step; never raises for a checker failure."""
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
            res = subprocess.run(
                with_xdist(step, _xdist_workers[0]),
                capture_output=True,
                text=True,
                cwd=step.cwd,
                env=step.env,
            )
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
    return Result(step.name, res.returncode, output, elapsed)


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
        tool_suffixes = frozenset({".py", ".cjs", ".js", ".json", ".sh"})
        digest.update(self.of_root(self.tools_dir, tool_suffixes).encode())
        digest.update(self.versions().encode())
        return digest.hexdigest()


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
            if result.returncode == 0 and key:
                self.data["ok"][result.name] = {
                    "key": key,
                    "output": result.output,
                }
            else:
                self.data["ok"].pop(result.name, None)

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


def execute(steps, jobs, cache=None, fingerprints=None, out=None):
    """Run `steps` on `jobs` workers; print each step's output in declaration order as soon
    as every earlier step has been printed. Returns the list of Results (declaration order).
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

    def work(index):
        step = steps[index]
        key = None
        if cache is not None and fingerprints is not None and step.cacheable:
            if step.argv is not None:
                key = fingerprints.step_key(step)
                stored = cache.hit(step.name, key)
                if stored is not None:
                    return index, Result(step.name, 0, stored, 0.0, cached=True), key
        result = run_step(step, locks)
        return index, result, key

    order = list(range(len(steps)))
    if cache is not None:
        order.sort(key=lambda i: -cache.known_seconds(steps[i].name))
    printed = 0
    pending = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = [pool.submit(work, i) for i in order]
        for future in futures:
            index, result, key = future.result()
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
