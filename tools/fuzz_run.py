#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Unattended fuzzing: cargo-fuzz (libFuzzer) targets and Hypothesis property tests with large example counts, a time budget per target, crash
minimization, stored artifacts and a to-do stub for every NEW crash. No agent turn until a finding needs judgment.

    fuzz_run.py TARGETS.toml [--out-dir DIR] [--only NAME[,NAME]] [--force] [--budget-total MINUTES] [--dry-run]

TARGETS.toml, one `[[target]]` table per target. Common keys: `name`, `kind` (`cargo-fuzz`, `libfuzzer` or `hypothesis`), `budget_minutes` (default 10).

    [[target]]                                  # cargo-fuzz
    name = "relay_frame"
    kind = "cargo-fuzz"
    dir = "~/workspace/tmp/job/hams_com/daemons/hams_local_relay"   # the crate that holds fuzz/ (a clean worktree, NOT a live main checkout)
    fuzz_target = "parse_frame"
    rss_mb = 2048                               # optional (default 2048)
    max_len = 4096                              # optional
    toolchain = "nightly"                       # optional: `cargo +nightly fuzz`

    [[target]]                                  # libFuzzer on a STABLE toolchain, no cargo-fuzz (RUSTC_BOOTSTRAP + sancov flags, a fuzz crate with libfuzzer-sys bins)
    name = "radiod_tlv"
    kind = "libfuzzer"
    dir = "~/workspace/tmp/job/hams_open/daemons/radiod_client/fuzz"   # the fuzz crate (Cargo.toml with the bins), a clean worktree
    bin = "tlv_parse"                           # the bin to build and run (`cargo build --offline --release --target x86_64-unknown-linux-gnu --bin`)

    [[target]]                                  # Hypothesis
    name = "callsign_props"
    kind = "hypothesis"
    root = "~/workspace/hams_com"
    cwd = "ham_com/tests"                       # optional, relative to root
    test = "test_callsign_validation_properties.py"   # pytest arguments (file or node ids), space separated
    examples = 20000                            # max_examples for every @given test (default 20000)

What runs: a `libfuzzer` target builds the bin and runs it directly (corpus, seeds, `-max_total_time` and `-artifact_prefix` as below) and minimizes a crash with libFuzzer's own
`-minimize_crash=1` (a worker inside one `ram_dirs.py run` job, so the build directory is removed afterwards). A cargo-fuzz target is `cargo fuzz run` with its build in a RAM target directory (`ram_dirs.py run`, removed afterwards), nice 10,
`-max_total_time` = the budget, the corpus in `<out-dir>/state/<name>/corpus` (the crate's own `fuzz/corpus/<target>` is read as a seed only)
and `-artifact_prefix` in `<out-dir>/state/<name>/artifacts`. A hypothesis target is pytest with `hypothesis_scale.py` (raised example count, no
deadline, failures stored in `<out-dir>/state/<name>/hypothesis-db`, nothing written to the checkout). Each is a named `agent_procs.py` job; a
Hypothesis job over its time budget is stopped by `agent_procs.py stop` (status `budget`, not a failure).

A crash is NEW when its sha256 (the crash file; for Hypothesis the falsifying example) is not in `<out-dir>/state/<name>/seen.json`. For each
new cargo-fuzz crash: `cargo fuzz tmin` (minimized input kept in `<out-dir>/state/<name>/min/<sha12>.min`, the original in `crashes/`), a
reproduction run for the stack, and a stub `<reports>/findings/fuzz-<name>-<sha12>.md` in the night_shift_todo format (repro command, minimized
input path, stack). Stubs are never committed: an agent turns each into a regression test, a fix and one PR.

Result `<out-dir>/<name>.json`: name, kind, status (`clean`, `known` (fails on an example already stubbed), `budget`, `crashes`, `unavailable` (cargo-fuzz or hypothesis missing), `error`, `timeout`), seconds,
new_crashes `[{sha, input, minimized, stub}]`, log. Exit 0 clean or unavailable, 1 a crash or error, 4 load HOLD or low memory (nothing ran).
Reference: tools/README.md "Overnight automation".
"""

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import overnight_common as common  # noqa: E402

DEFAULT_BUDGET_MINUTES = 10
BUILD_ALLOWANCE_SECONDS = 1800  # `-max_total_time` counts fuzzing only; a cold build comes on top
OUT_SUBDIR = "fuzz"
CRASH_PREFIXES = ("crash-", "oom-", "timeout-", "leak-", "slow-unit-")
LIVE_CHECKOUTS = ("hams_com", "hams_open", os.path.join("hams_open", "hams_shared"))
LIBFUZZER_TARGET = "x86_64-unknown-linux-gnu"
LIBFUZZER_RUSTFLAGS = ("-Cpasses=sancov-module -Cllvm-args=-sanitizer-coverage-level=4 -Cllvm-args=-sanitizer-coverage-inline-8bit-counters "
                       "-Cllvm-args=-sanitizer-coverage-pc-table -Cllvm-args=-sanitizer-coverage-trace-compares --cfg fuzzing -Cdebug-assertions -Coverflow-checks -Cdebuginfo=0")
STACK_START = re.compile(r"panicked at|ERROR: AddressSanitizer|ERROR: libFuzzer|deadly signal|stack overflow|Falsifying example|Failing test case|Traceback \(most recent")


def cargo_command(target):
    """`[cargo, +toolchain, fuzz]` honouring `$HAMS_FUZZ_CARGO` (tests point it at a fake)."""
    cargo = os.environ.get("HAMS_FUZZ_CARGO") or "cargo"
    return [cargo] + (["+" + target["toolchain"]] if target.get("toolchain") else []) + ["fuzz"]


# [@ANCHOR: fuzz_run:cargo_fuzz_available]
def cargo_fuzz_available(target):
    """True when `cargo fuzz --version` works (a short bounded probe, nothing is started)."""
    try:
        return subprocess.run(cargo_command(target) + ["--version"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# [@ANCHOR: fuzz_run:is_live_checkout]
def is_live_checkout(path, workspace=None):
    """True when `path` (symlinks resolved) is the main hams_com, hams_open or hams_shared checkout under `workspace` (default ~/workspace) or inside one
    that is not a worktree: a fuzz run builds there and writes `Cargo.lock`, so it belongs in a clean worktree."""
    workspace = workspace or os.path.expanduser("~/workspace")
    real = os.path.realpath(path)
    return any(real == os.path.realpath(os.path.join(workspace, live)) for live in LIVE_CHECKOUTS)


# [@ANCHOR: fuzz_run:state_paths]
def state_paths(out_dir, name):
    """The per-target directories under `<out_dir>/state/<slug(name)>`: corpus, artifacts, crashes, min, hypothesis-db, and the `seen.json` path."""
    base = os.path.join(out_dir, "state", common.slug(name))
    return {key: os.path.join(base, key) for key in ("corpus", "artifacts", "crashes", "min", "hypothesis-db")} | {"seen": os.path.join(base, "seen.json"), "base": base}


# [@ANCHOR: fuzz_run:new_crash_files]
def new_crash_files(artifact_dir, seen):
    """Sorted `(path, sha256)` of the crash files in `artifact_dir` (names starting crash-, oom-, timeout-, leak-, slow-unit-) whose hash is not in `seen`."""
    found = []
    try:
        names = sorted(os.listdir(artifact_dir))
    except OSError:
        return []
    for name in names:
        path = os.path.join(artifact_dir, name)
        if name.startswith(CRASH_PREFIXES) and os.path.isfile(path):
            digest = common.sha256_file(path)
            if digest not in seen:
                found.append((path, digest))
    return found


# [@ANCHOR: fuzz_run:extract_stack]
def extract_stack(text, limit=60):
    """The part of a reproduction log that is the failure: from the first line that looks like a panic, sanitizer error, deadly signal or
    Hypothesis falsifying example to the end, at most `limit` lines. The last `limit` lines when none matches. Pure."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if STACK_START.search(line):
            return "\n".join(lines[index:index + limit])
    return "\n".join(lines[-limit:])


# [@ANCHOR: fuzz_run:repo_of]
def repo_of(path):
    """`hams_com`, `hams_open` or `hams_shared` when `path` lies in a directory of that name (a job directory holds `hams_shared_wt`), else ""."""
    parts = os.path.realpath(path).split(os.sep)
    for part in reversed(parts):
        if part in ("hams_com", "hams_open", "hams_shared"):
            return part
        if part == "hams_shared_wt":
            return "hams_shared"
    return ""


# [@ANCHOR: fuzz_run:stub_text]
def stub_text(target, sha, repro, minimized, stack, when=None):
    """The night_shift_todo stub for one new crash: front matter (status open, repo, category bug, created_at), the title, the repro command, the minimized input
    path and the stack. Pure apart from the date."""
    when = when or time.strftime("%Y-%m-%d")
    where = target.get("dir") or target.get("root") or ""
    return (
        "---\nstatus: open\nblocked_on: \nclaimed_by:\nrepo: %s\ncategory: bug\ncreated_at: %s\n---\n\n"
        "# Fuzz finding: %s (%s) %s\n\n"
        "Origin: `fuzz_run.py` overnight run, target `%s` in `%s`. Written without a model; an agent turns it into a regression test, a root-cause fix and one PR "
        "(batch with other findings of the same module).\n\n"
        "## Reproduce\n`%s`\n\nMinimized input: `%s`\n\n## Stack\n```\n%s\n```\n\n"
        "## To do\n- Reproduce, reduce to a regression test (the minimized input is the test vector), fix at the root, one PR.\n"
        "- Delete this file in that PR. Do not edit the fuzz corpus in the repository.\n"
        % (repo_of(where), when, target["name"], target.get("kind", ""), sha[:12], target["name"], where, repro, minimized, stack.strip())
    )


def write_stub(findings_dir, target, sha, repro, minimized, stack):
    """Writes `<findings_dir>/fuzz-<name>-<sha12>.md` unless it exists; returns the path."""
    os.makedirs(findings_dir, exist_ok=True)
    path = os.path.join(findings_dir, "fuzz-%s-%s.md" % (common.slug(target["name"]), sha[:12]))
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(stub_text(target, sha, repro, minimized, stack))
    return path


def _ram_wrap(job, argv):
    return [sys.executable, os.path.join(HERE, "ram_dirs.py"), "run", job, "--", "nice", "-n", "10"] + argv


def _minimized_path(text, artifact_dir, crash, target_dir, started):
    """Where `cargo fuzz tmin` put the minimized input: the path it prints after `Minimized artifact:`, else the newest `minimized-from-*` in the
    artifact directory made since `started`, else the original crash."""
    found = re.search(r"Minimized artifact:\s*\n\s*(\S+)", text)
    if found:
        candidate = found.group(1)
        candidate = candidate if os.path.isabs(candidate) else os.path.join(target_dir, candidate)
        if os.path.isfile(candidate):
            return candidate
    try:
        newest = sorted((os.path.getmtime(os.path.join(artifact_dir, n)), n) for n in os.listdir(artifact_dir)
                        if n.startswith("minimized-from-") and os.path.getmtime(os.path.join(artifact_dir, n)) >= started - 1)
    except OSError:
        newest = []
    return os.path.join(artifact_dir, newest[-1][1]) if newest else crash


def _handle_cargo_crash(target, paths, crash, sha, findings_dir, started):
    """Minimize one new crash, reproduce it for the stack, store both and write the stub; returns the record for the result file."""
    name = common.slug(target["name"])
    os.makedirs(paths["crashes"], exist_ok=True)
    os.makedirs(paths["min"], exist_ok=True)
    stored = os.path.join(paths["crashes"], sha[:12] + ".crash")
    shutil.copyfile(crash, stored)
    base = cargo_command(target)
    tmin = common.run_job("fuzzmin-%s-%s" % (name, sha[:8]),
                          _ram_wrap("fuzzmin-%s" % name, base + ["tmin", target["fuzz_target"], crash, "--runs", str(target.get("tmin_runs", 5000))]),
                          cwd=target["dir"], timeout=1800 + BUILD_ALLOWANCE_SECONDS)
    minimized = _minimized_path(common.tail(tmin.log, 200), paths["artifacts"], crash, target["dir"], started)
    kept = os.path.join(paths["min"], sha[:12] + ".min")
    shutil.copyfile(minimized, kept)
    repro_argv = base + ["run", target["fuzz_target"], kept]
    repro = common.run_job("fuzzrepro-%s-%s" % (name, sha[:8]), _ram_wrap("fuzzrepro-%s" % name, repro_argv), cwd=target["dir"],
                           timeout=900 + BUILD_ALLOWANCE_SECONDS, env=dict(os.environ, RUST_BACKTRACE="1"))
    stack = extract_stack(common.tail(repro.log, 300))
    command = "cd %s && %s" % (target["dir"], " ".join(repro_argv))
    return {"sha": sha, "input": stored, "minimized": kept, "stub": write_stub(findings_dir, target, sha, command, kept, stack)}


# [@ANCHOR: fuzz_run:run_cargo_fuzz]
def run_cargo_fuzz(target, out_dir, findings_dir, dry_run=False):
    """One cargo-fuzz target; returns the result document. Refuses a live main checkout (`error`), reports `unavailable` when cargo-fuzz is missing."""
    name, started = common.slug(target["name"]), time.time()
    result = {"name": target["name"], "kind": "cargo-fuzz", "status": "error", "new_crashes": [], "seconds": 0, "log": ""}
    crate = target.get("dir", "")
    if not os.path.isdir(os.path.join(crate, "fuzz")):
        return dict(result, detail="%s has no fuzz/ directory" % crate)
    if is_live_checkout(crate):
        return dict(result, detail="%s is a live main checkout: point `dir` at a clean worktree (new_agent_worktree.py)" % crate)
    if not cargo_fuzz_available(target):
        return dict(result, status="unavailable", detail="cargo-fuzz is not installed (cargo install cargo-fuzz; it needs a nightly toolchain)")
    if dry_run:
        return dict(result, status="dry-run")
    paths = state_paths(out_dir, target["name"])
    for key in ("corpus", "artifacts"):
        os.makedirs(paths[key], exist_ok=True)
    seen = set(common.read_json(paths["seen"], []))
    budget = float(target.get("budget_minutes", DEFAULT_BUDGET_MINUTES))
    seeds = os.path.join(crate, "fuzz", "corpus", target["fuzz_target"])
    argv = cargo_command(target) + ["run", target["fuzz_target"], paths["corpus"]] + ([seeds] if os.path.isdir(seeds) else []) + [
        "--", "-max_total_time=%d" % int(budget * 60), "-rss_limit_mb=%d" % int(target.get("rss_mb", 2048)),
        "-artifact_prefix=%s/" % paths["artifacts"]] + (["-max_len=%d" % int(target["max_len"])] if target.get("max_len") else [])
    outcome = common.run_job("fuzz-" + name, _ram_wrap("fuzz-" + name, argv), cwd=crate, timeout=budget * 60 + BUILD_ALLOWANCE_SECONDS)
    found = new_crash_files(paths["artifacts"], seen)
    records = [_handle_cargo_crash(target, paths, path, sha, findings_dir, started) for path, sha in found]
    common.write_json(paths["seen"], sorted(seen | {sha for _, sha in found}))
    status = "crashes" if records else ("clean" if outcome.status in ("ok", "failed") and outcome.exit_code in (0, None, 77) else "error")
    if outcome.status == "timeout" and not records:
        status = "timeout"
    return dict(result, status=status, new_crashes=records, seconds=round(time.time() - started, 1), log=outcome.log)


def libfuzzer_environment(base=None):
    """The environment of a stable-toolchain libFuzzer build: RUSTC_BOOTSTRAP=1 and the sanitizer-coverage RUSTFLAGS (the method the 2026-11-09 fuzz round used)."""
    return dict(os.environ if base is None else base, RUSTC_BOOTSTRAP="1", RUSTFLAGS=LIBFUZZER_RUSTFLAGS)


# [@ANCHOR: fuzz_run:run_libfuzzer_worker]
def run_libfuzzer_worker(spec_path):
    """Worker mode, started inside `ram_dirs.py run` (so `CARGO_TARGET_DIR` is the job's own directory): build the bin, run it for the budget, minimize and reproduce every
    new crash with the binary itself, and write `{status, crashes: [{sha, input, minimized, stack, repro}], log}` to the spec's `worker_result`. Returns 0 when it wrote a result."""
    spec = common.read_json(spec_path)
    scratch = os.environ.get("CARGO_TARGET_DIR")
    if not spec or not scratch:
        return 2
    target, paths = spec["target"], spec["paths"]
    name, environ = common.slug(target["name"]), libfuzzer_environment()
    environ["CARGO_TARGET_DIR"] = scratch
    result = {"status": "error", "crashes": [], "log": ""}
    binary = os.path.join(scratch, LIBFUZZER_TARGET, "release", target["bin"])
    build = common.run_job("fuzzbuild-" + name, ["nice", "-n", "10", "cargo", "build", "--offline", "--release", "--target", LIBFUZZER_TARGET, "--bin", target["bin"]],
                           cwd=target["dir"], timeout=BUILD_ALLOWANCE_SECONDS, env=environ)
    result["log"] = build.log
    if build.status == "ok" and os.path.exists(binary):
        seeds = os.path.join(target["dir"], "corpus", target["bin"])
        budget = float(target.get("budget_minutes", DEFAULT_BUDGET_MINUTES))
        argv = ["nice", "-n", "10", binary, paths["corpus"]] + ([seeds] if os.path.isdir(seeds) else []) + [
            "-max_total_time=%d" % int(budget * 60), "-rss_limit_mb=%d" % int(target.get("rss_mb", 2048)), "-artifact_prefix=%s/" % paths["artifacts"]]
        if target.get("max_len"):
            argv.append("-max_len=%d" % int(target["max_len"]))
        run = common.run_job("fuzz-" + name, argv, cwd=target["dir"], timeout=budget * 60 + 600, env=environ)
        result["log"] = run.log
        clean = run.status in ("ok", "failed") and run.exit_code in (0, None, 77)
        result["status"] = "clean" if clean else ("timeout" if run.status == "timeout" else "error")
        os.makedirs(paths["crashes"], exist_ok=True)
        os.makedirs(paths["min"], exist_ok=True)
        for crash, sha in new_crash_files(paths["artifacts"], set(spec["seen"])):
            stored = os.path.join(paths["crashes"], sha[:12] + ".crash")
            shutil.copyfile(crash, stored)
            kept = os.path.join(paths["min"], sha[:12] + ".min")
            common.run_job("fuzzmin-%s-%s" % (name, sha[:8]), ["nice", "-n", "10", binary, "-minimize_crash=1", "-runs=%d" % int(target.get("tmin_runs", 5000)),
                                                               "-exact_artifact_path=" + kept, crash], cwd=target["dir"], timeout=1800, env=environ)
            if not os.path.isfile(kept):
                shutil.copyfile(crash, kept)  # minimization did not shrink it: the original is the input
            repro = common.run_job("fuzzrepro-%s-%s" % (name, sha[:8]), [binary, kept], cwd=target["dir"], timeout=600, env=dict(environ, RUST_BACKTRACE="1"))
            build_command = "RUSTC_BOOTSTRAP=1 RUSTFLAGS=%s cargo build --offline --release --target %s --bin %s" % (shlex.quote(LIBFUZZER_RUSTFLAGS), LIBFUZZER_TARGET, target["bin"])
            result["crashes"].append({"sha": sha, "input": stored, "minimized": kept, "stack": extract_stack(common.tail(repro.log, 300)),
                                      "repro": "cd %s && %s && target/%s/release/%s %s" % (target["dir"], build_command, LIBFUZZER_TARGET, target["bin"], kept)})
    common.write_json(spec["worker_result"], result)
    return 0


# [@ANCHOR: fuzz_run:run_libfuzzer]
def run_libfuzzer(target, out_dir, findings_dir, dry_run=False):
    """One `libfuzzer` target (stable toolchain, no cargo-fuzz); returns the result document. Refuses a live main checkout."""
    name, started = common.slug(target["name"]), time.time()
    result = {"name": target["name"], "kind": "libfuzzer", "status": "error", "new_crashes": [], "seconds": 0, "log": ""}
    crate = target.get("dir", "")
    if not os.path.isfile(os.path.join(crate, "Cargo.toml")) or not target.get("bin"):
        return dict(result, detail="`dir` must hold a Cargo.toml and `bin` must name the bin")
    if is_live_checkout(crate):
        return dict(result, detail="%s is a live main checkout: point `dir` at a clean worktree (new_agent_worktree.py)" % crate)
    if shutil.which("cargo") is None:
        return dict(result, status="unavailable", detail="cargo is not on PATH")
    if dry_run:
        return dict(result, status="dry-run")
    paths = state_paths(out_dir, target["name"])
    for key in ("corpus", "artifacts"):
        os.makedirs(paths[key], exist_ok=True)
    seen = set(common.read_json(paths["seen"], []))
    spec_path = os.path.join(paths["base"], "worker-spec.json")
    worker_result = os.path.join(paths["base"], "worker-result.json")
    if os.path.exists(worker_result):
        os.unlink(worker_result)
    common.write_json(spec_path, {"target": target, "paths": paths, "seen": sorted(seen), "worker_result": worker_result})
    budget = float(target.get("budget_minutes", DEFAULT_BUDGET_MINUTES))
    outcome = common.run_job("fuzzw-" + name, _ram_wrap("fuzzlib-" + name, [sys.executable, os.path.abspath(__file__), "--libfuzzer-worker", spec_path]),
                             timeout=budget * 60 + 3 * BUILD_ALLOWANCE_SECONDS)
    worker = common.read_json(worker_result)
    if not isinstance(worker, dict):
        return dict(result, status="timeout" if outcome.status == "timeout" else "error", detail="the worker wrote no result", log=outcome.log, seconds=round(time.time() - started, 1))
    records = [{"sha": crash["sha"], "input": crash["input"], "minimized": crash["minimized"],
                "stub": write_stub(findings_dir, target, crash["sha"], crash["repro"], crash["minimized"], crash["stack"])} for crash in worker["crashes"]]
    common.write_json(paths["seen"], sorted(seen | {crash["sha"] for crash in worker["crashes"]}))
    return dict(result, status="crashes" if records else worker["status"], new_crashes=records, seconds=round(time.time() - started, 1), log=worker.get("log", ""))


# [@ANCHOR: fuzz_run:falsifying_signature]
def falsifying_signature(text):
    """`(sha256, block)` of the first `Falsifying example` (newer Hypothesis: `Failing test case`) block in a pytest log (the block runs to the first blank line), or None. Two runs that fail on
    the same shrunk example give the same sha, so a re-run does not write a second stub."""
    found = re.search(r"(?:Falsifying example|Failing test case):.*?(?:\n\s*\n|\Z)", text, re.S)
    if not found:
        return None
    block = found.group(0).strip()
    return hashlib.sha256(re.sub(r"\s+", " ", block).encode()).hexdigest(), block


# [@ANCHOR: fuzz_run:run_hypothesis]
def run_hypothesis(target, out_dir, findings_dir, dry_run=False):
    """One Hypothesis target (pytest with `hypothesis_scale.py`); returns the result document."""
    name, started = common.slug(target["name"]), time.time()
    result = {"name": target["name"], "kind": "hypothesis", "status": "error", "new_crashes": [], "seconds": 0, "log": ""}
    root = target.get("root", "")
    cwd = os.path.join(root, target.get("cwd", "."))
    if not os.path.isdir(cwd):
        return dict(result, detail="%s is not a directory" % cwd)
    try:
        import hypothesis  # noqa: F401
    except ImportError:
        return dict(result, status="unavailable", detail="the hypothesis package is not installed")
    if dry_run:
        return dict(result, status="dry-run")
    paths = state_paths(out_dir, target["name"])
    os.makedirs(paths["hypothesis-db"], exist_ok=True)
    budget = float(target.get("budget_minutes", DEFAULT_BUDGET_MINUTES))
    examples = int(target.get("examples", 20000))
    environ = dict(os.environ, HAMS_HYPOTHESIS_EXAMPLES=str(examples), HAMS_HYPOTHESIS_DB=paths["hypothesis-db"], PYTHONDONTWRITEBYTECODE="1",
                   HYPOTHESIS_STORAGE_DIRECTORY=os.path.join(paths["base"], "hypothesis-storage"),
                   PYTHONPATH=HERE + os.pathsep + os.environ.get("PYTHONPATH", ""))
    pytest_args = ["-p", "hypothesis_scale", "-p", "no:cacheprovider", "-q", "-x"] + target["test"].split()
    argv = ["nice", "-n", "10", sys.executable, "-m", "pytest"] + pytest_args
    outcome = common.run_job("hyp-" + name, argv, cwd=cwd, timeout=budget * 60, env=environ)
    text = common.tail(outcome.log, 400)
    seen = set(common.read_json(paths["seen"], []))
    records, status = [], "clean"
    signature = falsifying_signature(text) if outcome.status == "failed" else None
    if signature:
        sha, block = signature
        if sha not in seen:
            os.makedirs(paths["min"], exist_ok=True)
            kept = os.path.join(paths["min"], sha[:12] + ".txt")
            with open(kept, "w", encoding="utf-8") as handle:
                handle.write(block + "\n")
            command = "cd %s && HAMS_HYPOTHESIS_EXAMPLES=%d HAMS_HYPOTHESIS_DB=%s PYTHONPATH=%s python3 -m pytest -p hypothesis_scale -p no:cacheprovider -x %s" % (
                cwd, examples, paths["hypothesis-db"], HERE, target["test"])
            records.append({"sha": sha, "input": kept, "minimized": kept,
                            "stub": write_stub(findings_dir, target, sha, command, kept, extract_stack(text))})
            common.write_json(paths["seen"], sorted(seen | {sha}))
        status = "crashes" if records else "known"  # a failure already stubbed is not reported again, and not hidden either
    elif outcome.status == "failed" and outcome.exit_code not in (0, None):
        status = "error"  # a test failure that is not a Hypothesis counterexample (an import error, a plain assertion): the log says which
    elif outcome.status == "timeout":
        status = "budget"  # the example count was not finished in the time budget and nothing failed: the normal end of a budgeted run
    elif outcome.status == "error":
        status = "error"
    return dict(result, status=status, new_crashes=records, seconds=round(time.time() - started, 1), log=outcome.log)


def run_target(target, out_dir, findings_dir, dry_run=False):
    kind = target.get("kind")
    if kind == "cargo-fuzz":
        document = run_cargo_fuzz(target, out_dir, findings_dir, dry_run)
    elif kind == "libfuzzer":
        document = run_libfuzzer(target, out_dir, findings_dir, dry_run)
    elif kind == "hypothesis":
        document = run_hypothesis(target, out_dir, findings_dir, dry_run)
    else:
        document = {"name": target["name"], "kind": kind, "status": "error", "new_crashes": [], "detail": "kind must be cargo-fuzz, libfuzzer or hypothesis"}
    document["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if document["status"] != "dry-run":
        common.write_json(os.path.join(out_dir, common.slug(target["name"]) + ".json"), document)
    return document


# [@ANCHOR: fuzz_run:summary_line]
def summary_line(documents):
    """`fuzz: 3 target(s): 2 clean, 1 crashes; new crashes 1` from the result documents."""
    counts = {}
    for document in documents:
        counts[document["status"]] = counts.get(document["status"], 0) + 1
    return "fuzz: %d target(s): %s; new crashes %d" % (
        len(documents), ", ".join("%d %s" % (n, s) for s, n in sorted(counts.items())) or "none", sum(len(d.get("new_crashes", [])) for d in documents))


# [@ANCHOR: fuzz_run:main]
def main(argv=None, load=common.read_load):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("targets", nargs="?")
    parser.add_argument("--libfuzzer-worker", help="internal: run one libfuzzer target from this spec file")
    parser.add_argument("--out-dir")
    parser.add_argument("--only")
    parser.add_argument("--budget-total", type=float, default=0.0, help="minutes: start no new target after this long (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    if options.libfuzzer_worker:
        return run_libfuzzer_worker(options.libfuzzer_worker)
    if not options.targets:
        parser.error("a targets file is required")
    reports = common.reports_dir()
    out_dir = options.out_dir or os.path.join(reports, OUT_SUBDIR)
    findings = os.path.join(reports, "findings")
    log_path = os.path.join(reports, "overnight.log")
    try:
        targets = common.load_targets(options.targets, required=("name", "kind"))
    except ValueError as error:
        print("fuzz_run: %s" % error, file=sys.stderr)
        return 2
    if options.only:
        wanted = set(options.only.split(","))
        targets = [t for t in targets if t["name"] in wanted]
    documents, started = [], time.time()
    for target in targets:
        reason = load().critical()
        if reason and not options.dry_run:
            common.log_line(log_path, "fuzz: stopped before %s: %s" % (target["name"], reason))
            print("fuzz_run: %s" % reason, file=sys.stderr)
            if not documents:
                return common.EXIT_NOT_IDLE
            break
        if options.budget_total and time.time() - started > options.budget_total * 60:
            break
        os.makedirs(out_dir, exist_ok=True)
        document = run_target(target, out_dir, findings, options.dry_run)
        documents.append(document)
        common.log_line(log_path, "fuzz: %s %s%s" % (target["name"], document["status"], " (%d new)" % len(document["new_crashes"]) if document["new_crashes"] else ""))
    line = summary_line(documents)
    common.log_line(log_path, line)
    print(line)
    return 1 if any(d["status"] in ("crashes", "error", "timeout") for d in documents) else 0


if __name__ == "__main__":
    sys.exit(main())
