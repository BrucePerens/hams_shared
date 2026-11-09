#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Lint side of the coalescing batch queue (ADR 0115, docs/runbooks/LINT_TEST_BATCHING.md).

`run_linters.py --changed` calls `run_member` once it knows the run is heavy. The call joins the batch
for this repository (batch_queue.py); the leader builds one combined tree (origin/main plus every
member's branch), runs `run_linters.py --changed --solo` there once, attributes any failing step to the
branches whose changed files it names, and writes a pass stamp for every branch that passed (the same
stamp file prmerge.sh and lint_stamp_gate.py read for a single run). Every caller prints its own
verdict and exits with its own code.

Only a committed branch tip is batched. A dirty tree, `--only`/`--skip`/module names, `--solo`, the cheap
bypass, HAMS_LINT_NO_LOCK, a run started by another run, the main hams_shared checkout, and a hams_shared branch that
changes the batching tools themselves run alone as before.

A branch of hams_shared is batched like any other (2026-11-08): its queue is the one of the hams_shared git directory,
the combined tree is a worktree of hams_shared (origin/main plus every member's branch) named `SHARED_TREE_NAME`, and
the combined run is that tree's own `run_linters.py`, which builds the sibling layout for it (lint_layout.py).
"""

import os
import re
import subprocess
import sys
import time

import batch_queue
import lint_lock
import lock_queue

KIND = "lint"
# An anchor tag in a diff line. Written so that this file's own source is not itself read as an anchor.
ANCHOR_TAG = re.compile(r"\[@ANCHOR(?:-BEGIN|-END)?:\s*([A-Za-z0-9_:]+)\s*\]")
WORD = re.compile(r"[A-Za-z0-9_:]+")
RUNNER_ENV = "HAMS_LINT_BATCH_RUNNER"
# The directory name of a hams_shared combined tree. `run_linters._resolve_repo_root` treats a directory NAMED
# `hams_shared` as a link into hams_open and steps up to its parent, which would lint an empty directory.
SHARED_TREE_NAME = "hams_shared_wt"
# Tools whose change makes a hams_shared branch run alone: they ARE the batching, so the leader (which may be that
# branch) must not run a batch of other branches with them.
BATCHING_TOOLS = ("tools/batch_queue.py", "tools/lint_batch.py", "tools/lint_layout.py", "tools/lint_lock.py",
                  "tools/lock_queue.py", "tools/odoo_test_batch.py", "tools/run_linters.py", "tools/runner_slots.py")
MAX_OUTPUT_PER_STEP = 200000


# [@ANCHOR: lint_batch:ineligible_reason]
def ineligible_reason(changed, only, skip, modules, solo, bundle, environ, dirty, is_shared_checkout,
                      lock_needed, foreign_shared=False, batching_tools=()):
    """Why this run must NOT join a batch (a short sentence), or None when it may. The run is
    eligible only as a plain committed `--changed` run that would take the lint lock (a heavy run).
    `is_shared_checkout` is True for the main hams_shared checkout itself (a branch worktree of hams_shared is batched).
    `foreign_shared` is True when the tree's `hams_shared` is not the main checkout's (see `foreign_shared_checkout`).
    `batching_tools` lists the `BATCHING_TOOLS` the branch changed (see `batching_tools_changed`)."""
    if solo:
        return "--solo"
    if environ.get(RUNNER_ENV):
        return "this is the combined run of a batch"
    if environ.get("HAMS_LINT_NO_LOCK") == "1":
        return "HAMS_LINT_NO_LOCK=1"
    if environ.get("HAMS_LINT_LOCK_HELD"):
        return "started by another lint run"
    if environ.get("HAMS_LINT_BATCH") == "off":
        return "HAMS_LINT_BATCH=off"
    if changed is None:
        return "not a --changed run"
    if only or skip or modules:
        return "--only/--skip/module names select part of the steps"
    if bundle:
        return "the remote side of a bundle"
    if dirty:
        return "uncommitted changes in the tree (only committed tips are batched)"
    if is_shared_checkout:
        return "the main hams_shared checkout runs alone (only a branch worktree of it is batched)"
    if batching_tools:
        return "the branch changes the batching tools itself (%s)" % ", ".join(batching_tools)
    if foreign_shared:
        return "this tree's hams_shared is not the main checkout's (the combined tree would lint with the main one)"
    if not lock_needed:
        return "cheap or fully cached run: it takes no lock"
    return None


# [@ANCHOR: lint_batch:foreign_shared_checkout]
def foreign_shared_checkout(tree_root, main_root):
    """True when `tree_root`'s `hams_shared` resolves to a different directory than `main_root`'s.

    The combined tree of a batch is a fresh worktree of the repository whose `hams_shared` is linked to the
    MAIN checkout's (fix_worktree_symlinks.py), so it never carries what a branch linked by hand (a hams_shared
    worktree with a change under test, or a submodule checked out inside the worktree): the batch linted and
    passed the branch with main's tools and wrote its pass stamp (reproduced 2026-11-06 with a deliberately
    failing checker: batched PASSED, `--solo` FAILED). Such a tree runs alone. A missing `hams_shared` on
    either side is not foreign (nothing to compare)."""
    own = os.path.join(tree_root, "hams_shared")
    main = os.path.join(main_root, "hams_shared")
    if not os.path.exists(own) or not os.path.exists(main):
        return False
    return os.path.realpath(own) != os.path.realpath(main)


# [@ANCHOR: lint_batch:changed_paths]
def changed_paths(repo, base, tip):
    """Repository-relative paths the branch changed against `base` (``git diff --ignore-submodules=none --name-only base...tip``: a moved submodule pointer counts).
    Empty when git cannot say."""
    result = subprocess.run(["git", "-C", repo, "diff", "--ignore-submodules=none", "--name-only", "%s...%s" % (base, tip)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


# [@ANCHOR: lint_batch:batching_tools_changed]
def batching_tools_changed(paths):
    """The entries of `BATCHING_TOOLS` that `paths` (repository-relative, as `changed_paths` returns them) contain, in
    the order of `BATCHING_TOOLS`. A hams_shared branch with any of them runs alone (`ineligible_reason`)."""
    changed = set(paths)
    return [tool for tool in BATCHING_TOOLS if tool in changed]


# [@ANCHOR: lint_batch:path_finder]
def path_finder(paths):
    """Return `find_tokens(line)` for `blame_by_tokens`: the entries of `paths` that a line of linter
    output names, as whole path suffixes (the character before must not be part of a file name, so
    `a.py` is not found in `data.py`)."""
    ordered = sorted(set(paths), key=len, reverse=True)
    boundary = re.compile(r"[\w.\-]")

    def find(line):
        hits = []
        for path in ordered:
            at = line.find(path)
            while at != -1:
                if at == 0 or not boundary.match(line[at - 1]):
                    hits.append(path)
                    break
                at = line.find(path, at + 1)
        return hits

    return find


# [@ANCHOR: lint_batch:changed_anchors]
def changed_anchors(repo, base, tip):
    """Anchor ids on lines the branch added or removed against `base` (``git diff -U0 base...tip``): the set of
    ids the branch defines, cites or deletes. Empty when git cannot say.

    The anchor checks (`verify_anchors`, `check_function_test_anchors`) name anchors, not files, so a failure
    there names no changed path; the ids found here let `anchor_finder` attribute it."""
    result = subprocess.run(["git", "-C", repo, "diff", "-U0", "--no-color", "%s...%s" % (base, tip)],
                            capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        return set()
    ids = set()
    in_header = True
    for line in result.stdout.splitlines():
        if line.startswith("diff --git "):
            in_header = True
        elif line.startswith("@@"):
            in_header = False
        elif not in_header and line[:1] in ("+", "-"):
            ids.update(ANCHOR_TAG.findall(line))
    return ids


# [@ANCHOR: lint_batch:anchor_finder]
def anchor_finder(ids):
    """Return `find_tokens(line)` for `blame_by_tokens`: the entries of `ids` (anchor ids) that a line of linter
    output names as a whole word. A word is a run of letters, digits, `_` and `:`; quotes, brackets and
    punctuation around it, and a trailing `:`, are ignored (`'m:feature'` and `m:feature:` both find `m:feature`)."""
    known = set(ids)

    def find(line):
        hits = []
        for word in WORD.findall(line):
            word = word.strip(":")
            if word in known and word not in hits:
                hits.append(word)
        return hits

    return find


# [@ANCHOR: lint_batch:owners_of]
def owners_of(members_files):
    """{path: [tips that changed it]} from {tip: [paths]}."""
    owners = {}
    for tip, paths in members_files.items():
        for path in paths:
            owners.setdefault(path, []).append(tip)
    return owners


# [@ANCHOR: lint_batch:default_command]
def default_command(tree, results_json, passthrough, base):
    """argv of the combined run: ``nice -n 10 python3 <tree>/tools/run_linters.py --changed BASE --solo
    --results-json PATH`` plus `passthrough` options (jobs, remote, timeouts) of the leader's own command."""
    return ["nice", "-n", "10", sys.executable or "python3", os.path.join(tree, "tools", "run_linters.py"),
            "--changed", base, "--solo", "--results-json", results_json, *passthrough]


# [@ANCHOR: lint_batch:read_results]
def read_results(path):
    """(selected step names, {failed step: output}) from the combined run's `--results-json` file, or
    ([], {}) when it is missing or unreadable."""
    import json
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return [], {}
    selected = data.get("selected") or [s.get("name") for s in data.get("steps", [])]
    failed = {s["name"]: (data.get("outputs") or {}).get(s["name"], "")
              for s in data.get("steps", []) if s.get("returncode") not in (0, None)}
    return [n for n in selected if n], failed


# [@ANCHOR: lint_batch:read_known_red]
def read_known_red(path):
    """The `known_red` stamp field (` known_red=... baseline=...`, see `lint_baseline.stamp_field`) the combined run wrote into
    its `--results-json` file: the known-red failures it tolerated. "" when there are none or the file is unreadable."""
    import json
    try:
        with open(path, encoding="utf-8") as handle:
            return str(json.load(handle).get("known_red") or "")
    except (OSError, ValueError, AttributeError):
        return ""


# [@ANCHOR: lint_batch:LintBatch]
class LintBatch:
    """The leader's side: everything needed to run one batch of branches of one repository.

    Fields: `repo` (a checkout or worktree of the repository, used for git commands), `repo_name`
    (directory name of the tree, e.g. hams_com), `base` (ref the trees start from), `passthrough`
    (options copied to the combined run), `stamp_writer(tip, step_names, batch_id, with_tips)` (writes the pass
    stamp of one tip; `with_tips`: the other tips of the run it passed in), `command(tree, results_json)` (argv; tests replace it with a fake linter),
    `prepare(tree)` (links the tree for linting; default: run fix_worktree_symlinks.py when present),
    `out` (print function), `environ`. `__call__(batch_id, members, log_path)` is the `run_batch`
    callable of `batch_queue.coalesce`.

    Subclasses (odoo_test_batch.OdooTestBatch) change the kind of run through `kind`, `collect` (what a run
    reports) and `tokens` (who owns which finding); `current_members` is the group being run, for `command`."""

    kind = KIND
    runner_env = RUNNER_ENV

    def __init__(self, repo, repo_name, base, passthrough, stamp_writer, command=None, prepare=None,
                 out=print, environ=None, fetch=True):
        self.repo = repo
        self.repo_name = repo_name
        self.base = base
        self.passthrough = list(passthrough)
        self.stamp_writer = stamp_writer
        self.command = command or (lambda tree, results: default_command(tree, results, self.passthrough, self.base))
        self.prepare = prepare or self._prepare_default
        self.out = out
        self.environ = dict(os.environ if environ is None else environ)
        self.fetch = fetch
        self.last_selected = []
        self.last_known_red = ""
        self.current_members = []
        self._run_no = 0
        self._log = None

    def _prepare_default(self, tree):
        fixer = os.path.join(tree, "fix_worktree_symlinks.py")
        if os.path.isfile(fixer):
            subprocess.run([sys.executable or "python3", fixer], cwd=tree, capture_output=True, text=True)

    def _emit(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()
        if self._log:
            self._log.write(text)
            self._log.flush()

    def _run_once(self, batch_id, group):
        self._run_no += 1
        parent = batch_queue.tree_parent(self.kind, self.repo_name, suffix="_b%d_r%d" % (batch_id, self._run_no),
                                         environ=self.environ)
        dest = os.path.join(parent, self.repo_name)
        results = os.path.join(parent, "results.json")
        try:
            built = batch_queue.build_combined_tree(self.repo, self.base, group, dest)
            if built["head"] is None:
                return {"exit_code": 75, "fatal": True, "failures": {}, "conflicted": [],
                        "detail": "could not create the combined tree: %s" % built.get("error", "")}
            if not built["merged"]:
                return {"exit_code": 0, "failures": {}, "conflicted": built["conflicted"]}
            self.prepare(dest)
            self.current_members = built["merged"]
            self._emit("\n===== combined run: %d branch(es): %s =====\n" % (
                len(built["merged"]), ", ".join(m["branch"] for m in built["merged"])))
            env = dict(self.environ, **{self.runner_env: str(os.getpid())})
            env.pop("HAMS_LINT_LOCK_HELD", None)
            proc = subprocess.Popen(self.command(dest, results), cwd=dest, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace")
            captured = []
            try:
                for line in proc.stdout:
                    captured.append(line)
                    self._emit(line)
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            selected, failed = self.collect(dest, results, "".join(captured), proc.returncode)
            self.last_selected = selected or self.last_selected
            self.last_known_red = read_known_red(results) if proc.returncode == 0 else ""
            code = proc.returncode
            if code == lock_queue.EXIT_WAIT_TIMEOUT:
                return {"exit_code": code, "fatal": True, "failures": {}, "conflicted": built["conflicted"],
                        "detail": "the combined run gave up waiting for the lint lock or the load gate (exit 75)"}
            if code != 0 and not failed:
                failed = {"run_linters": "".join(captured)[-MAX_OUTPUT_PER_STEP:]}
            return {"exit_code": code, "failures": failed, "conflicted": built["conflicted"], "selected": selected}
        finally:
            batch_queue.remove_tree(self.repo, dest)

    def collect(self, tree, results, output, code):
        """(selected step names, {failed step: output}) of the run that just ended. Lint: the results file."""
        return read_results(results)

    def tokens(self, members):
        """(owners {token: [tips]}, find_tokens(line)) for attribution. Lint: changed files by path, and anchor ids on changed lines."""
        files = {}
        for member in members:
            if member["tip"] not in files:
                files[member["tip"]] = changed_paths(self.repo, self.base, member["tip"])
        owners = owners_of(files)
        anchors = owners_of({tip: sorted(changed_anchors(self.repo, self.base, tip)) for tip in files})
        # Some checkers name a file by its base name and its directory separately (`File 'x.py' in '<dir>'`):
        # a base name that only one changed path has is a token of its own.
        names = {}
        for path in owners:
            names.setdefault(os.path.basename(path), []).append(path)
        for name, paths in names.items():
            if len(paths) == 1 and len(name) >= 6 and name not in owners:
                owners[name] = owners[paths[0]]
        path_find, anchor_find = path_finder(sorted(owners)), anchor_finder(anchors)
        owners.update({anchor: tips for anchor, tips in anchors.items() if anchor not in owners})

        def find(line):
            return path_find(line) + anchor_find(line)

        return owners, find

    def __call__(self, batch_id, members, log_path):
        batch_queue.reap_orphan_trees(self.repo, self.kind, self.repo_name, environ=self.environ)
        if self.fetch:
            subprocess.run(["git", "-C", self.repo, "fetch", "-q", "origin"], capture_output=True, text=True)
        owners, finder = self.tokens(members)

        def attribute(failures, live):
            live_tips = {m["tip"] for m in live}
            scoped = {path: [t for t in tips if t in live_tips] for path, tips in owners.items()}
            return batch_queue.blame_by_tokens(failures, scoped, finder)

        self._log = None
        if log_path:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            self._log = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - closed below
        try:
            verdicts = batch_queue.settle(members, lambda group: self._run_once(batch_id, group), attribute,
                                          log=lambda text: self._emit(text + "\n"))
        finally:
            if self._log:
                self._log.close()
                self._log = None
        for member in members:
            verdict = verdicts.get(member["tip"], {})
            verdict["log"] = log_path
            verdict["batch"] = batch_id
            if verdict.get("status") == batch_queue.PASSED:
                # The combined run's own record of the known-red failures it tolerated (ADR 0117) goes into the stamp.
                extra = {"known_red": self.last_known_red} if self.last_known_red else {}
                self.stamp_writer(member["tip"], self.last_selected, batch_id, verdict.get("with") or [], **extra)
        return {"verdicts": verdicts, "log": log_path}


# [@ANCHOR: lint_batch:batches_that_may_run]
def batches_that_may_run(environ=None):
    """How many lint batches of one repository may run at once: this machine's lint slot count (`lint_lock.slot_count`; the dev
    box 4, a small machine 1), because each batch's combined run takes a lint slot like any run. None when `HAMS_BATCH_MAX_RUNNING`
    is set, so the explicit number (`batch_queue.caps_from_env`) wins. Passed as `run_member(max_running=...)` by `run_linters.main`.
    A request that arrives while fewer batches than that run starts a batch of its own at once instead of waiting for the running
    one; when every slot has a batch it joins the forming batch. Reads `lint_machines.json`; takes no lock."""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_BATCH_MAX_RUNNING"):
        return None
    return lint_lock.slot_count(environ)


# [@ANCHOR: lint_batch:batch_limit]
def batch_limit(environ=None, headroom=None, ttl=5.0, now=time.monotonic):
    """The `max_running` of a lint batch queue: None when `HAMS_BATCH_MAX_RUNNING` is set (that number wins), the base slot count
    (`batches_that_may_run`) on a machine with no burst slots, else a function that returns the base count plus the burst slots while the machine has
    spare processors (`lint_lock.burst_headroom`, measured at most once per `ttl` seconds) and the base count otherwise. So a request that arrives
    when every base slot has a batch starts a batch of its own at once if the processors are mostly idle, and joins the forming batch (the
    coalescing that saves work under load) when they are not. `headroom` replaces the measurement (tests). Full entry: tools/README.md."""
    environ = os.environ if environ is None else environ
    base = batches_that_may_run(environ)
    if base is None:
        return None
    burst = lint_lock.burst_slot_count(environ)
    if not burst:
        return base
    memo = {"at": None, "ok": False}

    def limit():
        stamp = now()
        if memo["at"] is None or stamp - memo["at"] >= ttl:
            if headroom is not None:
                memo["ok"] = bool(headroom())
            else:
                busy_limit, psi_limit, min_mem, _spacing = lint_lock.burst_limits(environ)
                memo["ok"] = lint_lock.burst_headroom(lint_lock.cpu_busy_percent(0.5), lint_lock.cpu_stall(), lint_lock.mem_available_gb(),
                                                      busy_limit, psi_limit, min_mem)[0]
            memo["at"] = stamp
        return base + burst if memo["ok"] else base

    return limit


# [@ANCHOR: lint_batch:run_member]
def run_member(repo, repo_name, branch, tip, base, scope, runner, timeout=None, out=print, environ=None,
               poll=1.0, sdir=None, estimate=None, kind=KIND, solo_hint="run_linters.py --changed --solo", install=(),
               max_running=None):
    """The caller's side. Joins the batch for `repo` and blocks until this branch's verdict is known.

    Returns an exit code (0 passed, 1 failed, 75 gave up waiting) after printing the verdict, or None
    when the caller must run alone as before (a merge conflict in the combined tree, or a batch runner
    that kept dying). `runner` is a `LintBatch` (used only if this process becomes the leader). `max_running`: how many
    batches of this kind may run at once (default: HAMS_BATCH_MAX_RUNNING, else 1); the test queue passes the machine's
    test slot count."""
    environ = os.environ if environ is None else environ
    sdir = sdir or batch_queue.state_dir(kind, batch_queue.repo_key(repo), environ)
    member = batch_queue.make_member(branch, tip, scope=scope, info=solo_hint, install=install)
    caps = batch_queue.caps_from_env(environ)
    if max_running:
        caps["max_running"] = max_running if callable(max_running) else max(1, int(max_running))
    outcome = batch_queue.coalesce(
        sdir, member, runner, caps=caps, estimate=estimate, poll=poll,
        timeout=timeout, out=out, log_for=lambda n: os.path.join(sdir, "logs", "batch-%d.log" % n))
    status = outcome["status"]
    if status == batch_queue.TIMEOUT:
        out("ERROR: gave up waiting for a %s batch after %g s." % (kind, timeout))
        return lock_queue.EXIT_WAIT_TIMEOUT
    if status == batch_queue.GAVE_UP:
        out("[*] the batch runner kept dying; running this branch alone")
        return None
    if status == batch_queue.SOLO:
        out("[*] this branch conflicts with another branch of the batch: running it alone")
        return None
    doc, verdict = outcome["doc"], outcome["verdict"]
    for line in batch_queue.format_verdicts(doc, tip):
        out(line)
    if status == batch_queue.PASSED:
        out("[*] %s batch %d: %s PASSED (checked together with %d other branch%s; %s written)" % (
            kind, outcome["batch"], branch, len(outcome["others"]), "" if len(outcome["others"]) == 1 else "es",
            "pass stamp" if kind == KIND else "pass record"))
        if verdict.get("note"):
            out("    note: " + verdict["note"])
        return 0
    for line in verdict.get("findings", [])[:40]:
        out("   " + line)
    out("\n[!] Halting: this branch failed in the combined %s run. Full log: %s" % (kind, verdict.get("log") or doc.get("log")))
    if outcome["others"]:
        out("    (found in a combined run of %d branches; `%s` repeats it alone)"
            % (len(outcome["others"]) + 1, solo_hint))
    return verdict.get("exit_code") or 1
