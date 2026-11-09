#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Which Rust crates a `--changed` lint run must check: the crates with a changed file and the crates that depend on one by path.

`check_cargo_clippy.py` and `check_cargo_fmt.py` used to check every crate of both repositories whenever any `.rs`, `.toml` or `.lock` file
changed (17 crates one after another: 139 s on average, 1,329 s at worst, the longest step of 25 of 142 measured runs, 2026-11-08). Under
`--changed` the runner now hands them the changed paths (`--changed-files PATH...`) and they skip a crate whose directory, and the directories of
every crate it depends on by `path = "..."`, hold no changed file. The same premise as the step selection of `--changed`: the base commit passes, so
a crate whose inputs did not change still passes.

Conservative: anything it cannot decide checks every crate (a manifest that does not parse, a changed Rust file or toolchain pin outside every crate, a
changed path list too long to pass, no list at all). Every skipped crate is printed by the step, so a skip is never silent.
"""

import os

try:
    import tomllib
except ImportError:  # Python before 3.11: every crate is checked
    tomllib = None

CHANGED_FLAG = "--changed-files"
MAX_PATHS = 5000  # run_linters passes no list when more files than this changed: the whole tree changed, check everything
DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")


def _under(path, directory):
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


# [@ANCHOR: rust_impact:split_argv]
def split_argv(argv):
    """(positional arguments, changed paths or None) from a checker's command line: everything after `--changed-files` is a changed path (made absolute and
    resolved), everything before it is positional. None means no list was given: check every crate. Pure. Full entry: tools/README.md."""
    args = list(argv)
    if CHANGED_FLAG not in args:
        return args, None
    at = args.index(CHANGED_FLAG)
    return args[:at], [os.path.realpath(path) for path in args[at + 1:]]


# [@ANCHOR: rust_impact:path_dependencies]
def path_dependencies(crate_dir):
    """The directories `crate_dir`'s Cargo.toml names with `path = ...` (dependencies, dev-dependencies, build-dependencies, their `[target.*]` forms,
    `[workspace.dependencies]` and `[patch.*]`), as resolved absolute paths. None when the manifest cannot be read or parsed (the caller then checks the
    crate). Reads one file. Full entry: tools/README.md."""
    if tomllib is None:
        return None
    try:
        with open(os.path.join(crate_dir, "Cargo.toml"), "rb") as handle:
            manifest = tomllib.load(handle)
    except (OSError, ValueError):
        return None
    tables = [manifest.get(name, {}) for name in DEPENDENCY_TABLES]
    tables += [manifest.get("workspace", {}).get("dependencies", {})]
    for target in manifest.get("target", {}).values():
        tables += [target.get(name, {}) for name in DEPENDENCY_TABLES]
    for patched in manifest.get("patch", {}).values():
        tables.append(patched)
    found = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        for spec in table.values():
            if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                found.append(os.path.realpath(os.path.join(crate_dir, spec["path"])))
    return found


def _is_global_trigger(path):
    name = os.path.basename(path)
    return name.startswith("rust-toolchain") or ".cargo" in path.split(os.sep)


# [@ANCHOR: rust_impact:select_crates]
def select_crates(crate_dirs, changed):
    """(selected, skipped): `crate_dirs` is a list of crate directories, `changed` the resolved changed paths or None. Returns two lists of the same
    directories; `skipped` is a list of (crate_dir, reason). A crate is selected when a changed path lies under it or under any directory it depends
    on by path (followed through those directories' own manifests), when its manifest cannot be read, or when a changed `.rs` file, `rust-toolchain*`
    or `.cargo/` file lies outside every crate (a file `include!`d from elsewhere, a pin of the toolchain): then every crate is selected. `changed`
    None selects every crate. A path-dependency directory that is not itself a listed crate still counts. Reads manifests only. Full entry: tools/README.md."""
    crates = [os.path.realpath(path) for path in crate_dirs]
    if changed is None:
        return list(crate_dirs), []
    owned = [path for path in changed if any(_under(path, crate) for crate in crates)]
    outside = [path for path in changed if path not in owned]
    trigger = next((path for path in outside if path.endswith(".rs") or _is_global_trigger(path)), None)
    if trigger is not None:
        return list(crate_dirs), []
    memo = {}

    def closure(directory):
        """Every directory `directory` depends on, itself included; None when a manifest cannot be read."""
        if directory in memo:
            return memo[directory]
        memo[directory] = {directory}  # a cycle ends here
        deps = path_dependencies(directory) if os.path.isfile(os.path.join(directory, "Cargo.toml")) else []
        if deps is None:
            memo[directory] = None
            return None
        result = {directory}
        for dep in deps:
            inner = closure(dep)
            if inner is None:
                memo[directory] = None
                return None
            result |= inner
        memo[directory] = result
        return result

    selected, skipped = [], []
    for given, crate in zip(crate_dirs, crates):
        reach = closure(crate)
        if reach is None:
            selected.append(given)
            continue
        hit = next((path for path in changed if any(_under(path, directory) for directory in reach)), None)
        if hit is None:
            skipped.append((given, "no changed file in the crate or in a crate it depends on"))
        else:
            selected.append(given)
    return selected, skipped
