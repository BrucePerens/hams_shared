#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Differential harness: a Python checker against its `hams_lint` (Rust engine) twin.

The PARITY RULE: a check moves from its Python checker to the engine only when both produce the same
result on the whole tree, on the rule file's own examples and on injected violations. "The same" means
byte-identical stdout and stderr and the same exit status, so pass stamps, logs and the lint cache see
no difference. The Python checker stays in the tree as the reference until the check is retired here.

    lint_engine_parity.py tree     [--check NAME]... ROOT...
        Each check, Python vs `hams_lint check`, with each ROOT alone and (for a check that takes
        several roots) with all of them.
    lint_engine_parity.py examples [--check NAME]...
        Each `[[check.example]]` of the rule file, written to a fresh directory: both must agree, and
        both must report exactly the declared line numbers.
    lint_engine_parity.py inject   --workdir DIR [--check NAME]... [--per-check N] [--seed S] ROOT...
        Clones each ROOT into DIR (`git clone --shared`: objects borrowed read-only, files checked out
        fresh; populated submodules cloned the same way), appends the check's `inject` lines to N files
        the engine selects and N files it does not, and compares the two on the clones. At least half of
        the injected files the engine selects must be reported by the Python checker (else the injection
        proves too little and the run fails; the rest are exempt by the check's own rules, e.g. a file
        with a real listener). Nothing outside DIR is ever written.
    lint_engine_parity.py bench    [--check NAME]... [--repeat R] ROOT...
        Median wall time of each Python checker and of its drop-in twin per ROOT, and of one engine
        `scan` of all selected checks over all ROOTs (each file read once), cold and with a warm cache.
    lint_engine_parity.py all      --workdir DIR ROOT...      tree + examples + inject

Common options: `--rules FILE` (another rule file: the engine reads it, and the harness takes checks and
examples from it). Exit status: 0 all agree, 1 a difference (printed as a unified diff), 2 could not
compare (no engine binary, a checker that does not exist, a clone that failed).
"""

import argparse
import difflib
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib

import hams_lint_bin

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(TOOLS_DIR, "hams_lint", "rules", "checks.toml")
EXIT_DIFF = 1
EXIT_NOT_COMPARED = 2
MULTI_ROOT_KINDS = ("multi", "multi_shared_parent")


# [@ANCHOR: lint_engine_parity:lint_engine_parity_not_compared_class]
class NotCompared(Exception):
    """The comparison could not be made (exit 2)."""


def load_checks(rules_path, names=()):
    """The `[[check]]` tables of the rule file, in file order; only `names` when given."""
    with open(rules_path, "rb") as handle:
        checks = tomllib.load(handle).get("check", [])
    if names:
        known = {c["name"] for c in checks}
        missing = [n for n in names if n not in known]
        if missing:
            raise NotCompared(f"no check named {', '.join(missing)} in {rules_path}")
        checks = [c for c in checks if c["name"] in names]
    return checks


# [@ANCHOR: lint_engine_parity:lint_engine_parity_runner_class]
class Runner:
    """Runs one check both ways with the same arguments, environment and working directory."""

    def __init__(self, binary, rules_path, environ=None):
        self.binary = binary
        self.rules_path = rules_path
        self.env = dict(os.environ if environ is None else environ)

    def legacy(self, check, args):
        script = os.path.join(TOOLS_DIR, check["legacy"])
        if not os.path.exists(script):
            raise NotCompared(f"{check['name']}: Python checker {script} does not exist")
        return self._run([sys.executable, script] + list(args))

    def engine(self, check, args):
        return self._run([self.binary, "--rules", self.rules_path, "check", check["name"]] + list(args))

    def _run(self, command):
        started = time.monotonic()
        done = subprocess.run(command, capture_output=True, env=self.env)
        return done.returncode, done.stdout, done.stderr, time.monotonic() - started


def diff_text(label, old, new):
    """A unified diff of two outputs (bytes), or '' when equal."""
    if old == new:
        return ""
    lines = difflib.unified_diff(
        old.decode("utf-8", "replace").splitlines(),
        new.decode("utf-8", "replace").splitlines(),
        f"python {label}",
        f"engine {label}",
        lineterm="",
        n=1,
    )
    return "\n".join(list(lines)[:80])


def compare(runner, check, args, label, out):
    """Runs both; prints the verdict line and any difference. Returns (equal, python stdout)."""
    py_code, py_out, py_err, py_s = runner.legacy(check, args)
    rs_code, rs_out, rs_err, rs_s = runner.engine(check, args)
    problems = []
    if py_code != rs_code:
        problems.append(f"exit status python {py_code}, engine {rs_code}")
    for stream, old, new in (("stdout", py_out, rs_out), ("stderr", py_err, rs_err)):
        text = diff_text(f"{stream} {label}", old, new)
        if text:
            problems.append(text)
    verdict = "SAME" if not problems else "DIFF"
    shown = len(py_out.splitlines())
    out(f"{verdict} {check['name']} [{label}] exit {py_code}, {shown} output lines, python {py_s:.2f}s, engine {rs_s:.2f}s")
    for problem in problems:
        out(problem)
    return not problems, py_out


def root_sets(check, roots):
    """Argument lists to compare a check with: each root alone, then all together when it takes several."""
    sets = [[r] for r in roots]
    if check["roots"] in MULTI_ROOT_KINDS and len(roots) > 1:
        sets.append(list(roots))
    return sets


def cmd_tree(runner, checks, roots, out=print):
    equal = True
    for check in checks:
        for args in root_sets(check, roots):
            label = " ".join(os.path.basename(a.rstrip(os.sep)) for a in args)
            equal &= compare(runner, check, args, label, out)[0]
    return equal


def cmd_examples(runner, checks, out=print):
    """Each example in its own fresh directory; both sides must report exactly its declared lines."""
    equal = True
    ran = 0
    for check in checks:
        for number, example in enumerate(check.get("example", [])):
            with tempfile.TemporaryDirectory(prefix="hams_lint_parity_") as work:
                path = os.path.join(work, example["path"])
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(example["content"])
                same, py_out = compare(runner, check, [work], f"example {number} {example['path']}", out)
                ran += 1
                reported = reported_lines(py_out.decode("utf-8", "replace"), example["path"])
                if reported != example["lines"]:
                    same = False
                    out(f"DIFF {check['name']} example {number}: declared lines {example['lines']}, python reported {reported}")
                equal &= same
    if ran == 0:
        raise NotCompared("the rule file has no examples")
    return equal


def reported_lines(text, rel):
    """Line numbers a checker's output reports for `rel` (`rel:N ` and `rel:N:` forms)."""
    lines = []
    for line in text.splitlines():
        at = line.find(rel + ":")
        if at < 0:
            continue
        rest = line[at + len(rel) + 1 :]
        digits = ""
        for char in rest:
            if not char.isdigit():
                break
            digits += char
        if digits:
            lines.append(int(digits))
    return lines


def _git(args, cwd, check=True):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=check)


def submodules(repo):
    """[(name, path, recorded sha)] of the populated submodules of `repo`."""
    stage = _git(["ls-files", "-z", "--stage"], repo).stdout.split("\0")
    recorded = {}
    for entry in stage:
        meta, _, path = entry.partition("\t")
        if meta.startswith("160000 "):
            recorded[path] = meta.split()[1]
    names = {}
    if os.path.exists(os.path.join(repo, ".gitmodules")):
        done = _git(["config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"], repo, check=False)
        for line in done.stdout.splitlines():
            key, _, path = line.partition(" ")
            names[path] = key[len("submodule.") : -len(".path")]
    found = []
    for path, sha in sorted(recorded.items()):
        if path in names and os.path.exists(os.path.join(repo, path, ".git")):
            found.append((names[path], path, sha))
    return found


def clone_tree(root, dest):
    """A throwaway clone of `root`'s HEAD (and its populated submodules at their recorded commits) at `dest`."""
    head = _git(["rev-parse", "HEAD"], root).stdout.strip()
    _git(["clone", "--quiet", "--shared", "--no-checkout", root, dest], os.path.dirname(dest))
    _git(["checkout", "--quiet", head], dest)
    for name, path, sha in submodules(root):
        sub_dest = os.path.join(dest, path)
        if os.path.isdir(sub_dest):
            os.rmdir(sub_dest)
        source = os.path.join(root, path)
        _git(["clone", "--quiet", "--shared", "--no-checkout", source, sub_dest], dest)
        if _git(["checkout", "--quiet", sha], sub_dest, check=False).returncode != 0:
            _git(["checkout", "--quiet", _git(["rev-parse", "HEAD"], source).stdout.strip()], sub_dest)
        _git(["config", f"submodule.{name}.url", source], dest)
        _git(["config", f"submodule.{name}.active", "true"], dest)
    return dest


def inside(path, work):
    """True when `path`, symlinks followed, is a file inside the throwaway directory `work`."""
    real_work = os.path.realpath(work)
    return os.path.realpath(path).startswith(real_work + os.sep)


def inject_file(path, lines, work):
    """Appends `lines` to the file at `path` (on a new line). Refuses anything outside `work`."""
    if not inside(path, work) or not os.path.isfile(path):
        return False
    with open(path, "rb") as handle:
        data = handle.read()
    prefix = b"" if not data or data.endswith((b"\n", b"\r")) else b"\n"
    payload = prefix + "".join(line + "\n" for line in lines).encode("utf-8")
    real = os.path.realpath(path)
    os.unlink(real)  # a fresh file: never write through a hard link
    with open(real, "wb") as handle:
        handle.write(data + payload)
    return True


def engine_files(binary, rules_path, check, root):
    done = subprocess.run(
        [binary, "--rules", rules_path, "files", check["name"], root], capture_output=True, text=True, check=True
    )
    return done.stdout.splitlines()


def tracked_files(root):
    return [p for p in _git(["ls-files", "-z", "--recurse-submodules"], root).stdout.split("\0") if p]


def cmd_inject(runner, checks, roots, workdir, per_check, seed, out=print):
    """Clones, injects, compares. Every check gets fresh clones (injections of one never meet another)."""
    equal = True
    rng = random.Random(seed)
    for check in checks:
        if not check.get("inject"):
            raise NotCompared(f"{check['name']}: no `inject` lines in the rule file")
        work = tempfile.mkdtemp(prefix=f"inject_{check['name']}_", dir=workdir)
        try:
            clones = [clone_tree(r, os.path.join(work, os.path.basename(r.rstrip(os.sep)))) for r in roots]
            expected = {}
            for clone in clones:
                selected = engine_files(runner.binary, runner.rules_path, check, clone)
                unselected = sorted(set(tracked_files(clone)) - set(selected))
                picks = rng.sample(selected, min(per_check, len(selected)))
                others = rng.sample(unselected, min(per_check, len(unselected)))
                injected = [rel for rel in picks if inject_file(os.path.join(clone, rel), check["inject"], work)]
                injected_others = [rel for rel in others if inject_file(os.path.join(clone, rel), check["inject"], work)]
                expected[clone] = injected
                out(f"[*] {check['name']} {os.path.basename(clone)}: injected {len(injected)} selected and "
                    f"{len(injected_others)} unselected files")
            for args in root_sets(check, clones):
                label = "injected " + " ".join(os.path.basename(a) for a in args)
                same, py_out = compare(runner, check, args, label, out)
                equal &= same
                text = py_out.decode("utf-8", "replace")
                for clone in args:
                    missed = [rel for rel in expected[clone] if rel + ":" not in text]
                    reported = len(expected[clone]) - len(missed)
                    note = f"{check['name']} [{label}] {os.path.basename(clone)}: python reported {reported} of {len(expected[clone])} injected files"
                    if missed:
                        note += f"; exempt by the check's own rules (a real listener, a decode error, a here-document): {missed[:3]}"
                    if expected[clone] and reported * 2 < len(expected[clone]):
                        equal = False
                        out(f"DIFF {note} -- under half: the injection proves too little")
                    else:
                        out(f"[*] {note}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return equal


def cmd_bench(runner, checks, roots, repeat, out=print):
    rows = []
    as_linted = 0.0  # what the lint run spends: single-root checkers once per root, the others once with all roots
    for check in checks:
        multi = check["roots"] in MULTI_ROOT_KINDS
        for args in root_sets(check, roots):
            py = statistics.median(runner.legacy(check, args)[3] for _ in range(repeat))
            rs = statistics.median(runner.engine(check, args)[3] for _ in range(repeat))
            label = " ".join(os.path.basename(a.rstrip(os.sep)) for a in args)
            rows.append((check["name"], label, py, rs))
            if (len(args) == len(roots)) if multi else True:
                as_linted += py
    for name, label, py_s, rs_s in rows:
        out(f"{name:36} {label:20} python {py_s:6.2f}s  engine {rs_s:6.3f}s  x{py_s / max(rs_s, 1e-6):5.1f}")
    total_py = as_linted
    names = []
    for check in checks:
        names += ["--check", check["name"]]
    with tempfile.TemporaryDirectory(prefix="hams_lint_bench_cache_") as cache:
        scan = [runner.binary, "--rules", runner.rules_path, "scan", "--cache-dir", cache] + names + list(roots)
        cold = [runner._run(scan + ["--no-cache"])[3] for _ in range(repeat)]
        runner._run(scan)
        warm = [runner._run(scan)[3] for _ in range(repeat)]
    out(f"one engine scan of {len(checks)} checks over {len(roots)} roots: cold {statistics.median(cold):.3f}s, "
        f"warm cache {statistics.median(warm):.3f}s; the Python checkers, one root at a time: {total_py:.2f}s")
    return True


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("tree", "examples", "inject", "bench", "all"))
    parser.add_argument("roots", nargs="*")
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--rules", default=DEFAULT_RULES)
    parser.add_argument("--workdir")
    parser.add_argument("--per-check", type=int, default=40)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)
    if args.mode in ("tree", "inject", "bench", "all") and not args.roots:
        parser.error(f"{args.mode} needs at least one ROOT")
    if args.mode in ("inject", "all") and not args.workdir:
        parser.error(f"{args.mode} needs --workdir (a scratch directory outside the repositories)")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        binary = hams_lint_bin.ensure_built()
        rules_path = os.path.abspath(args.rules)
        checks = load_checks(rules_path, args.check)
        runner = Runner(binary, rules_path)
        roots = [os.path.abspath(r) for r in args.roots]
        equal = True
        if args.mode in ("tree", "all"):
            equal &= cmd_tree(runner, checks, roots)
        if args.mode in ("examples", "all"):
            equal &= cmd_examples(runner, checks)
        if args.mode in ("inject", "all"):
            os.makedirs(args.workdir, exist_ok=True)
            equal &= cmd_inject(runner, checks, roots, os.path.abspath(args.workdir), args.per_check, args.seed)
        if args.mode == "bench":
            cmd_bench(runner, checks, roots, args.repeat)
    except (NotCompared, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"[!] lint_engine_parity: could not compare: {error}", file=sys.stderr)
        return EXIT_NOT_COMPARED
    print("PARITY: all compared runs agree" if equal else "PARITY FAILED: see the DIFF lines above")
    return 0 if equal else EXIT_DIFF


if __name__ == "__main__":
    sys.exit(main())
