#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Remote execution for run_linters.py: run heavy steps on another machine instead of the dev box.

    run_linters.py --changed --remote auto        spread capable steps over the machines
    run_linters.py --changed --remote mac1-vm     everything that machine can run
    run_linters.py --changed --remote auto --where    print the routing plan, run nothing

How it works (all of it read-only toward the machines' systems; nothing is deployed, and the
inventory marks hams1 and the home router `never`):

  1. INVENTORY (`lint_machines.json`): how to reach each machine, its worker count, nice level, the
     load ratio above which it counts as busy, and why a machine is NOT a runner (Windows hosts,
     Bruce's Mac itself, production).
  2. PROBE (one ssh command per machine, no writes): cores, 1-minute load, and which tools exist.
     Capabilities are measured, not assumed. An unreachable machine is skipped.
  3. ROUTING: every step needs some tools (`requirements_for`). Steps that need git history, the
     network or the dev box's state stay local. The rest go to the capable machine with the least
     estimated finish time (assigned seconds plus its current load), never to a machine already
     above its load ratio, and short steps (under 60 s) stay local because syncing costs more.
  4. SYNC: a persistent mirror under the machine's workdir. Each repository is shipped as the tracked
     tree of its HEAD commit (git archive), incrementally from the commit the mirror already has, plus
     the working tree's uncommitted regular files; excluded: .git, target/, node_modules, symlinks and
     secrets (.env, *.pem, *.key, id_*). The mirror gets a synthetic git repository so checkers that
     call `git ls-files` work. The sync manifest records the exact commits.
  5. RUN: `run_linters.py --only-exact <names> --results-json` on the machine, under its own nice level,
     its own run lock and load gate (the lock is per machine), a per-run TMPDIR. Output streams back
     prefixed with the machine name.
  6. VERIFY and STAMP: the machine's results file must name every step it was given, all passed, for
     the commit the manifest recorded. Only then does the local pass stamp get written, with
     `hosts=` metadata. A machine that cannot be reached or whose run cannot be read is not a failure of
     the code: its steps run locally instead.
"""

import dataclasses
import fnmatch
import io
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
INVENTORY = os.path.join(TOOLS, "lint_machines.json")
REMOTE_MIN_SECONDS = 60.0
UNKNOWN_SECONDS = 120.0
LOCAL = "devbox"

# Steps that need what only this machine has: git history, the network, the checkout as it is.
LOCAL_ONLY = frozenset(
    {
        "check_summation_bias",
        "check_pip_audit",
        "check_noupdate_edits",
        "check_staged_files",
        "check_claims_freshness",
        "check_cargo_deny",
    }
)

# Never shipped to a machine: secrets, VCS metadata, build output, other sessions' worktrees.
EXCLUDE_PATTERNS = (
    ".git", ".claude", "target", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.p12", "*.pfx",
    "secrets", ".secrets", ".ssh", "credentials*",
)


def requirements_for(step_name):
    """The tools a step needs on the machine that runs it; {'local'} for steps that must stay here."""
    base = step_name.split("[")[0]
    if base in LOCAL_ONLY:
        return {"local"}
    if base == "flake8":
        return {"python3", "flake8"}
    if base == "eslint":
        return {"node", "eslint"}
    if base in ("check_cargo_clippy", "check_cargo_fmt"):
        return {"cargo", "clippy", "python3"}
    if base == "check_rust_function_test_anchors":
        return {"cargo", "python3"}
    if step_name == "pytest tools/[host-bound]":
        return {"local"}  # needs this checkout and this login session (HOST_BOUND_TOOL_TESTS in run_linters.py)
    if base.startswith("pytest"):
        return {"python3", "pytest"}
    if base == "check_owl_templates":
        return {"python3", "node", "chrome", "odoo"}
    if base in ("check_minified_js_syntax_errors", "check_minified_js_nested_templates", "pre_flight_check"):
        return {"python3", "odoo"} | ({"node"} if "minified_js_syntax" in base else set())
    if base in ("check_js_syntax", "check_js_function_test_anchors", "check_js_test_hook_gating", "check_hoot_runner_coverage"):
        return {"python3", "node"}
    return {"python3"}


# --- inventory and probes ----------------------------------------------------------------------


def load_inventory(path=None, environ=None):
    environ = os.environ if environ is None else environ
    path = path or environ.get("HAMS_LINT_MACHINES") or INVENTORY
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["machines"]


def runners(inventory):
    """name -> machine for every machine a step may be sent to (excluding the local one)."""
    return {
        name: m
        for name, m in inventory.items()
        if m.get("ssh") and m.get("runner", True) and not m.get("never")
    }


# Tests commit in scratch repositories; a lint machine has no global git identity.
GIT_IDENTITY = (
    "GIT_AUTHOR_NAME=lint GIT_AUTHOR_EMAIL=lint@localhost GIT_COMMITTER_NAME=lint GIT_COMMITTER_EMAIL=lint@localhost"
)
FORWARDED_ENV = ("HAMS_LINT_XDIST",)  # run_linters settings that must reach the machine's own run


def remote_wait_args(environ=None):
    """A machine's run lock and load gate are waited for only this long (HAMS_LINT_REMOTE_WAIT seconds, default
    120): a lock held by a stuck run, or a machine that stays loaded, must not hang this run. The remote run then
    exits 75 without a results file and its steps run here (infrastructure fallback)."""
    environ = os.environ if environ is None else environ
    try:
        seconds = float(environ.get("HAMS_LINT_REMOTE_WAIT", "120"))
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(seconds)
    except ValueError:
        seconds = 120.0
    return f"--lock-timeout {seconds:g} --load-timeout {seconds:g}"


def _forwarded_env(environ=None):
    environ = os.environ if environ is None else environ
    return "".join(f" {name}={shlex.quote(environ[name])}" for name in FORWARDED_ENV if environ.get(name))


REMOTE_PATH_PREFIX = 'export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"; '

PROBE_SCRIPT = r"""
echo "os=$(uname -s)"
echo "cores=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null)"
echo "load1=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"
echo "pyver=$(/usr/bin/python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
for t in git tar node flake8; do command -v $t >/dev/null 2>&1 && echo "has_$t=1"; done
cargo --version >/dev/null 2>&1 && echo "has_cargo=1"
cargo clippy --version >/dev/null 2>&1 && cargo fmt --version >/dev/null 2>&1 && echo "has_clippy=1"
for t in google-chrome chromium chromium-browser; do command -v $t >/dev/null 2>&1 && echo "has_chrome=1"; done
command -v eslint >/dev/null 2>&1 && echo "has_eslint=1"
/usr/bin/python3 -c 'import pytest' >/dev/null 2>&1 && echo "has_pytest=1"
/usr/bin/python3 -c 'import xdist' >/dev/null 2>&1 && echo "has_xdist=1"
/usr/bin/python3 -c 'import odoo' >/dev/null 2>&1 && echo "has_odoo=1"
q="$HOME/.cache/hams-lint/run.lock.queue.d"
n=0
for f in "$q"/*; do
  b=${f##*/}
  case $b in holder*|.*|\*) continue;; esac
  pid=${b#*-}; pid=${pid%%-*}
  [ -d "/proc/$pid" ] && n=$((n+1))
done
echo "queued=$n"
l="$HOME/.cache/hams-lint/run.lock"
if [ -e "$l" ] && command -v flock >/dev/null 2>&1 && ! flock -n "$l" true; then echo "held=1"; else echo "held=0"; fi
echo "awake=1"
"""


@dataclasses.dataclass
class Probe:
    name: str
    reachable: bool
    cores: int = 1
    load1: float = 0.0
    queued: int = 0
    caps: frozenset = frozenset()
    error: str = ""
    held: bool = False  # a lint run holds the machine's run lock right now


def parse_probe(name, text):
    values = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key and _:
            values[key.strip()] = value.strip()
    if "os" not in values:
        return Probe(name, False, error="no probe output")
    caps = {key[4:] for key, value in values.items() if key.startswith("has_") and value == "1"}
    try:
        major, minor = values.get("pyver", "0.0").split(".")
        if (int(major), int(minor)) >= (3, 10):
            caps.add("python3")
    except ValueError:
        pass
    if "git" not in caps or "tar" not in caps:
        caps.discard("python3")  # cannot receive a mirror
    try:
        cores = max(1, int(values.get("cores") or 1))
    except ValueError:
        cores = 1
    try:
        load1 = float(values.get("load1") or 0.0)
        if not math.isfinite(load1):
            raise ValueError(load1)
    except ValueError:
        load1 = 0.0
    try:
        queued = int(values.get("queued") or 0)
    except ValueError:
        queued = 0
    return Probe(name, True, cores, load1, queued, frozenset(caps), held=values.get("held") == "1")


# --- transport ----------------------------------------------------------------------------------


class SshTransport:
    """One machine reached through ssh. Never prompts (BatchMode), never forwards agents."""

    def __init__(self, name, machine, timeout=30):
        self.name = name
        self.machine = machine
        self.timeout = timeout

    def argv(self, command):
        # A non-interactive ssh command gets no login PATH: put the user-level toolchains (rustup in
        # ~/.cargo/bin, pip --user scripts in ~/.local/bin) in front, as tools/provision_build_host.sh installs them.
        command = REMOTE_PATH_PREFIX + command
        ssh = self.machine["ssh"]
        args = [os.path.expanduser(a) if a.startswith("~") else a for a in ssh.get("args", [])]
        args = [a.replace("~/", os.path.expanduser("~") + "/") for a in args]
        return [
            "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.timeout}", "-o", "ForwardAgent=no",
            "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=6", *args, ssh["target"], command,
        ]

    def run(self, command, data=None, timeout=None):
        try:
            res = subprocess.run(
                self.argv(command), input=data, capture_output=True, timeout=timeout or self.timeout
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            return 255, b"", str(error).encode()
        return res.returncode, res.stdout, res.stderr

    def stream(self, command, on_line, timeout=None):
        proc = subprocess.Popen(self.argv(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + timeout if timeout else None
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
            if deadline and time.monotonic() > deadline:
                proc.kill()
                break
        return proc.wait()


class ShellTransport:
    """A 'machine' that is a directory on this one (tests): commands run through `sh -c`."""

    def __init__(self, name="fake", home=None, env=None):
        self.name = name
        self.home = home
        self.env = dict(os.environ, **(env or {}))
        if home:
            self.env["HOME"] = home

    def run(self, command, data=None, timeout=None):
        res = subprocess.run(["sh", "-c", command], input=data, capture_output=True, env=self.env, timeout=timeout or 600)
        return res.returncode, res.stdout, res.stderr

    def stream(self, command, on_line, timeout=None):
        proc = subprocess.Popen(["sh", "-c", command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env)
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        return proc.wait()


def probe_machine(name, transport):
    code, out, err = transport.run(PROBE_SCRIPT)
    if code != 0 and not out:
        return Probe(name, False, error=(err.decode(errors="replace").strip().splitlines() or ["unreachable"])[-1][:200])
    return parse_probe(name, out.decode(errors="replace"))


# --- routing ------------------------------------------------------------------------------------


@dataclasses.dataclass
class Plan:
    local: list
    remote: dict  # host -> [step names]
    notes: list
    seconds: dict  # host -> estimated seconds assigned
    skipped_hosts: dict  # host -> reason


# Steps that are fast only when their build output is warm. A fresh worktree has none (check_cargo_clippy took
# 783 s in one on 2026-11-02, against a recorded median of seconds); a machine's mirror keeps its target/ directories
# from run to run, so these are routed as if they were heavy.
COLD_COST_SECONDS = {"check_cargo_clippy": 600.0, "check_cargo_fmt": 0.0}


def _est(name, durations):
    return max(durations.get(name, UNKNOWN_SECONDS), COLD_COST_SECONDS.get(name.split("[")[0], 0.0))


def make_plan(step_names, static_names, durations, inventory, probes, mode, local_cores, local_load,
              whole=False, exclude=(), unfit=None):
    """Route steps. `mode`: 'local', 'auto' or a machine name. `static_names`: steps with no command
    (always local). `probes`: name -> Probe for every runner. Pure: no I/O.

    `whole` (mode 'auto' only): this machine's run lock is busy, so the whole run is shipped, see
    `make_whole_plan`. `exclude`: machines another run has claimed (whole runs only)."""
    notes = []
    skipped = {}
    if mode == "local" or not mode:
        return Plan(list(step_names), {}, ["--remote local: everything runs here"], {LOCAL: sum(_est(n, durations) for n in step_names)}, {})
    candidates = {}
    wanted = runners(inventory)
    if mode != "auto":
        if mode not in inventory:
            raise SystemExit(f"--remote {mode}: no such machine in lint_machines.json (known: {', '.join(sorted(inventory))})")
        machine = inventory[mode]
        if machine.get("never") or machine.get("runner") is False or not machine.get("ssh"):
            raise SystemExit(f"--remote {mode}: not a lint runner: {machine.get('reason', 'no ssh entry')}")
        wanted = {mode: machine}
    for name, machine in wanted.items():
        probe = probes.get(name)
        if probe is None or not probe.reachable:
            skipped[name] = "unreachable" + (f" ({probe.error})" if probe and probe.error else "")
            continue
        jobs = int(machine.get("jobs", max(1, probe.cores // 2)))
        ratio = float(machine.get("max_load_ratio", 0.8))
        if probe.load1 > probe.cores * ratio:
            skipped[name] = f"busy: load {probe.load1:.1f} above {probe.cores * ratio:.1f}"
            continue
        if whole and mode == "auto":
            # A whole run waits for nothing: a machine with a run in progress or in line is not a place to send it.
            if name in exclude:
                skipped[name] = "claimed by another run started on this machine"
                continue
            if probe.held or probe.queued >= int(machine.get("max_concurrent", 1)):
                skipped[name] = f"busy: {max(probe.queued, 1)} lint run(s) hold or wait for its lock"
                continue
        candidates[name] = (probe, jobs)
    if whole and mode == "auto":
        return make_whole_plan(step_names, static_names, durations, candidates, skipped, notes, local_cores, unfit or {})
    assigned = {LOCAL: []}
    finish = {LOCAL: local_load * 60.0 / max(1, local_cores)}
    for name, (probe, jobs) in candidates.items():
        assigned[name] = []
        finish[name] = (probe.load1 + probe.queued * probe.cores) * 60.0 / max(1, jobs)
    jobs_of = {LOCAL: max(1, local_cores // 2)}
    jobs_of.update({name: jobs for name, (_p, jobs) in candidates.items()})
    for step in sorted(step_names, key=lambda n: -_est(n, durations)):
        need = requirements_for(step)
        seconds = _est(step, durations)
        if step in static_names or "local" in need:
            assigned[LOCAL].append(step)
            finish[LOCAL] += seconds / jobs_of[LOCAL]
            continue
        options = [
            name for name, (probe, _jobs) in candidates.items()
            if need <= probe.caps and (mode != "auto" or seconds >= REMOTE_MIN_SECONDS)
        ]
        if mode != "auto" and not options and step not in static_names and "local" not in need:
            notes.append(f"{step}: {mode} lacks {', '.join(sorted(need - (candidates[mode][0].caps if mode in candidates else set())))}: runs here")
        # A step is one process: it finishes when the work queued ahead of it (spread over the workers) is done, plus its own length.
        best = LOCAL
        best_finish = finish[LOCAL] + seconds
        for name in options:
            candidate = finish[name] + seconds
            # The point is to keep the shared dev box free: a capable machine takes a heavy step unless it would
            # finish it more than one step-length (at most twice as slow) later than the dev box.
            if candidate <= best_finish + seconds or mode != "auto":
                best, best_finish = name, candidate
        assigned[best].append(step)
        finish[best] += seconds / jobs_of[best]
    remote = {name: sorted(names, key=step_names.index) for name, names in assigned.items() if name != LOCAL and names}
    local = sorted(assigned[LOCAL], key=step_names.index)
    seconds_by = {name: sum(_est(n, durations) for n in names) for name, names in assigned.items()}
    for name, reason in skipped.items():
        notes.append(f"{name}: skipped, {reason}")
    if not remote:
        notes.append("nothing is routed away: every step runs here")
    return Plan(local, remote, notes, seconds_by, skipped)


def make_whole_plan(step_names, static_names, durations, candidates, skipped, notes, local_cores, unfit=None):
    """The plan for a run that would have to queue for this machine's run lock (2026-11-03).

    One idle machine takes EVERY step it can run, however short (the 60 s floor of the spreading plan is
    for a free dev box), under its own run lock and load gate; the machine that can run the most work
    (least loaded on a tie) is the primary. A step it cannot run goes to another idle machine only when
    those steps add up to at least REMOTE_MIN_SECONDS there (a second sync costs more than a few short
    steps); everything else stays here. Steps that need this checkout (LOCAL_ONLY) or have no command stay
    here. The run therefore waits for this machine's lock only for those."""
    movable = [n for n in step_names if n not in static_names and "local" not in requirements_for(n)]
    assigned = {LOCAL: [n for n in step_names if n not in movable]}
    if not candidates or not movable:
        for name, reason in skipped.items():
            notes.append(f"{name}: skipped, {reason}")
        notes.append("whole run: nothing can be shipped, the run queues here as before")
        return Plan(list(step_names), {}, notes, {LOCAL: sum(_est(n, durations) for n in step_names)}, skipped)

    unfit = unfit or {}

    def can_run(step, name):
        return requirements_for(step) <= candidates[name][0].caps and step not in unfit.get(name, ())

    def covered(name):
        return sum(_est(n, durations) for n in movable if can_run(n, name))

    def load_fraction(name):
        probe, _jobs = candidates[name]
        return (probe.load1 + probe.queued * probe.cores) / max(1, probe.cores)

    primary = max(candidates, key=lambda name: (covered(name), -load_fraction(name)))
    if covered(primary) <= 0:
        notes.append("whole run: no machine can run any step, the run queues here as before")
        return Plan(list(step_names), {}, notes, {LOCAL: sum(_est(n, durations) for n in step_names)}, skipped)
    assigned[primary] = [n for n in movable if can_run(n, primary)]
    rest = [n for n in movable if n not in assigned[primary]]
    extra = {}
    for step in sorted(rest, key=lambda n: -_est(n, durations)):
        options = [name for name in candidates if name != primary and can_run(step, name)]
        if options:
            best = min(options, key=lambda name: (sum(_est(n, durations) for n in extra.get(name, [])), load_fraction(name)))
            extra.setdefault(best, []).append(step)
    for name, names in extra.items():
        if sum(_est(n, durations) for n in names) >= REMOTE_MIN_SECONDS:
            assigned[name] = names
    placed = {n for key, names in assigned.items() if key != LOCAL for n in names}
    assigned[LOCAL] = [n for n in step_names if n not in placed]
    remote = {name: sorted(names, key=step_names.index) for name, names in assigned.items() if name != LOCAL and names}
    seconds_by = {name: sum(_est(n, durations) for n in names) for name, names in assigned.items()}
    notes.append(f"whole run: this machine's lint lock is busy; {primary} takes {len(remote[primary])} of {len(step_names)} steps")
    for name, reason in skipped.items():
        notes.append(f"{name}: skipped, {reason}")
    return Plan(sorted(assigned[LOCAL], key=step_names.index), remote, notes, seconds_by, skipped)


def format_plan(plan, inventory, probes):
    lines = ["Routing plan (--where):"]
    local_sec = plan.seconds.get(LOCAL, 0)
    lines.append(f"  {LOCAL:<12} {len(plan.local):>3} steps  ~{local_sec:>5.0f} s of work")
    for name, names in sorted(plan.remote.items()):
        probe = probes.get(name)
        extra = f" cores={probe.cores} load={probe.load1:.1f} queued={probe.queued}" if probe else ""
        lines.append(f"  {name:<12} {len(names):>3} steps  ~{plan.seconds.get(name, 0):>5.0f} s of work{extra}")
        for step in names:
            lines.append(f"      {step}")
    for name, probe in sorted(probes.items()):
        if probe.reachable:
            lines.append(
                f"  probed {name}: {probe.cores} cores, load {probe.load1:.1f}, {probe.queued} queued"
                + (", run lock held" if probe.held else "") + "; has "
                + (", ".join(sorted(probe.caps)) or "nothing usable")
            )
        else:
            lines.append(f"  probed {name}: unreachable ({probe.error})")
    for note in plan.notes:
        lines.append(f"  note: {note}")
    for name, machine in sorted(inventory.items()):
        if name == LOCAL or machine.get("ssh"):
            continue
        lines.append(f"  not a runner: {name}: {machine.get('reason', '')}")
    return "\n".join(lines)


# --- sync ---------------------------------------------------------------------------------------


def _under(path, prefixes):
    """True when `path` is one of `prefixes` or below it ('hams_shared/' also covers the gitlink 'hams_shared')."""
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def excluded(path):
    parts = path.split("/")
    return any(fnmatch.fnmatch(part, pattern) for part in parts for pattern in EXCLUDE_PATTERNS)


def git(repo, *args, data=None):
    res = subprocess.run(["git", "-C", repo, *args], capture_output=True, input=data)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed in {repo}: {res.stderr.decode(errors='replace')[:200]}")
    return res.stdout


def head_sha(repo):
    return git(repo, "rev-parse", "HEAD").decode().strip()


def dirty_files(repo):
    """(modified_or_untracked regular files, deleted files) in the working tree, as repo-relative
    paths, secrets and symlinks excluded."""
    out = git(repo, "status", "--porcelain", "-z", "--untracked-files=all").split(b"\0")
    changed, deleted = [], []
    index = 0
    while index < len(out):
        entry = out[index].decode(errors="replace")
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":
            index += 1  # the rename's source follows as its own field
        if excluded(path):
            continue
        full = os.path.join(repo, path)
        if "D" in status and not os.path.exists(full):
            deleted.append(path)
        elif os.path.isfile(full) and not os.path.islink(full):
            changed.append(path)
    return sorted(changed), sorted(deleted)


def _tar_of_files(repo, paths):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in paths:
            archive.add(os.path.join(repo, path), arcname=path, recursive=False)
    return buffer.getvalue()


def _tree_tar(repo, sha, paths=None):
    """A tar of `paths` (default: everything) as committed in `sha`. Not `git archive`: that honours
    `export-ignore` in .gitattributes, which silently left 300+ tracked files (hams_local_relay's claims/, fuzz/
    and a test suite) out of every mirror. A scratch index and `checkout-index` write the files exactly as
    committed."""
    with tempfile.TemporaryDirectory(prefix="lint-tree-") as scratch:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(scratch, "index"))
        out = os.path.join(scratch, "tree")
        os.makedirs(out)
        subprocess.run(["git", "-C", repo, "read-tree", sha], env=env, check=True, capture_output=True)
        args = ["git", "-C", repo, "checkout-index", "-f", f"--prefix={out}/"]
        if paths is None:
            res = subprocess.run(args + ["-a"], env=env, capture_output=True)
        else:
            res = subprocess.run(args + ["-z", "--stdin"], env=env, capture_output=True, input=("\0".join(paths) + "\0").encode())
        if res.returncode != 0:
            raise RuntimeError(f"git checkout-index failed in {repo}: {res.stderr.decode(errors='replace')[:200]}")
        tar = subprocess.run(["tar", "-cf", "-", "-C", out, "."], capture_output=True)
        if tar.returncode != 0:
            raise RuntimeError(f"tar failed: {tar.stderr.decode(errors='replace')[:200]}")
        return tar.stdout


def _archive(repo, sha, paths=None):
    if paths is None:
        return _tree_tar(repo, sha)
    return [_tree_tar(repo, sha, paths[start : start + 2000]) for start in range(0, len(paths), 2000)]


def _extract_cmd(dest):
    return f"mkdir -p {shlex.quote(dest)} && tar -x -C {shlex.quote(dest)} -f -"


SYNC_FORMAT = 2


def sync_repo(transport, repo, dest, sub_exclude=(), log=print, label=None):
    """Bring `dest` (a path on the machine) to `repo`'s HEAD tree plus its uncommitted regular files.
    Incremental from the commit the mirror last received. Returns {'sha', 'dirty': [...]}."""
    sha = head_sha(repo)
    code, out, _err = transport.run(f"cat {shlex.quote(dest)}/.hams_lint_sync 2>/dev/null")
    previous = {}
    if code == 0 and out.strip():
        try:
            previous = json.loads(out)
        except ValueError:
            previous = {}
    # A mirror written by an older sync method (git archive: it dropped export-ignore files) is rebuilt in full.
    base = previous.get("sha") if previous.get("format") == SYNC_FORMAT else None
    known = False
    if base:
        known = subprocess.run(["git", "-C", repo, "cat-file", "-e", base + "^{commit}"], capture_output=True).returncode == 0
    removed = set(previous.get("overlay", [])) | set(previous.get("deleted_overlay", []))
    if known:
        diff = git(repo, "diff", "--name-status", "-z", "--no-renames", base, sha).split(b"\0")
        touched, gone = set(removed), set()
        index = 0
        while index + 1 < len(diff):
            status, path = diff[index].decode(), diff[index + 1].decode()
            index += 2
            (gone if status.startswith("D") else touched).add(path)
        in_sha = set(git(repo, "ls-tree", "-r", "-z", "--name-only", sha).decode().split("\0"))
        gone |= {p for p in touched if p not in in_sha}
        touched = {p for p in touched if p in in_sha}
        touched = sorted(p for p in touched if not excluded(p) and not _under(p, sub_exclude))
        gone = sorted(p for p in gone if not excluded(p))
        log(f"sync {label or os.path.basename(repo)}: {len(touched)} changed, {len(gone)} removed since {base[:9]}")
        for piece in _archive(repo, sha, touched) if touched else []:
            _check(transport.run(_extract_cmd(dest), piece, timeout=600), "extract")
        if gone:
            _check(transport.run(f"cd {shlex.quote(dest)} && xargs -0 rm -f --", ("\0".join(gone) + "\0").encode()), "remove")
    else:
        log(f"sync {label or os.path.basename(repo)}: full tree at {sha[:9]}")
        _check(transport.run(f"rm -rf {shlex.quote(dest)}.new && mkdir -p {shlex.quote(dest)}.new"), "mkdir")
        _check(transport.run(_extract_cmd(dest + ".new"), _archive(repo, sha), timeout=1800), "extract")
        _check(transport.run(f"rm -rf {shlex.quote(dest)} && mv {shlex.quote(dest)}.new {shlex.quote(dest)}"), "swap")
    changed, deleted = dirty_files(repo)
    changed = [p for p in changed if not _under(p, sub_exclude)]
    if changed:
        _check(transport.run(_extract_cmd(dest), _tar_of_files(repo, changed), timeout=600), "overlay")
    if deleted:
        _check(transport.run(f"cd {shlex.quote(dest)} && xargs -0 rm -f --", ("\0".join(deleted) + "\0").encode()), "overlay-delete")
    manifest = {"format": SYNC_FORMAT, "sha": sha, "overlay": changed, "deleted_overlay": deleted, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _check(transport.run(f"cat > {shlex.quote(dest)}/.hams_lint_sync", json.dumps(manifest).encode()), "manifest")
    return {"sha": sha, "dirty": bool(changed or deleted)}


def commit_mirror(transport, dest, nested=()):
    """Give the mirror a synthetic git repository so `git ls-files`-based checkers work.

    `nested`: directories inside `dest` that are repositories of their own (hams_open's hams_shared). They get their own
    synthetic repository first, and files an older run committed into this one are dropped from its index: git then
    records the directory as a gitlink, exactly as the real hams_open lists its hams_shared submodule. Without that,
    `git ls-files '*.rs'` in hams_open also listed hams_shared's own Rust sources and check_rust_function_test_anchors
    failed on the runners for functions the dev box never sees (2026-11-03)."""
    q = shlex.quote(dest)
    drop = "".join(f"git rm -r -q --cached --ignore-unmatch -- {shlex.quote(n)} >/dev/null 2>&1; " for n in nested)
    cmd = (
        f"cd {q} && {{ [ -d .git ] || git init -q; }} && mkdir -p .git/info && "
        f"for e in .hams_lint_sync .lint_results.json '.tmp-*'; do grep -qxF \"$e\" .git/info/exclude 2>/dev/null || echo \"$e\" >> .git/info/exclude; done; "
        f"{drop}git add -A . >/dev/null 2>&1; "
        f"git -c user.name=lint -c user.email=lint@localhost commit -q --allow-empty -m 'lint mirror' >/dev/null 2>&1; true"
    )
    _check(transport.run(cmd, timeout=900), "mirror commit")


def _check(result, what):
    code, _out, err = result
    if code != 0:
        raise RuntimeError(f"{what} failed ({code}): {err.decode(errors='replace')[-200:]}")


# --- run ----------------------------------------------------------------------------------------


PLACEHOLDERS = {"hams_com": "@HAMS_COM@", "hams_open": "@HAMS_OPEN@", "hams_shared": "@HAMS_SHARED@", "home": "@HOME@"}


def _substitutions(roots, extra_prefixes=()):
    """[(local prefix, placeholder)] longest first. `roots`: role -> local path."""
    pairs = []
    for role, path in roots.items():
        if not path:
            continue
        for variant in {path, os.path.realpath(path)}:
            pairs.append((variant.rstrip(os.sep), PLACEHOLDERS[role]))
    for prefix, placeholder in extra_prefixes:
        pairs.append((prefix.rstrip(os.sep), placeholder))
    return sorted(set(pairs), key=lambda pair: -len(pair[0]))


def _replace(text, pairs):
    if not isinstance(text, str):
        return text
    for prefix, placeholder in pairs:
        if prefix:
            text = text.replace(prefix, placeholder)
    return text


def export_step(step_dict, roots, home=None):
    """A step as portable JSON, or None when it still names a path of this machine (then it must
    run here). `step_dict` is lint_exec.step_to_dict() plus print_ok/fail_header/missing_msg."""
    pairs = _substitutions(roots, [(home or os.path.expanduser("~"), PLACEHOLDERS["home"])])
    out = dict(step_dict)
    out["argv"] = [_replace(a, pairs) for a in step_dict["argv"]]
    out["cwd"] = _replace(step_dict.get("cwd"), pairs)
    out["env_delta"] = {k: _replace(v, pairs) for k, v in (step_dict.get("env_delta") or {}).items()}
    out.pop("roots", None)
    out.pop("suffixes", None)
    local_homes = {home or os.path.expanduser("~"), os.path.realpath(home or os.path.expanduser("~"))}
    blob = json.dumps(out)
    if any(h and h in blob for h in local_homes if h != "/") or "/workspace/" in blob:
        return None
    if out["argv"] and out["argv"][0].startswith("@"):
        return None  # a repository-local interpreter (a daemon's .venv, untracked) is not on the mirror
    return out


def import_bundle(bundle, roots, home=None):
    """Remote side: turn a bundle back into lint_exec.Step objects with this machine's paths."""
    import lint_exec

    mapping = {PLACEHOLDERS[role]: path for role, path in roots.items() if path}
    mapping[PLACEHOLDERS["home"]] = home or os.path.expanduser("~")

    def back(value):
        if not isinstance(value, str):
            return value
        for placeholder, path in mapping.items():
            value = value.replace(placeholder, path)
        return value

    def program(argv):
        """The dev box names system programs by absolute path (/usr/bin/flake8); a machine that has the program
        elsewhere on its PATH (pip --user: ~/.local/bin) still runs the step."""
        if argv and isinstance(argv[0], str) and argv[0].startswith("/usr/bin/") and not os.path.exists(argv[0]):
            found = shutil.which(os.path.basename(argv[0]))
            if found:
                return [found] + argv[1:]
        return argv

    steps = []
    for item in bundle["steps"]:
        env = None
        if item.get("env_delta"):
            env = dict(os.environ)
            env.update({k: back(v) for k, v in item["env_delta"].items()})
        steps.append(
            lint_exec.Step(
                name=item["name"],
                argv=program([back(a) for a in item["argv"]]),
                cwd=back(item.get("cwd")),
                env=env,
                lock=item.get("lock"),
                fail_header=item.get("fail_header"),
                print_ok=item.get("print_ok", True),
                missing_msg=item.get("missing_msg"),
            )
        )
    return steps


def results_document(bundle_id, results):
    return {
        "bundle_id": bundle_id,
        "steps": [{"name": r.name, "returncode": r.returncode, "seconds": round(r.seconds, 2)} for r in results],
    }


def verify_results(raw, expected_names, bundle_id):
    """(ok, problems): the results file must name every expected step, all passed, for this bundle."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False, ["results file missing or unreadable"]
    problems = []
    if data.get("bundle_id") != bundle_id:
        problems.append("results belong to another run")
    got = {entry["name"]: entry for entry in data.get("steps", [])}
    for name in expected_names:
        if name not in got:
            problems.append(f"step did not run: {name}")
        elif got[name].get("returncode") != 0:
            problems.append(f"step failed: {name}")
    return not problems, problems


def ensure_node_modules(transport, shared_dir, log=print):
    """The mirror never carries node_modules; ESLint (and the tests that run it) need hams_shared's, so install
    them from package-lock.json (`npm ci --ignore-scripts`) once per lock-file content. Best effort: without
    npm or a network the eslint steps and tests that need it fail on the machine and are re-run here."""
    q = shlex.quote(shared_dir)
    command = (
        f"cd {q} 2>/dev/null && [ -f package-lock.json ] && command -v npm >/dev/null || exit 0; "
        "h=$(sha256sum package-lock.json | cut -d' ' -f1); "
        '[ "$(cat node_modules/.lock-hash 2>/dev/null)" = "$h" ] && exit 0; '
        'npm ci --ignore-scripts --no-audit --no-fund >/dev/null 2>&1 && mkdir -p node_modules && echo "$h" > node_modules/.lock-hash'
    )
    code, _out, _err = transport.run(command, timeout=900)
    if code != 0:
        log("[*] --remote: npm ci in the mirror failed; eslint-dependent steps will run here")


def run_on_machine(name, machine, transport, repos, invocation_role, bundle, on_line, log=print):
    """Sync, send the bundle, run it. Returns ('ok'|'failed'|'infra', problems, seconds_by_step).
    'infra' means the machine could not do the job (the caller runs these steps here); 'failed'
    means a step really failed."""
    workdir = machine.get("workdir", "~/hams-lint-mirror")
    code, out, _err = transport.run(f"mkdir -p {workdir} && cd {workdir} && pwd")
    if code != 0 or not out.strip():
        return "infra", ["cannot create the work directory"], {}
    root = out.decode().strip().splitlines()[-1]
    try:
        info = {}
        for role in ("hams_com", "hams_open"):
            path = repos.get(role)
            if path and os.path.isdir(path):
                info[role] = sync_repo(
                    transport, path, f"{root}/{role}",
                    sub_exclude=("hams_shared",) if role == "hams_open" else (), log=log, label=role,
                )
        shared = repos.get("hams_shared")
        if shared and "hams_open" in info:
            info["hams_shared"] = sync_repo(transport, shared, f"{root}/hams_open/hams_shared", log=log, label="hams_shared")
        if "hams_shared" in info:
            commit_mirror(transport, f"{root}/hams_open/hams_shared")
        for role in ("hams_com", "hams_open"):
            if role in info:
                commit_mirror(transport, f"{root}/{role}", nested=("hams_shared",) if role == "hams_open" and "hams_shared" in info else ())
        ensure_node_modules(transport, f"{root}/hams_open/hams_shared", log)
        results_path = f"{root}/.lint_results.json"
        _check(transport.run(f"rm -f {shlex.quote(results_path)}"), "clear")
        _check(transport.run(f"cat > {shlex.quote(root)}/.lint_bundle.json", json.dumps(bundle).encode()), "bundle")
    except (RuntimeError, OSError) as error:
        return "infra", [f"sync failed: {error}"], {}
    q = shlex.quote
    # Short on purpose: Chrome refuses a profile directory whose SingletonSocket path exceeds 107 bytes, and
    # mac1-vm's long home directory plus the mirror path made check_owl_templates fail there (2026-11-03).
    tmpdir = f"/tmp/hams-lint-$(id -u)-{os.getpid()}"
    roots_args = " ".join(
        f"--bundle-root {role}={q(root + '/' + role if role != 'hams_shared' else root + '/hams_open/hams_shared')}"
        for role in ("hams_com", "hams_open", "hams_shared")
        if role in info
    )
    command = (
        f"mkdir -p -m 700 {tmpdir} && cd {q(root)}/{invocation_role} && TMPDIR={tmpdir} {GIT_IDENTITY}{_forwarded_env()} "
        f"nice -n {int(machine.get('nice', 10))} python3 {q(root)}/hams_open/hams_shared/tools/run_linters.py "
        f"--run-bundle {q(root)}/.lint_bundle.json {roots_args} --results-json {q(results_path)} "
        f"-j {int(machine.get('jobs', 2))} --no-timing {remote_wait_args()} 2>&1; rm -rf {tmpdir}"
    )
    code = transport.stream(command, on_line)
    code2, raw, _err = transport.run(f"cat {q(results_path)} 2>/dev/null")
    if code2 != 0 or not raw.strip():
        return "infra", [f"run produced no results file (exit {code})"], {}
    expected = [item["name"] for item in bundle["steps"]]
    ok, problems = verify_results(raw.decode(errors="replace"), expected, bundle["bundle_id"])
    seconds = {entry["name"]: entry.get("seconds", 0.0) for entry in json.loads(raw).get("steps", [])}
    if ok:
        return "ok", [], seconds
    kind = "failed" if all(p.startswith("step failed") for p in problems) else "infra"
    return kind, problems, seconds


def stamp_hosts_text(hosts):
    return ",".join(sorted(set(hosts)))


class RemoteJob:
    """One machine's share of a run, in a thread. After `join()`: `.status` ('ok', 'failed', 'infra'),
    `.problems`, `.seconds` (step name -> seconds) and `.names`."""

    def __init__(self, host, machine, transport, repos, role, bundle, out, print_lock, claim=None):
        self.host, self.machine, self.transport = host, machine, transport
        self.claim = claim  # open file holding the machine's claim (lint_lock.try_claim); closed when the job ends
        self.repos, self.role, self.bundle = repos, role, bundle
        self.names = [item["name"] for item in bundle["steps"]]
        self.out, self.print_lock = out, print_lock
        self.status, self.problems, self.seconds = "infra", ["not run"], {}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _line(self, line):
        with self.print_lock:
            self.out(f"[{self.host}] {line}")

    def _run(self):
        try:
            self.status, self.problems, self.seconds = run_on_machine(
                self.host, self.machine, self.transport, self.repos, self.role, self.bundle, self._line, log=self._line
            )
        except Exception as error:  # noqa: BLE001 - any failure of the machinery means "run it here"
            self.status, self.problems, self.seconds = "infra", [f"{type(error).__name__}: {error}"], {}
        finally:
            if self.claim is not None:
                self.claim.close()
                self.claim = None

    def start(self):
        self.thread.start()
        return self

    def join(self):
        self.thread.join()


UNFIT_TTL_SECONDS = 3 * 86400.0


def unfit_path(environ=None):
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hams-lint", "remote_unfit.json")


def load_unfit(path=None, now=None):
    """host -> set of step names that failed on that machine and passed here in the last three days (a missing
    Python package or service there). A whole run does not send them again: it would only queue them here
    afterwards. Unreadable file: nothing is known."""
    now = time.time() if now is None else now
    try:
        with open(path or unfit_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    unfit = {}
    for host, steps in (data if isinstance(data, dict) else {}).items():
        if isinstance(steps, dict):
            names = {name for name, stamp in steps.items() if isinstance(stamp, (int, float)) and now - stamp < UNFIT_TTL_SECONDS}
            if names:
                unfit[host] = names
    return unfit


def record_unfit(host, names, path=None, now=None):
    """Remember that `names` failed on `host` but pass on this machine. Best effort."""
    now = time.time() if now is None else now
    path = path or unfit_path()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    entry = data.setdefault(host, {})
    for name in names:
        entry[name] = now
    for steps in data.values():
        for name in [n for n, stamp in steps.items() if not isinstance(stamp, (int, float)) or now - stamp >= UNFIT_TTL_SECONDS]:
            del steps[name]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        pass


def collect(jobs, all_steps, execute_here, result_class, out=print, on_unfit=None):
    """Wait for the remote jobs. Returns (results, hosts): a Result per remote step. The steps of a
    machine that could not do its job run here through `execute_here(steps)` (that machine is then not
    credited). A step that FAILED on a machine is run here again: the dev box is the reference
    environment, and a remote failure is often a package version or service the machine lacks; the
    local result stands, and the machine is not credited. `hosts` lists the machines whose steps all
    ran and passed there."""
    results, hosts = [], []
    for job in jobs:
        job.join()
        if job.status == "infra":
            out(f"[*] --remote: {job.host} could not run its steps ({'; '.join(job.problems)[:300]}): running them here")
            wanted = set(job.names)
            results += execute_here([s for s in all_steps if s.name in wanted])
            continue
        failed_names = {name for name in job.names if job.status == "failed" and f"step failed: {name}" in job.problems}
        for name in job.names:
            if name not in failed_names:
                results.append(result_class(name, 0, "", job.seconds.get(name, 0.0)))
        if failed_names:
            out(f"[*] --remote: {job.host} reported failing steps ({', '.join(sorted(failed_names))}): "
                "running them here to confirm (the dev box is the reference environment)")
            confirmed = execute_here([s for s in all_steps if s.name in failed_names])
            results += confirmed
            if on_unfit:
                on_unfit(job.host, [r.name for r in confirmed if r.returncode == 0])
        else:
            hosts.append(job.host)
    return results, hosts


def repo_roles(repo_root, sibling_dir, shared_dir):
    """role -> path for the three trees a run reads, and the role of the invocation repository."""
    is_open = os.path.exists(os.path.join(repo_root, "zero_sudo", "__manifest__.py"))
    roots = {"hams_open": repo_root, "hams_com": sibling_dir} if is_open else {"hams_com": repo_root, "hams_open": sibling_dir}
    top = subprocess.run(["git", "-C", shared_dir, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    roots["hams_shared"] = top.stdout.strip() if top.returncode == 0 else None
    return roots, ("hams_open" if is_open else "hams_com")


def dispatch(mode, steps, pending, durations, step_dicts, roots, role, local_cores, local_load,
             where=False, inventory=None, transports=None, probes=None, out=print, whole=False, claim=None, claimed=None,
             unfit=None):
    """Split `steps` between this machine and the others.

    Returns (local_steps, jobs). With `where` it prints the plan and returns ([], []). Steps that are
    cached passes, have no command, or still name a path of this machine stay here.

    `whole`: this machine's run lock is busy (lint_lock.local_busy), so ship the whole run to an idle
    machine (make_whole_plan). `claim(host)` returns a handle that keeps another run on this machine from
    choosing the same machine (lint_lock.try_claim; None when taken), `claimed(hosts)` the hosts already
    taken; a claimed machine is not used by a whole run."""
    inventory = inventory if inventory is not None else load_inventory()
    if not (roots.get("hams_open") and os.path.isdir(roots["hams_open"]) and roots.get("hams_shared")):
        out("[*] --remote: needs a hams_open checkout with its hams_shared; running everything here")
        return list(steps), []
    wanted = runners(inventory) if mode == "auto" else {mode: inventory[mode]} if mode in inventory and inventory[mode].get("ssh") else {}
    transports = transports if transports is not None else make_transports(inventory, wanted)
    probes = probes if probes is not None else probe_all(inventory, {n: t for n, t in transports.items() if n in wanted})
    pending_set = set(pending)
    movable = {}
    exported = {}
    for step in steps:
        if step.name not in pending_set or step.argv is None:
            continue
        item = export_step(dict(step_dicts[step.name], print_ok=step.print_ok, fail_header=step.fail_header,
                                missing_msg=step.missing_msg, lock=step.lock), roots)
        if item is None:
            continue
        movable[step.name] = step
        exported[step.name] = item
    names = [s.name for s in steps]
    ordered = [n for n in names if n in movable] + [n for n in names if n not in movable]
    static = {n for n in names if n not in movable}
    whole = whole and mode == "auto"
    excluded = set(claimed(list(wanted))) if (whole and claimed) else set()
    handles = {}
    for _attempt in range(len(wanted) + 1):
        plan = make_plan(ordered, static, durations, inventory, probes, mode, local_cores, local_load,
                         whole=whole, exclude=excluded, unfit=unfit)
        if where or not claim:
            break
        handles = {}
        lost = set()
        for host in plan.remote:
            handle = claim(host)
            if handle is None and whole:
                lost.add(host)  # another run took it between the probe and now: plan again without it
            else:
                handles[host] = handle  # a spreading run goes on without the claim: it queues on the machine
        if not lost:
            break
        for handle in handles.values():
            if handle is not None:
                handle.close()
        handles = {}
        excluded |= lost
    if where:
        out(format_plan(plan, inventory, probes))
        return [], []
    for note in plan.notes:
        out(f"[*] --remote: {note}")
    if not plan.remote:
        return list(steps), []
    print_lock = threading.Lock()
    jobs = []
    for host, host_names in plan.remote.items():
        bundle = {"bundle_id": f"{os.getpid()}-{int(time.time())}-{host}", "steps": [exported[n] for n in host_names]}
        jobs.append(RemoteJob(host, inventory[host], transports[host], roots, role, bundle, out, print_lock,
                              claim=handles.get(host)).start())
    sent = {n for names_ in plan.remote.values() for n in names_}
    return [s for s in steps if s.name not in sent], jobs

# --- orchestration used by run_linters.py -------------------------------------------------------


def make_transports(inventory, names):
    return {name: SshTransport(name, inventory[name]) for name in names}


def probe_all(inventory, transports=None, workers=4):
    transports = transports or make_transports(inventory, runners(inventory))
    probes = {}
    lock = threading.Lock()

    def one(name):
        probe = probe_machine(name, transports[name])
        with lock:
            probes[name] = probe

    threads = [threading.Thread(target=one, args=(n,)) for n in transports]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return probes


PROVISION_SCRIPT = os.path.join(TOOLS, "provision_build_host.sh")
PROVISION_REQUIREMENTS = os.path.join(TOOLS, "requirements-test.txt")
PROVISION_DIR = ".cache/hams-lint/provision-src"
PROVISION_UNDO = f"bash ~/{PROVISION_DIR}/provision_build_host.sh undo"
TOOLCHAIN_FILES = ("hams_open/daemons/radioctl/rust-toolchain.toml", "hams_com/daemons/hams_local_relay/rust-toolchain.toml")


def rust_pin(roots=None):
    """The `channel` of the repositories' rust-toolchain.toml files (the same everywhere; the first found)."""
    roots = roots or [os.path.expanduser("~/workspace")]
    for root in roots:
        for rel in TOOLCHAIN_FILES:
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as handle:
                    match = re.search(r'^channel\s*=\s*"([^"]+)"', handle.read(), re.M)
            except OSError:
                continue
            if match:
                return match.group(1)
    return None


def provision_run(transport, mode, pin=None, timeout=3600, on_line=None):
    """Copy tools/provision_build_host.sh and tools/requirements-test.txt to the machine and run
    `mode` (plan|apply|verify|versions|undo). Returns (exit code, output text)."""
    with open(PROVISION_SCRIPT, "rb") as script, open(PROVISION_REQUIREMENTS, "rb") as reqs:
        files = {"provision_build_host.sh": script.read(), "requirements-test.txt": reqs.read()}
    for name, data in files.items():
        code, _out, err = transport.run(f"mkdir -p ~/{PROVISION_DIR} && cat > ~/{PROVISION_DIR}/{name}", data=data)
        if code != 0:
            return code, f"cannot copy {name}: {err.decode(errors='replace')[-200:]}"
    env = f"HAMS_RUST_TOOLCHAIN={shlex.quote(pin)} " if pin else ""
    command = f"{env}bash ~/{PROVISION_DIR}/provision_build_host.sh {shlex.quote(mode)} 2>&1"
    lines = []
    code = transport.stream(command, lambda line: (lines.append(line), on_line and on_line(line)), timeout=timeout)
    return code, "\n".join(lines)


def main(argv):
    """`lint_remote.py probe [HOST]` (read-only survey); `lint_remote.py provision HOST [--apply|--verify|--versions|--undo]`
    (default: print the plan; changes happen only with --apply or --undo)."""
    inv = load_inventory()
    command = argv[0] if argv else "probe"
    hosts = [a for a in argv[1:] if not a.startswith("--")]
    selected = {h: inv[h] for h in hosts} if hosts else runners(inv)
    transports = make_transports(inv, selected)
    if command == "provision":
        mode = "apply" if "--apply" in argv else "undo" if "--undo" in argv else "verify" if "--verify" in argv else "versions" if "--versions" in argv else "plan"
        status = 0
        for name in sorted(selected):
            if name not in runners(inv):
                print(f"{name}: not a lint runner")
                continue
            print(f"== {name}: provision {mode}")
            code, _text = provision_run(transports[name], mode, pin=rust_pin(), on_line=print)
            status = status or code
        return status
    probes = probe_all(inv, transports)
    for name, probe in sorted(probes.items()):
        print(name, "unreachable: " + probe.error if not probe.reachable else probe)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
