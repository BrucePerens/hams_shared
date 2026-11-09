#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Odoo-Aware Type Checking, Phase 1 (see docs/proposals/ODOO_AWARE_TYPE_CHECKING.md)
------------------------------------------------------------------------------------
Runs `mypy --check-untyped-defs` over plain, non-Odoo-model utility files --
pure functions and daemon/ingestion scripts with no `_inherit`/`_name`
model class involved. This catches real "wrong method, wrong arg count"
bugs (the proposal's own motivating examples: notify_model_invalidation()
called with the wrong arg count, record.with_delay() when queue_job was
never installed) without needing the registry-aware mypy plugin Phase 2
would require to handle Odoo's `_inherit` merging correctly.

Any file that *defines* an Odoo Model/AbstractModel/TransientModel class
is automatically skipped -- Odoo Model classes are exactly the class of
file vanilla mypy cannot check correctly yet (see the proposal's
"cross-module _inherit" section), so silently skipping them here is a
safety filter, not a gap Phase 2 hasn't gotten to.

This is a real AST check for a class deriving from one of those three
bases, not a textual "imports models from odoo" scan (which used to be
this function's entire test, ODOO_AWARE_TYPE_CHECKING.md's original
Phase 1 shape) -- that textual check was a confirmed false negative for
files that reference `models.Model` for something other than defining a
class (e.g. an `isinstance(obj, models.Model)` runtime check in an
otherwise plain utility module), silently exempting exactly the kind of
file this tool exists to catch bugs in. Found via
distributed_redis_cache/redis_cache.py: eight plain module-level
functions, zero Model classes, one `isinstance(obj, models.Model)` check
-- the proposal's own motivating `notify_model_invalidation()` bug lived
in this exact file and was never actually scanned because of it.

Deliberately excluded even though it doesn't touch Odoo models:
`daemons/hams_local_relay/radae/` (vendored ML/DSP research code with
extensive real mypy findings -- untangling third-party research code's
typing is its own project, not Phase 1 scope), and
`daemons/cloudflared/` (the entire directory is vendored, upstream
`cloudflared` client source -- see its own README -- not code owned by
this repo). Confirmed real findings there, not assumed: 6 dataclass
fields typed non-Optional but defaulted to None (config.py), a missing
list[bytes] annotation (util.py), and a genuinely broken test --
test_service.py references `CfdModes.CLASSIC`, an enum member that does
not exist (only NAMED/QUICK are defined, and the fixture only handles
those two), which would AttributeError the instant either of the two
tests using it actually ran. None of this was patched here -- editing
vendored upstream code creates a merge conflict on the next re-vendor,
and CfdModes.CLASSIC specifically would mean guessing at Cloudflare's own
classic-tunnel config semantics inside code this repo doesn't own.
Recorded as an upstream defect instead.

**This also means the claim below (written before this
exclusion existed) needs a caveat**: before the exclusion, the
"hard, unconditional gate" was NOT actually green -- these vendored
cloudflared findings made `run_linters.py` step 23 exit 1 on unmodified
code (confirmed by running this exact checker). Whether that was dependency
drift after the `pytest`/`pytest-asyncio` bump or the claim never having
covered this vendored subtree was not determined. The exclusion above is
what makes the gate green again, verified by rerunning.

EXCLUDED_FILES started as 33 files with real, not-yet-reviewed mypy
findings on this check's introduction. That backlog has
been fully worked through -- every file now passes cleanly, with real
bugs fixed (e.g. a NoneType slice crash on a malformed NOAA payload, a
websockets 15.x API mismatch that would TypeError the instant a test
ran, a live-broken binary-discovery function that could never find
Debian's own `pat` package) and false positives resolved honestly rather
than blanket-suppressed (heterogeneous dict/list literals given real
type annotations instead of mypy's overly-broad inferred join type,
Python's function-wide variable typing conflicting across unrelated
code paths reusing a name like `f` or `key`, and confirmed third-party
stub gaps like telnetlib3.open_connection()'s bare
`Union[TelnetWriter, TelnetWriterUnicode]` return type with no static
way to narrow on the `encoding=` argument's runtime value). See git
history for the individual fix commits. This is now a hard, unconditional
gate (run_linters.py step 23 already fails the whole lint pass on any
finding here, for any file this check reaches) -- EXCLUDED_FILES stays
in the code, empty, as where a newly reviewed and deliberately accepted
exclusion would go if one is ever needed again, not repopulated
reflexively just because a new file trips this check.
"""

import ast
from concurrent.futures import ThreadPoolExecutor
import contextlib
import os
import lint_exec
import queue
import subprocess
import sys
from tree_files import tree_files


def _resolve_repo_root(given_path):
    """run_linters.py's own `dir_path` resolves to the hams_shared directory itself, not a real
    repo root (same bug found and fixed in check_model_extension_collisions.py and others) --
    confirmed directly: this checker was silently finding 0 utility-file candidates via
    run_linters.py's actual invocation, versus 2 at a real repo root. Detect the hams_shared case
    by name and redirect to its real parent repo."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """The fix above only ever resolved to ONE repo -- but SCAN_ROOTS spans both:
    `ham_com/models/callsign_validation.py`, `ham_base/models/geo_utils.py`, `daemons`, and
    `ingest` all live under hams_com, while `distributed_redis_cache/redis_cache.py` and
    `redis_pool.py` live under hams_open. Resolving to hams_open alone (what the fix above does
    when run_linters.py's real `dir_path`, hams_shared, gets redirected to its parent) silently
    scans only the 2 distributed_redis_cache files and misses every hams_com-rooted entry --
    confirmed directly: run_linters.py's own actual invocation found 0 errors, but a direct call
    against hams_com found 5 files with real mypy findings
    (daemons/gdpr_csv_export/test_main.py, daemons/hams_simulated_bots/fine_tuning/
    build_dataset.py, daemons/test_daemon_key_registration_coverage.py,
    daemons/test_daemon_provisioning_coverage.py, daemons/test_hams_config.py) that step 23 of
    run_linters.py has been silently never checking. Same sibling-repo-detection shape
    check_self_writeable_field_tests.py already uses for the identical two-repos problem."""
    repo_root = _resolve_repo_root(given_path)
    roots = [repo_root]
    sibling = lint_exec.sibling_repo_root(repo_root, lint_exec.has_odoo_module)
    if sibling:
        roots.append(sibling)
    return roots


# Mirrors check_model_extension_collisions.py's own MODEL_BASES/
# _is_model_class -- not imported directly, since these tool scripts are
# each self-contained (odoo_registry_builder.py mirrors the same AST
# logic independently rather than cross-importing, the established
# pattern in this directory).
MODEL_BASES = {"Model", "AbstractModel", "TransientModel"}


def _is_model_class(node):
    for base in node.bases:
        if isinstance(base, ast.Attribute) and base.attr in MODEL_BASES:
            return True
        if isinstance(base, ast.Name) and base.id in MODEL_BASES:
            return True
    return False

# Repo-relative paths (files or directories) to scan.
SCAN_ROOTS = [
    "ham_com/models/callsign_validation.py",
    "ham_base/models/geo_utils.py",
    "distributed_redis_cache/redis_cache.py",
    "distributed_redis_cache/redis_pool.py",
    "daemons",
    "ingest",
]

# Repo-relative directory prefixes to never scan, even under SCAN_ROOTS.
# daemons/hams_local_relay/vendor/opusic-sys is a vendored Rust FFI crate (confirmed via its own
# auto-generated Cargo.toml header and README.md/README.hams-patch.md, not assumed) -- its
# meson/get-version.py build helper has 5 real mypy findings (str/int type confusion in a version-
# string parser), same class of "not ours to fix, re-vendoring overwrites it" finding as
# radae/cloudflared below.
EXCLUDED_DIR_PREFIXES = [
    "daemons/hams_local_relay/radae",
    "daemons/cloudflared",
    "daemons/hams_local_relay/vendor/opusic-sys",
]

# Every file this check can reach passes
# mypy --check-untyped-defs cleanly -- the backlog that used to live
# here (33 files) has been fully worked through, fixing real bugs and
# false positives along the way (see git history for the individual
# commits). This set stays here, empty, as where a *newly reviewed and
# accepted* exclusion would go if one is ever needed again -- not
# repopulated reflexively just because a new file trips this check.
EXCLUDED_FILES: set = set()

# "site-packages" matches any virtualenv by its structurally-guaranteed lib/pythonX.Y/
# site-packages/ subdirectory regardless of the venv's own outer directory name (".venv"/"venv"
# alone missed a real stray "whisper_venv/" -- see check_float_isfinite.py's own comment and test
# for the full story).
IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git", "site-packages"}


def defines_odoo_model_class(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError subclass, not an OSError subclass -- pairing it with
        # OSError here matches every sibling checker in this tree's own established convention
        # for exactly this failure mode (a real file with invalid-UTF-8 bytes anywhere under the
        # scanned tree would otherwise crash the whole checker before mypy ever runs).
        return False
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        # Not this checker's job to report a syntax error in a file it's
        # only trying to classify -- mypy itself will fail loudly on this
        # file if it's ever actually scanned, once this returns False.
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _is_model_class(node):
            return True
    return False


def collect_candidates(repo_root):
    candidates = []
    for rel_root in SCAN_ROOTS:
        abs_root = os.path.join(repo_root, rel_root)
        if os.path.isfile(abs_root):
            files = [abs_root]
        elif os.path.isdir(abs_root):
            files = tree_files(abs_root, ".py", IGNORE_DIR_NAMES, untracked=True)
        else:
            continue

        for abs_path in files:
            rel_path = os.path.relpath(abs_path, repo_root)
            if any(
                rel_path == excluded or rel_path.startswith(excluded + os.sep)
                for excluded in EXCLUDED_DIR_PREFIXES
            ):
                continue
            if rel_path in EXCLUDED_FILES:
                continue
            if defines_odoo_model_class(abs_path):
                continue
            candidates.append(abs_path)
    return sorted(set(candidates))


def parse_args(argv):
    """(repo_root, only_files or None). `--only-files F...` limits the check to those files (those of them that the full scan
    would also check): `run_linters.py --changed` passes the changed Python files, as it does to flake8. Each file is checked on
    its own, so a file nobody changed is not affected by a finding in another."""
    if len(argv) < 2:
        return None, None
    if "--only-files" not in argv:
        return argv[1], None
    index = argv.index("--only-files")
    return argv[1], {os.path.realpath(path) for path in argv[index + 1:]}


class cache_slots:
    """Hands each concurrent mypy run a cache directory of its own (`.mypy_cache/w<N>`).

    mypy's cache is not safe for concurrent runs: with one shared directory and a cold or stale cache, parallel runs
    read each other's half-written files and crash with `AssertionError: Cannot find module for csv`. It never
    showed on the dev box, whose cache was already warm; a lint runner with a fresh mirror failed every time
    (measured). A worker keeps its directory between runs, so each directory warms up once."""

    def __init__(self, workers):
        self._free = queue.Queue()
        for slot in range(workers):
            self._free.put(slot)

    @contextlib.contextmanager
    def borrow(self):
        slot = self._free.get()
        try:
            yield slot
        finally:
            self._free.put(slot)


def main():
    repo_root_arg, only_files = parse_args(sys.argv)
    if repo_root_arg is None:
        print("Usage: check_untyped_utility_files.py <repo_root> [--only-files FILE ...]")
        sys.exit(1)

    repo_roots = _resolve_repo_roots(repo_root_arg)
    candidates = [(root, path) for root in repo_roots for path in collect_candidates(root)]
    if only_files is not None:
        candidates = [(root, path) for root, path in candidates if os.path.realpath(path) in only_files]
    if not candidates:
        sys.exit(0)

    # Run mypy one file at a time rather than as one batched invocation:
    # these are independent, unrelated CLI scripts (many literally named
    # main.py in different directories), not one coherent package, and
    # mypy's default module-name inference collides same-basename files
    # across different directories ("Duplicate module named main") when
    # given as a single batch with no __init__.py markers to disambiguate.
    # The files are independent, so the mypy runs overlap (a whole-repo scan of hundreds of
    # files took five minutes one after another). Output is printed in candidate order, so the
    # text is the same as the sequential run's. $HAMS_LINT_JOBS bounds the workers.
    workers = max(1, int(os.environ.get("HAMS_LINT_JOBS", "0")) or (os.cpu_count() or 2) // 2)
    slots = cache_slots(workers)

    def run_one(item):
        repo_root, candidate = item
        with slots.borrow() as slot:
            return subprocess.run(
                ["mypy", "--check-untyped-defs", "--ignore-missing-imports", f"--cache-dir=.mypy_cache/w{slot}", candidate],
                capture_output=True,
                text=True,
                cwd=repo_root,
            )

    any_failed = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(run_one, candidates):
            if res.returncode != 0:
                if not any_failed:
                    print("❌ mypy findings in non-Odoo-model utility files:")
                any_failed = True
                if res.stdout:
                    print(res.stdout, end="")
                if res.stderr:
                    print(res.stderr, end="")

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
