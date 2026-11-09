#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `hams_lint` engine binary (tools/hams_lint, Rust): built on demand, keyed by its sources.

    hams_lint_bin.py path              build if needed, print the binary's path
    hams_lint_bin.py run ARG...        build if needed, then run `hams_lint ARG...` (its exit status)

The binary is stored under `<cache>/hams-lint/engine-bin/<sha256 of the crate's sources>/hams_lint`, so a
change to any source, the lock file or the rule file builds a new binary and a stale one is never used
(an "exists, so it is current" check let an old binary keep running after its sources changed). The build
goes to the lint Cargo target directory (`lint_cache.cargo_target_dir`), under an exclusive lock, and the
binary is copied out atomically, so concurrent runs and worktrees share one build per source state.
A failed build exits 2 ("could not check") with cargo's own output; it never falls back to anything.
"""

import hashlib
import os
import shutil
import subprocess
import sys

import lint_cache

CRATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hams_lint")
EXIT_NOT_CHECKED = 2


def source_files(crate_dir=CRATE_DIR):
    """Every file that decides the binary: Cargo.toml, Cargo.lock, src/ and rules/ (sorted, relative)."""
    found = []
    for name in ("Cargo.toml", "Cargo.lock"):
        if os.path.exists(os.path.join(crate_dir, name)):
            found.append(name)
    for sub in ("src", "rules"):
        for base, dirs, files in os.walk(os.path.join(crate_dir, sub)):
            dirs.sort()
            for name in sorted(files):
                found.append(os.path.relpath(os.path.join(base, name), crate_dir))
    return sorted(found)


def source_hash(crate_dir=CRATE_DIR):
    """SHA-256 over each source file's relative path and bytes."""
    digest = hashlib.sha256()
    for rel in source_files(crate_dir):
        digest.update(rel.encode() + b"\0")
        with open(os.path.join(crate_dir, rel), "rb") as handle:
            digest.update(handle.read())
        digest.update(b"\0")
    return digest.hexdigest()


def binary_path(crate_dir=CRATE_DIR, environ=None):
    """Where the binary for the current sources lives (it may not be built yet)."""
    base = os.path.join(lint_cache.cache_root(environ), "hams-lint", "engine-bin")
    return os.path.join(base, source_hash(crate_dir)[:32], "hams_lint")


def ensure_built(crate_dir=CRATE_DIR, environ=None, out=sys.stderr):
    """The path of a binary built from the current sources; builds it when missing. Raises RuntimeError
    with cargo's output when the build fails."""
    target = binary_path(crate_dir, environ)
    if os.path.exists(target):
        return target
    target_dir = lint_cache.cargo_target_dir("hams_lint", environ)
    os.makedirs(target_dir, exist_ok=True)
    with lint_cache.locked(os.path.join(target_dir, "hams_lint_build")):
        if os.path.exists(target):
            return target
        print(f"[*] building hams_lint ({os.path.basename(os.path.dirname(target))})", file=out)
        command = ["cargo", "build", "--release", "--quiet", "--target-dir", target_dir]
        done = subprocess.run(command, cwd=crate_dir, capture_output=True, text=True)
        built = os.path.join(target_dir, "release", "hams_lint")
        if done.returncode != 0 or not os.path.exists(built):
            raise RuntimeError(f"cargo build of {crate_dir} failed (exit {done.returncode}):\n{done.stdout}{done.stderr}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        partial = f"{target}.partial{os.getpid()}"
        shutil.copy2(built, partial)
        os.replace(partial, target)
    return target


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("path", "run"):
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return EXIT_NOT_CHECKED
    try:
        binary = ensure_built()
    except RuntimeError as error:
        print(f"[!] hams_lint_bin: {error}", file=sys.stderr)
        return EXIT_NOT_CHECKED
    if argv[0] == "path":
        print(binary)
        return 0
    sys.stdout.flush()
    return subprocess.run([binary] + argv[1:]).returncode


if __name__ == "__main__":
    sys.exit(main())
