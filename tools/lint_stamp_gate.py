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

import re
import sys

RUST_SUFFIXES = (".rs", "Cargo.toml", "Cargo.lock")
JS_SUFFIXES = (".js", ".mjs", ".cjs")
# Steps a stamp must have selected when the branch changed a file of that kind.
RUST_STEPS = ("check_cargo_clippy", "check_cargo_fmt", "check_rust_function_test_anchors", "verify_anchors")
JS_STEPS = ("check_js_syntax",)
DOC_SUFFIXES = (".md",)


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


def refusal(stamp_text, changed_paths):
    """None when the stamp covers the change, otherwise the reason it does not."""
    mode, count, names = parse_stamp(stamp_text)
    paths = [path for path in changed_paths if path]
    code = [path for path in paths if not path.endswith(DOC_SUFFIXES)]
    if not code or mode == "full":
        return None
    if count == 0:
        return "the stamp selected zero steps for a branch that changed code"
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


def main(argv):
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
    note = known_red_note(stamp_text)
    if note:
        print(note)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
