#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
The relay's structural tests: the fast group of `cargo test` tests that read the relay's own sources and tables, as a lint step.

Three relay merges on 2026-11-08 turned relay main red with tests that no lint step runs (lint ran a pytest wrapper for daemons/hams_local_relay and never
its cargo tests): the demo_lock route table (#3474), the ELAD fixture compiled in with `include_str!` (#3502, `no_manual_is_compiled_into_the_relay`), and the
SNTP server (#3626), which left `sntp_service` out of docs/privilege_inventory.json and read local_api.json by a bare name (two scan tests failed until #3654).
This step runs those tests on every `--changed` run that touches the relay or a crate that depends on it, and in every combined run of the merge queue (it
runs the same `run_linters.py --changed`). The whole suite stays out of lint (it takes far longer); the nightly canary runs it (`gate_canary.py`).

    relay_structural.py REPO_ROOT              run the group (the lint step)
    relay_structural.py REPO_ROOT --list       print the tests the group selects, one per line

`STRUCTURAL_FILTERS` is the one place the group is named: each entry is a substring of a Rust test path (`module::test`), as `cargo test` filters work. Before it
runs anything the step lists the tests (`cargo test -- --list`) and FAILS when a filter matches no test, so a renamed or deleted module can never leave the group
quietly smaller. The build goes to `lint_cache.named_cargo_session("relay-structural")`: one warm Cargo directory per lint slot, in RAM when `/dev/shm` has room.
"""

import os
import re
import subprocess
import sys

import lint_cache
import rust_impact

RELAY_REL = os.path.join("daemons", "hams_local_relay")
FEATURES = "radioctl,shared_receiver"
BIN = "hams_local_relay"
SESSION_NAME = "relay-structural"
# The group. Each entry names the tests it exists for.
STRUCTURAL_FILTERS = (
    "exposure_tests::",  # what the relay exposes on the network and to which caller
    "path_inventory_scan_tests::",  # every file path the sources use goes through the data directory
    "key_material_scan_tests::",  # no key material is read or written outside its module
    "keying_scan_tests::",  # every transmit-keying call site goes through the guarded path
    "supervisor_scan_tests::",  # the supervisor's source rules
    "certificate_cull_source_scan_tests::",  # the certificate-cull source rules
    "demo_lock::",  # the demo-mode route table: every route classified
    "relay_manuals::",  # manuals manifest, and no manual compiled into the relay (`no_manual_is_compiled_into_the_relay`)
    "data_dir::",  # the data-directory rules (the bare-name scan that failed with #3626)
# Not listed: the ELAD source's own tests (`elad_source_tests`) are compiled only with the `elad` feature, which is off; the include_str! guard that
# failed with #3502 is `no_manual_is_compiled_into_the_relay` above. The privilege inventory is read by `key_material_scan_tests` and
# `path_inventory_scan_tests`, the two that failed with #3626.
)


# [@ANCHOR: relay_structural:watched_dirs]
def watched_dirs(repo_root):
    """The directories whose change makes the step run: the relay crate, every directory it depends on by path (transitively), and every crate in `repo_root`
    that depends on the relay by path (`shack_console`, `relay_signer` and the like, found by reading the manifests with `rust_impact`). Returns a sorted list of
    resolved absolute paths, [] when the repository has no relay crate. Reads manifests only. Full entry: tools/README.md."""
    relay = os.path.realpath(os.path.join(repo_root, RELAY_REL))
    if not os.path.isfile(os.path.join(relay, "Cargo.toml")):
        return []
    found, queue = {relay}, [relay]
    while queue:
        for dep in rust_impact.path_dependencies(queue.pop()) or ():
            if dep not in found and os.path.isfile(os.path.join(dep, "Cargo.toml")):
                found.add(dep)
                queue.append(dep)
    import check_cargo_clippy

    crates = check_cargo_clippy.find_cargo_crates(os.path.realpath(repo_root))
    selected, _skipped = rust_impact.select_crates(crates, [os.path.join(relay, "Cargo.toml")])
    found.update(os.path.realpath(path) for path in selected)
    return sorted(found)


# [@ANCHOR: relay_structural:cargo_command]
def cargo_command(filters=STRUCTURAL_FILTERS, list_only=False):
    """The `cargo test` command line of the group: `cargo test --locked --features <FEATURES> --bin hams_local_relay -- [--list] <filter>...`. Pure. Full entry: tools/README.md."""
    return (["cargo", "test", "--locked", "--features", FEATURES, "--bin", BIN, "--"] + (["--list"] if list_only else []) + list(filters))


# [@ANCHOR: relay_structural:listed_tests]
def listed_tests(text):
    """The test paths in the output of `cargo test -- --list` (lines of the form `module::test: test`). Pure. Full entry: tools/README.md."""
    return [line[: -len(": test")] for line in text.splitlines() if line.endswith(": test")]


# [@ANCHOR: relay_structural:unmatched_filters]
def unmatched_filters(tests, filters=STRUCTURAL_FILTERS):
    """The filters that select no test of `tests` (a filter is a substring, as in `cargo test`). A non-empty result is a group that shrank without anyone
    deciding it: the step fails. Pure. Full entry: tools/README.md."""
    return [pattern for pattern in filters if not any(pattern in name for name in tests)]


# [@ANCHOR: relay_structural:passed_count]
def passed_count(text):
    """The sum of the `N passed` counts of every `test result:` line of a `cargo test` output (0 when there is none). Pure. Full entry: tools/README.md."""
    return sum(int(match) for match in re.findall(r"test result: \w+\. (\d+) passed", text))


def _run(command, cwd, env):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)


def main(argv):
    positional = [arg for arg in argv if not arg.startswith("--")]
    if len(positional) != 1:
        print("usage: relay_structural.py REPO_ROOT [--list]", file=sys.stderr)
        return 2
    relay = os.path.join(positional[0], RELAY_REL)
    if not os.path.isfile(os.path.join(relay, "Cargo.toml")):
        print("relay structural tests: %s has no relay crate (%s): nothing to run here" % (positional[0], RELAY_REL))
        return 0
    if _run(["cargo", "--version"], relay, None).returncode != 0:
        print("❌ cargo is not installed: the relay's structural tests cannot run")
        return 1
    with lint_cache.named_cargo_session(SESSION_NAME, log=print) as target:
        env = dict(os.environ, CARGO_INCREMENTAL="0")
        if target:
            env["CARGO_TARGET_DIR"] = target
        listing = _run(cargo_command(filters=(), list_only=True), relay, env)  # every test of the binary: the filters are checked against it
        if listing.returncode != 0:
            print("❌ the relay's tests did not build:\n" + listing.stdout + listing.stderr)
            return 1
        tests = listed_tests(listing.stdout)
        if "--list" in argv:
            print("\n".join(name for name in tests if any(pattern in name for pattern in STRUCTURAL_FILTERS)))
            return 0
        missing = unmatched_filters(tests)
        if missing:
            print("❌ relay structural tests: these filters match no test, so the group shrank (a renamed or deleted module?): " + ", ".join(missing)
                  + "\nFix STRUCTURAL_FILTERS in tools/relay_structural.py.")
            return 1
        done = _run(cargo_command(), relay, env)
    chosen = [name for name in tests if any(pattern in name for pattern in STRUCTURAL_FILTERS)]
    if done.returncode != 0:
        print("❌ relay structural tests failed:\n" + done.stdout[-12000:] + done.stderr[-4000:])
        return 1
    print("relay structural tests: %d tests of %d filters passed (%d tests in the relay in all)" % (passed_count(done.stdout), len(STRUCTURAL_FILTERS), len(tests)))
    print("  selected: %d tests" % len(chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
