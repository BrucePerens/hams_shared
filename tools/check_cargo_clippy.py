#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
cargo-clippy gate (see docs/proposals/CODE_REVIEW_PROCESS.md, "Formal verification tooling")
-------------------------------------------------------------------------------------------
The Rust code-quality scan that is flake8/ESLint's direct parallel on the Rust side, but --
like cargo-deny (check_cargo_deny.py) -- was never actually wired into run_linters.py at
all, a real gap found 2026-08-26 running `cargo clippy` by hand for the first time in this
codebase's history: 3 real findings in hams_local_relay alone (a genuinely dead struct
field, an unused-but-load-bearing match-arm variable that turned out to be an honestly
documented MVP stub, and two more findings in each of the other 3 daemon crates). Runs
`cargo clippy -- -W clippy::all` in every daemons/ subdirectory that has a Cargo.toml.

Scans the whole repo rather than a possibly-scoped `targets` list, same reasoning as
check_cargo_deny.py/check_pip_audit.py: a clippy-flagged pattern can be introduced in any
crate, not just near whatever file a targeted lint run happens to be scoped to.
"""

import filecmp
import glob
import os
import re
import shutil
import subprocess
import sys

import lint_cache
import lint_exec
import rust_impact
from tree_files import tree_walk

# "site-packages" matches any virtualenv by its structurally-guaranteed lib/pythonX.Y/
# site-packages/ subdirectory regardless of the venv's own outer directory name (".venv"/"venv"
# alone missed a real stray "whisper_venv/" -- see check_float_isfinite.py's own comment and test
# for the full story).
IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git", "site-packages"}

# Real gap found 2026-09-06 running this gate end to end via run_linters.py:
# this standalone `#![no_std]` codegen-inspection crate (see its own README)
# is deliberately never buildable for the host triple at all -- it only ever
# builds cross-compiled for a real embedded target (`--target
# xtensa-esp32-none-elf -Z build-std=core` or `--target thumbv7em-none-
# eabihf`), which this gate's own blanket `cargo clippy` (no `--target`) can
# never supply. That's not a real code defect ("unwinding panics are not
# supported without std" is expected and correct for a no_std crate built
# for a std-having host target), just this generic host-target sweep
# reaching a crate it was never meant to build. Excluded by its exact
# relative path, not a blanket dirname, since nothing about "docs/
# references" in general implies "not host-buildable" -- this one crate's
# own README is what establishes that.
# Real gap found 2026-09-07 running this gate after vendoring kchmck/imbe.rs as a read-only
# AMBE cross-check reference (see reference/ambe/imbe.rs's own commit message): it's a
# third-party crate frozen at whatever Rust edition it was written for, and its
# `#![feature(inclusive_range_syntax)]` (stable since 1.26.0, now a hard `E0554` error on
# any non-nightly toolchain) fails this gate's blanket `cargo clippy` outright. We don't own
# or maintain this crate's source -- it exists purely to diff our own from-scratch AMBE port
# against, matching `check_rust_function_test_anchors.py`'s own `reference/` exclusion for the
# exact same reason -- so it's excluded from the build/lint gate rather than patched to track
# a toolchain it was never written against.
#
# Found 2026-09-28 running this gate on a clean checkout: daemons/ham_digital_modes/tools/codec2_riscv32_bench
# is a bare-metal harness (riscv32imc, run under QEMU) whose main.rs does
# `include_bytes!("../speech.raw")`. That file is gitignored -- prep.sh cuts it out of a speech recording -- so
# on any checkout without a local copy the crate cannot build at all, and the gate reported a "build error"
# every time. Its code path is already covered by the host build of the same fixed-point codec and by the
# harness's own checksum comparison (see its prep.sh), so it is excluded here rather than given a fake data file.
EXCLUDE_CRATE_RELPATHS = {
    "daemons/ham_digital_modes/docs/references/codec2_fixedpoint_codegen_check",
    "daemons/ham_digital_modes/tools/codec2_riscv32_bench",
    "reference/ambe/imbe.rs",
}


# Cargo names a build script's run directory after the package and a hash that does not contain
# the checkout's path, so every worktree that shares one CARGO_TARGET_DIR (this project's
# agents do: one target directory, many `git worktree`s of the same repo) shares it too, and
# cargo reuses the cached output while the `rerun-if-changed` paths look unchanged. A build
# script that writes absolute paths into that output (bench_mcp's build.rs writes
# `include_str!("<checkout>/manual/...")` into $OUT_DIR/manual_files.rs) then hands the next
# worktree's build a file that names the FIRST worktree's files: when that worktree has been
# removed the build fails ("couldn't read <old checkout>/manual/..."), and when it has not, the
# build silently uses the other worktree's files. Found 2026-10-30, when check_cargo_clippy
# failed twice in one night until the `debug/build/bench_mcp-*` directories were deleted by hand;
# reproduced with a two-worktree crate that has the same build.rs.
_MAX_SCANNED_BYTES = 2 * 1024 * 1024
_PATH_CHARS = r"[^\s\"'<>;,=()\[\]{}`]"


def _cached_text(run_dir):
    """(name relative to `run_dir`, text) of what a build script left there: its `output` (the cargo: lines) and
    every small text file under `out/` (the generated sources). Binary and huge files are skipped.

    A binary file is recognised by the NUL byte in its first 4 KiB, and only those 4 KiB are read to find out: a
    warm target directory holds gigabytes of object files under `build/*/out/`, and reading each of them whole,
    for every crate, took 129 s of an otherwise 2 s warm run (measured 2026-11-03)."""
    names = [os.path.join(run_dir, "output")]
    for root, _dirs, files in os.walk(os.path.join(run_dir, "out")):
        names.extend(os.path.join(root, f) for f in files)
    for name in names:
        try:
            if os.path.getsize(name) > _MAX_SCANNED_BYTES:
                continue
            with open(name, "rb") as f:
                head = f.read(4096)
                if b"\0" in head:
                    continue
                data = head + f.read()
        except OSError:
            continue
        yield os.path.relpath(name, run_dir).replace(os.sep, "/"), data.decode("utf-8", errors="replace")


def _other_checkout_hits(text, crate_relpath, current_roots):
    """[(other checkout root, the whole line it was found on, the path found)] for every absolute path in `text`
    that goes through `<some checkout>/<crate_relpath>` where the checkout is not one of `current_roots`."""
    if crate_relpath not in text:  # the pattern needs this literal; a substring test is far cheaper than the regex scan
        return []
    pattern = re.compile("(/" + _PATH_CHARS + "*?)/" + re.escape(crate_relpath) + r"(?=/|$|[\s\"'])(" + _PATH_CHARS + "*)")
    hits = []
    for m in pattern.finditer(text):
        if m.group(1) in current_roots:
            continue
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        hits.append((m.group(1), text[start:len(text) if end < 0 else end], m.group(1) + "/" + crate_relpath + m.group(2)))
    return hits


def _names_another_checkout(text, crate_relpath, current_roots):
    """True when `text` holds an absolute path through `<some checkout>/<crate_relpath>` where
    the checkout is not one of `current_roots`."""
    return bool(_other_checkout_hits(text, crate_relpath, current_roots))


# What a build script's output may name of another checkout without making it unusable here.
#
# A match inside `output` (any `cargo:` line but rerun-if-changed) or inside a generated source is consumed by the
# compiler or the linker of the crates that depend on it: that output is wrong in every other checkout, always
# purged (the bench_mcp `include_str!` bug above). Two kinds of match are different (measured 2026-11-03: they were
# 210-280 s per switch between checkouts, because they rebuilt `opusic-sys` and `hams_local_relay`, which compile C):
#   * `cargo:rerun-if-changed=<absolute path>`: only says which files cargo should watch.
#   * files under `out/build/`: the script's private build tree (CMake's cache, ninja files), whose only reader is
#     the script itself, the next time it runs.
# Both are harmless if the script does not run again here and the files it was built from are the same here. That is
# decided by comparing the files (`_same_tree`) and their times against the run's `invoked.timestamp`, the time
# cargo itself compares: a file changed since the script ran, or one that differs between the two checkouts, makes the
# output unusable and it is purged as before.
_RERUN_PREFIXES = ("cargo:rerun-if-changed=", "cargo::rerun-if-changed=")
_SCRATCH_PREFIX = "out/build/"
_MAX_COMPARED_BYTES = 512 * 1024 * 1024


def _same_tree(a, b, not_after_ns):
    """True when `a` and `b` are the same file, or the same directory tree, byte for byte, and no file in either
    was modified after `not_after_ns`. A tree larger than _MAX_COMPARED_BYTES, a missing path, or anything
    unreadable is "not the same" (the caller then purges, which is always safe)."""
    budget = [_MAX_COMPARED_BYTES]

    def files(root):
        if os.path.isfile(root) and not os.path.islink(root):
            return {"": root}
        found = {}
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIR_NAMES)
            for n in names:
                full = os.path.join(dirpath, n)
                found[os.path.relpath(full, root).replace(os.sep, "/")] = full
        return found

    try:
        left, right = files(a), files(b)
        if set(left) != set(right) or not left:
            return False
        for rel in sorted(left):
            pa, pb = left[rel], right[rel]
            sa, sb = os.lstat(pa), os.lstat(pb)
            if os.path.islink(pa) or os.path.islink(pb):
                if not (os.path.islink(pa) and os.path.islink(pb) and os.readlink(pa) == os.readlink(pb)):
                    return False
                continue
            if sa.st_mtime_ns > not_after_ns or sb.st_mtime_ns > not_after_ns or sa.st_size != sb.st_size:
                return False
            budget[0] -= sa.st_size
            if budget[0] < 0 or not filecmp.cmp(pa, pb, shallow=False):
                return False
    except OSError:
        return False
    return True


def _reusable_here(run_dir, hits, repo_root):
    """True when every match in `hits` ((other root, text file name, line, path found)) is of a harmless kind (see above)
    and the same sources exist unchanged in this checkout, so this run directory's output is valid here."""
    try:
        stamp = os.stat(os.path.join(run_dir, "invoked.timestamp")).st_mtime_ns
    except OSError:
        stamp = os.stat(os.path.join(run_dir, "output")).st_mtime_ns
    pairs = set()
    scratch = {}  # other root -> directories its private build tree was built from
    for other_root, name, line, found in hits:
        if name == "output" and line.startswith(_RERUN_PREFIXES):
            path = os.path.normpath(line.split("=", 1)[1].strip())
            if not path.startswith(other_root + os.sep):
                return False
            pairs.add((path, os.path.join(repo_root, path[len(other_root) + 1:])))
        elif name.startswith(_SCRATCH_PREFIX):
            path = os.path.normpath(found)
            if not os.path.exists(path):
                return False
            scratch.setdefault(other_root, set()).add(path if os.path.isdir(path) else os.path.dirname(path))
        else:
            return False
    for other_root, dirs in scratch.items():
        common = os.path.commonpath(sorted(dirs))
        pairs.add((common, os.path.join(repo_root, os.path.relpath(common, other_root))))
    return all(_same_tree(there, here, stamp) for there, here in sorted(pairs))


def purge_stale_build_script_output(target_dir, repo_root, crate_dir, memo=None, kept=None):
    """Deletes the cached run directory (and its fingerprint) of every build script under
    `target_dir` whose output names `crate_dir`'s path in a checkout other than `repo_root`, so
    cargo runs the script again here, unless the only such names are harmless and the sources are
    the same here (`_reusable_here`): then it is kept and added to the list `kept` if one is given.
    Returns the directories removed. Always safe: a deleted run directory only costs a rerun of that script.

    `memo` (a dict the caller keeps for the whole run) holds the text of each run directory, read once for all crates:
    the same directories were read again for every crate."""
    relpath = os.path.relpath(crate_dir, repo_root).replace(os.sep, "/")
    current_roots = {os.path.abspath(repo_root), os.path.realpath(repo_root)}
    removed = []
    if relpath == ".":
        return removed
    memo = {} if memo is None else memo
    # <target>/<profile>/build and <target>/<triple>/<profile>/build
    base = glob.escape(target_dir)
    build_roots = glob.glob(os.path.join(base, "*", "build")) + glob.glob(os.path.join(base, "*", "*", "build"))
    for build_root in sorted(build_roots):
        for run_dir in sorted(glob.glob(os.path.join(glob.escape(build_root), "*"))):
            if not os.path.isfile(os.path.join(run_dir, "output")):
                continue
            try:
                stamp = os.stat(os.path.join(run_dir, "output")).st_mtime_ns  # a rerun of the script rewrites `output`
            except OSError:
                continue
            if memo.get(run_dir, (None, None))[0] != stamp:
                memo[run_dir] = (stamp, list(_cached_text(run_dir)))
            hits = [
                (root, name, line, found)
                for name, text in memo[run_dir][1]
                for root, line, found in _other_checkout_hits(text, relpath, current_roots)
            ]
            if not hits:
                continue
            if _reusable_here(run_dir, hits, os.path.abspath(repo_root)):
                if kept is not None:
                    kept.append(run_dir)
                continue
            remove_run_dir(run_dir)
            memo.pop(run_dir, None)
            removed.append(run_dir)
    return removed


def remove_run_dir(run_dir):
    """Deletes a build script's run directory and its fingerprint."""
    build_root = os.path.dirname(run_dir)
    fingerprint = os.path.join(os.path.dirname(build_root), ".fingerprint", os.path.basename(run_dir))
    shutil.rmtree(run_dir, ignore_errors=True)
    shutil.rmtree(fingerprint, ignore_errors=True)


def target_dirs(crate_dir):
    """The cargo target directories a clippy run in `crate_dir` can reuse build output from."""
    dirs = []
    shared = os.environ.get("CARGO_TARGET_DIR")
    if shared:
        dirs.append(os.path.abspath(shared))
    dirs.append(os.path.join(crate_dir, "target"))
    return [d for d in dirs if os.path.isdir(d)]


def _resolve_repo_root(given_path):
    """Same hams_shared-redirect fix as check_pip_audit.py/check_cargo_deny.py."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """The fix above only ever redirects to ONE repo (hams_open) -- but all four daemon crates
    this docstring's own findings came from (hams_local_relay, hams_relay_bridge,
    hams_data_relay, hams_simulated_band) live under hams_com/daemons/, not hams_open. Unlike
    check_cargo_deny.py this one isn't a vacuous pass (hams_open's own ham_digital_modes crate
    is real and does get checked), but it was still silently never checking the four crates this
    gate's own docstring cites findings from. Same sibling-repo shape as the other fixed
    checkers."""
    repo_root = _resolve_repo_root(given_path)
    roots = [repo_root]
    # The sibling is the OTHER repository by identity, never a second checkout of this one (lint_exec.sibling_repo_root).
    sibling = lint_exec.sibling_repo_root(repo_root, accept=_has_odoo_module)
    if sibling:
        roots.append(sibling)
    return roots


def _has_odoo_module(directory):
    """True when a direct subdirectory of `directory` is an Odoo module (has __manifest__.py)."""
    return any(
        os.path.isfile(os.path.join(directory, d, "__manifest__.py"))
        for d in os.listdir(directory)
        if os.path.isdir(os.path.join(directory, d))
    )


def find_cargo_crates(repo_root):
    """Top-level crate directories only (a Cargo.toml with a [package] table) -- workspace-member
    sub-crates (e.g. hams_local_relay/ham_digital_modes) are checked automatically as part of
    their parent workspace's own `cargo clippy` run, so listing them separately would just
    duplicate work."""
    found = []
    for root, _dirs, filenames in tree_walk(repo_root, "Cargo.toml", IGNORE_DIR_NAMES, skip_hidden=True, untracked=True):
        # Dot-directories (".claude", ".git", a stray ".venv" not already named above, etc.) are
        # never real crate locations -- critically including ".claude/worktrees/<session>/", this
        # project's own standing convention for running concurrent bug-hunt dispatches in
        # isolated git worktrees INSIDE the repo root. Without this, a concurrently running
        # session's own worktree gets walked as if it were part of the real repo: confirmed live
        # while reviewing this exact file, finding the same daemon crates listed three times over
        # (the real repo plus two other real, concurrently active sessions' worktrees) -- wasted
        # work at best, a spurious finding against another session's unrelated in-progress,
        # uncommitted code at worst.
        if "Cargo.toml" not in filenames:
            continue
        if os.path.relpath(root, repo_root) in EXCLUDE_CRATE_RELPATHS:
            continue
        with open(os.path.join(root, "Cargo.toml")) as f:
            if "[package]" in f.read():
                found.append(root)
    return sorted(found)


def narrow_to_changed(crate_dirs, changed, label):
    """`crate_dirs` ((repo_root, crate_dir) pairs) without the crates `rust_impact.select_crates` skips for the changed paths; prints each skipped crate and how many were kept."""
    if changed is None:
        return crate_dirs
    selected, skipped = rust_impact.select_crates([crate for _root, crate in crate_dirs], changed)
    for crate, why in skipped:
        print(f"{label}: skipped {os.path.relpath(crate, os.path.dirname(os.path.dirname(crate)))}: {why}")
    print(f"{label}: {len(selected)} of {len(crate_dirs)} crates checked ({len(skipped)} skipped)")
    keep = set(selected)
    return [(root, crate) for root, crate in crate_dirs if crate in keep]


def main():
    positional, changed = rust_impact.split_argv(sys.argv[1:])
    if not positional:
        print("Usage: check_cargo_clippy.py <repo_root> [--changed-files PATH...]")
        sys.exit(1)

    crate_dirs = [
        (repo_root, crate_dir)
        for repo_root in _resolve_repo_roots(positional[0])
        for crate_dir in find_cargo_crates(repo_root)
    ]
    crate_dirs = narrow_to_changed(crate_dirs, changed, "clippy")
    if not crate_dirs:
        sys.exit(0)

    check = subprocess.run(["cargo", "clippy", "--version"], capture_output=True, text=True)
    if check.returncode != 0:
        print(
            "❌ cargo-clippy is not installed (rustup component add clippy) -- required by "
            "CODE_REVIEW_PROCESS.md's Rust code-quality gate."
        )
        sys.exit(1)

    # The persistent target directory is pruned to its disk budget under an exclusive lock before the first
    # build, and the lock is held until the last one has finished (lint_cache.cargo_session).
    with lint_cache.cargo_session(log=print) as session_target:
        if session_target:
            os.environ["CARGO_TARGET_DIR"] = session_target
        any_failed = _clippy_all(crate_dirs)
    sys.exit(1 if any_failed else 0)


# Crates whose own test code is also linted (`--all-targets`), and the features that put all of their code in
# the build. hams_local_relay was fixed clean 2026-11-07 (to-do 9f24e10d); before that the gate never looked at
# its `#[cfg(test)]` code or at the code behind `radioctl` / `shared_receiver`. Add a crate here once
# `cargo clippy --all-targets` is clean for it.
ALL_TARGETS_CRATES = {
    os.path.join("daemons", "hams_local_relay"): ["--features", "radioctl,shared_receiver"],
}


def _clippy_command(crate_dir):
    """The cargo clippy command line for `crate_dir` (all targets and features for ALL_TARGETS_CRATES)."""
    extra = []
    for rel, flags in ALL_TARGETS_CRATES.items():
        if os.path.normpath(crate_dir).endswith(os.sep + rel):
            extra = ["--all-targets"] + flags
    return ["cargo", "clippy"] + extra + ["--", "-W", "clippy::all"]


def _run_clippy(crate_dir):
    return subprocess.run(
        _clippy_command(crate_dir),
        capture_output=True,
        text=True,
        cwd=crate_dir,
    )


def _clippy_all(crate_dirs):
    any_failed = False
    scanned = {}
    for repo_root, crate_dir in crate_dirs:
        rel_path = os.path.relpath(crate_dir, repo_root)
        kept = []
        for target_dir in target_dirs(crate_dir):
            for removed in purge_stale_build_script_output(target_dir, repo_root, crate_dir, scanned, kept):
                print(f"note: removed build-script output cached from another checkout: {removed}")
        res = _run_clippy(crate_dir)
        if res.returncode != 0 and kept:
            # A script's private build tree (CMake's cache) kept because it was built from identical sources can still
            # make a rerun of that script fail; the old behaviour (purge it, build again) is the fallback.
            for run_dir in kept:
                print(f"note: build failed with build-script output kept from another checkout; removing it and building again: {run_dir}")
                remove_run_dir(run_dir)
                scanned.pop(run_dir, None)
            res = _run_clippy(crate_dir)
        # clippy's own exit code is 0 even for warnings unless -D is passed; this gate treats
        # any "warning:" line in stderr as a real finding, matching how -W clippy::all is meant
        # to surface style/correctness issues without hard-denying the whole build on every one.
        #
        # Real gap found 2026-09-06: a naive "warning:" substring match also catches Cargo's own
        # relay of a build script's `cargo:warning=...` output (printed as "warning: <pkg>@<ver>:
        # <message>", e.g. hams_local_relay's build.rs announcing how many Hamlib rig/rotator
        # models it baked in) -- a real, intentional, informational message, not a clippy
        # finding, and this gate was silently "failing" on it every single run. A genuine
        # clippy/rustc diagnostic always follows its own "warning: ..." line with a "  --> " source
        # pointer; a build-script relay line never does. Distinguishing on that, rather than
        # matching the pkg-name-prefix convention build.rs output happens to use today, since nothing
        # stops a real clippy warning from also mentioning a package name and version in its text.
        stderr_lines = res.stderr.splitlines()
        has_real_clippy_warning = any(
            line.startswith("warning:")
            and i + 1 < len(stderr_lines)
            and stderr_lines[i + 1].lstrip().startswith("-->")
            for i, line in enumerate(stderr_lines)
        )
        if has_real_clippy_warning:
            if not any_failed:
                print("❌ cargo-clippy findings:")
            any_failed = True
            print(f"-- {rel_path} --")
            print(res.stderr, end="")
        if res.returncode != 0:
            if not any_failed:
                print("❌ cargo-clippy findings:")
            any_failed = True
            print(f"-- {rel_path} (build error) --")
            if res.stdout:
                print(res.stdout, end="")
            print(res.stderr, end="")
    return any_failed


if __name__ == "__main__":
    main()
