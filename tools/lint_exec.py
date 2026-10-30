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


@dataclasses.dataclass
class Result:
    name: str
    returncode: int
    output: str
    seconds: float
    cached: bool = False
    skipped: str = None


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
    configured = git("--git-dir", common, "config", "core.worktree")
    if configured:
        return os.path.realpath(os.path.join(common, configured))
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
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
                step.argv,
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
