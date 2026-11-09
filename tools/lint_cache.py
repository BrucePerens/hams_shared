#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Persistent lint caches with a disk budget: warm Cargo target directories and per-file result caches.

Why: `cargo clippy` in a fresh checkout compiles every dependency (1,100-1,250 s on a cold build);
the slow Python scanners re-parse files that did not change. Both are cached outside the repository,
shared by every worktree on the machine, and capped so they cannot fill the disk.

CARGO TARGET DIRECTORIES
  * ONE directory per repository and toolchain per machine: `<cache>/hams-lint-cargo/<repo>-<rustc>`.
    Never per worktree or per branch: registry dependencies are keyed by version and features, not by
    the checkout, so every worktree reuses them. (Only the workspace's own crates, whose unit hash
    contains their path, are rebuilt in a new worktree.)
  * Incremental compilation is OFF (`CARGO_INCREMENTAL=0`) for these builds. The old shared directory
    measured 31 GB of which 26 GB was `incremental/`, one session per worktree path that is never
    reused; the dependencies were 4 GB. See docs/runbooks/LINT_CACHES.md for the measurement.
  * LOCK: `<dir>.lock` (beside the directory, so clearing it cannot remove the lock) is flocked
    exclusively for the whole of prune + build. Cargo has its own lock on the directory, so two builds
    never corrupt it either way; this one also guarantees that a prune never runs under a live build.
  * BUDGET, per machine, from `cache_budgets` in lint_machines.json (the machine name is
    HAMS_LINT_MACHINE, `devbox` when unset): `cargo_gb` is the cap of one directory, `free_floor_gb`
    the free space the filesystem must keep. Enforced BEFORE every build by `prepare_cargo_target`:
    over budget or under the floor, prune; if that is not enough, clear the directory and build cold.
    It never fails the run for lack of space.
  * PRUNING is least-recently-used by whole compilation unit. A unit is every file that carries one
    16-hex-digit cargo unit hash within a profile directory (`deps/`, `.fingerprint/`, `build/`,
    `incremental/`); deleting all of them together leaves cargo a clean "not built" unit instead of
    a half-deleted one. Use time is max(atime, mtime). Pruning stops at 80 % of the budget so the next
    build does not prune again.
  * A directory whose toolchain (or repository) is no longer used for UNUSED_DAYS days is removed.
  * HAMS_LINT_NO_CACHE=1 (run_linters.py --no-cache) builds in a throwaway directory.
  * NAMED DIRECTORIES: a test that compiles (the relay export test) takes `named_cargo_session(name)`, a directory
    `<cache>/hams-lint-cargo/<name>-<rustc>` under the same budget, LRU pruning, lock and unused-days rule.

OTHER CACHES the lint and test steps leave outside the budget (`other_caches`) are only reported by `show`, never
pruned: a mirror, a test source tree, a browser download and container root file systems the builds need.

RESULT CACHES (`ResultCache`): per-file results of the slow Python static checks keyed by the content
hash of the file and of the checker's own version; stored in `<cache>/hams-lint-results/<checker>/`,
one small JSON file per (checker, version). Capped by `results_mb`.

CLI:  python3 tools/lint_cache.py show | prune | clear [cargo|results|all]
"""

import contextlib
import fcntl
import glob
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

try:
    import ram_dirs
except ImportError:  # this file alone (`lint_remote.py caches` runs it over stdin on a runner): the disk locations
    ram_dirs = None

GB = 1024 ** 3
MB = 1024 ** 2
UNUSED_DAYS = 14
PRUNE_TARGET_FRACTION = 0.8
DEFAULT_CARGO_GB = 25
DEFAULT_FLOOR_GB = 20
DEFAULT_RESULTS_MB = 200
MACHINES_FILE = os.path.join(os.path.dirname(os.path.abspath(globals().get("__file__", "lint_cache.py"))), "lint_machines.json")
UNIT_SUFFIX_CHARS = frozenset("0123456789abcdef")


def cache_root(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")


# [@ANCHOR: lint_cache:disk_cargo_base]
def disk_cargo_base(environ=None):
    """`<cache>/hams-lint-cargo` on the disk: where the Cargo caches live when RAM is refused (see `cargo_base`)."""
    return os.path.join(cache_root(environ), "hams-lint-cargo")


# [@ANCHOR: lint_cache:cargo_base]
def cargo_base(environ=None):
    """`/dev/shm/hams-lint-cargo` when `ram_dirs.lint_cargo_base` allows RAM (a big machine with room, no explicit
    XDG_CACHE_HOME), else `disk_cargo_base`. A refusal prints a warning (once per process)."""
    return (ram_dirs.lint_cargo_base(environ) if ram_dirs else None) or disk_cargo_base(environ)


# [@ANCHOR: lint_cache:in_ram]
def in_ram(path, environ=None):
    """True when `path` is below the RAM root of `ram_dirs` (always False when ram_dirs is not importable)."""
    return bool(ram_dirs and ram_dirs.under_ram_root(path, environ=environ))


# [@ANCHOR: lint_cache:cargo_bases]
def cargo_bases(environ=None):
    """Every base that may hold Cargo caches: the RAM one (when it exists) and the disk one, for show/prune/clear."""
    environ = os.environ if environ is None else environ
    bases = []
    if ram_dirs and not environ.get("XDG_CACHE_HOME"):
        ram = os.path.join(ram_dirs.policy(environ).root, ram_dirs.LINT_CARGO_NAME)
        if os.path.isdir(ram):
            bases.append(ram)
    bases.append(disk_cargo_base(environ))
    return bases


def results_base(environ=None):
    return os.path.join(cache_root(environ), "hams-lint-results")


def machine_name(environ=None, machines_file=None):
    """HAMS_LINT_MACHINE, else the `cache_budgets` entry whose `hostname` is this host's, else `devbox`. (The
    runner machines run this file from their own mirror and are not told their inventory name.)"""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_LINT_MACHINE"):
        return environ["HAMS_LINT_MACHINE"]
    try:
        with open(machines_file or MACHINES_FILE, encoding="utf-8") as handle:
            table = json.load(handle).get("cache_budgets", {})
        host = socket.gethostname()
        for name, entry in table.items():
            if isinstance(entry, dict) and host in (entry.get("hostnames") or []):
                return name
    except (OSError, ValueError):
        pass
    return "devbox"


def budgets(environ=None, machines_file=None):
    """{cargo_gb, free_floor_gb, results_mb} for this machine: lint_machines.json `cache_budgets`
    (key = machine name, "default" otherwise), then the module defaults. HAMS_LINT_CARGO_GB,
    HAMS_LINT_FREE_FLOOR_GB and HAMS_LINT_RESULTS_MB override."""
    environ = os.environ if environ is None else environ
    out = {"cargo_gb": DEFAULT_CARGO_GB, "free_floor_gb": DEFAULT_FLOOR_GB, "results_mb": DEFAULT_RESULTS_MB}
    try:
        with open(machines_file or MACHINES_FILE, encoding="utf-8") as handle:
            table = json.load(handle).get("cache_budgets", {})
    except (OSError, ValueError):
        table = {}
    for source in (table.get("default", {}), table.get(machine_name(environ), {})):
        for key in out:
            if isinstance(source.get(key), (int, float)) and math.isfinite(source[key]) and source[key] >= 0:
                out[key] = source[key]
    for key, env_name in (("cargo_gb", "HAMS_LINT_CARGO_GB"), ("free_floor_gb", "HAMS_LINT_FREE_FLOOR_GB"), ("results_mb", "HAMS_LINT_RESULTS_MB")):
        try:
            if environ.get(env_name):
                value = float(environ[env_name])
                if math.isfinite(value) and value >= 0:  # "nan" and "inf" parse as floats
                    out[key] = value
        except ValueError:
            pass
    return out


def toolchain_id(environ=None):
    """`<release>-<8 hex of the full `rustc -vV` text>` (the host triple and commit are in the hash), or
    "norustc" when there is no rustc."""
    try:
        res = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, timeout=60, env=environ)
    except (OSError, subprocess.SubprocessError):
        return "norustc"
    if res.returncode != 0:
        return "norustc"
    release = "unknown"
    for line in res.stdout.splitlines():
        if line.startswith("release:"):
            release = line.split(":", 1)[1].strip()
    return f"{release}-{hashlib.sha256(res.stdout.encode()).hexdigest()[:8]}"


# [@ANCHOR: lint_cache:lint_slot]
def lint_slot(environ=None):
    """The lint run slot (lint_lock.RunLock) this process runs in, 1 when it holds none."""
    environ = os.environ if environ is None else environ
    try:
        return max(1, int(environ.get("HAMS_LINT_SLOT") or 1))
    except ValueError:
        return 1


# [@ANCHOR: lint_cache:cargo_target_dir]
def cargo_target_dir(repo_name, environ=None):
    """`<cache>/hams-lint-cargo/<repo>-<toolchain>`, with `-sK` appended for a run in lint slot K above 1: concurrent runs on a
    machine with several slots each build in a directory of their own (they would only wait for one another's exclusive lock
    on one shared directory, and a build directory is not safe to share between branches that build at the same time)."""
    slot = lint_slot(environ)
    return os.path.join(cargo_base(environ), f"{repo_name}-{toolchain_id(environ)}" + (f"-s{slot}" if slot > 1 else ""))


# [@ANCHOR: lint_cache:legacy_cargo_dirs]
def legacy_cargo_dirs(environ=None):
    """The pre-budget cargo target directories that the budgeted `hams-lint-cargo` replaced, in `cache_root`:
    the old single lint directory (`adopt_legacy_dir` renames it into the budget) and the relay export test's
    `hams-relay-export-test-target` (19 GB on mac1-vm, 2026-11-03). `show` reports them; `clear cargo` removes them."""
    root = cache_root(environ)
    return [os.path.join(root, "hams-cargo-target-lint"), os.path.join(root, "hams-relay-export-test-target")]


# [@ANCHOR: lint_cache:other_caches]
def other_caches(environ=None):
    """`[(label, path, why)]`: the directories the lint and test steps create or need outside the Cargo and
    result budgets. They are reported by `show` (and so by `lint_remote.py caches`) and never pruned here:
    each is needed by the next run or by another build."""
    environ = os.environ if environ is None else environ
    home = environ.get("HOME") or os.path.expanduser("~")
    return [
        ("lint mirror", os.path.join(home, "hams-lint-mirror"), "the runner's copy of the repositories, refreshed by every run"),
        ("test sources", "/opt/hams-testsrc", "the sources of the Odoo test hosts' test trees"),
        ("playwright", os.path.join(home, ".cache", "ms-playwright"), "browsers for the browser tests, downloaded once"),
        ("machines", "/var/lib/machines", "root file systems for the arm64 Pi builds; needed"),
        ("relay test target", os.path.join(home, "ctest-target"),
         "Cargo target of the relay tests, pruned by remote_cargo_test.py (ctest_prune), not here"),
        ("relay test sources", os.path.join(home, "ctest-src"),
         "source copies for the relay tests, pruned by remote_cargo_test.py (ctest_prune), not here"),
    ]


def dir_bytes(path):
    """Sum of file sizes under `path` (hard links counted once, symlinks not followed)."""
    total = 0
    seen = set()
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            key = (info.st_dev, info.st_ino)
            if info.st_nlink > 1:
                if key in seen:
                    continue
                seen.add(key)
            total += info.st_size
    return total


def free_bytes(path):
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


@contextlib.contextmanager
def locked(path, exclusive=True, wait=True):
    """flock on `path` + ".lock" (created beside `path`). Yields True when held, False when `wait` is False and
    another process holds it."""
    lock_path = path.rstrip(os.sep) + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        flags = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if wait else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _unit_hash(filename):
    """The 16-hex-digit cargo unit hash in a file or directory name (`libfoo-0123456789abcdef.rlib`,
    `foo-0123456789abcdef`), or None."""
    stem = filename.split(".", 1)[0]
    if "-" not in stem:
        return None
    tail = stem.rsplit("-", 1)[1]
    if len(tail) == 16 and set(tail) <= UNIT_SUFFIX_CHARS:
        return tail
    return None


def cargo_units(target_dir):
    """{(profile_dir, hash): [paths, bytes, last_use]} over every profile directory of `target_dir`. A path is
    either a file or a whole directory (a `.fingerprint/<unit>` or `build/<unit>` or `incremental/<unit>`
    directory is one path)."""
    units = {}

    def add(profile, unit_hash, path):
        entry = units.setdefault((profile, unit_hash), [[], 0, 0.0])
        entry[0].append(path)
        if os.path.isdir(path) and not os.path.islink(path):
            size = 0
            use = 0.0
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        info = os.lstat(os.path.join(root, name))
                    except OSError:
                        continue
                    size += info.st_size
                    use = max(use, info.st_atime, info.st_mtime)
            entry[1] += size
            entry[2] = max(entry[2], use)
        else:
            try:
                info = os.lstat(path)
            except OSError:
                return
            entry[1] += info.st_size
            entry[2] = max(entry[2], info.st_atime, info.st_mtime)

    try:
        top = os.listdir(target_dir)
    except OSError:
        return units
    profile_dirs = []
    for name in top:
        full = os.path.join(target_dir, name)
        if not os.path.isdir(full) or os.path.islink(full):
            continue
        if os.path.isdir(os.path.join(full, "deps")) or os.path.isdir(os.path.join(full, ".fingerprint")):
            profile_dirs.append(full)
        else:  # <triple>/<profile>
            for sub in os.listdir(full):
                subfull = os.path.join(full, sub)
                if os.path.isdir(os.path.join(subfull, "deps")) or os.path.isdir(os.path.join(subfull, ".fingerprint")):
                    profile_dirs.append(subfull)
    for profile in profile_dirs:
        for kind in ("deps", ".fingerprint", "build", "incremental"):
            kind_dir = os.path.join(profile, kind)
            try:
                names = os.listdir(kind_dir)
            except OSError:
                continue
            for name in names:
                # incremental/<crate>-<hash> holds session dirs: one path per crate+hash
                unit_hash = _unit_hash(name)
                if unit_hash:
                    add(profile, unit_hash, os.path.join(kind_dir, name))
    return units


def _remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


def prune_cargo_target(target_dir, max_bytes, stop_fraction=PRUNE_TARGET_FRACTION):
    """Delete least-recently-used units until the directory is at most `stop_fraction * max_bytes`. Incremental
    data goes first (it is rebuilt cheaply and is never reused across worktree paths). Caller holds the lock.
    Returns (bytes_before, bytes_after, units_removed)."""
    before = dir_bytes(target_dir)
    if before <= max_bytes:
        return before, before, 0
    goal = max_bytes * stop_fraction
    current = before
    removed = 0
    # 1. incremental sessions, oldest first (whole directories)
    sessions = []
    for profile in {p for (p, _h) in cargo_units(target_dir)}:
        inc = os.path.join(profile, "incremental")
        try:
            names = os.listdir(inc)
        except OSError:
            continue
        for name in names:
            path = os.path.join(inc, name)
            use = 0.0
            size = 0
            for root, _d, files in os.walk(path):
                for f in files:
                    try:
                        info = os.lstat(os.path.join(root, f))
                    except OSError:
                        continue
                    size += info.st_size
                    use = max(use, info.st_atime, info.st_mtime)
            sessions.append((use, size, path))
    for _use, size, path in sorted(sessions):
        if current <= goal:
            break
        _remove(path)
        current -= size
        removed += 1
    if current > goal:
        # 2. whole units, oldest first
        ordered = sorted(cargo_units(target_dir).items(), key=lambda item: item[1][2])
        for _key, (paths, size, _use) in ordered:
            if current <= goal:
                break
            for path in paths:
                _remove(path)
            current -= size
            removed += 1
    return before, dir_bytes(target_dir), removed


def drop_unused_dirs(base, keep, days=UNUSED_DAYS, now=None):
    """Remove directories of `base` (never `keep`) not used for `days` days. A directory's last use is the mtime
    of its `.last_used` stamp (touched by prepare_cargo_target), else of the directory. Returns the names."""
    now = time.time() if now is None else now
    gone = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return gone
    for name in names:
        full = os.path.join(base, name)
        if full == keep or not os.path.isdir(full) or name.endswith(".lock"):
            continue
        stamp = os.path.join(full, ".last_used")
        try:
            used = os.stat(stamp).st_mtime if os.path.exists(stamp) else os.stat(full).st_mtime
        except OSError:
            continue
        if now - used > days * 86400:
            with locked(full, exclusive=True, wait=False) as held:
                if held:
                    shutil.rmtree(full, ignore_errors=True)
                    shutil.rmtree(os.path.join(cache_root(), "hams-lint", "work", name), ignore_errors=True)  # its work checkouts (work_checkout)
                    gone.append(name)
    return gone


# [@ANCHOR: lint_cache:prepare_cargo_target]
def prepare_cargo_target(target_dir, environ=None, log=None, budget=None):
    """Enforce the budget on `target_dir` before a build. Call while holding `locked(target_dir)`. Returns the log
    lines (also passed to `log`). Never raises for lack of space."""
    environ = os.environ if environ is None else environ
    lines = []

    def say(text):
        lines.append(text)
        if log:
            log(text)

    budget = budget or budgets(environ)
    os.makedirs(target_dir, exist_ok=True)
    try:
        open(os.path.join(target_dir, ".last_used"), "a").close()
        os.utime(os.path.join(target_dir, ".last_used"))
    except OSError:
        pass
    if in_ram(target_dir, environ):
        ram_dirs.sweep(environ=environ, say=lambda text: say(f"lint cache: {text}"))  # crashed jobs' directories free their memory first
        ram_dirs.prune_lint_cache(os.path.dirname(target_dir), keep=target_dir, environ=environ, say=lambda text: say(text))
        pol = ram_dirs.policy(environ)
        used = ram_dirs.tree_bytes(os.path.dirname(target_dir)) / GB
        say(f"lint cache: RAM cache {used:.1f} GB of {pol.lint_budget_gb:g} GB budget in {os.path.dirname(target_dir)}, "
            f"{(ram_dirs.free_gb(pol.root) or 0):.0f} GB free in {pol.root}")
        ram_dirs.write_usage(pol)
    days = ram_dirs.LINT_UNUSED_DAYS if in_ram(target_dir, environ) else UNUSED_DAYS
    for name in drop_unused_dirs(os.path.dirname(target_dir), keep=target_dir, days=days):
        say(f"lint cache: removed {name} (unused for {days} days)")
    max_bytes = int(budget["cargo_gb"] * GB)
    floor_gb = ram_dirs.policy(environ).min_free_gb if in_ram(target_dir, environ) else budget["free_floor_gb"]  # RAM: its own minimum
    floor = int(floor_gb * GB)
    size = dir_bytes(target_dir)
    free = free_bytes(target_dir)
    say(f"lint cache: cargo target {target_dir} {size / GB:.1f} GB of {budget['cargo_gb']:g} GB budget, {(free or 0) / GB:.0f} GB free (floor {floor_gb:g} GB)")
    low_space = free is not None and free < floor
    if size > max_bytes or low_space:
        goal = max_bytes
        if low_space:
            # Free what the floor lacks: the directory has to shrink by at least the shortfall.
            goal = min(goal, max(0, size - (floor - free)))
        before, after, removed = prune_cargo_target(target_dir, goal)
        say(f"lint cache: pruned {removed} unit(s), {before / GB:.1f} -> {after / GB:.1f} GB (least recently used first)")
        free = free_bytes(target_dir)
        if after > max_bytes or (low_space and free is not None and free < floor):
            shutil.rmtree(target_dir, ignore_errors=True)
            os.makedirs(target_dir, exist_ok=True)
            say("lint cache: still over budget or under the free-space floor; cleared the directory (this build is cold)")
    return lines


def adopt_legacy_dir(target, environ=None):
    """The first managed directory on a machine takes over (renames) the old single shared directory
    `~/.cache/hams-cargo-target-lint`, so the dependencies it holds stay warm. Returns its old path or None."""
    legacy = os.path.join(cache_root(environ), "hams-cargo-target-lint")
    if os.path.exists(target) and os.listdir(target) or not os.path.isdir(legacy):
        return None
    for lock_file in (os.path.join(legacy, "debug", ".cargo-lock"), os.path.join(legacy, ".cargo-lock")):
        if os.path.exists(lock_file):  # a cargo run by an older runner is building in it: leave it alone
            try:
                with open(lock_file, "a+") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                return None
    try:
        if os.path.isdir(target):
            os.rmdir(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.rename(legacy, target)
    except OSError:
        return None
    # The old directory was built with incremental compilation (26 of its 31 GB); these builds run with
    # CARGO_INCREMENTAL=0, so those sessions are never read again.
    for profile in glob.glob(os.path.join(glob.escape(target), "*", "incremental")) + glob.glob(os.path.join(glob.escape(target), "*", "*", "incremental")):
        shutil.rmtree(profile, ignore_errors=True)
    return legacy


# [@ANCHOR: lint_cache:cargo_session]
@contextlib.contextmanager
def cargo_session(environ=None, log=None):
    """Context for a checker that builds: yields the target directory to use (CARGO_TARGET_DIR) with the budget
    enforced and the exclusive lock held. When CARGO_TARGET_DIR is not the managed directory (HAMS_LINT_CARGO_MANAGED
    names it), the caller's own directory is used untouched. HAMS_LINT_NO_CACHE=1: a throwaway directory."""
    environ = os.environ if environ is None else environ
    target = environ.get("CARGO_TARGET_DIR")
    managed = environ.get("HAMS_LINT_CARGO_MANAGED")
    if environ.get("HAMS_LINT_NO_CACHE") == "1":
        scratch = tempfile.mkdtemp(prefix="hams-lint-cold-cargo-")
        try:
            if log:
                log(f"lint cache: --no-cache: cold cargo build in {scratch}")
            yield scratch
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return
    if not target or not managed or os.path.abspath(target) != os.path.abspath(managed):
        yield target
        return
    repo = environ.get("HAMS_LINT_CARGO_REPO")
    if repo:
        # The dispatcher names the directory with ITS toolchain; this host may run another (a runner is a different
        # machine): the name is derived again here, so one toolchain on one machine is one directory.
        target = cargo_target_dir(repo, environ)
    with locked(target, exclusive=True):
        adopted = None if in_ram(target, environ) else adopt_legacy_dir(target, environ)
        if adopted and log:
            log(f"lint cache: adopted the old shared directory {adopted} as {target} (its incremental data is pruned first)")
        prepare_cargo_target(target, environ, log)
        yield target


# [@ANCHOR: lint_cache:named_cargo_session]
@contextlib.contextmanager
def named_cargo_session(name, environ=None, log=None):
    """Context for a test that builds with cargo (the relay export test): yields the Cargo target directory to use
    for it, `<cache>/hams-lint-cargo/<name>-<rustc>`, with the machine's budget enforced (LRU prune, then clear;
    never a failure) and the exclusive lock held until the block ends. A `CARGO_TARGET_DIR` set by the caller is
    yielded untouched; HAMS_LINT_NO_CACHE=1 yields a throwaway directory deleted afterwards."""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_LINT_NO_CACHE") == "1":
        scratch = tempfile.mkdtemp(prefix=f"hams-{name}-cold-cargo-")
        try:
            if log:
                log(f"lint cache: --no-cache: cold cargo build in {scratch}")
            yield scratch
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return
    if environ.get("CARGO_TARGET_DIR"):
        yield environ["CARGO_TARGET_DIR"]
        return
    target = cargo_target_dir(name, environ)
    if in_ram(target, environ):
        # In RAM one directory serves every slot: the exclusive lock below already makes concurrent builds take turns, and four
        # copies of a 4.5 GB dependency build cost 13.5 GB of memory (measured 2026-11-08).
        target = cargo_target_dir(name, dict(environ, HAMS_LINT_SLOT="1"))
    with locked(target, exclusive=True):
        prepare_cargo_target(target, environ, log)
        yield target


# ---------------------------------------------------------------------------------------------------------
# Fixed work checkouts: the path cargo sees stays the same from one lint run to the next

# Directories a work checkout never holds: build output, caches and virtual environments (never sources), the checkout's own `.git`.
WORK_SKIP = frozenset({".git", "target", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv", ".claude"})


def _same_bytes(a, b):
    """True when files `a` and `b` have the same content (size first, then the bytes)."""
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as left, open(b, "rb") as right:
        while True:
            block = left.read(1 << 20)
            if block != right.read(1 << 20):
                return False
            if not block:
                return True


def _under(path, directory):
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


# [@ANCHOR: lint_cache:sync_tree]
def sync_tree(source, destination, skip=WORK_SKIP, submodules=()):
    """Make `destination` hold exactly the files of `source`, rewriting only files whose bytes differ. An unchanged file keeps its
    modification time: cargo's fingerprint of a path package is its files' mtimes, so a copy made afresh every run is rebuilt in full.
    Directories named in `skip` are left out, and so is a directory that holds another checkout (its own `.git`) unless its path
    relative to `source` is in `submodules`. A symlink is copied as a link; a relative link that leaves `source` is made absolute so it
    still resolves. Special files are skipped and a file that vanishes while the tree is read is left out. Full entry: tools/README.md."""
    source = os.path.realpath(source)
    os.makedirs(destination, exist_ok=True)
    wanted_files, wanted_dirs = set(), {destination}
    for directory, dirs, names in os.walk(source):
        relative = os.path.relpath(directory, source)
        target_dir = destination if relative == "." else os.path.join(destination, relative)
        kept = []
        for name in dirs:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                names.append(name)
            elif name in skip:
                continue
            elif os.path.lexists(os.path.join(path, ".git")) and os.path.relpath(path, source) not in submodules:
                continue
            else:
                kept.append(name)
        dirs[:] = kept
        if os.path.lexists(target_dir) and (os.path.islink(target_dir) or not os.path.isdir(target_dir)):
            os.remove(target_dir)  # it was a file or a link and is a directory now
        os.makedirs(target_dir, exist_ok=True)
        wanted_dirs.add(target_dir)
        wanted_dirs.update(os.path.join(target_dir, name) for name in kept)
        for name in names:
            src, dst = os.path.join(directory, name), os.path.join(target_dir, name)
            try:
                if os.path.islink(src):
                    link = os.readlink(src)
                    if not os.path.isabs(link):
                        resolved = os.path.realpath(os.path.join(directory, link))
                        if not _under(resolved, source):
                            link = resolved
                    wanted_files.add(dst)
                    if not (os.path.islink(dst) and os.readlink(dst) == link):
                        if os.path.lexists(dst):
                            shutil.rmtree(dst) if os.path.isdir(dst) and not os.path.islink(dst) else os.remove(dst)
                        os.symlink(link, dst)
                    continue
                if not os.path.isfile(src):
                    continue
                wanted_files.add(dst)
                if not (os.path.isfile(dst) and not os.path.islink(dst) and _same_bytes(src, dst)):
                    if os.path.lexists(dst):
                        shutil.rmtree(dst) if os.path.isdir(dst) and not os.path.islink(dst) else os.remove(dst)
                    shutil.copyfile(src, dst)
                if os.stat(src).st_mode & 0o777 != os.stat(dst).st_mode & 0o777:
                    shutil.copymode(src, dst)
            except FileNotFoundError:
                wanted_files.discard(dst)
    for directory, dirs, names in os.walk(destination, topdown=False):
        for name in names + [d for d in dirs if os.path.islink(os.path.join(directory, d))]:
            path = os.path.join(directory, name)
            if path not in wanted_files:
                os.remove(path)
        if directory not in wanted_dirs:
            shutil.rmtree(directory, ignore_errors=True)


# [@ANCHOR: lint_cache:work_checkout]
def work_checkout(repo_root, name, target, environ=None, log=None):
    """A copy of the checkout `repo_root` at a FIXED path, `<cache>/hams-lint/work/<target directory name>/<name>`, or None when the
    build should run in `repo_root` itself. Cargo hashes absolute paths into the fingerprints and build-script outputs of the
    workspace crates, so a build run in a different worktree each time (every lint run is one) recompiles the relay and its path
    crates in a warm target (measured 2026-11-09: 163-183 s of 183 s, against 0.6 s when the path is the same and nothing changed).
    The copy is synced with `sync_tree` (unchanged files keep their mtime), so a build from it is fresh exactly when a build in the
    checkout would be, and the files cargo and the tests read are the checkout's, byte for byte.

    Call it while holding the target's lock (`named_cargo_session`, `cargo_session`): the lock keeps two runs out of the directory.
    None when `target` is not a managed Cargo directory (an explicit `CARGO_TARGET_DIR`), under `HAMS_LINT_NO_CACHE=1`, or when
    the copy fails (full disk): the caller then builds in place as before. `name` tells repositories apart (hams_com, hams_open).
    Full entry: tools/README.md."""
    environ = os.environ if environ is None else environ
    if not target or environ.get("HAMS_LINT_NO_CACHE") == "1":
        return None
    target = os.path.abspath(target)
    if not (in_ram(target, environ) or any(_under(target, os.path.abspath(base)) for base in cargo_bases(environ))):
        return None
    work = os.path.join(cache_root(environ), "hams-lint", "work", os.path.basename(target), name)
    try:
        import tree_files

        submodules = {path for path in tree_files._submodule_paths(repo_root)}
        sync_tree(repo_root, work, submodules=submodules)
    except (OSError, ImportError) as error:
        if log:
            log(f"lint cache: work checkout {work} not made ({error}); building in {repo_root}")
        return None
    return work


# ---------------------------------------------------------------------------------------------------------
# Per-file result caches


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# [@ANCHOR: lint_cache:checker_version]
def checker_version(*paths):
    """Hash of the checker's own source files: any edit to the checker invalidates every entry. Each file is
    identified by its base name and text, never its directory: the checkout of every lint run is a new path, and a
    version that included the path gave each run its own cache file, so the cache was cold every time (347 files of
    one checker on the dev box; two 42 s steps)."""
    parts = []
    for path in paths:
        with open(path, "rb") as handle:
            parts.append((os.path.basename(path).encode(), handle.read()))
    h = hashlib.sha256()
    for name, text in sorted(parts):
        h.update(name + b"\0" + text)
    return h.hexdigest()[:16]


class ResultCache:
    """Per-file results for a checker, keyed by the file's content hash, valid for one checker version.

    `get(path)` returns the stored result (any JSON value) or None on a miss; `put(path, result)` stores it.
    A changed file has a different content hash, so it can never read another content's entry. `enabled` False
    (`--no-cache`, HAMS_LINT_NO_CACHE=1) makes every `get` a miss and `put` a no-op. `save()` merges with what
    other runs wrote meanwhile (under an flock) and writes atomically."""

    def __init__(self, checker, version, environ=None, enabled=None):
        environ = os.environ if environ is None else environ
        self.enabled = (environ.get("HAMS_LINT_NO_CACHE") != "1") if enabled is None else enabled
        self.path = os.path.join(results_base(environ), checker, f"{version}.json")
        self.base = results_base(environ)
        self.max_bytes = int(budgets(environ)["results_mb"] * MB)
        self.entries = {}
        self.new = {}
        self.hits = 0
        self.misses = 0
        if self.enabled:
            self.entries = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, path):
        if not self.enabled:
            self.misses += 1
            return None
        try:
            digest = file_digest(path)
        except OSError:
            self.misses += 1
            return None
        entry = self.entries.get(digest)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry["r"]

    def put(self, path, result):
        if not self.enabled:
            return
        try:
            digest = file_digest(path)
        except OSError:
            return
        entry = {"r": result, "t": int(time.time())}
        self.entries[digest] = entry
        self.new[digest] = entry

    def save(self):
        if not self.enabled or not self.new:
            return
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, exist_ok=True)
            with locked(self.path):
                merged = self._load()
                merged.update(self.new)
                # budget: drop the oldest entries until the file fits
                text = json.dumps(merged, separators=(",", ":"))
                if len(text) > self.max_bytes:
                    ordered = sorted(merged.items(), key=lambda kv: kv[1].get("t", 0))
                    while ordered and len(text) > self.max_bytes * PRUNE_TARGET_FRACTION:
                        ordered = ordered[max(1, len(ordered) // 10):]
                        text = json.dumps(dict(ordered), separators=(",", ":"))
                    merged = dict(ordered)
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(merged, handle, separators=(",", ":"))
                os.replace(tmp, self.path)
            self.new = {}
        except OSError as error:
            print(f"(result cache not saved: {error})", file=sys.stderr)
        _prune_results_dirs(self.base, keep=self.path)


def cached_file_result(cache, path, compute):
    """`compute(path)` (a JSON-serialisable result, e.g. a list of (line, message)), from `cache` when the file's
    content is unchanged since an earlier run; stored on a miss."""
    result = cache.get(path)
    if result is None:
        result = compute(path)
        cache.put(path, result)
    return result


def _prune_results_dirs(base, keep, days=UNUSED_DAYS):
    """Remove result files of old checker versions (not used for `days` days)."""
    now = time.time()
    for root, _dirs, files in os.walk(base):
        for name in files:
            full = os.path.join(root, name)
            if full == keep or not name.endswith(".json"):
                continue
            try:
                if now - os.stat(full).st_mtime > days * 86400:
                    os.unlink(full)
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------------------
# CLI


# [@ANCHOR: lint_cache:show_cache_lines]
def show_lines(environ=None):
    environ = os.environ if environ is None else environ
    b = budgets(environ)
    lines = [f"machine {machine_name(environ)}: cargo budget {b['cargo_gb']:g} GB per directory, free floor {b['free_floor_gb']:g} GB, results {b['results_mb']:g} MB"]
    for base in cargo_bases(environ):
        if os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                full = os.path.join(base, name)
                if os.path.isdir(full):
                    lines.append(f"  cargo {full}: {dir_bytes(full) / GB:.2f} GB")
    for legacy in legacy_cargo_dirs(environ):
        if os.path.isdir(legacy):
            lines.append(f"  legacy {legacy}: {dir_bytes(legacy) / GB:.2f} GB (a pre-budget directory; `clear cargo` removes it)")
    rb = results_base(environ)
    if os.path.isdir(rb):
        lines.append(f"  results {rb}: {dir_bytes(rb) / MB:.2f} MB")
    for label, path, why in other_caches(environ):
        if os.path.isdir(path):
            lines.append(f"  other {path}: {dir_bytes(path) / GB:.2f} GB (not budgeted: {label}, {why})")
    free = free_bytes(cache_root(environ))
    if free is not None:
        lines.append(f"  free on {cache_root(environ)}: {free / GB:.0f} GB")
    return lines


# [@ANCHOR: lint_cache:cache_main]
def _clear_work_checkouts(work_base, environ=None):
    """Remove `<work_base>/<target name>` for every target whose lock nobody holds. A build holds its target's lock (the one beside
    the target directory, in whichever Cargo base it uses) while it compiles from that work checkout (`work_checkout`), so a
    checkout whose lock is held stays, as the busy target itself does."""
    try:
        names = sorted(os.listdir(work_base))
    except OSError:
        return
    bases = [base for base in cargo_bases(environ) if os.path.isdir(base)]
    for name in names:
        full = os.path.join(work_base, name)
        if not os.path.isdir(full) or os.path.islink(full):
            continue
        with contextlib.ExitStack() as stack:
            if all(stack.enter_context(locked(os.path.join(base, name), exclusive=True, wait=False)) for base in bases):
                print(f"removing {full}")
                shutil.rmtree(full, ignore_errors=True)
            else:
                print(f"{full} is in use by a build; not removed")
    with contextlib.suppress(OSError):
        os.rmdir(work_base)  # only when it is now empty


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "show"
    which = argv[1] if len(argv) > 1 else "all"
    if command == "show":
        print("\n".join(show_lines()))
        return 0
    if command == "prune":
        b = budgets()
        for base in cargo_bases():
            for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
                full = os.path.join(base, name)
                if os.path.isdir(full):
                    with locked(full):
                        print("\n".join(prepare_cargo_target(full, budget=b)))
        return 0
    if command == "clear":
        targets = []
        if which in ("cargo", "all"):
            targets += [*cargo_bases(), *legacy_cargo_dirs()]
        if which in ("results", "all"):
            targets.append(results_base())
        work_base = os.path.join(cache_root(), "hams-lint", "work")  # the fixed work checkouts of the Cargo builds (work_checkout)
        for path in targets:
            if not os.path.isdir(path):
                continue
            if path in cargo_bases():
                for name in sorted(os.listdir(path)):
                    full = os.path.join(path, name)
                    if os.path.isdir(full):
                        with locked(full, wait=False) as held:
                            if held:
                                print(f"removing {full}")
                                shutil.rmtree(full, ignore_errors=True)
                            else:
                                print(f"{full} is in use by a build; not removed")
                continue
            print(f"removing {path}")
            shutil.rmtree(path, ignore_errors=True)
        if which in ("cargo", "all"):
            _clear_work_checkouts(work_base)
        return 0
    print(__doc__.split("CLI:")[-1].strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
