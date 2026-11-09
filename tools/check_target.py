# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a `check_*.py` linter must do before it may print "Passed": prove it looked at something.

A checker that is handed a directory that does not exist, or one that holds none of the files it reads, walks nothing, finds
nothing and used to exit 0 ("Checked 0 files ... Passed"). Run by hand (or by a step whose path was mistyped, renamed or not
checked out) that is a pass with no evidence behind it (docs/GATE_AUDIT.md, "a green gate is a claim, not proof"). Every checker
that takes a directory calls these two functions:

  * `require_directory(path, checker)` first: the target must exist and be a directory (exit 2 otherwise).
  * `require_scanned(count, root, checker, what)` after the scan: at least one file of the kind the checker reads was examined
    (exit 2 otherwise), unless the checker documents that an empty scan is normal for it.

The cause and the next action are printed to stderr, so a reader sees why the run is not a pass and what to do.

Opt-out, for a step that legitimately can see zero files: `HAMS_CHECK_ALLOW_EMPTY=1` in the checker's environment turns the
zero-file failure into a pass (a missing or non-directory target still fails). `run_linters.py` sets it on every checker step it
launches, because it hands them whole repositories and modules, and a repository (hams_shared has no SCSS, a module has no Rust)
may hold none of a given type. A checker run by hand gets the strict behaviour. The tests clear the variable.

Exit status 2 means "could not check" (the same convention as `run_linters --changed` when git cannot list a diff), distinct from
1 ("checked, found violations").
"""

import os
import sys

ALLOW_EMPTY_ENV = "HAMS_CHECK_ALLOW_EMPTY"
EXIT_NOT_CHECKED = 2


def allow_empty(environ=None):
    """True when the caller declared that a scan of zero files is acceptable for this run."""
    environ = os.environ if environ is None else environ
    return environ.get(ALLOW_EMPTY_ENV, "") == "1"


def _refuse(message):
    print(message, file=sys.stderr)
    sys.exit(EXIT_NOT_CHECKED)


# [@ANCHOR: check_target:require_directory]
def require_directory(path, checker):
    """`path` as an absolute path, after proving it exists and is a directory. Anything else exits 2 with the cause."""
    absolute = os.path.abspath(path)
    if not os.path.exists(absolute):
        _refuse(
            f"[!] {checker}: target {path!r} does not exist (resolved to {absolute}); nothing was scanned, so this is not a pass. "
            "Next: pass the directory that holds the sources (a mistyped path, a renamed module or an unpopulated checkout)."
        )
    if not os.path.isdir(absolute):
        _refuse(
            f"[!] {checker}: target {path!r} is not a directory (resolved to {absolute}); nothing was scanned, so this is not a pass. "
            "Next: pass the directory that holds the sources, not a file."
        )
    return absolute


# [@ANCHOR: check_target:require_file]
def require_file(path, checker):
    """`path` as an absolute path, after proving it is an existing regular file (for a checker whose target is a manifest, not a
    directory). Anything else exits 2 with the cause."""
    absolute = os.path.abspath(path)
    if not os.path.exists(absolute):
        _refuse(
            f"[!] {checker}: {path!r} does not exist (resolved to {absolute}); nothing was read, so this is not a pass. "
            "Next: pass the manifest file's path."
        )
    if not os.path.isfile(absolute):
        _refuse(f"[!] {checker}: {path!r} is not a file (resolved to {absolute}); nothing was read, so this is not a pass. Next: pass the manifest file's path.")
    return absolute


# [@ANCHOR: check_target:require_scanned]
def require_scanned(count, root, checker, what, environ=None):
    """Return `count` when at least one file was scanned; otherwise exit 2 naming the cause and the next action.

    `what` names the kind of file the checker reads ("Python files", "*.xml and *.js files"). With HAMS_CHECK_ALLOW_EMPTY=1 an
    empty scan returns 0 and the checker carries on, printing its own "Checked 0 files" summary."""
    if count > 0 or allow_empty(environ):
        return count
    _refuse(
        f"[!] {checker}: scanned 0 {what} under {root}; a scan of nothing is not a pass. "
        "Cause: the directory holds none of the files this checker reads (the wrong directory, a module that was moved or is not "
        "checked out, or an empty tree). "
        f"Next: pass the directory that holds them. A step that legitimately can see none sets {ALLOW_EMPTY_ENV}=1."
    )
