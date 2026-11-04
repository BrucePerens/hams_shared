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
bypass, HAMS_LINT_NO_LOCK, a run started by another run, and a hams_shared checkout run alone as before.
"""

import os
import re
import subprocess
import sys

import batch_queue
import lock_queue

KIND = "lint"
RUNNER_ENV = "HAMS_LINT_BATCH_RUNNER"
MAX_OUTPUT_PER_STEP = 200000


# [@ANCHOR: lint_batch:ineligible_reason]
def ineligible_reason(changed, only, skip, modules, solo, bundle, environ, dirty, is_shared_checkout,
                      lock_needed):
    """Why this run must NOT join a batch (a short sentence), or None when it may. The run is
    eligible only as a plain committed `--changed` run that would take the lint lock (a heavy run)."""
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
        return "hams_shared runs alone (its tools are the linter)"
    if not lock_needed:
        return "cheap or fully cached run: it takes no lock"
    return None


# [@ANCHOR: lint_batch:changed_paths]
def changed_paths(repo, base, tip):
    """Repository-relative paths the branch changed against `base` (``git diff --name-only base...tip``).
    Empty when git cannot say."""
    result = subprocess.run(["git", "-C", repo, "diff", "--name-only", "%s...%s" % (base, tip)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


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


# [@ANCHOR: lint_batch:LintBatch]
class LintBatch:
    """The leader's side: everything needed to run one batch of branches of one repository.

    Fields: `repo` (a checkout or worktree of the repository, used for git commands), `repo_name`
    (directory name of the tree, e.g. hams_com), `base` (ref the trees start from), `passthrough`
    (options copied to the combined run), `stamp_writer(tip, step_names, batch_id)` (writes the pass
    stamp of one tip), `command(tree, results_json)` (argv; tests replace it with a fake linter),
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
        """(owners {token: [tips]}, find_tokens(line)) for attribution. Lint: changed files by path."""
        files = {}
        for member in members:
            if member["tip"] not in files:
                files[member["tip"]] = changed_paths(self.repo, self.base, member["tip"])
        return owners_of(files), path_finder(sorted({p for paths in files.values() for p in paths}))

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
                self.stamp_writer(member["tip"], self.last_selected, batch_id)
        return {"verdicts": verdicts, "log": log_path}


# [@ANCHOR: lint_batch:run_member]
def run_member(repo, repo_name, branch, tip, base, scope, runner, timeout=None, out=print, environ=None,
               poll=1.0, sdir=None, estimate=None, kind=KIND, solo_hint="run_linters.py --changed --solo"):
    """The caller's side. Joins the batch for `repo` and blocks until this branch's verdict is known.

    Returns an exit code (0 passed, 1 failed, 75 gave up waiting) after printing the verdict, or None
    when the caller must run alone as before (a merge conflict in the combined tree, or a batch runner
    that kept dying). `runner` is a `LintBatch` (used only if this process becomes the leader)."""
    environ = os.environ if environ is None else environ
    sdir = sdir or batch_queue.state_dir(kind, batch_queue.repo_key(repo), environ)
    member = batch_queue.make_member(branch, tip, scope=scope, info=solo_hint)
    outcome = batch_queue.coalesce(
        sdir, member, runner, caps=batch_queue.caps_from_env(environ), estimate=estimate, poll=poll,
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
        return 0
    for line in verdict.get("findings", [])[:40]:
        out("   " + line)
    out("\n[!] Halting: this branch failed in the combined %s run. Full log: %s" % (kind, verdict.get("log") or doc.get("log")))
    if outcome["others"]:
        out("    (found in a combined run of %d branches; `%s` repeats it alone)"
            % (len(outcome["others"]) + 1, solo_hint))
    return verdict.get("exit_code") or 1
