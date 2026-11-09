#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
pip-audit gate (see docs/proposals/CODE_REVIEW_PROCESS.md, "Formal verification tooling")
-------------------------------------------------------------------------------------------
The direct Python parallel to `cargo-deny`/`cargo-audit` on the Rust side
(RELAY_SUPPLY_CHAIN_SECURITY.md section 3): scans every `requirements*.txt`
in the repo against the Python Packaging Advisory Database (via PyPI's JSON
API) and fails the linter run if any pinned or resolvable dependency has a
known vulnerability. Requires network access, the same way the Rust
advisory scan does.

Scans the whole repo rather than a possibly-scoped `targets` list, same
reasoning as the other full-repo steps in run_linters.py: a vulnerable
dependency can be introduced by a `requirements.txt` anywhere, not just
near whatever file a targeted lint run happens to be scoped to.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
import lint_exec
import subprocess
import sys
import time

# "site-packages" matches any virtualenv by its structurally-guaranteed lib/pythonX.Y/
# site-packages/ subdirectory regardless of the venv's own outer directory name (".venv"/"venv"
# alone missed a real stray "whisper_venv/" -- see check_float_isfinite.py's own comment and test
# for the full story).
IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git", ".claude", "site-packages"}

# Real bug found 2026-09-12, confirmed empirically with a real `git worktree add`: this
# project's own standing convention runs concurrent bug-hunt dispatches in isolated git
# worktrees under `.claude/worktrees/<session>/` INSIDE the repo root -- and a git worktree
# checks out a full copy of every tracked file at its own commit/branch, `requirements.txt`
# included. Before `.claude` was added to IGNORE_DIR_NAMES above,
# `find_requirements_files()` walked straight into a live worktree and found its own
# `requirements.txt` as a second, real scan target -- confirmed directly against a real
# worktree created from this exact repo (found 2 real requirements.txt files under it). A
# previous review pass (see night_shift_todo.md) wrongly dismissed this checker as immune to
# the ".claude worktree" bug class on the reasoning that "a worktree wouldn't contain a
# requirements.txt to falsely match in the first place" -- that reasoning was never verified
# against a real worktree and was wrong: a worktree is a full checkout, not an empty
# directory. Running pip-audit a second time against another session's own in-progress,
# uncommitted requirements.txt wastes real network calls and could report spurious findings
# against dependency changes that were never actually merged.

# Real gap found 2026-09-12: neither subprocess.run() call below passed a `timeout=`, despite
# this module's own docstring stating pip-audit "Requires network access" (PyPI's JSON Advisory
# Database) -- a stalled/hung network condition (or a hung pip-audit subprocess for any other
# reason) would block this call, and everything downstream of it in a run_linters.py run,
# indefinitely. check_dependency_releases.py (a sibling network-calling checker in this same
# tree) already establishes a REQUEST_TIMEOUT_SECONDS convention for exactly this class of call;
# mirrored here as two separate constants since the version-probe and the real per-file audit
# have very different realistic durations (a full dependency scan legitimately takes much longer
# than a `--version` check).
VERSION_CHECK_TIMEOUT_SECONDS = 30
AUDIT_TIMEOUT_SECONDS = 300
# On a saturated machine run_linters.py exports HAMS_LINT_TIMEOUT_SCALE (1 to 4): the limit stretches with it, so a starved audit is not
# reported as a stalled network (ADR 0117). A bad value means 1.
def _timeout_scale(text):
    """The scale in `text` (1 to 4), 1.0 for anything that is not a finite number."""
    try:
        value = float(text or 1)
    except ValueError:
        return 1.0
    return min(4.0, max(1.0, value)) if math.isfinite(value) else 1.0


AUDIT_TIMEOUT_SECONDS = int(AUDIT_TIMEOUT_SECONDS * _timeout_scale(os.environ.get("HAMS_LINT_TIMEOUT_SCALE")))


# A passing audit of an unchanged requirements file is reused for a few hours: the audit is a wait on PyPI's advisory API, and a stalled
# PyPI (or a starved box) used to fail unrelated branches after 300 s. Only a PASS is cached: a finding, a timeout and any other error
# never are, and the entry expires (advisories do change), so a new vulnerability is still found within the window.
CACHE_DIR_ENV = "HAMS_PIP_AUDIT_CACHE_DIR"
CACHE_HOURS_ENV = "HAMS_PIP_AUDIT_CACHE_HOURS"
DEFAULT_CACHE_HOURS = 6.0


# [@ANCHOR: check_pip_audit:cache_hours]
def cache_hours(environ=None):
    """How long a passing audit is reused: `HAMS_PIP_AUDIT_CACHE_HOURS` (a finite number of at least 0; 0 turns the cache off) or 6.
    A value that is not a finite number of at least 0 means the default. Pure."""
    environ = os.environ if environ is None else environ
    try:
        value = float(environ.get(CACHE_HOURS_ENV) or DEFAULT_CACHE_HOURS)
    except ValueError:
        return DEFAULT_CACHE_HOURS
    if not math.isfinite(value) or value < 0:
        return DEFAULT_CACHE_HOURS
    return value


def _cache_directory(environ):
    explicit = environ.get(CACHE_DIR_ENV)
    if explicit:
        return explicit
    base = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hams-lint", "pip-audit")


# [@ANCHOR: check_pip_audit:cache_key]
def cache_key(req_file, version_text):
    """The cache key of one requirements file: the SHA-256 of its real path, its content and the pip-audit version text, as 64 hex
    digits. Another path (another checkout), an edited file or another pip-audit version is another key. Raises OSError when the
    file cannot be read."""
    with open(req_file, "rb") as handle:
        content = handle.read()
    digest = hashlib.sha256()
    for part in (os.path.realpath(req_file).encode(), content, str(version_text).strip().encode()):
        digest.update(len(part).to_bytes(8, "big") + part)
    return digest.hexdigest()


# [@ANCHOR: check_pip_audit:cached_pass_age_hours]
def cached_pass_age_hours(key, now=None, environ=None):
    """Age in hours of the recorded pass for `key`, or None when there is none, it is older than `cache_hours()`, it is dated in the
    future, the cache is off (0 hours) or the file cannot be read. Reads one small file; never raises."""
    environ = os.environ if environ is None else environ
    limit = cache_hours(environ)
    if limit == 0:
        return None
    now = time.time() if now is None else now
    try:
        with open(os.path.join(_cache_directory(environ), key + ".json"), encoding="utf-8") as handle:
            recorded = float(json.load(handle)["passed_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = (now - recorded) / 3600.0
    return age if 0 <= age <= limit else None


# [@ANCHOR: check_pip_audit:store_pass]
def store_pass(key, now=None, environ=None):
    """Records that the audit for `key` passed at `now` (default the current time), atomically. Does nothing when the cache is off. A
    directory that cannot be written is ignored (the cache is an optimisation, never a reason to fail). Returns True when it wrote."""
    environ = os.environ if environ is None else environ
    if cache_hours(environ) == 0:
        return False
    directory = _cache_directory(environ)
    temporary = os.path.join(directory, "%s.%d.tmp" % (key, os.getpid()))
    try:
        os.makedirs(directory, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"passed_at": time.time() if now is None else now}, handle)
        os.replace(temporary, os.path.join(directory, key + ".json"))
    except OSError:
        return False
    return True


def _resolve_repo_root(given_path):
    """run_linters.py's own `dir_path` resolves to the hams_shared directory itself, not a real
    repo root (same bug found and fixed in check_model_extension_collisions.py and others) --
    confirmed directly: this checker was silently finding 0 requirements.txt files via
    run_linters.py's actual invocation, versus 2 at a real repo root -- this codebase's Python
    supply-chain vulnerability gate (CODE_REVIEW_PROCESS.md) has never actually scanned anything
    in CI. Detect the hams_shared case by name and redirect to its real parent repo."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """The fix above only ever redirects to ONE repo (hams_open) -- but real requirements.txt
    files exist in both: hams_open's own root, and two under hams_com/daemons/
    (gdpr_csv_export, hams_simulated_bots). run_linters.py's own actual invocation was catching
    hams_open's but silently missing both hams_com ones. Same sibling-repo shape as the other
    fixed checkers."""
    repo_root = _resolve_repo_root(given_path)
    roots = [repo_root]
    sibling = lint_exec.sibling_repo_root(repo_root, lint_exec.has_odoo_module)
    if sibling:
        roots.append(sibling)
    return roots


def find_requirements_files(repo_root):
    found = []
    for root, dirs, filenames in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES]
        for name in filenames:
            if name.startswith("requirements") and name.endswith(".txt"):
                found.append(os.path.join(root, name))
    return sorted(found)


def main():
    if len(sys.argv) < 2:
        print("Usage: check_pip_audit.py <repo_root>")
        sys.exit(1)

    requirements_files = [
        (repo_root, req_file)
        for repo_root in _resolve_repo_roots(sys.argv[1])
        for req_file in find_requirements_files(repo_root)
    ]
    if not requirements_files:
        sys.exit(0)

    try:
        check = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"❌ pip-audit --version did not respond within "
            f"{VERSION_CHECK_TIMEOUT_SECONDS}s -- treating as not installed/unusable."
        )
        sys.exit(1)
    if check.returncode != 0:
        print(
            "❌ pip-audit is not installed (python3 -m pip install --user "
            "--break-system-packages pip-audit) -- required by "
            "CODE_REVIEW_PROCESS.md's supply-chain scanning gate."
        )
        sys.exit(1)

    # The requirements files are independent and each audit is a wait on PyPI's advisory API plus a resolver
    # run, so they overlap (up to HAMS_PIP_AUDIT_JOBS, default 4). Output is printed in file order, so the
    # text is the same as the one-after-another run's.
    version_text = check.stdout if isinstance(check.stdout, str) else ""

    def audit_one(item):
        repo_root, req_file = item
        try:
            key = cache_key(req_file, version_text)
        except OSError:
            key = None
        age = cached_pass_age_hours(key) if key else None
        if age is not None:
            return ("cached", age)
        res = None
        # A stalled call to PyPI is not a finding. One retry, with the same limit, before failing the gate; the bound is
        # two limits, never indefinite.
        for _attempt in range(2):
            try:
                res = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip_audit",
                        "-r",
                        req_file,
                        "--progress-spinner",
                        "off",
                    ],
                    capture_output=True,
                    text=True,
                    cwd=repo_root,
                    timeout=AUDIT_TIMEOUT_SECONDS,
                )
                break
            except subprocess.TimeoutExpired:
                res = None
        if res is not None and res.returncode == 0 and key:
            store_pass(key)
        return res

    jobs = max(1, int(os.environ.get("HAMS_PIP_AUDIT_JOBS", "4") or 4))
    any_failed = False
    with ThreadPoolExecutor(max_workers=min(jobs, len(requirements_files))) as pool:
        for (repo_root, req_file), res in zip(requirements_files, pool.map(audit_one, requirements_files)):
            rel_path = os.path.relpath(req_file, repo_root)
            if isinstance(res, tuple):
                print(f"[*] pip-audit: {rel_path} passed {res[1]:.1f} h ago and is unchanged: reusing that result "
                      f"(cache {CACHE_HOURS_ENV}, default {DEFAULT_CACHE_HOURS:g} h)")
                continue
            if res is None:
                if not any_failed:
                    print("❌ pip-audit findings:")
                any_failed = True
                print(
                    f"-- {rel_path} --\npip-audit did not complete within "
                    f"{AUDIT_TIMEOUT_SECONDS}s, twice (stalled network call to PyPI's Advisory Database?) -- "
                    "failing the gate rather than blocking the run indefinitely."
                )
                continue
            if res.returncode != 0:
                if not any_failed:
                    print("❌ pip-audit findings:")
                any_failed = True
                print(f"-- {rel_path} --")
                if res.stdout:
                    print(res.stdout, end="")
                if res.stderr:
                    print(res.stderr, end="")
    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
