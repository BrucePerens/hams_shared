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

import glob
import os
import re
import shutil
import subprocess
import sys

import lint_cache

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
    """The text of what a build script left in `run_dir`: its `output` (the cargo: lines) and
    every small text file under `out/` (the generated sources). Binary and huge files are skipped."""
    names = [os.path.join(run_dir, "output")]
    for root, _dirs, files in os.walk(os.path.join(run_dir, "out")):
        names.extend(os.path.join(root, f) for f in files)
    for name in names:
        try:
            if os.path.getsize(name) > _MAX_SCANNED_BYTES:
                continue
            with open(name, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        yield data.decode("utf-8", errors="replace")


def _names_another_checkout(text, crate_relpath, current_roots):
    """True when `text` holds an absolute path through `<some checkout>/<crate_relpath>` where
    the checkout is not one of `current_roots`."""
    pattern = re.compile("(/" + _PATH_CHARS + "*?)/" + re.escape(crate_relpath) + r"(?=/|$|[\s\"'])")
    return any(m.group(1) not in current_roots for m in pattern.finditer(text))


def purge_stale_build_script_output(target_dir, repo_root, crate_dir):
    """Deletes the cached run directory (and its fingerprint) of every build script under
    `target_dir` whose output names `crate_dir`'s path in a checkout other than `repo_root`, so
    cargo runs the script again here. Returns the directories removed. Always safe: a deleted run
    directory only costs a rerun of that script."""
    relpath = os.path.relpath(crate_dir, repo_root).replace(os.sep, "/")
    current_roots = {os.path.abspath(repo_root), os.path.realpath(repo_root)}
    removed = []
    if relpath == ".":
        return removed
    # <target>/<profile>/build and <target>/<triple>/<profile>/build
    base = glob.escape(target_dir)
    build_roots = glob.glob(os.path.join(base, "*", "build")) + glob.glob(os.path.join(base, "*", "*", "build"))
    for build_root in sorted(build_roots):
        for run_dir in sorted(glob.glob(os.path.join(glob.escape(build_root), "*"))):
            if not os.path.isfile(os.path.join(run_dir, "output")):
                continue
            if not any(_names_another_checkout(t, relpath, current_roots) for t in _cached_text(run_dir)):
                continue
            fingerprint = os.path.join(os.path.dirname(build_root), ".fingerprint", os.path.basename(run_dir))
            shutil.rmtree(run_dir, ignore_errors=True)
            shutil.rmtree(fingerprint, ignore_errors=True)
            removed.append(run_dir)
    return removed


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
    sibling_name = "hams_open" if os.path.basename(repo_root) != "hams_open" else "hams_com"
    sibling = os.path.abspath(os.path.join(repo_root, "..", sibling_name))
    if os.path.isdir(sibling) and any(
        os.path.isfile(os.path.join(sibling, d, "__manifest__.py"))
        for d in os.listdir(sibling)
        if os.path.isdir(os.path.join(sibling, d))
    ):
        roots.append(sibling)
    return roots


def find_cargo_crates(repo_root):
    """Top-level crate directories only (a Cargo.toml with a [package] table) -- workspace-member
    sub-crates (e.g. hams_local_relay/ham_digital_modes) are checked automatically as part of
    their parent workspace's own `cargo clippy` run, so listing them separately would just
    duplicate work."""
    found = []
    for root, dirs, filenames in os.walk(repo_root):
        # Dot-directories (".claude", ".git", a stray ".venv" not already named above, etc.) are
        # never real crate locations -- critically including ".claude/worktrees/<session>/", this
        # project's own standing convention for running concurrent bug-hunt dispatches in
        # isolated git worktrees INSIDE the repo root. Without this, a concurrently running
        # session's own worktree gets walked as if it were part of the real repo: confirmed live
        # while reviewing this exact file, finding the same daemon crates listed three times over
        # (the real repo plus two other real, concurrently active sessions' worktrees) -- wasted
        # work at best, a spurious finding against another session's unrelated in-progress,
        # uncommitted code at worst.
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES and not d.startswith(".")]
        if "Cargo.toml" not in filenames:
            continue
        if os.path.relpath(root, repo_root) in EXCLUDE_CRATE_RELPATHS:
            continue
        with open(os.path.join(root, "Cargo.toml")) as f:
            if "[package]" in f.read():
                found.append(root)
    return sorted(found)


def main():
    if len(sys.argv) < 2:
        print("Usage: check_cargo_clippy.py <repo_root>")
        sys.exit(1)

    crate_dirs = [
        (repo_root, crate_dir)
        for repo_root in _resolve_repo_roots(sys.argv[1])
        for crate_dir in find_cargo_crates(repo_root)
    ]
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


def _clippy_all(crate_dirs):
    any_failed = False
    for repo_root, crate_dir in crate_dirs:
        rel_path = os.path.relpath(crate_dir, repo_root)
        for target_dir in target_dirs(crate_dir):
            for removed in purge_stale_build_script_output(target_dir, repo_root, crate_dir):
                print(f"note: removed build-script output cached from another checkout: {removed}")
        res = subprocess.run(
            ["cargo", "clippy", "--", "-W", "clippy::all"],
            capture_output=True,
            text=True,
            cwd=crate_dir,
        )
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
