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
It is built and run from a copy of the checkout at a fixed path (`lint_cache.work_checkout`): cargo hashes absolute paths into its fingerprints, so the relay was recompiled for every worktree.
"""

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

import lint_cache
import lint_exec
import rust_impact
import warm_build

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


# Paths outside the relay crate that the group's tests read (the privilege inventory and the local API table under docs/, the CA's public certificates).
EXTRA_INPUTS = ("docs", os.path.join("daemons", "relay_ca"))


# [@ANCHOR: relay_structural:input_key]
def input_key(repo_root, environ=None):
    """The reuse key of the group's last pass: a SHA-256 of the git index entries (mode, blob hash, path) of every watched directory and of EXTRA_INPUTS, of
    `rustc -vV`, of STRUCTURAL_FILTERS and FEATURES and of this file. None (never reuse) when any of those directories has an uncommitted change or untracked
    file, or git or rustc cannot be read. Full entry: tools/README.md."""
    digest = hashlib.sha256()
    here = os.path.realpath(__file__)
    with open(here, "rb") as handle:
        digest.update(handle.read())
    digest.update(repr((STRUCTURAL_FILTERS, FEATURES)).encode())
    rustc = _run(["rustc", "-vV"], repo_root, environ)
    if rustc.returncode != 0:
        return None
    digest.update(rustc.stdout.encode())
    paths = set(watched_dirs(repo_root)) | {os.path.realpath(os.path.join(repo_root, extra)) for extra in EXTRA_INPUTS}
    for path in sorted(path for path in paths if os.path.isdir(path)):
        dirty = _run(["git", "status", "--porcelain", "--", "."], path, environ)
        listing = _run(["git", "ls-files", "-s", "--", "."], path, environ)
        if dirty.returncode != 0 or dirty.stdout.strip() or listing.returncode != 0:
            return None
        digest.update(path.encode() + b"\0" + listing.stdout.encode())
    return digest.hexdigest()


def _stamp_path(key, environ=None):
    environ = os.environ if environ is None else environ
    return os.path.join(lint_cache.cache_root(environ), "hams-relay-structural", key)


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
    mount = next((arg.split("=", 1)[1] for arg in argv if arg.startswith("--overlay-mnt=")), None)
    if mount:
        return _run_in_overlay(argv, positional[0], mount)
    key = None if "--list" in argv or os.environ.get("HAMS_LINT_NO_CACHE") == "1" else input_key(positional[0])
    if key and os.path.exists(_stamp_path(key)):
        print("relay structural tests: reused the recorded pass for identical relay inputs and toolchain (key %s)" % key[:12])
        return 0
    code = None if "--list" in argv else _overlay_group(positional[0])
    if code is None:
        try:
            code = _run_group(argv, positional[0], relay)
        except lint_cache.LockTimeout as error:
            print("❌ relay structural tests did not run: %s\nAnother lint run holds the relay build directory; rerun when the box is quieter." % error)
            return 1
    if code == 0 and key and "--list" not in argv:
        _record_pass(key)
    return code


def _builds_main(repo_root):
    """True when `repo_root` is a clean checkout of exactly `origin/main`: the one kind of run whose build directory is worth keeping as a warm layer."""
    head = _run(["git", "rev-parse", "HEAD", "origin/main"], repo_root, None)
    dirty = _run(["git", "status", "--porcelain"], repo_root, None)
    shas = head.stdout.split()
    return head.returncode == 0 and dirty.returncode == 0 and not dirty.stdout.strip() and len(shas) == 2 and shas[0] == shas[1]


def _overlay_group(repo_root):
    """Run the group in a `warm_build` overlay over the warm layers of main, parallel with other runs (no shared lock). The exit code, or None to use the
    single shared directory as before (overlay off or not available, an explicit CARGO_TARGET_DIR, `HAMS_LINT_NO_CACHE=1`, or the mount failed)."""
    environ = os.environ
    if environ.get("CARGO_TARGET_DIR") or environ.get("HAMS_LINT_NO_CACHE") == "1" or environ.get("HAMS_LINT_RELAY_OVERLAY") == "0":
        return None
    base = lint_cache.cargo_base(environ)
    if environ.get("HAMS_LINT_RELAY_OVERLAY") != "1" and not lint_cache.in_ram(base, environ):
        return None
    os.makedirs(base, exist_ok=True)
    if lint_cache.ram_dirs and lint_cache.in_ram(base, environ):
        lint_cache.ram_dirs.prune_lint_cache(base, environ=environ)
    if not warm_build.available(base, environ):
        return None
    key = "relay" + hashlib.sha256(("%s|%s|%s" % (lint_exec.repo_name(repo_root), lint_cache.toolchain_id(environ), FEATURES)).encode()).hexdigest()[:12]
    promote = _builds_main(repo_root)
    if not promote and not warm_build.current_stack(base, key):
        _bootstrap_base(base, key, repo_root, environ)
    return _overlay_run(base, key, repo_root, environ, promote)


def _overlay_run(base, key, repo_root, environ, promote):
    with warm_build.Run(base, key, promote) as run:
        done = subprocess.run(run.argv([sys.executable, os.path.abspath(__file__), repo_root, "--overlay-mnt=" + run.mount]), env=environ)
        if done.returncode == warm_build.WARM_MOUNT_FAILED:
            return None
        run.passed = done.returncode == 0
        return done.returncode


def _bootstrap_base(base, key, repo_root, environ):
    """No warm base exists and this run is not main: build the base from `origin/main` first (a detached worktree of it, beside the sibling repositories of
    `repo_root`), under one exclusive lock so that runs arriving together build it once, and never from this run's own tree. A failure leaves no base and the run goes on cold."""
    sha = _run(["git", "rev-parse", "--verify", "origin/main"], repo_root, None)
    if sha.returncode != 0:
        return
    with open(os.path.join(base, key + ".bootstrap.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if warm_build.current_stack(base, key):
            return  # another run built it while this one waited
        parent = os.path.dirname(os.path.abspath(repo_root))
        scratch = tempfile.mkdtemp(prefix="hams-relay-base-", dir=os.path.dirname(os.path.abspath(base)) if lint_cache.in_ram(base, environ) else None)
        main_root = os.path.join(scratch, os.path.basename(os.path.abspath(repo_root)))
        try:
            if _run(["git", "worktree", "add", "--detach", main_root, sha.stdout.strip()], repo_root, None).returncode != 0:
                return
            for name in os.listdir(parent):  # the sibling repositories the tests reach (hams_open, hams_shared): the same links as the checkout has
                link = os.path.join(scratch, name)
                if not os.path.exists(link) and name != os.path.basename(os.path.abspath(repo_root)):
                    os.symlink(os.path.join(parent, name), link)
            print("relay structural tests: no warm base yet: building it from origin/main (%s)" % sha.stdout.strip()[:12])
            _overlay_run(base, key, main_root, environ, True)
        finally:
            _run(["git", "worktree", "remove", "--force", main_root], repo_root, None)
            shutil.rmtree(scratch, ignore_errors=True)


def _run_in_overlay(argv, repo_root, mount):
    """The group inside the overlay mounted at `mount`: the checkout is synced to `<mount>/work`, cargo builds in `<mount>/target` (made first: cargo creates it by
    renaming a temporary directory, which an overlay refuses)."""
    os.makedirs(os.path.join(mount, "target"), exist_ok=True)
    env = dict(os.environ, CARGO_INCREMENTAL="0", CARGO_TARGET_DIR=os.path.join(mount, "target"))
    work = lint_cache.work_checkout(repo_root, lint_exec.repo_name(repo_root) or "checkout", None, log=print, work=os.path.join(mount, "work"))
    relay = os.path.join(work or repo_root, RELAY_REL)
    return _build_and_test(argv, repo_root, relay, env, work)


def _record_pass(key):
    path = _stamp_path(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("passed\n")
    except OSError:
        pass  # a pass that cannot be recorded only costs the next run a rerun


def _run_group(argv, repo_root, relay):
    with lint_cache.named_cargo_session(SESSION_NAME, log=print) as target:
        env = dict(os.environ, CARGO_INCREMENTAL="0")
        if target:
            env["CARGO_TARGET_DIR"] = target
        # The build runs in a copy of the checkout at a fixed path (cargo hashes paths into its fingerprints: a different worktree every run
        # recompiled the relay each time). The tests read the same bytes there; the lock held by the session keeps other runs out of it.
        work = lint_cache.work_checkout(repo_root, lint_exec.repo_name(repo_root) or "checkout", target, log=print)
        if work:
            relay = os.path.join(work, RELAY_REL)
        return _build_and_test(argv, repo_root, relay, env, work)


def _build_and_test(argv, repo_root, relay, env, work):
    """List the relay's tests, check the filters against them and run the group with `env`; `work` is the synced copy the build runs in (None: the checkout itself)."""
    shown = (lambda text: text.replace(work, os.path.abspath(repo_root))) if work else (lambda text: text)
    listing = _run(cargo_command(filters=(), list_only=True), relay, env)  # every test of the binary: the filters are checked against it
    if listing.returncode != 0:
        print("❌ the relay's tests did not build:\n" + shown(listing.stdout + listing.stderr))
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
        print("❌ relay structural tests failed:\n" + shown(done.stdout[-12000:] + done.stderr[-4000:]))
        return 1
    print("relay structural tests: %d tests of %d filters passed (%d tests in the relay in all)" % (passed_count(done.stdout), len(STRUCTURAL_FILTERS), len(tests)))
    print("  selected: %d tests" % len(chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
