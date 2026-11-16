#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The whole-system gate tree for the current `origin/main` commits, built and verified in one command (ADR 0118).

A whole-system run needs three checkouts at exact commits (hams_com, hams_open, and the hams_shared commit hams_open
records), the build outputs some tests load (`libcloudflared.so`, the relay test binary), and an environment that points at
that tree. Built by hand, three runs in one night failed on a stale hams_open checkout and on a `libcloudflared.so` whose
timestamp predated its source. This tool builds the tree, builds the outputs, writes a manifest and the shard files, prints
the exact `test.py` command lines, and REFUSES (exit status 1, every problem named) when an input is stale or missing.

    gate_snapshot.py create  [--workspace DIR] [--parent DIR] [--shards N] [--shards-file FILE] [--no-fetch] [--no-build]
    gate_snapshot.py verify  GATE_DIR [--no-fetch]      # refuses to pass when anything is stale
    gate_snapshot.py commands GATE_DIR                  # the test.py command line of every shard
    gate_snapshot.py prune   [--parent DIR] [--keep N]  # remove old gate trees
    test.py --snapshot-gate [same options as create]

It runs on the machine that holds the checkouts and never starts a test. The reference is the "Whole-system gate tree"
section of tools/README.md.
"""

import argparse
import dataclasses
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time

import infrastructure

MANIFEST_NAME = "gate_snapshot.json"
SHARDS_NAME = "gate_shards.json"
STAMP_SUFFIX = ".gate_stamp"
MANIFEST_VERSION = 1
DEFAULT_WORKSPACE = os.path.expanduser("~/workspace")
DEFAULT_SHARDS = 4
ENV_FILE = "/etc/profile.d/hams_test_environment.sh"
RELAY_CRATE = "daemons/hams_local_relay"
CONSOLE_DIR = "daemons/shack_console/console"
SHACK_CONSOLE_CRATE = "daemons/shack_console"  # linked into the relay (with its hams_core), so its sources decide staleness too
CFFI_DIR = "daemons/cloudflared-ffi"
RELAY_BIN_ENV = "HAMS_CONSOLE_TEST_RELAY_BIN"
OPEN_TREE_ENV = "HAMS_OPEN_TREE"


# [@ANCHOR: gate_snapshot:gate_snapshot_snapshot_error_class]
class SnapshotError(RuntimeError):
    """The snapshot cannot be built or trusted; the message names the cause."""


def _git(repo, *args, check=True):
    done = subprocess.run(
        ["git", "-c", "safe.directory=" + repo, "-C", repo, *args], capture_output=True, text=True, timeout=600
    )
    if check and done.returncode != 0:
        raise SnapshotError("git %s in %s failed: %s" % (" ".join(args), repo, (done.stderr or done.stdout).strip()[-300:]))
    return done.stdout.strip()


# [@ANCHOR: gate_snapshot:refuse_production]
def refuse_production(hostname=None, marker_files=("/opt/hams/etc/production", "/opt/hams/.production")):
    """Raises `SnapshotError` on the production host (host name `hams1`, or a production marker file). The gate runs on
    the dev box only. Returns None otherwise."""
    name = (socket.gethostname() if hostname is None else hostname).lower()
    if name == "hams1" or name.startswith("hams1."):
        raise SnapshotError("this is hams1, the production host: the gate never runs there")
    for marker in marker_files:
        if os.path.exists(marker):
            raise SnapshotError("%s exists: this is a production host, the gate never runs there" % marker)


# [@ANCHOR: gate_snapshot:origin_main]
def origin_main(repo, fetch=True):
    """The commit `origin/main` of `repo` (after `git fetch origin main` unless `fetch` is False). Raises `SnapshotError`
    when the fetch fails (a stale answer must not look current) or there is no `origin/main`."""
    if fetch:
        _git(repo, "fetch", "-q", "origin", "main")
    return _git(repo, "rev-parse", "origin/main")


# [@ANCHOR: gate_snapshot:recorded_shared_commit]
def recorded_shared_commit(open_repo, open_commit):
    """The hams_shared commit that hams_open's tree at `open_commit` records (its `hams_shared` gitlink). Raises
    `SnapshotError` when the tree has no such gitlink."""
    listing = _git(open_repo, "ls-tree", open_commit, "hams_shared")
    found = re.match(r"^160000 commit ([0-9a-f]{40})\thams_shared$", listing)
    if not found:
        raise SnapshotError("hams_open %s has no hams_shared gitlink (got %r)" % (open_commit[:12], listing))
    return found.group(1)


# [@ANCHOR: gate_snapshot:gate_snapshot_trees_class]
@dataclasses.dataclass(frozen=True)
class Trees:
    """Where the three checkouts of a gate tree are: `parent`, `com`, `open`, `shared` (paths), and the commits
    `com_commit`, `open_commit`, `shared_commit` they are at."""

    parent: str
    com: str
    open: str
    shared: str
    com_commit: str
    open_commit: str
    shared_commit: str


# [@ANCHOR: gate_snapshot:gate_dir_name]
def gate_dir_name(com_commit, open_commit):
    """`gate_<9 hex of hams_com>_<9 hex of hams_open>`: the directory name of a snapshot, so the same commits give the
    same directory and different commits never share one. Pure."""
    return "gate_%s_%s" % (com_commit[:9], open_commit[:9])


# [@ANCHOR: gate_snapshot:create_trees]
def create_trees(workspace, parent, com_commit, open_commit, shared_commit):
    """Makes `<parent>/gate_*/{hams_com,hams_open,hams_open/hams_shared}` as detached git worktrees at the given commits
    and returns `Trees`. The hams_com tree's `hams_shared` link then resolves into the hams_open tree. An existing directory
    is verified, not rebuilt: it must already be at these commits, else `SnapshotError` (use `prune`). Raises `SnapshotError`
    when a commit is missing from its repository."""
    gate = os.path.join(parent, gate_dir_name(com_commit, open_commit))
    com_main, open_main = os.path.join(workspace, "hams_com"), os.path.join(workspace, "hams_open")
    shared_main = os.path.join(open_main, "hams_shared")
    com, open_, shared = (os.path.join(gate, "hams_com"), os.path.join(gate, "hams_open"),
                          os.path.join(gate, "hams_open", "hams_shared"))
    for repo, commit in ((com_main, com_commit), (open_main, open_commit), (shared_main, shared_commit)):
        if subprocess.run(["git", "-c", "safe.directory=" + repo, "-C", repo, "cat-file", "-e", commit + "^{commit}"],
                          capture_output=True).returncode != 0:
            raise SnapshotError("%s does not contain commit %s (fetch it)" % (repo, commit[:12]))
    os.makedirs(gate, exist_ok=True)
    for source, target, commit in ((com_main, com, com_commit), (open_main, open_, open_commit)):
        if os.path.exists(os.path.join(target, ".git")):
            continue
        _git(source, "worktree", "add", "--detach", "-f", target, commit)
    if not os.path.exists(os.path.join(shared, ".git")):
        if os.path.isdir(shared) and not os.listdir(shared):
            os.rmdir(shared)  # hams_open's checkout leaves the gitlink as an empty directory
        _git(shared_main, "worktree", "add", "--detach", "-f", shared, shared_commit)
    link = os.path.join(com, "hams_shared")
    if os.path.realpath(link) != os.path.realpath(shared):
        raise SnapshotError("%s resolves to %s, not to %s" % (link, os.path.realpath(link), shared))
    return Trees(gate, com, open_, shared, com_commit, open_commit, shared_commit)


# [@ANCHOR: gate_snapshot:gate_snapshot_artifact_class]
@dataclasses.dataclass(frozen=True)
class Artifact:
    """A build output the tests load: `name` (`cloudflared_ffi` or `relay_test_hooks`), `tree` (`com` or `open`: the
    checkout it is built from) and `sources` (tree-relative directories whose git tree ids are stamped beside the output)."""

    name: str
    tree: str
    sources: tuple


ARTIFACTS = (
    Artifact("cloudflared_ffi", "open", (CFFI_DIR,)),
    Artifact("relay_test_hooks", "com", (RELAY_CRATE, SHACK_CONSOLE_CRATE, CONSOLE_DIR)),
)


def _tree_path(trees, which):
    return trees.com if which == "com" else trees.open


# [@ANCHOR: gate_snapshot:artifact_output]
def artifact_output(artifact, trees, cargo_target_dir):
    """The path of an artifact's output: `<hams_open tree>/daemons/cloudflared-ffi/libcloudflared.so`, or
    `<cargo_target_dir>/debug/hams_local_relay` (the path `HAMS_CONSOLE_TEST_RELAY_BIN` must name). Pure."""
    if artifact.name == "cloudflared_ffi":
        return os.path.join(trees.open, CFFI_DIR, infrastructure.CFFI_LIB_NAME)
    return os.path.join(cargo_target_dir, "debug", "hams_local_relay")


# [@ANCHOR: gate_snapshot:source_stamp]
def source_stamp(artifact, trees):
    """`{source directory: git tree id}` of the artifact's sources at the tree's commit (`git rev-parse HEAD:<dir>`). The
    stamp written beside a built output; a later checkout of other sources makes it differ."""
    base = _tree_path(trees, artifact.tree)
    return {source: _git(base, "rev-parse", "HEAD:" + source) for source in artifact.sources}


_BUILD_PRODUCTS = (infrastructure.CFFI_LIB_NAME, "libcloudflared.h")


def _newest_mtime(base, directory):
    """The newest modification time of a git-tracked source file under `base/directory`. Untracked files (a .pytest_cache,
    __pycache__ or editor file written into the tree after the build) are not sources: the stamp's tree ids cover what is
    committed, and a tracked file changed in place is caught here."""
    newest = 0.0
    for name in _git(base, "ls-files", "-z", "--", directory).split("\0"):
        if not name or os.path.basename(name) in _BUILD_PRODUCTS or name.endswith((STAMP_SUFFIX, ".console_tree")):
            continue
        try:
            newest = max(newest, os.path.getmtime(os.path.join(base, name)))
        except OSError:
            pass
    return newest


# [@ANCHOR: gate_snapshot:artifact_problems]
def artifact_problems(artifact, trees, cargo_target_dir, environ=None):
    """Why `artifact` cannot be used now: a list of sentences (empty when it can). Checks that the output exists, is
    older than none of its source files and carries a stamp equal to the sources' current tree ids (a copied-in or
    old-timestamp file has none, or a wrong one); for the shared library also that it is built for this CPU
    (`infrastructure.cffi_library_status`); for the relay also that `<binary>.console_tree` names the current console tree
    and that `HAMS_CONSOLE_TEST_RELAY_BIN`, when set, is this binary."""
    environ = os.environ if environ is None else environ
    output = artifact_output(artifact, trees, cargo_target_dir)
    base = _tree_path(trees, artifact.tree)
    if not os.path.isfile(output):
        return ["%s: %s does not exist (build it: gate_snapshot.py create)" % (artifact.name, output)]
    problems = []
    try:
        with open(output + STAMP_SUFFIX, encoding="utf-8") as handle:
            stamped = json.load(handle).get("sources")
    except (OSError, ValueError):
        stamped = None
    if stamped != source_stamp(artifact, trees):
        problems.append("%s: %s has no stamp for the current sources (copied in, or built from other sources)" % (artifact.name, output))
    built = os.path.getmtime(output)
    for source in artifact.sources:
        if _newest_mtime(base, source) > built:
            problems.append("%s: %s is older than its source %s" % (artifact.name, output, source))
    if artifact.name == "cloudflared_ffi":
        ok, reason = infrastructure.cffi_library_status(os.path.dirname(output))
        if not ok:
            problems.append("%s: %s" % (artifact.name, reason))
    else:
        if not os.access(output, os.X_OK):
            problems.append("%s: %s is not executable" % (artifact.name, output))
        try:
            with open(output + ".console_tree", encoding="utf-8") as handle:
                console = handle.read().strip()
        except OSError:
            console = None
        if console != source_stamp(artifact, trees)[CONSOLE_DIR]:
            problems.append("%s: %s.console_tree is missing or names another console tree" % (artifact.name, output))
        pointed = environ.get(RELAY_BIN_ENV)
        if pointed and os.path.realpath(pointed) != os.path.realpath(output):
            problems.append("%s=%s is not the gate's binary %s" % (RELAY_BIN_ENV, pointed, output))
    return problems


# [@ANCHOR: gate_snapshot:build_artifact]
def build_artifact(artifact, trees, cargo_target_dir, run=None, out=print):
    """Builds one artifact and writes its stamp. `run(command, cwd, env)` executes a command and returns its exit status
    (default: `subprocess.call` under `nice -n 10`). The shared library is built by `build_cloudflared_ffi.py --force
    --ffi-dir <tree>` (the existing build script); the relay by `cargo build --features test-hooks --locked` in the crate with
    `CARGO_TARGET_DIR=<cargo_target_dir>`, followed by the `.console_tree` file `test_parity.py` writes. Raises
    `SnapshotError` when a command fails or the output is missing afterwards."""
    run = run or (lambda command, cwd, env: subprocess.call(["nice", "-n", "10", *command], cwd=cwd, env=env))
    env = dict(os.environ)
    output = artifact_output(artifact, trees, cargo_target_dir)
    if artifact.name == "cloudflared_ffi":
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_cloudflared_ffi.py")
        command, cwd = [sys.executable, script, "--force", "--ffi-dir", os.path.dirname(output)], trees.open
    else:
        command, cwd = ["cargo", "build", "--features", "test-hooks", "--locked"], os.path.join(trees.com, RELAY_CRATE)
        env["CARGO_TARGET_DIR"] = cargo_target_dir
    out("gate_snapshot: building %s: %s" % (artifact.name, " ".join(command)))
    if run(command, cwd, env) != 0:
        raise SnapshotError("building %s failed (command: %s)" % (artifact.name, " ".join(command)))
    if not os.path.isfile(output):
        raise SnapshotError("building %s produced no %s" % (artifact.name, output))
    stamp = source_stamp(artifact, trees)
    if artifact.name != "cloudflared_ffi":
        with open(output + ".console_tree", "w", encoding="utf-8") as handle:
            handle.write(stamp[CONSOLE_DIR] + "\n")
    with open(output + STAMP_SUFFIX, "w", encoding="utf-8") as handle:
        json.dump({"sources": stamp, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, handle)
    return output


# [@ANCHOR: gate_snapshot:discover_modules]
def discover_modules(trees):
    """The Odoo modules of the two trees: names of the directories at the root of the hams_com and hams_open trees that
    contain `__manifest__.py`, sorted and de-duplicated."""
    found = set()
    for base in (trees.com, trees.open):
        for name in os.listdir(base):
            if os.path.isfile(os.path.join(base, name, "__manifest__.py")):
                found.add(name)
    return sorted(found)


# [@ANCHOR: gate_snapshot:make_shards]
def make_shards(modules, count, previous=None, seconds=None, splits=None):
    """Splits `modules` into `count` shards (lists, in `gate_shards.json` form). With `previous` (a list of lists from an
    earlier file) every module keeps its old shard, modules that no longer exist are dropped, and a new module goes to the
    smallest shard; otherwise the sorted modules are dealt round robin. Every module appears in exactly one shard; a shard
    may end up empty only when there are fewer modules than shards (it is then omitted).
    With `seconds` ({module: measured test seconds}, see gate_module_seconds.json) the shards are balanced instead: longest
    module first onto the currently lightest shard (a module without a measurement counts as the median), `previous` is
    ignored. Only WHERE a module runs changes, never whether. With `splits` too (see `make_split_shards`) a module listed
    there may appear in several shards, each running part of its test classes. Returns the module lists only."""
    return make_split_shards(modules, count, previous, seconds, splits)[0]


# [@ANCHOR: gate_snapshot:make_split_shards]
def make_split_shards(modules, count, previous=None, seconds=None, splits=None):
    """`make_shards` that also returns, per shard, the `--test-tags` selector it needs (None when every module in the shard
    runs whole): `(shards, tags)`. `splits` is `{module: {TestClass: seconds}}` (gate_module_splits.json): the one module that
    is bigger than a fair shard (theme_hams, two browser contrast classes) is balanced as several parts, one per listed class
    plus the rest of the module (its measured seconds minus the classes'), so no shard is pinned by one module. The parts of a
    module that land in the same shard are run together again as the whole module; a split module's tags are
    `/module:Class` per class part and `/module,-/module:A` (minus the class parts that run in other shards) for the rest part, so the parts together run every
    test of the module exactly once."""
    if not (seconds and splits):
        shards = make_shards_plain(modules, count, previous, seconds)
        return shards, [None] * len(shards)
    units = []  # (module, class or None for the whole module/rest, seconds)
    split = {m: splits[m] for m in modules if m in splits and m in seconds}
    known = sorted(seconds[m] for m in modules if m in seconds)
    median = known[len(known) // 2] if known else 1.0
    for module in modules:
        if module in split:
            for cls, sec in sorted(split[module].items()):
                units.append((module, cls, float(sec)))
            units.append((module, None, max(1.0, seconds[module] - sum(split[module].values()))))
        else:
            units.append((module, None, seconds.get(module, median)))
    parts = [[] for _ in range(max(1, count))]
    load = [0.0] * len(parts)
    for unit in sorted(units, key=lambda u: (-u[2], u[0], u[1] or "")):
        index = load.index(min(load))
        parts[index].append(unit)
        load[index] += unit[2]
    shards, tags = [], []
    for part in parts:
        if not part:
            continue
        specs, partial = [], False
        for module in sorted({u[0] for u in part}):
            mine = [u for u in part if u[0] == module]
            if module not in split or len(mine) == len(split[module]) + 1:
                specs.append("/" + module)  # the whole module, or every part of it
                continue
            partial = True
            here = {u[1] for u in mine}
            if None in here:  # the rest part: the module minus the classes that run in other shards (Odoo subtracts excludes last)
                specs += ["/" + module] + ["-/%s:%s" % (module, cls) for cls in sorted(split[module]) if cls not in here]
            else:
                specs += ["/%s:%s" % (module, cls) for cls in sorted(here)]
        shards.append(sorted({u[0] for u in part}))
        tags.append(",".join(specs) if partial else None)
    return shards, tags


def make_shards_plain(modules, count, previous=None, seconds=None):
    """The one-module-one-shard dealing `make_shards` describes."""
    if seconds:
        known = sorted(seconds[m] for m in modules if m in seconds)
        median = known[len(known) // 2] if known else 1.0
        shards = [[] for _ in range(max(1, count))]
        load = [0.0] * len(shards)
        for module in sorted(modules, key=lambda m: (-seconds.get(m, median), m)):
            index = load.index(min(load))
            shards[index].append(module)
            load[index] += seconds.get(module, median)
        return [shard for shard in shards if shard]
    if previous:
        shards = [[m for m in shard if m in set(modules)] for shard in previous]
        placed = {m for shard in shards for m in shard}
        for module in modules:
            if module not in placed:
                min(shards, key=len).append(module)
        return [shard for shard in shards if shard]
    shards = [[] for _ in range(max(1, count))]
    for index, module in enumerate(sorted(modules)):
        shards[index % len(shards)].append(module)
    return [shard for shard in shards if shard]


# [@ANCHOR: gate_snapshot:shard_command]
def shard_command(trees, shard, log_directory, relay_binary, extra_env=None, tags=None):
    """The exact shell command line that runs one shard: from the gate's hams_com tree, the test environment sourced, as the
    invoking user (the full pipeline: `test.py` takes a slot, a private network namespace and its own databases), at `nice -n 10`,
    with `HAMS_OPEN_TREE` and the relay binary set, queued (`--wait`), and with `HAMS_TEST_BATCH=off`: a snapshot run tests exactly
    the snapshot's commits, so it must not join a combined batch of other branches. No `sudo -u odoo`, no `HAMS_ISOLATED_NS`, no
    `HAMS_CI_LOAD_GATE_RATIO` (those stall runs). `--preflight-done`: the lint gate runs the anchor scan and linters on its own; a
    lint-red snapshot must still run its tests (the first real canary, 2026-11-10, ran no test at all: every shard stopped in test.py's
    pre-flight on a main that was lint-red). Returns the string. Pure."""
    env = {OPEN_TREE_ENV: shlex.quote(trees.open), RELAY_BIN_ENV: shlex.quote(relay_binary), "HAMS_TEST_BATCH": "off"}
    env.update(extra_env or {})
    assignments = " ".join("%s=%s" % (k, v) for k, v in env.items())
    return (
        "cd %s && source %s && %s nice -n 10 python3 hams_shared/tools/test.py --wait --preflight-done -l %s -u %s%s"
        % (shlex.quote(trees.com), ENV_FILE, assignments, shlex.quote(log_directory), ",".join(shard),
           " --test-tags %s" % shlex.quote(tags) if tags else "")
    )


# [@ANCHOR: gate_snapshot:gate_snapshot_snapshot_behind_class]
class SnapshotBehind(SnapshotError):
    """The snapshot is complete but origin/main moved past it while it was built; `manifest` is that snapshot."""

    manifest = None


NOTE_PATHS = ("night_shift_todo/", "night_shift_history/", "night_shift_questions/")


# [@ANCHOR: gate_snapshot:only_notes_ahead]
def only_notes_ahead(repo, snapshot, head):
    """True when `head` is ahead of `snapshot` (the snapshot is an ancestor of it) and every path that differs between them lies
    under `night_shift_todo/`, `night_shift_history/` or `night_shift_questions/`: to-do notes pushed straight to hams_com main,
    which change no code or test. Any other difference, or a snapshot that is not an ancestor of `head`, is False."""
    ancestor = subprocess.run(["git", "-c", "safe.directory=" + repo, "-C", repo, "merge-base", "--is-ancestor", snapshot, head],
                              capture_output=True, timeout=600)
    if ancestor.returncode != 0:
        return False
    paths = _git(repo, "diff", "--name-only", "-z", snapshot, head, check=False).split("\0")
    paths = [path for path in paths if path]
    return bool(paths) and all(path.startswith(NOTE_PATHS) for path in paths)


# [@ANCHOR: gate_snapshot:verify_snapshot]
def verify_snapshot(gate_dir, fetch=True, environ=None, cargo_target_dir=None):
    """Every reason the snapshot in `gate_dir` must not be used; an empty list means it is current and complete.

    Checks, from `gate_snapshot.json`: each of the three trees exists, is at its recorded commit, has no uncommitted change,
    and that commit is still `origin/main` of its repository (hams_com may be ahead by notes-only commits, see `only_notes_ahead`; hams_shared: the gitlink of hams_open's `origin/main`), the last
    after a fetch when `fetch`; `HAMS_OPEN_TREE` in `environ` names the gate's hams_open;
    the hams_com tree's `hams_shared` link resolves into the gate; every shard module has a manifest in a tree; and every
    artifact passes `artifact_problems`. Never raises for a stale input (that is a returned problem); raises `SnapshotError`
    for an unreadable manifest."""
    environ = os.environ if environ is None else environ
    try:
        with open(os.path.join(gate_dir, MANIFEST_NAME), encoding="utf-8") as handle:
            manifest = json.load(handle)
        trees = Trees(**manifest["trees"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SnapshotError("%s: cannot read the snapshot manifest: %s" % (gate_dir, error)) from error
    cargo_target_dir = cargo_target_dir or manifest.get("cargo_target_dir", "")
    problems = []
    workspace = manifest.get("workspace", DEFAULT_WORKSPACE)
    mains = {"com": os.path.join(workspace, "hams_com"), "open": os.path.join(workspace, "hams_open")}
    expected = {}
    try:
        for key in ("com", "open"):
            expected[key] = origin_main(mains[key], fetch)
        if fetch:
            _git(os.path.join(mains["open"], "hams_shared"), "fetch", "-q", "origin", "main")
        expected["shared"] = recorded_shared_commit(mains["open"], expected["open"])
    except SnapshotError as error:
        problems.append("cannot learn origin/main: %s" % error)
    for key, path, commit in (("com", trees.com, trees.com_commit), ("open", trees.open, trees.open_commit),
                              ("shared", trees.shared, trees.shared_commit)):
        if not os.path.isdir(path):
            problems.append("%s tree %s is missing" % (key, path))
            continue
        head = _git(path, "rev-parse", "HEAD", check=False)
        if head != commit:
            problems.append("%s tree %s is at %s, the snapshot says %s" % (key, path, head[:12], commit[:12]))
        if _git(path, "status", "--porcelain", "--untracked-files=no", check=False):
            problems.append("%s tree %s has uncommitted changes" % (key, path))
        if key in expected and expected[key] != commit and not (key == "com" and only_notes_ahead(mains["com"], commit, expected[key])):
            problems.append("%s is behind origin/main: snapshot %s, origin/main %s" % (key, commit[:12], expected[key][:12]))
    pointed = environ.get(OPEN_TREE_ENV)
    if not pointed:
        problems.append("%s is not set: set it to %s" % (OPEN_TREE_ENV, trees.open))
    elif os.path.realpath(pointed) != os.path.realpath(trees.open):
        problems.append("%s=%s is not the gate's hams_open %s" % (OPEN_TREE_ENV, pointed, trees.open))
    link = os.path.join(trees.com, "hams_shared")
    if os.path.realpath(link) != os.path.realpath(trees.shared):
        problems.append("%s resolves to %s, not to the gate's hams_shared %s" % (link, os.path.realpath(link), trees.shared))
    known = set(discover_modules(trees)) if os.path.isdir(trees.com) and os.path.isdir(trees.open) else set()
    for module in sorted({m for shard in manifest.get("shards", []) for m in shard} - known):
        problems.append("shard module %s has no manifest in either tree" % module)
    for artifact in ARTIFACTS:
        problems += artifact_problems(artifact, trees, cargo_target_dir, environ)
    return problems


def _create_snapshot_once(workspace=DEFAULT_WORKSPACE, parent=None, shards=DEFAULT_SHARDS, shards_file=None, fetch=True,
                    build=True, cargo_target_dir=None, run=None, out=print, hostname=None, balance=None):
    """One attempt of `create_snapshot`; raises `SnapshotBehind` when the finished snapshot verifies except that origin/main moved on."""
    refuse_production(hostname)
    parent = os.path.abspath(parent or os.path.join(workspace, "tmp"))
    cargo_target_dir = cargo_target_dir or os.environ.get("CARGO_TARGET_DIR") or os.path.expanduser("~/.cache/hams-test/gate/cargo-target")
    com_commit = origin_main(os.path.join(workspace, "hams_com"), fetch)
    open_commit = origin_main(os.path.join(workspace, "hams_open"), fetch)
    if fetch:
        _git(os.path.join(workspace, "hams_open", "hams_shared"), "fetch", "-q", "origin", "main")
    shared_commit = recorded_shared_commit(os.path.join(workspace, "hams_open"), open_commit)
    trees = create_trees(workspace, parent, com_commit, open_commit, shared_commit)
    out("gate_snapshot: hams_com %s, hams_open %s, hams_shared %s in %s" % (
        com_commit[:12], open_commit[:12], shared_commit[:12], trees.parent))
    os.makedirs(cargo_target_dir, exist_ok=True)
    if build:
        for artifact in ARTIFACTS:
            if artifact_problems(artifact, trees, cargo_target_dir, {}):
                build_artifact(artifact, trees, cargo_target_dir, run, out)
    previous = None
    if shards_file:
        try:
            with open(shards_file, encoding="utf-8") as handle:
                previous = json.load(handle)
        except (OSError, ValueError) as error:
            raise SnapshotError("%s: cannot read the shard file: %s" % (shards_file, error)) from error
        if not (isinstance(previous, list) and all(isinstance(s, list) for s in previous)):
            raise SnapshotError("%s: expected a JSON list of lists of module names" % shards_file)
    if balance is None:  # round_gate.py calls with no arguments and used to get round-robin shards: 4043 s vs 834 s in round 14
        balance = not shards_file
    seconds = splits = None
    if balance:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "gate_module_seconds.json"), encoding="utf-8") as handle:
            seconds = json.load(handle)
        with open(os.path.join(here, "gate_module_splits.json"), encoding="utf-8") as handle:
            splits = json.load(handle)
    shard_lists, shard_tags = make_split_shards(discover_modules(trees), shards, previous, seconds, splits)
    if not shard_lists:
        # No module found in either tree (a wrong checkout, an empty sparse tree): a snapshot with nothing to test would give the canary a vacuous green.
        raise SnapshotError("no Odoo module (no __manifest__.py at the root of %s or %s): there is nothing to test" % (trees.com, trees.open))
    relay = artifact_output(ARTIFACTS[1], trees, cargo_target_dir)
    commands = []
    for number, shard in enumerate(shard_lists, 1):
        log_directory = os.path.join(trees.parent, "logs", "shard%d" % number)
        os.makedirs(log_directory, exist_ok=True)
        os.chmod(log_directory, 0o755)  # the shard command runs as the invoking user; nobody else may replace the verdict files
        commands.append(shard_command(trees, shard, log_directory, relay, tags=shard_tags[number - 1]))
    manifest = {
        "version": MANIFEST_VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "workspace": workspace,
        "trees": dataclasses.asdict(trees), "cargo_target_dir": cargo_target_dir, "shards": shard_lists, "commands": commands,
    }
    with open(os.path.join(trees.parent, MANIFEST_NAME), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1)
        handle.write("\n")
    with open(os.path.join(trees.parent, SHARDS_NAME), "w", encoding="utf-8") as handle:
        json.dump(shard_lists, handle)
    problems = verify_snapshot(trees.parent, fetch=fetch, environ={OPEN_TREE_ENV: trees.open, RELAY_BIN_ENV: relay},
                               cargo_target_dir=cargo_target_dir)
    message = "the new snapshot does not verify:\n  " + "\n  ".join(problems)
    if problems and all("is behind origin/main" in problem for problem in problems):
        behind = SnapshotBehind(message)
        behind.manifest = manifest
        raise behind
    if problems:
        raise SnapshotError(message)
    return manifest


# [@ANCHOR: gate_snapshot:create_snapshot]
def create_snapshot(workspace=DEFAULT_WORKSPACE, parent=None, shards=DEFAULT_SHARDS, shards_file=None, fetch=True,
                    build=True, cargo_target_dir=None, run=None, out=print, hostname=None, balance=None):
    """Builds the gate tree for the current `origin/main` commits and returns its manifest (also written to
    `<gate>/gate_snapshot.json`, with `gate_shards.json` beside it). Steps: refuse on a production host; fetch and read the
    three commits; make the worktrees; build every artifact (unless `build` is False); choose the shards (from `shards_file`,
    a list of lists, when given); verify. Raises `SnapshotError` when any step fails or the finished snapshot does not verify
    (nothing is returned for a stale one). The manifest holds `trees`, `workspace`, `cargo_target_dir`, `shards`, `commands`
    and `created`. `run` and `hostname` are for tests.
    A snapshot tests an exact set of commits (recorded in the manifest), so one that origin/main moved past while it was being
    built (merges during a long build) is kept as it is: `deploy_to_production.py` still refuses a checkout that is not at
    origin/main. Only a real problem (a tree missing, dirty or at the wrong commit, a missing artifact, an inconsistent
    hams_shared pointer) raises `SnapshotError`; nothing is retried."""
    try:
        return _create_snapshot_once(workspace, parent, shards, shards_file, fetch, build, cargo_target_dir, run, out, hostname, balance)
    except SnapshotBehind as error:
        out("gate_snapshot: origin/main moved during the snapshot; keeping it, it tests the commits it built")
        return error.manifest


# [@ANCHOR: gate_snapshot:prune_snapshots]
def prune_snapshots(parent, keep=1, out=print):
    """Removes all but the newest `keep` `gate_*` directories of `parent` (by modification time): each is a set of git
    worktrees, removed with `git worktree remove --force` through the repository that owns it (its common git directory, wherever
    that is: a submodule keeps it under the superproject's `.git/modules`), then the directory. Returns the removed paths. Never
    touches a directory whose name does not start with `gate_`, nor one that is neither a finished snapshot (`gate_snapshot.json`)
    nor holds a git worktree of ours: that is somebody's directory, and is reported."""
    found = []
    for name in os.listdir(parent):
        gate = os.path.join(parent, name)
        if not (name.startswith("gate_") and os.path.isdir(gate)):
            continue
        ours = os.path.exists(os.path.join(gate, MANIFEST_NAME)) or any(
            os.path.exists(os.path.join(gate, sub, ".git")) for sub in ("hams_open/hams_shared", "hams_open", "hams_com"))
        if ours:
            found.append(gate)
        else:
            out("gate_snapshot: %s has no %s and no worktree of ours; not touching it" % (gate, MANIFEST_NAME))
    found.sort(key=os.path.getmtime, reverse=True)
    removed = []
    for gate in found[max(keep, 0):]:
        for sub in ("hams_open/hams_shared", "hams_open", "hams_com"):
            path = os.path.join(gate, sub)
            if os.path.exists(os.path.join(path, ".git")):
                owner = _git(path, "rev-parse", "--git-common-dir", check=False)
                if owner:
                    subprocess.run(["git", "--git-dir", os.path.abspath(os.path.join(path, owner)), "worktree", "remove", "--force", path],
                                   capture_output=True)
        shutil.rmtree(gate, ignore_errors=True)
        removed.append(gate)
        out("gate_snapshot: removed %s" % gate)
    return removed


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="build the gate tree and its build outputs for the current origin/main")
    create.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    create.add_argument("--parent", default=None, help="where gate_* directories go (default <workspace>/tmp)")
    create.add_argument("--shards", type=int, default=DEFAULT_SHARDS)
    create.add_argument("--balance", action=argparse.BooleanOptionalAction, default=None, help="balance the shards by measured module seconds (gate_module_seconds.json); ignores the modules' old shards. On by default (Bruce, 2026-11-09), except with --shards-file, which keeps the file's shards; --no-balance turns it off")
    create.add_argument("--shards-file", default=None, help="a gate_shards.json whose modules keep their shard")
    create.add_argument("--no-fetch", action="store_true")
    create.add_argument("--no-build", action="store_true")
    create.add_argument("--cargo-target-dir", default=None)
    verify = sub.add_parser("verify", help="exit 1 and name every problem when the snapshot is stale or incomplete")
    verify.add_argument("gate_dir")
    verify.add_argument("--no-fetch", action="store_true")
    commands = sub.add_parser("commands", help="print the test.py command line of every shard")
    commands.add_argument("gate_dir")
    prune = sub.add_parser("prune", help="remove old gate trees")
    prune.add_argument("--parent", default=os.path.join(DEFAULT_WORKSPACE, "tmp"))
    prune.add_argument("--keep", type=int, default=1)
    return parser


# [@ANCHOR: gate_snapshot:main]
def main(argv=None, out=print):
    """The command line: `create`, `verify`, `commands`, `prune`. Returns the exit status: 0 success, 1 a stale or
    incomplete snapshot or a failed step (the problems are printed, one per line, after `REFUSING:`), 2 a production host."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prune":
            refuse_production()
            prune_snapshots(args.parent, args.keep, out)
            return 0
        if args.command == "create":
            manifest = create_snapshot(
                args.workspace, args.parent, args.shards, args.shards_file, not args.no_fetch, not args.no_build,
                args.cargo_target_dir, out=out, balance=(args.balance if args.balance is not None else not args.shards_file),
            )
            gate = manifest["trees"]["parent"]
            out("gate_snapshot: snapshot ready in %s (verified current)" % gate)
            out("gate_snapshot: shards: %s" % os.path.join(gate, SHARDS_NAME))
            for command in manifest["commands"]:
                out(command)
            return 0
        refuse_production()
        if args.command == "commands":
            with open(os.path.join(args.gate_dir, MANIFEST_NAME), encoding="utf-8") as handle:
                for command in json.load(handle)["commands"]:
                    out(command)
            return 0
        problems = verify_snapshot(args.gate_dir, fetch=not args.no_fetch)
        if problems:
            out("REFUSING: the gate snapshot is stale or incomplete:")
            for problem in problems:
                out("  - " + problem)
            return 1
        out("gate_snapshot: %s is current and complete" % args.gate_dir)
        return 0
    except SnapshotError as error:
        out("REFUSING: %s" % error)
        return 2 if "production" in str(error) or "hams1" in str(error) else 1


if __name__ == "__main__":
    sys.exit(main())
