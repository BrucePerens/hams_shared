#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Pass-stamp gate for devbox_tools/prmerge.sh.

`run_linters.py` writes a stamp named for the commit it passed on. The stamp's one line says which
mode ran, how many steps it selected and their names:

    changed 2026-10-30T15:00:00 steps=31 names=check_cargo_clippy,flake8,...

A `--changed` run that selected no step wrote a stamp all the same, and so did one that never
selected the Rust or JavaScript checkers for a branch that changed Rust or JavaScript (found
2026-10-30, three merges). This gate reads the stamp and the branch's changed paths and refuses
those stamps. A full run always runs every step and is accepted.

    lint_stamp_gate.py STAMP_FILE < changed_paths     (one path per line)

Exit 0 and a one-line summary when the stamp covers the change, exit 1 and the reason otherwise.
"""

import os
import re
import subprocess
import sys
import tempfile
import atomic_io

RUST_SUFFIXES = (".rs", "Cargo.toml", "Cargo.lock")
JS_SUFFIXES = (".js", ".mjs", ".cjs")
# Steps a stamp must have selected when the branch changed a file of that kind.
RUST_STEPS = ("check_cargo_clippy", "check_cargo_fmt", "check_rust_function_test_anchors", "verify_anchors")
JS_STEPS = ("check_js_syntax",)
DOC_SUFFIXES = (".md",)
# The first word of a stamp `run_linters.py` wrote; an empty file (a write that failed after the file was created) is none of them.
KNOWN_MODES = ("full", "changed")


def parse_stamp(text):
    """(mode, step_count or None, set of step names or None) from a stamp line."""
    fields = text.split()
    mode = fields[0] if fields else ""
    count = None
    names = None
    for field in fields[1:]:
        match = re.fullmatch(r"steps=(\d+)", field)
        if match:
            count = int(match.group(1))
        if field.startswith("names="):
            names = set(filter(None, field[len("names="):].split(",")))
    return mode, count, names


# [@ANCHOR: lint_stamp_gate:refusal]
def refusal(stamp_text, changed_paths):
    """None when the stamp covers the change, otherwise the reason it does not."""
    mode, count, names = parse_stamp(stamp_text)
    paths = [path for path in changed_paths if path]
    code = [path for path in paths if not path.endswith(DOC_SUFFIXES)]
    if not code:
        return None
    if mode not in KNOWN_MODES:
        return "the stamp is empty or unreadable (its first word is %r, not one of %s)" % (mode, "/".join(KNOWN_MODES))
    if count == 0:
        # A full run that ran no step is as empty as a --changed one (a crashed or filtered run must not read as a pass).
        return "the stamp selected zero steps for a branch that changed code"
    if mode == "full":
        return None
    if names is None:
        needs_rust = any(path.endswith(RUST_SUFFIXES) for path in code)
        needs_js = any(path.endswith(JS_SUFFIXES) for path in code)
        if needs_rust or needs_js:
            return "the stamp does not list its steps (old run_linters.py): run it again"
        return None
    if any(path.endswith(RUST_SUFFIXES) for path in code):
        missing = [step for step in RUST_STEPS if step not in names]
        if missing:
            return "Rust files changed but the stamp did not run: " + ", ".join(missing)
    if any(path.endswith(JS_SUFFIXES) for path in code):
        missing = [step for step in JS_STEPS if step not in names]
        if missing:
            return "JavaScript files changed but the stamp did not run: " + ", ".join(missing)
    return None


# Paths a new commit may add or change without the pass stamp of its parent being lost (`inherit_stamp`). One list, here, beside the markdown-only
# exemption in prmerge.sh. Deliberately narrower than "any .md": verify_anchors, check_skill_integrity and the documentation checkers READ other
# markdown files, so a change to one of those forces a new run. These directories hold the records every branch adds (a history entry, a to-do
# claim or its notes, a claim file) and no step reads them except check_night_shift_history, which `inherit_stamp` runs itself.
EXEMPT_PREFIXES = ("night_shift_history/", "night_shift_todo/", "claims/")
INHERIT_MAX_COMMITS = 60


# [@ANCHOR: lint_stamp_gate:exempt_path]
def exempt_path(path):
    """True for a repository-relative path whose change cannot alter what the linters concluded: a markdown file under one of EXEMPT_PREFIXES.
    A path with `..`, a leading slash, or any other directory or suffix is not exempt. Pure. Full entry: tools/README.md."""
    return (path.endswith(".md") and path.startswith(EXEMPT_PREFIXES) and not path.startswith("/")
            and ".." not in path.split("/"))


# A moved submodule pointer (gitlink) is a code change. `submodule.<name>.ignore=all` in a clone's config (hams_open sets it) makes a plain `git diff`
# hide it, which read as "no files changed"; every diff here names `--ignore-submodules=none` so the setting cannot hide it.
def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


# [@ANCHOR: lint_stamp_gate:inherit_stamp]
def inherit_stamp(repo, tip, passed_dir, base="origin/main", log=print):
    """Give `tip` the pass stamp of the nearest commit below it that has one, when every file that differs between the two is `exempt_path`.
    Walks the commits of `base..tip` newest first (at most INHERIT_MAX_COMMITS, the tip itself excluded) and stops at the first one with a stamp
    file in `passed_dir`: if the files changed since then are all exempt (a non-empty or empty list: a rebuilt commit with the same tree also
    inherits) and `night_shift_history.check_tree` accepts the history of `tip`, writes `passed_dir/<tip>` holding the old line plus
    ` inherited-from=<old>` (and returns that text); otherwise returns None after logging why. Never replaces an existing stamp of `tip`. A
    stamp written by a run on `tip` itself always wins. The old stamp's step list still has to satisfy `refusal` for the whole branch diff,
    which the caller checks as for any stamp. Full entry: tools/README.md."""
    target = os.path.join(passed_dir, tip)
    if os.path.exists(target):
        with open(target, encoding="utf-8") as handle:
            return handle.read()
    listing = _git(repo, "rev-list", "--max-count=%d" % (INHERIT_MAX_COMMITS + 1), "%s..%s" % (base, tip))
    if listing.returncode != 0:
        log("stamp inheritance: cannot list %s..%s: %s" % (base, tip, listing.stderr.strip()))
        return None
    for old in listing.stdout.split():
        if old == tip:
            continue
        old_stamp = os.path.join(passed_dir, old)
        if not os.path.exists(old_stamp):
            continue
        diff = _git(repo, "diff", "--ignore-submodules=none", "--name-only", "--no-renames", old, tip)
        if diff.returncode != 0:
            return None
        changed = [line for line in diff.stdout.splitlines() if line]
        other = [path for path in changed if not exempt_path(path)]
        if other:
            log("stamp inheritance: %s changed since the stamped commit %s, not only records: %s" % (len(other), old[:9], ", ".join(other[:5])))
            return None
        problems = _history_problems(repo, tip)
        if problems:
            log("stamp inheritance: the night-shift history of %s is not valid: %s" % (tip[:9], "; ".join(problems[:3])))
            return None
        with open(old_stamp, encoding="utf-8") as handle:
            line = handle.read().strip()
        text = "%s inherited-from=%s\n" % (line, old)
        os.makedirs(passed_dir, exist_ok=True)
        atomic_io.atomic_write(target, text, mode=0o644)
        return text
    log("stamp inheritance: no stamped commit between %s and %s" % (base, tip[:9]))
    return None


def _history_problems(repo, tip):
    """What `night_shift_history.check_tree` says about the history files of `tip`.

    An empty list means the history is fine, or `tip` has none (hams_shared itself has no `night_shift_history`: proven by `git
    ls-tree` succeeding and listing neither path). Anything git cannot do (list the tree, archive the history, unpack it) is a
    problem naming the cause, never "no problems": an unreadable history was not checked."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import night_shift_history

    paths = ["night_shift_history", "night_shift_history.md"]
    listing = subprocess.run(["git", "-C", repo, "ls-tree", "--name-only", tip, "--", *paths], capture_output=True)
    if listing.returncode != 0:
        return ["git ls-tree %s failed (exit %s): %s" % (tip[:9], listing.returncode, listing.stderr.decode("utf-8", "replace").strip())]
    present = [line for line in listing.stdout.decode("utf-8", "replace").splitlines() if line]
    if not present:
        return []  # the repository has no such history (hams_shared itself)
    with tempfile.TemporaryDirectory(prefix="stamp-history-") as scratch:
        archive = subprocess.run(["git", "-C", repo, "archive", tip, *present], capture_output=True)
        if archive.returncode != 0:
            return ["git archive %s of %s failed (exit %s), so the history was not checked: %s" % (tip[:9], ", ".join(present), archive.returncode, archive.stderr.decode("utf-8", "replace").strip())]
        unpacked = subprocess.run(["tar", "-x", "-C", scratch], input=archive.stdout, capture_output=True)
        if unpacked.returncode != 0:
            return ["unpacking the history of %s failed (tar exit %s), so it was not checked: %s" % (tip[:9], unpacked.returncode, unpacked.stderr.decode("utf-8", "replace").strip())]
        return night_shift_history.check_tree(scratch)


# [@ANCHOR: lint_stamp_gate:inherited_note]
def inherited_note(stamp_text):
    """A loud line when the stamp was inherited (` inherited-from=<sha>`), else None: the pass rests on the earlier commit's run plus a diff of records only."""
    for field in stamp_text.split():
        if field.startswith("inherited-from="):
            return "NOTE: this pass is inherited from %s: only night-shift records (history, to-do, claim files) changed since that run" % field[len("inherited-from="):][:12]
    return None


# [@ANCHOR: lint_stamp_gate:known_red_note]
def known_red_note(stamp_text):
    """A loud line when the stamp records anything that was tolerated or retried, else None: ` known_red=a,b baseline=<sha>` (failures
    that are known red on main, ADR 0117) and/or ` timeout-retried=a,b` (steps that timed out on a saturated machine and passed a serial
    rerun). Both are named, so a reader of the merge output sees what the pass rests on."""
    notes = []
    for field in stamp_text.split():
        if field.startswith("known_red="):
            notes.append("NOTE: this pass tolerated failures that are known red on main (not caused by the branch): " + field[len("known_red="):])
        elif field.startswith("timeout-retried="):
            notes.append("NOTE: this pass rests on a serial rerun of steps that timed out while the machine was saturated: " + field[len("timeout-retried="):])
    return "\n".join(notes) or None


# [@ANCHOR: lint_stamp_gate:gate_main]
def main(argv):
    if len(argv) >= 2 and argv[1] == "--inherit":
        # lint_stamp_gate.py --inherit TIP PASSED_DIR [BASE]   (run inside the repository): writes the stamp of TIP from an earlier one, or exits 1
        if len(argv) not in (4, 5):
            print("usage: lint_stamp_gate.py --inherit TIP PASSED_DIR [BASE]", file=sys.stderr)
            return 2
        text = inherit_stamp(os.getcwd(), argv[2], argv[3], argv[4] if len(argv) == 5 else "origin/main", log=lambda m: print(m, file=sys.stderr))
        if text is None:
            return 1
        print(text.strip())
        return 0
    if len(argv) != 2:
        print("usage: lint_stamp_gate.py STAMP_FILE < changed_paths", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        stamp_text = handle.read()
    changed = [line.strip() for line in sys.stdin]
    reason = refusal(stamp_text, changed)
    if reason:
        print(f"REFUSING MERGE: {reason}.")
        return 1
    print(stamp_text.strip())
    for note in (known_red_note(stamp_text), inherited_note(stamp_text)):
        if note:
            print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
