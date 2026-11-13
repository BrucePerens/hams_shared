#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Odoo DevSecOps Linter Orchestrator
----------------------------------
Replaces the legacy bash script to enforce the structural integrity
of the repository, including child-directory detection and anti-symlink rules.
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys
import subprocess
import time

import batch_queue
import check_target
import lint_cache
import lint_exec
import lint_trace_cache
import lint_batch
import relay_structural
import pytest_impact
import rust_impact
import lint_layout
import lint_lock
import lint_remote
import lint_test_durations
import atomic_io


def _resolve_repo_root(given_path):
    """`dir_path` (below) resolves to the hams_shared directory itself when this script is
    invoked from hams_shared directly -- AGENTS.md documents that as a third, intentionally valid
    invocation root (alongside hams_com and hams_open), but hams_shared has no Odoo addon modules
    of its own, so anything that needs a real repo root to scan (module discovery, `targets`, the
    sibling-repo/addons-path resolution used by pre_flight_check.py) must not use `dir_path`
    as-is in that case. check_burn_list.py scans only 3 files
    via this exact invocation without the redirect, versus 78+ real files against a real repo root --
    every `targets`-based step (7-18, 21) was silently scanning almost nothing. Same redirect as the 9
    individual checker scripts that handle this same case (see
    docs/proposals/LINTER_POLICY_REVISIT.md) -- detect the hams_shared case by name and redirect
    to its real parent repo. Deliberately NOT used for steps 27-29 (the tools/scripts/daemons
    test-suite runners) or the anti-symlink/child-directory structural checks just below: those
    need `dir_path` to genuinely BE hams_shared (or resolve through a symlink to it) to find their
    own tooling correctly, and redirecting it there would scan the wrong daemons/ tree entirely
    (hams_open has its own separate, real daemons/ directory, unrelated to hams_shared's)."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    # A worktree of hams_shared has any name. It is recognised by its layout: hams_shared's `tools`
    # is a real directory next to eslint.config.js (the other two repositories link `tools` to it).
    if (
        os.path.isdir(os.path.join(given_path, "tools"))
        and not os.path.islink(os.path.join(given_path, "tools"))
        and os.path.isfile(os.path.join(given_path, "eslint.config.js"))
        and not os.path.isfile(os.path.join(given_path, "zero_sudo", "__manifest__.py"))
    ):
        main_checkout = lint_exec.main_worktree_of(given_path)
        if main_checkout:
            return os.path.dirname(main_checkout)
    return given_path


# Heap option given to ESLint's node process (see eslint_node_env).
ESLINT_NODE_OPTIONS = "--max-old-space-size=4096"


# [@ANCHOR: run_linters:eslint_node_env]
def eslint_node_env(base_env):
    """A copy of `base_env` in which NODE_OPTIONS carries a heap size for ESLint.

    The type-aware ESLint project makes node need more than its default heap (about 2 GB): a --changed run
    died with "JavaScript heap out of memory" (2026-11-02). `setdefault` did not help when the shell exported
    NODE_OPTIONS as an empty string (the agents' shells did, 2026-11-03), so an unset, empty or
    whitespace-only value is treated alike and replaced by ESLINT_NODE_OPTIONS; a real value is the caller's
    choice and is kept. `base_env` is not changed."""
    env = dict(base_env)
    if not env.get("NODE_OPTIONS", "").strip():
        env["NODE_OPTIONS"] = ESLINT_NODE_OPTIONS
    return env


# Steps that also scan a hams_shared worktree's own files (see shared_worktree_root). Only the checkers whose findings
# lie in hams_shared's tools: the others are module checkers (init imports, test tags, ...) that judge a tree of Odoo
# modules and flag hams_shared's `tools/odoo_type_stubs` and the like.
SHARED_WORKTREE_STEPS = frozenset({"flake8", "check_absolute_paths", "check_shebang"})


# [@ANCHOR: run_linters:shared_worktree_root]
def shared_worktree_root(dir_path, repo_root):
    """`dir_path` when it is a git worktree of hams_shared, else None.

    A hams_shared worktree resolves `repo_root` to the hams_open main checkout (`_resolve_repo_root`), so
    flake8, the absolute-path and the shebang checker scanned that checkout and never the worktree: a
    branch's own findings were not seen, and whatever the main checkout held (stale, or another session's
    edits) failed the run on files the branch did not touch (for example 13 flake8 hits and 40
    absolute-path hits, among them an undefined name the branch had already fixed). The worktree is
    therefore a scan root of its own for those steps (SHARED_WORKTREE_STEPS). Started in the main checkout
    (or through the `hams_shared` link into it) it is already under `repo_root`: nothing is added."""
    real = os.path.realpath(dir_path)
    root = os.path.realpath(repo_root)
    if real == root or real.startswith(root + os.sep):
        return None
    if (
        os.path.isdir(os.path.join(dir_path, "tools"))
        and not os.path.islink(os.path.join(dir_path, "tools"))
        and os.path.isfile(os.path.join(dir_path, "eslint.config.js"))
        and not os.path.isfile(os.path.join(dir_path, "zero_sudo", "__manifest__.py"))
    ):
        return real
    return None


def _resolve_main_checkout_root(repo_root):
    """Resolves the REAL main checkout of `repo_root`'s own git repository, so the sibling-repo
    paths computed from it (hams_open from hams_com, or vice versa) are correct even when
    `repo_root` is itself a git worktree nested under `.claude/worktrees/<name>/`. Naively
    joining `repo_root` with `".."` lands inside that nested worktree directory instead of back
    at the real `~/workspace/` level -- confirmed live 2026-09-28: every module that depends on
    the other repo (zero_sudo, compliance, user_websites, and everything else that lives in
    hams_open, not hams_com) silently failed pre-flight in a worktree, deterministically, on
    every run, not as a flake -- because `community_dir` above was computed from `repo_root`
    directly and always resolved to a directory that doesn't exist. Same technique
    fix_worktree_symlinks.py (hams_com repo root) already uses for the identical class of
    problem with checked-in cross-repo symlinks: ask git itself where the shared object database
    lives (`git rev-parse --git-common-dir`), which resolves correctly regardless of how deeply
    the current worktree is nested, then take its parent directory as the true main checkout
    root. Falls back to `repo_root` unchanged if git can't answer (not actually a git repository,
    or git isn't on PATH) -- preserves the old, pre-fix behavior for that edge case rather than
    raising, since every call site already has its own reasonable fallback for a missing
    sibling."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return repo_root
    git_common_dir = result.stdout.strip()
    if not git_common_dir:
        return repo_root
    if not os.path.isabs(git_common_dir):
        git_common_dir = os.path.join(repo_root, git_common_dir)
    return os.path.dirname(os.path.normpath(git_common_dir))


def _sibling_checkout(repo_root, main_checkout_root, name):
    """Directory of the sibling repo `name` (hams_com / hams_open). A checkout or worktree that
    sits in a workspace directory next to a `name` directory uses THAT one (so a worktree set
    `<ws>/hams_open` + `<ws>/hams_com` is linted against itself, not against whatever the main
    checkouts happen to be at); otherwise the sibling of the real main checkout, as before."""
    beside = os.path.join(os.path.dirname(repo_root), name)
    if os.path.isdir(beside) and os.path.realpath(beside) != os.path.realpath(repo_root):
        return os.path.abspath(beside)
    return os.path.abspath(os.path.join(main_checkout_root, "..", name))


def _default_inputs(dir_path, sibling_dir, shared_dir=None, repo_root=None):
    """Roots and suffixes a step reads when it declares nothing narrower: both repos and the real
    hams_shared directory, any file.

    hams_shared is a root of its own because the repositories reach it through symlinks (`docs`,
    `agents`, `tools`, `AGENTS.md`): `git diff` reports a change to a skill or a guide under the
    hams_shared path, which is not below either repository root, so a step rooted only at the two
    repositories never saw it and a change there was reported as touching nothing."""
    roots = [os.path.realpath(dir_path)]
    if repo_root and os.path.isdir(repo_root) and os.path.realpath(repo_root) not in roots:
        roots.append(os.path.realpath(repo_root))
    if sibling_dir and os.path.isdir(sibling_dir):
        roots.append(os.path.realpath(sibling_dir))
    if shared_dir and os.path.isdir(shared_dir):
        shared_real = os.path.realpath(shared_dir)
        if not any(shared_real == root or shared_real.startswith(root + os.sep) for root in roots):
            roots.append(shared_real)
    return tuple(roots), None


def _resolve_module_path(mod, repo_root, community_dir):
    """Resolve a named module to its real manifest directory: `repo_root` first, falling back to
    `community_dir` (the sibling repo) if the module has no `__manifest__.py` there. Returns the
    resolved path, or `None` if it exists in neither. Shared by main()'s own pre-flight-check
    loop and its `targets` computation for `flake8`/`check_burn_list`/etc -- see the comment at
    that loop's own call site for the real bug this sharing fixes (those two used to compute the
    module's path two different, divergent ways)."""
    mod_path = os.path.join(repo_root, mod)
    if os.path.isfile(os.path.join(mod_path, "__manifest__.py")):
        return mod_path
    if community_dir:
        comm_mod_path = os.path.join(community_dir, mod)
        if os.path.isfile(os.path.join(comm_mod_path, "__manifest__.py")):
            return comm_mod_path
    return None


# Per-step scheduling metadata, keyed by the step-name prefix before any "[...]" suffix.
#   lock       steps with the same lock never overlap (one cargo target dir, one clippy cache).
#   suffixes   the file types the checker reads (None = any file); decides what `--changed`
#              runs and what invalidates a cached pass. Kept to the types read in the
#              checker's own source; a checker that reads anything unusual stays at None.
#   cacheable  False when the verdict depends on more than repository files (network
#              advisory databases, the toolchain's own state).
_PY = frozenset({".py"})
_JS = frozenset({".js", ".mjs", ".cjs"})
_XML = frozenset({".xml"})
_RS = frozenset({".rs", ".toml", ".lock"})
STEP_META = {
    "flake8": {"suffixes": frozenset({".py", ".cfg", ".ini", ".toml"})},
    # Read from each checker's own file filter (os.walk plus endswith); a checker whose filter is
    # not one short list (manifest dependencies, shebang, absolute paths) stays at None so any
    # changed file runs it.
    "check_burn_list": {"suffixes": frozenset({".py", ".xml", ".js", ".csv", ".html"})},
    "verify_anchors": {
        # ".toml" and ".lock": lint_stamp_gate.py demands verify_anchors for a branch that changes only
        # Cargo.toml/Cargo.lock, so the runner has to select it too (2026-11-03: a dependency-only
        # branch could not get a passing stamp, the step was always skipped).
        "suffixes": frozenset(
            {".py", ".js", ".xml", ".html", ".rs", ".md", ".test", ".toml", ".lock"}
        )
    },
    "check_js_syntax": {"suffixes": _JS},
    # The signal guard hook must be registered in the repository's own .claude/settings.json (2026-11-09 incident).
    "check_claude_hook_registration": {"suffixes": frozenset({".json", ".py"})},
    # Backup encryption, stanza creation, key generation and secret-file overwrites need Bruce's recorded approval (2026-11-09).
    "check_backup_encryption_changes": {"suffixes": frozenset({".py", ".sh", ".service", ".timer", ".conf", ".md"})},
    "check_test_tags": {"suffixes": _PY},
    "check_rabbitmq_pool": {"suffixes": _PY},
    "check_init_imports": {"suffixes": _PY},
    "check_model_extension_collisions": {"suffixes": _PY},
    "check_self_writeable_field_tests": {"suffixes": _PY},
    "check_test_depends": {"suffixes": frozenset({".py"})},
    "check_registry_test_cr_usage": {"suffixes": _PY},
    "check_gdpr_erasure_uses_service_utility": {"suffixes": _PY},
    # night_shift_history.py check: the night_shift_history/ directory (one markdown file per entry) and its stub file.
    "check_night_shift_history": {"suffixes": frozenset({".md"})},
    # ADR 0114 enforcement: the relay's Rust sources, its installers and release scripts, the ADR and dependency_watch.json.
    "check_external_programs_documented": {"suffixes": frozenset({".rs", ".sh", ".ps1", ".md", ".json"})},
    # ADR 0113 opt-in gate: the paths of tools/anchor_doc_standard.txt (documentation and source) and the list itself.
    "check_anchor_docs_standard": {"suffixes": frozenset({".md", ".rs", ".py", ".js", ".mjs", ".txt"})},
    "check_int_float_overflow": {"suffixes": _PY},
    "check_float_isfinite": {"suffixes": _PY},
    # Reads every .py and .sh file of both repositories and its own known list (boundary_hazards_known.json).
    "check_boundary_hazards": {"suffixes": frozenset({".py", ".sh", ".json"})},
    # A secret written to ir.config_parameter or kept in a model field instead of the secret store (2026-11-09).
    "check_secret_storage": {"suffixes": frozenset({".py", ".xml", ".json"})},
    "check_untyped_utility_files": {
        "suffixes": frozenset({".py", ".ini", ".cfg", ".toml", ".pyi"})
    },
    "check_access_csv_group_order": {"suffixes": frozenset({".csv", ".xml", ".py"})},
    "check_xml_comment_double_hyphen": {"suffixes": _XML},
    "check_xml_attribute_position_comment": {"suffixes": _XML},
    "check_owl_templates": {"suffixes": frozenset({".xml", ".js"})},
    "check_scss_math_functions": {"suffixes": frozenset({".scss"})},
    "check_window_fetch_reassignment": {"suffixes": _JS},
    "check_js_test_hook_gating": {"suffixes": _JS},
    "check_minified_js_nested_templates": {"suffixes": frozenset({".js", ".py"})},
    "check_minified_js_syntax_errors": {"suffixes": frozenset({".js", ".py"})},
    "check_hoot_runner_coverage": {"suffixes": frozenset({".js", ".py"})},
    "check_async_lock_scope": {"suffixes": frozenset({".rs"})},
    "check_test_temp_dirs": {"suffixes": frozenset({".rs", ".py", ".json"})},
    "check_fake_network_listener": {"suffixes": frozenset({".rs", ".py"})},
    "check_function_test_anchors": {"suffixes": _PY},
    "check_private_by_default": {"suffixes": frozenset({".json", ".py"})},
    "check_doc_references": {"suffixes": frozenset({".md", ".py", ".json"})},
    "check_doc_references_shared": {"suffixes": frozenset({".md", ".py", ".json"})},
    "check_settings_secrets": {"suffixes": frozenset({".json", ".py"})},
    "check_js_function_test_anchors": {"suffixes": frozenset({".js", ".py"})},
    "check_rust_function_test_anchors": {
        "suffixes": frozenset({".rs", ".py", ".toml"}),
        "lock": "cargo",
    },
    "check_cargo_deny": {"suffixes": _RS, "lock": "cargo", "cacheable": False},
    "check_cargo_clippy": {"suffixes": _RS, "lock": "cargo"},
    "check_cargo_fmt": {"suffixes": _RS, "lock": "cargo"},
    "check_pip_audit": {
        "suffixes": frozenset({".txt"}),
        "cacheable": False,
    },
}


def default_remote_applies(options, environ):
    """True when a plain `run_linters.py --changed` (or a full run) uses `--remote auto` without being told
    (2026-11-03): the stamp-producing runs that agents start. Never for a selected run (--only/--skip, which
    never stamps), a listing, the remote side of a bundle, a run started by another run (tests), CI, a run
    that takes no lock, or when HAMS_LINT_REMOTE=local says so. A machine that is down costs seconds
    (the probe is bounded) and the run goes on here."""
    if environ.get("HAMS_LINT_REMOTE") == "local" or environ.get("CI") or environ.get("HAMS_LINT_NO_LOCK") == "1":
        return False
    if environ.get(lint_lock.HELD_ENV) or options.run_bundle or options.only or options.skip:
        return False
    if options.list_steps or options.list_json or options.lock_status or options.where:
        return False
    return True


# [@ANCHOR: run_linters:parse_args]
def _parse_args(argv):
    """`run_linters.py [MODULES] [options]`: MODULES is the old comma-separated module list."""
    parser = argparse.ArgumentParser(
        prog="run_linters.py",
        description=(
            "Run every repository linter. Steps run in parallel (-j), their output is "
            "printed in a fixed order, and a timing table shows where the time went."
        ),
    )
    parser.add_argument(
        "modules",
        nargs="?",
        default="",
        help="comma-separated modules to scope the per-module checkers to (default: all)",
    )
    # Half the CPUs for a lone run; with several lint slots each run takes its share of three quarters of the CPUs.
    default_jobs = int(os.environ.get("HAMS_LINT_JOBS", "0")) or lint_lock.jobs_per_slot(os.cpu_count() or 2, lint_lock.slot_count())
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=default_jobs,
        help="parallel steps (default: half the cores divided by the lint slots, or $HAMS_LINT_JOBS); 1 = sequential",
    )
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated step names (or name prefixes) to run; see --list",
    )
    parser.add_argument("--skip", default="", help="comma-separated step names to leave out")
    parser.add_argument("--list", dest="list_steps", action="store_true", help="print step names")
    parser.add_argument(
        "--list-json",
        dest="list_json",
        action="store_true",
        help="print every step as JSON (name, argv, cwd, lock, roots, suffixes) for tools/lint_step_inventory.py",
    )
    parser.add_argument(
        "--changed",
        nargs="?",
        const="origin/main",
        default=None,
        metavar="BASE",
        help=(
            "pre-merge mode: run only the steps whose inputs differ from BASE (default "
            "origin/main), scoped to the changed modules and the modules that depend on them"
        ),
    )
    parser.add_argument(
        "--cache",
        dest="cache",
        action="store_true",
        default=None,
        help="skip steps whose inputs are unchanged since their last passing run "
        "(default with --changed)",
    )
    parser.add_argument(
        "--no-cache",
        dest="cache",
        action="store_false",
        help="run every step even when its inputs are unchanged, and bypass the persistent lint caches "
        "(warm Cargo build directory, per-file result caches): a cold, from-scratch run",
    )
    parser.add_argument(
        "--timing-top",
        type=int,
        default=0,
        help="print only the N slowest steps in the timing table (default: all)",
    )
    parser.add_argument("--no-timing", dest="timing", action="store_false", default=True)
    parser.add_argument("--run-bundle", default="", help=argparse.SUPPRESS)
    parser.add_argument("--bundle-root", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument(
        "--results-json",
        default="",
        metavar="PATH",
        help="write each step's name, exit code and seconds, and the commit, to PATH",
    )
    parser.add_argument(
        "--remote",
        default="",
        metavar="auto|HOST|local",
        help="run capable steps on other machines (lint_machines.json): auto = spread by load, HOST = that "
        "machine, local = this one. Steps that cannot run there, and unreachable machines, run here",
    )
    parser.add_argument(
        "--distribute",
        choices=("auto", "off"),
        default=os.environ.get("HAMS_LINT_DISTRIBUTE", "auto"),
        help="with --remote auto: auto = this machine (the primary lint runner, several runs side by side) runs everything; only when "
        "every lint slot is taken or the load is above the limit is the whole run shipped to an idle machine instead of "
        "queueing; off = never ship, queue here ($HAMS_LINT_DISTRIBUTE; the 'always' mode of 2026-11-03 is gone)",
    )
    parser.add_argument(
        "--where", action="store_true", help="with --remote: print the routing plan and the probes, run nothing"
    )
    parser.add_argument(
        "--solo",
        action="store_true",
        help="run alone, never joining a coalesced batch with other branches' lint runs "
        "(docs/runbooks/LINT_TEST_BATCHING.md; $HAMS_LINT_BATCH=off does the same)",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="give up (exit 75) after waiting this long for the machine-wide lint run lock "
        "(default: wait; $HAMS_LINT_LOCK_TIMEOUT). Heavy runs queue one at a time per machine; a run "
        "of steps that total at most $HAMS_LINT_CHEAP_SECONDS (60) takes no lock",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="take neither the run lock nor the load gate ($HAMS_LINT_NO_LOCK=1; for CI)",
    )
    parser.add_argument(
        "--lint-caches",
        choices=("show", "prune", "clear"),
        help="show the size of the persistent lint caches (warm Cargo build directories, per-file result "
        "caches) against this machine's budget, prune them to the budget, or delete them; then exit",
    )
    parser.add_argument(
        "--baseline-refresh",
        action="store_true",
        help="the canary: lint a clean origin/main and record the failing steps and tests as the known-red baseline "
        "(~/.cache/hams-lint/baseline; ADR 0117), then exit. Skipped when the baseline already belongs to origin/main "
        "(--baseline-force repeats it)",
    )
    parser.add_argument("--baseline-force", action="store_true", help="with --baseline-refresh: run even when current")
    parser.add_argument("--baseline-show", action="store_true", help="print the recorded known-red baseline, oldest first, then exit")
    parser.add_argument(
        "--baseline-renew",
        metavar="ITEM",
        help="keep tolerating this known-red item for another window (HAMS_LINT_BASELINE_MAX_AGE_DAYS, default 3 days); needs "
        "--baseline-reason; --baseline-owner changes its owner; then exit",
    )
    parser.add_argument("--baseline-reason", help="with --baseline-renew: why it stays red")
    parser.add_argument("--baseline-owner", help="with --baseline-renew: who fixes it")
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="do not tolerate failures that are known red on main: every failure blocks ($HAMS_LINT_BASELINE=off)",
    )
    parser.add_argument(
        "--lock-status", action="store_true", help="print who holds the lint run lock and who is queued, then exit"
    )
    parser.add_argument(
        "--load-limit",
        type=float,
        default=None,
        metavar="LOAD",
        help="a heavy run waits until the 1-minute load average is at most this (default: the number "
        "of cores; $HAMS_LINT_LOAD_LIMIT; $HAMS_CI_LOAD_GATE_RATIO scales the default; "
        "$HAMS_SKIP_CI_LOAD_GATE=1 skips the gate)",
    )
    parser.add_argument(
        "--load-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="how long the load gate waits before failing (default 900; $HAMS_LINT_LOAD_TIMEOUT)",
    )
    options = parser.parse_args(argv)
    if options.no_baseline:
        os.environ["HAMS_LINT_BASELINE"] = "off"
    if options.no_lock:
        os.environ["HAMS_LINT_NO_LOCK"] = "1"
    if options.lock_timeout is None and os.environ.get("HAMS_LINT_LOCK_TIMEOUT"):
        options.lock_timeout = lint_lock._finite(os.environ["HAMS_LINT_LOCK_TIMEOUT"], None)
    options.timing_to_stderr = False
    if not options.remote and default_remote_applies(options, os.environ):
        options.remote = "auto"
    if options.cache is None:
        options.cache = options.changed is not None
    options.jobs = max(1, options.jobs)
    return options


def pass_stamp_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hams-lint", "passed")


# [@ANCHOR: run_linters:stamp_repos]
def stamp_repos(repo_root, tools_dir, dir_path, layout=None):
    """Repositories a pass may stamp. The real hams_shared checkout is stamped only when this run
    scanned it: started from it, or reachable below `repo_root` without crossing a symlink. A
    repository's `hams_shared` link to the main checkout is not followed by the checkers' directory
    walks, so a run from a worktree that links it covers none of its files and must not vouch for
    its commit (otherwise Rust and tool PRs carry stamps that checked nothing).

    `layout` (lint_layout.Layout or None): given when a hams_shared worktree was linted in a layout of throwaway
    sibling clones; only the hams_shared tree is stamped then."""
    shared_repo = os.path.dirname(os.path.realpath(tools_dir))
    if layout is not None:
        # The layout's siblings are throwaway clones at main with a foreign hams_shared: they must not carry a stamp.
        return [shared_repo]
    repos = [repo_root]
    if os.path.realpath(dir_path) == shared_repo:
        repos.append(shared_repo)
        return repos
    inside = os.path.join(os.path.realpath(repo_root), "hams_shared")
    if os.path.realpath(inside) == inside and shared_repo == inside:
        repos.append(shared_repo)
    return repos


# [@ANCHOR: run_linters:stamp_text]
def stamp_text(mode, step_names, hosts=(), known_red="", siblings=(), impact=()):
    """One line: mode, time, how many steps were selected and their names (the part of a name
    before `[`). prmerge.sh reads it to refuse a stamp that selected nothing for a code change."""
    bases = sorted({name.split("[")[0].split(" ")[0] for name in step_names})
    stamp_time = time.strftime("%Y-%m-%dT%H:%M:%S")
    host_field = f" hosts={','.join(sorted(set(hosts)))}" if hosts else ""
    sibling_field = f" siblings={','.join(sorted(set(siblings)))}" if siblings else ""  # the commits a remote machine's mirrors held
    impact_field = f" impact={','.join(impact)}" if impact else ""  # pytest steps run on a selection of their test files (pytest_impact.py)
    return f"{mode} {stamp_time} steps={len(step_names)} names={','.join(bases)}{host_field}{sibling_field}{impact_field}{known_red}\n"


def write_stamp_file(path, text):
    """Writes a pass stamp whole or not at all (temporary file, then rename): a crash or power loss never leaves a cut-off stamp the gate could read."""
    atomic_io.atomic_write(path, text, mode=0o644)


# [@ANCHOR: run_linters:write_batch_stamp]
def write_batch_stamp(tip, step_names, batch_id, with_tips=(), known_red=""):
    """Pass stamp for one branch tip that passed in a coalesced batch (lint_batch.py): the same file and line
    format `_write_pass_stamps` writes, plus ` batch=N` and, when other branches were in the run, ` with=a,b` (their
    first 12 hex digits: what this tip was checked with). Nothing is written when no step names are known (the
    gate refuses a stamp that selected zero steps)."""
    if not step_names:
        print(f"(pass stamp for {tip[:10]} not written: the combined run did not report its steps)", file=sys.stderr)
        return
    try:
        os.makedirs(pass_stamp_dir(), exist_ok=True)
        write_stamp_file(os.path.join(pass_stamp_dir(), tip), stamp_text("changed", step_names, known_red=known_red).rstrip("\n")
                         + f" batch={batch_id}{batch_queue.with_field(with_tips)}\n")
    except OSError as error:
        print(f"(pass stamp not written: {error})", file=sys.stderr)


def stamped_head_repo(repo_root, dir_path, layout=None):
    """The repository whose HEAD must not move during a run for the pass stamp to be written. A hams_shared worktree is linted in a
    layout of throwaway clones (`layout`): `repo_root` is then a clone that never moves, and the stamp goes to the worktree
    `dir_path`, so that is the HEAD to watch. Otherwise it is `repo_root`."""
    return dir_path if layout is not None else repo_root


# [@ANCHOR: run_linters:tree_status_lines]
def tree_status_lines(repo):
    """`git status --porcelain` of `repo`, WITHOUT the working-tree (unstaged) state of its `hams_shared` submodule path.

    A hams_open worktree never has that path in the state its commit records: `fix_worktree_symlinks.py` replaces the
    submodule directory with a symlink to the main checkout's `hams_shared`, and a worktree that ran
    `git submodule update` and then moved the checkout to origin/main (the preflight demands current tools) shows
    ` M hams_shared`. Neither is part of the commit a pass stamp vouches for, yet either made the tree "dirty" and the
    stamp was silently not written (a symlink the skip-worktree bit no longer covers makes `git status` itself fail,
    which reads as dirty too). The setting is applied to this one call only (`-c`, never written to a config file), so
    plain `git status` keeps showing the pointer. A STAGED pointer change (`M  hams_shared`) is still reported, and so
    is every other path: only the unstaged column of the submodule path is dropped. None when git fails."""
    return _git_lines(repo, "-c", "submodule.hams_shared.ignore=all", "status", "--porcelain")


# [@ANCHOR: run_linters:write_pass_stamps]
def _write_pass_stamps(repos, mode, step_names=(), hosts=(), known_red="", siblings=(), impact=()):
    """Record "run_linters.py passed (mode) with a clean tree at commit <sha>" for each repo
    involved. devbox_tools/prmerge.sh refuses to merge a branch whose tip has no stamp, so a
    PR cannot land without the full or `--changed` run having passed on exactly that code.
    Nothing is stamped when the tree has uncommitted changes (the sha would not describe what
    was linted) or when the run was scoped by --only/--skip/module names."""
    for repo in repos:
        head = _git_lines(repo, "rev-parse", "HEAD")
        dirty = tree_status_lines(repo)
        if not head or dirty is None or dirty:
            continue
        try:
            os.makedirs(pass_stamp_dir(), exist_ok=True)
            write_stamp_file(os.path.join(pass_stamp_dir(), head[0]), stamp_text(mode, step_names, hosts, known_red, siblings, impact))
        except OSError as error:
            print(f"(pass stamp not written: {error})", file=sys.stderr)


def _cache_path(dir_path):
    key = os.path.realpath(dir_path).replace(os.sep, "_")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hams-lint", f"{key}.json")


def _finalise_steps(steps, repo_name=None):
    """Apply STEP_META (lock, suffixes, cacheability) by step name, keeping declared order."""
    for step in steps:
        meta = STEP_META.get(step.name.split("[")[0])
        if not meta:
            continue
        if "suffixes" in meta and step.suffixes is None:
            step.suffixes = meta["suffixes"]
        if "lock" in meta:
            step.lock = meta["lock"]
            if meta["lock"] == "cargo" and not os.environ.get("CARGO_TARGET_DIR"):
                # One build directory per repository and toolchain on this machine, outside every
                # worktree (AGENTS.md: never a per-worktree target/): warm after the first run. The
                # "cargo" lock keeps the steps of one run that share it from overlapping, and
                # lint_cache (flock + disk budget + least-recently-used pruning) manages it; see
                # docs/runbooks/LINT_CACHES.md. HAMS_LINT_CARGO_MANAGED marks it as ours to prune.
                env = dict(step.env if step.env is not None else os.environ)
                managed = lint_cache.cargo_target_dir(repo_name or "lint")
                env["CARGO_TARGET_DIR"] = managed
                env["HAMS_LINT_CARGO_MANAGED"] = managed
                env["HAMS_LINT_CARGO_REPO"] = repo_name or "lint"  # the executing host derives the final name itself
                # Incremental data was 26 of the 31 GB of the old shared directory and is not reused.
                env.setdefault("CARGO_INCREMENTAL", "0")
                step.env = env
        if "cacheable" in meta:
            step.cacheable = meta["cacheable"]
    return steps


def _filter_by_name(steps, only, skip):
    wanted = [n.strip() for n in only.split(",") if n.strip()]
    dropped = [n.strip() for n in skip.split(",") if n.strip()]

    def matches(step, names):
        # `aliases`: the names a merged step had before the merge (`--only "pytest daemons/x/tools"`).
        return any(
            step.name == n or step.name.startswith(n + "[") or step.name.startswith(n) or n in step.aliases
            for n in names
        )

    if wanted:
        steps = [s for s in steps if matches(s, wanted)]
    if dropped:
        steps = [s for s in steps if not matches(s, dropped)]
    return steps


def _git_lines(repo, *args):
    try:
        res = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    except OSError:
        return None
    if res.returncode != 0:
        return None
    return [line for line in res.stdout.split("\n") if line]


def changed_files(repo, base):
    """Absolute real paths of files differing from `merge-base(base, HEAD)` in the work tree
    (committed on the branch, staged, unstaged, and untracked). None if `repo` has no `base` or either git listing fails."""
    mb = _git_lines(repo, "merge-base", base, "HEAD")
    if not mb:
        return None
    top = _git_lines(repo, "rev-parse", "--show-toplevel")
    root = os.path.realpath(top[0] if top else repo)
    # A moved submodule pointer is a change even where `submodule.<name>.ignore=all` is set (hams_open clones set it); only untracked files inside
    # a submodule are left out.
    diff = _git_lines(repo, "diff", "--ignore-submodules=untracked", "--name-only", "--no-renames", mb[0])
    untracked = _git_lines(repo, "ls-files", "--others", "--exclude-standard")
    if diff is None or untracked is None:
        # A failed `git diff` is not "nothing changed": that skipped every step that depends on a changed file and still wrote a pass stamp.
        return None
    return {os.path.join(root, name) for name in diff + untracked}


def manifest_depends(module_dir):
    import ast

    try:
        with open(os.path.join(module_dir, "__manifest__.py"), encoding="utf-8") as handle:
            data = ast.literal_eval(handle.read())
    except (OSError, ValueError, SyntaxError):
        return []
    return list(data.get("depends", [])) if isinstance(data, dict) else []


def dependents_closure(changed_modules, module_dirs):
    """Modules that (transitively) depend on any of `changed_modules`, plus those modules."""
    reverse = {}
    for name, path in module_dirs.items():
        for dep in manifest_depends(path):
            reverse.setdefault(dep, set()).add(name)
    result = set(changed_modules)
    frontier = list(changed_modules)
    while frontier:
        for dependent in reverse.get(frontier.pop(), ()):
            if dependent not in result:
                result.add(dependent)
                frontier.append(dependent)
    return result


def _collect_changed(base, repo_root, sibling_dir, tools_dir):
    """(set of changed absolute real paths in all three repos, whether the checker tooling
    itself changed)."""
    repos = [repo_root, sibling_dir, os.path.dirname(os.path.realpath(tools_dir))]
    changed = set()
    for repo in repos:
        if repo and os.path.isdir(repo):
            found = changed_files(repo, base)
            if found is None:
                # An unreadable base used to count as "nothing changed", which skipped every
                # step and still wrote a pass stamp.
                print(
                    f"🛑 --changed {base}: cannot compare {repo} with {base} (no such ref, or not a "
                    f"git checkout). Run `git -C {repo} fetch origin`, or pass the base to compare with.",
                    file=sys.stderr,
                )
                sys.exit(2)
            changed |= found
    tools_real = os.path.realpath(tools_dir) + os.sep
    return changed, any(path.startswith(tools_real) for path in changed)


def _module_dirs(repo_root, community_dir):
    """{module name: real directory} for every Odoo module in `repo_root` and the sibling repo."""
    module_dirs = {}
    for base in (community_dir, repo_root):
        if not base:
            continue
        for item in os.listdir(base):
            if os.path.isfile(os.path.join(base, item, "__manifest__.py")):
                module_dirs[item] = os.path.realpath(os.path.join(base, item))
    return module_dirs


def _changed_modules(changed, repo_root, community_dir):
    """Names of the modules (in `repo_root`, falling back to the sibling) that contain a changed
    file, plus every module that depends on one of them."""
    module_dirs = _module_dirs(repo_root, community_dir)
    touched = set()
    for name, path in module_dirs.items():
        prefix = path + os.sep
        if any(changed_path.startswith(prefix) for changed_path in changed):
            touched.add(name)
    closure = dependents_closure(touched, module_dirs)
    return sorted(name for name in closure if _resolve_module_path(name, repo_root, community_dir))


def changed_outside_modules(changed, repo_root, community_dir):
    """Changed files in `repo_root` that belong to no Odoo module: daemons/, ingest/, devbox_tools/,
    hardware/, top-level files and the like.

    The per-module checkers (burn list, JS syntax, test tags, shebang, ...) used to be handed only
    the changed modules, so a change that touched none of them (a Rust or Python daemon, a script)
    gave them an empty target list and they were not run at all, although a full run scans those
    directories. Whenever this is non-empty `--changed` hands them the whole repository, exactly
    as a full run does; the per-step suffix tables then keep a step from running on a file type it
    cannot read."""
    root = os.path.realpath(repo_root) + os.sep
    module_prefixes = tuple(
        path + os.sep for path in _module_dirs(repo_root, community_dir).values()
    )
    outside = []
    for path in sorted(changed):
        if not path.startswith(root) or path.startswith(module_prefixes):
            continue
        parts = path[len(root):].split(os.sep)
        if any(part in lint_exec.PRUNE_DIRS or part.startswith(".claude") for part in parts):
            continue
        outside.append(path)
    return outside


# Rust sources and cargo manifests: none of the per-module checkers (burn list, JS syntax, test tags, shebang, absolute paths,
# manifest dependencies, flake8, ...) reads them, and the Rust steps take their own whole-repository arguments.
_RUST_ONLY_NAMES = frozenset({"Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "deny.toml", "clippy.toml", "rustfmt.toml"})


def widens_module_scope(path):
    """Whether a changed file outside every module hands the per-module checkers the whole repository.

    It did for any file, so a Rust change in daemons/ made the burn list scan every Python, XML and JavaScript file in the
    repository: a change to one JavaScript file and one Rust test link could not pass while an unrelated Python file elsewhere
    had a finding (2026-10-30, the circular red main). A Rust file cannot change what those checkers report, so it leaves the
    scope alone; every other file type still widens it."""
    name = os.path.basename(path)
    return not (name.endswith(".rs") or name in _RUST_ONLY_NAMES)


ESLINT_MODULE_SUFFIXES = (".js", ".mjs", ".cjs")
# A directory listed here (relative to `daemons/`) has a requirements.txt of packages that must not be installed
# system-wide (an isolated venv by design): the pytest step of the tests at or below it runs under a cached venv built
# from that file (lint_daemon_venv.py) wherever no `.venv` was found in the tree. The nearest listed directory above a
# test directory wins (hams_simulated_bots/fine_tuning/tests uses fine_tuning's, not the bot's). ISOLATED_VENV_PREFIX
# marks such a group's "interpreter" in the step grouping. Documentation: docs/runbooks/LINT_DAEMON_VENVS.md.
# [@ANCHOR: run_linters:ISOLATED_VENV_DIRS]
ISOLATED_VENV_DIRS = ("hams_simulated_bots", "hams_simulated_bots/fine_tuning")
ISOLATED_VENV_PREFIX = "isolated-venv:"


# [@ANCHOR: run_linters:isolated_venv_marker_for]
def isolated_venv_marker_for(test_dir, daemons_root):
    """`ISOLATED_VENV_PREFIX + <directory relative to daemons_root, with "/">` for the nearest directory at or above
    `test_dir` (and below `daemons_root`) that is in ISOLATED_VENV_DIRS and holds a requirements.txt; otherwise None."""
    rel = os.path.relpath(test_dir, daemons_root).replace(os.sep, "/")
    while rel not in (".", "") and not rel.startswith(".."):
        if rel in ISOLATED_VENV_DIRS and os.path.isfile(os.path.join(daemons_root, rel, "requirements.txt")):
            return ISOLATED_VENV_PREFIX + rel
        rel = rel.rpartition("/")[0]
    return None


# [@ANCHOR: run_linters:pytest_argv_prefix]
def pytest_argv_prefix(interpreter, python_exec, dir_path):
    """The argv that starts `python -m pytest -q` for a group's `interpreter`: a venv's python path as it is, or for
    an isolated-venv marker the system `python_exec` running `<dir_path>/tools/lint_daemon_venv.py` for that
    directory's requirements.txt (which builds or reuses the cached venv, then execs pytest in it). The venv's name
    is the marker's directory with "/" replaced by "_"."""
    if interpreter.startswith(ISOLATED_VENV_PREFIX):
        rel = interpreter[len(ISOLATED_VENV_PREFIX):]
        return [
            python_exec,
            os.path.join(dir_path, "tools", "lint_daemon_venv.py"),
            "--name", rel.replace("/", "_"),
            "--requirements", os.path.join(dir_path, "daemons", *rel.split("/"), "requirements.txt"),
            "--", "-m", "pytest", "-q",
        ]
    return [interpreter, "-m", "pytest", "-q"]


_ESLINT_SKIP_DIRS = frozenset({"node_modules", "target", ".git", "__pycache__", ".venv", "venv"})
_QUOTED_SPECIFIER = re.compile(r"""['"`]([^'"`\n]+)['"`]""")
_MODULE_SYNTAX = re.compile(r"^\s*(?:import\s|import\(|export\s)|\bmodule\.exports\b|\brequire\(", re.M)


def narrow_eslint_targets(targets, changed_paths, full_triggers=()):
    """The JavaScript files `--changed` has to give ESLint, or None when the directories in `targets` must be linted whole.

    ESLint's cost is per linted file (measured 2026-11-03, hams_com, type-aware config: one file 8.8 s, the
    daemons/shack_console directory 93 s), and a one-file console change used to hand it the changed module's
    directory or, for a change outside every Odoo module (daemons/), the whole repository. The findings of a file depend
    on that file and on the types it imports, so the narrowed set is the changed files plus every file under `targets`
    that imports one of them (a quoted import/require/from specifier on an import-like line whose last path part is the
    changed file's name, or the directory name for an `index` file: a superset of the real importers). Everything
    else is exactly as before: same config, same project, same rules.

    Returns None (lint `targets` whole) when anything makes the narrowing unsafe: a changed config file
    (`full_triggers`: eslint.config.js, tsconfig.json, package.json), a changed JSON or other non-script file under
    `targets`, no changed script, or a changed script that is not a module (a classic script shares its globals with
    every other file in the TypeScript program, so who is affected cannot be read from imports). A script that was
    deleted still counts: its importers are linted."""
    changed = [os.path.realpath(p) for p in changed_paths]
    if any(p in {os.path.realpath(t) for t in full_triggers} for p in changed):
        return None
    roots = [os.path.realpath(t) for t in targets]

    def under_targets(path):
        return any(path == r or path.startswith(r + os.sep) for r in roots)

    mine = [p for p in changed if under_targets(p)]
    scripts = [p for p in mine if p.endswith(ESLINT_MODULE_SUFFIXES)]
    if not scripts or any(p.endswith(".json") for p in mine):
        return None
    names = set()
    for path in scripts:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    if not _MODULE_SYNTAX.search(handle.read()):
                        return None
            except OSError:
                return None
        stem = os.path.basename(path).rsplit(".", 1)[0]
        names.add(stem)
        if stem == "index":
            names.add(os.path.basename(os.path.dirname(path)))
    selected = {p for p in scripts if os.path.isfile(p)}
    for root in roots:
        if os.path.isfile(root):
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _ESLINT_SKIP_DIRS and not d.startswith(".")]
            for name in files:
                full = os.path.join(current, name)
                if not name.endswith(ESLINT_MODULE_SUFFIXES) or full in selected:
                    continue
                try:
                    with open(full, encoding="utf-8", errors="replace") as handle:
                        lines = [line for line in handle if "import" in line or "require" in line or "from" in line or "define" in line]
                except OSError:
                    continue
                for line in lines:
                    for spec in _QUOTED_SPECIFIER.findall(line):
                        last = spec.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0] if not spec.endswith("/") else ""
                        if last in names or spec.rstrip("/").rsplit("/", 1)[-1] in names:
                            selected.add(full)
                            break
                    else:
                        continue
                    break
    return sorted(selected) or None


def find_eslint_toolchain(hams_shared_dir):
    """{"eslint_bin", "node_modules"} for the first hams_shared checkout that has ESLint installed,
    or None: `hams_shared_dir` itself, then the main worktree of its git repository (a worktree
    of hams_shared has no node_modules)."""
    candidates = [hams_shared_dir, lint_exec.main_worktree_of(hams_shared_dir)]
    for candidate in candidates:
        if not candidate:
            continue
        modules = os.path.join(candidate, "node_modules")
        binary = os.path.join(modules, ".bin", "eslint")
        if os.path.isfile(binary):
            return {"eslint_bin": binary, "node_modules": modules}
    return None


def write_eslint_project(hams_shared_dir, repos):
    """Write a TypeScript project file for the type-aware ESLint rules that lists the JavaScript
    of `repos` (the checkouts being linted), extending hams_shared's tsconfig.json for its
    compiler options. Returns its path; the directory is removed when the process exits."""
    import atexit
    import json
    import tempfile

    directory = tempfile.mkdtemp(prefix="hams-eslint-project-")
    atexit.register(shutil.rmtree, directory, True)
    # Every JavaScript file eslint.config.js's type-aware block (files: **/*.js) can be handed must be in the project, or the
    # parser fails with "file not found in the provided project(s)": module static sources, and the daemons' own JavaScript
    # (the relay's trust pages, the shack console, test scripts). 2026-10-29: only */static/** was listed, so any run that
    # linted daemons/ failed on files that had nothing wrong with them.
    includes = []
    excludes = []
    for repo in repos:
        if repo and os.path.isdir(repo):
            real = os.path.realpath(repo)
            includes.append(os.path.join(real, "*", "static", "**", "*.js"))
            includes.append(os.path.join(real, "daemons", "**", "*.js"))
            includes.append(os.path.join(real, "daemons", "**", "*.mjs"))
            # `cargo test` leaves thousands of generated JavaScript files under daemons/*/target; reading them made
            # the type-aware project run out of memory (2026-11-03). Patterns are relative to this file's directory.
            excludes.append(os.path.join(real, "**", "target", "**"))
            excludes.append(os.path.join(real, "**", "node_modules", "**"))
    path = os.path.join(directory, "tsconfig.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {"extends": os.path.join(hams_shared_dir, "tsconfig.json"), "include": includes, "exclude": excludes},
            handle,
        )
    return path


def checker_env(argv):
    """The environment of a step that runs a `tools/check_*.py` linter: the parent's, plus HAMS_CHECK_ALLOW_EMPTY=1 (check_target.py).

    run_linters hands a checker a whole repository or a module directory it chose itself, and one of them may hold none of the
    files that checker reads (hams_shared has no SCSS, a module has no Rust), so an empty scan is normal here and is not a failure.
    A target that does not exist or is not a directory still fails inside the checker. Run by hand a checker is strict.
    A checker migrated to the Rust engine (`hams_lint_bin.py run check NAME ...`, see ENGINE_CHECKS) gets the same.
    None (inherit) for every other step."""
    script = os.path.basename(str(argv[1])) if argv and len(argv) > 1 else ""
    engine_check = script == "hams_lint_bin.py" and list(argv[2:4]) == ["run", "check"]
    if not (script.startswith("check_") and script.endswith(".py")) and not engine_check:
        return None
    return dict(os.environ, **{check_target.ALLOW_EMPTY_ENV: "1"})


def _run_per_target_checker(python_exec, dir_path, script_name, targets):
    """Run a `hams_shared/tools/check_*.py` script that only ever reads a SINGLE repo-dir
    argument -- once per entry in `targets`, instead of batching every target into one argv
    list (`[script] + targets`).

    `check_manifest_dependencies.py`,
    `check_test_tags.py`, `check_absolute_paths.py`, `check_rabbitmq_pool.py`,
    `check_shebang.py`, and `check_init_imports.py` all read only `sys.argv[1]`, silently
    ignoring `sys.argv[2:]` entirely (as each script's source shows);
    `check_burn_list.py` declares its positional `directory` argument with `nargs="?"`, which
    argparse rejects outright (a loud "unrecognized arguments" failure) given 2+ positional
    values. `targets` becomes a multi-element list whenever `target_modules_str` names more
    than one comma-separated module -- a real, deliberately supported feature of this file's own
    module-discovery step (`mod_array = [m.strip() for m in target_modules_str.split(",") ...]`),
    not a hypothetical. Before this fix, scoping a lint run to two or more named modules meant
    six of these seven checkers silently checked only the first module and never touched the
    rest -- no error, no warning, an exit code that looked clean -- while the seventh
    (`check_burn_list.py`) crashed instead. Running once per target and aggregating the result
    fixes both failure modes without touching any of those already-reviewed checker scripts'
    own CLI contracts. For the common unscoped case (`targets == [repo_root]`, a single
    element), this produces exactly one subprocess call with exactly the same argv as before --
    behavior for that invocation is unchanged.
    """
    any_failed = False
    for target in targets:
        res = subprocess.run(
            [python_exec, os.path.join(dir_path, "tools", script_name), target],
            capture_output=True,
            text=True,
            env=checker_env([python_exec, script_name]),
        )
        if res.returncode != 0:
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")
            any_failed = True
        elif res.stdout and res.stdout.strip():
            print(res.stdout, end="")
    return any_failed


def _per_target_steps(add, python_exec, dir_path, script_name, targets, **options):
    """The parallel-runner form of `_run_per_target_checker` (same contract, same argv): one
    Step per entry in `targets`, so a scoped multi-module run still checks every module and a
    unscoped run (`targets == [repo_root]`) is exactly one step with the same argv as before."""
    stem = script_name[: -len(".py")]
    for target in targets:
        suffix = "" if len(targets) == 1 else f"[{os.path.basename(target)}]"
        add(
            stem + suffix,
            checker_argv(python_exec, dir_path, script_name, [target]),
            **options,
        )


# Checks that run on the Rust lint engine (tools/hams_lint) instead of their Python checker. A check is listed here only
# after tools/lint_engine_parity.py showed byte-identical output and exit status on the whole tree, on the rule file's
# examples and on injected violations (the parity rule); its Python checker stays in tools/ as the parity reference. The
# step keeps the checker's name, so pass stamps, the lint cache and STEP_META are unchanged.
ENGINE_CHECKS = frozenset({
    "check_absolute_paths",
    "check_shebang",
    "check_window_fetch_reassignment",
    "check_fake_network_listener",
})


def checker_argv(python_exec, dir_path, script_name, targets):
    """The argv of the step that runs the checker `script_name` (`check_x.py`) on `targets`: the Python checker, or, for an
    ENGINE_CHECKS name, the same check on the Rust engine through `hams_lint_bin.py run check NAME` (built on demand)."""
    stem = script_name[: -len(".py")]
    if stem in ENGINE_CHECKS:
        return [python_exec, os.path.join(dir_path, "tools", "hams_lint_bin.py"), "run", "check", stem, *targets]
    return [python_exec, os.path.join(dir_path, "tools", script_name), *targets]


# [@ANCHOR: run_linters:shared_checkout_staleness]
def shared_checkout_staleness(shared_root):
    """(commits_behind, is_main_checkout, error) for the hams_shared checkout at `shared_root` against origin/main.

    Worktrees of hams_open and hams_com link their `hams_shared` to the MAIN hams_shared checkout
    (fix_worktree_symlinks.py), so a main checkout that has fallen behind makes every worktree lint
    and test against stale tools: on 2026-10-29 one that was 18 commits behind made `--changed` run
    the whole tools suite for 12 minutes, fail 5 tests that pass on origin/main, and never write a
    pass stamp. `git fetch` is tried first (20 s limit) so the comparison is not itself stale."""
    try:
        subprocess.run(["git", "-C", shared_root, "fetch", "-q", "origin"], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        pass
    behind = _git_lines(shared_root, "rev-list", "--count", "HEAD..origin/main")
    if not behind:
        return None, False, "no origin/main to compare with"
    git_dir = _git_lines(shared_root, "rev-parse", "--absolute-git-dir")
    common = _git_lines(shared_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    is_main = bool(git_dir and common and os.path.realpath(git_dir[0]) == os.path.realpath(common[0]))
    return int(behind[0]), is_main, None


# [@ANCHOR: run_linters:fast_forward_shared]
def fast_forward_shared(shared_root):
    """Bring the main hams_shared checkout at `shared_root` up to origin/main when that cannot lose anything.

    Returns (ok, text). Only `git merge --ff-only origin/main` is ever run (never a reset, checkout or discard). It is
    refused, with the reason, when tracked files are modified (`git status --porcelain --untracked-files=no` is not empty),
    when HEAD is not an ancestor of origin/main (a diverged or unpushed checkout), or when git itself refuses; a refusal
    that is only a lost race with another run doing the same is accepted when the checkout is current afterwards.
    Side effects: moves HEAD of that checkout (a detached submodule checkout stays detached) and so changes what every
    worktree linking to it sees; seconds of runtime, no network (the caller fetched)."""
    dirty = _git_lines(shared_root, "status", "--porcelain", "--untracked-files=no")
    if dirty is None or dirty:
        return False, "it has uncommitted changes to tracked files, so it is not fast-forwarded: " + "; ".join((dirty or ["git status failed"])[:3])
    ancestor = subprocess.run(["git", "-C", shared_root, "merge-base", "--is-ancestor", "HEAD", "origin/main"], capture_output=True)
    if ancestor.returncode != 0:
        return False, "HEAD is not an ancestor of origin/main (diverged or unpushed commits), so it is not fast-forwarded"
    old = (_git_lines(shared_root, "rev-parse", "--short", "HEAD") or ["?"])[0]
    merged = subprocess.run(["git", "-C", shared_root, "merge", "--ff-only", "-q", "origin/main"], capture_output=True, text=True)
    new = (_git_lines(shared_root, "rev-parse", "--short", "HEAD") or ["?"])[0]
    behind = _git_lines(shared_root, "rev-list", "--count", "HEAD..origin/main")
    if merged.returncode != 0 and behind != ["0"]:
        return False, "git merge --ff-only failed: " + (merged.stderr or merged.stdout).strip()[:300]
    return True, f"fast-forwarded the hams_shared checkout {old} -> {new}"


# [@ANCHOR: run_linters:ensure_shared_current]
def ensure_shared_current(shared_root):
    """The preflight of every run: is the hams_shared checkout that holds these tools current? Returns (status, text).

    status `current` (nothing to do, or origin/main could not be compared), `fast_forwarded` (the main checkout was behind and
    `fast_forward_shared` brought it up; the caller restarts the run so it uses the new files), `worktree_behind` (a linked
    worktree behind origin/main: only a warning, `text`, since the tools linked in are the main checkout's) or `refused` (the main
    checkout is behind and cannot be fast-forwarded safely: `text` names the checkout, how far behind and why, and the manual
    fix). It fetches (20 s limit, `shared_checkout_staleness`) and never queues, locks or runs a step; a refusal therefore
    costs seconds. `HAMS_ALLOW_STALE_SHARED=1` is handled by the caller (the check is skipped)."""
    behind, is_main, _error = shared_checkout_staleness(shared_root)
    if not behind:
        return "current", ""
    if not is_main:
        return "worktree_behind", f"\u26a0 This hams_shared worktree is {behind} commit(s) behind origin/main; rebase it to lint with the current tools."
    ok, reason = fast_forward_shared(shared_root)
    if ok:
        return "fast_forwarded", reason
    return "refused", (
        f"\U0001f6d1 The hams_shared checkout at {shared_root} is {behind} commit(s) behind origin/main, so these linters and their tests "
        f"are not the current ones and the results (and the pass stamp) cannot be trusted. It could not be fast-forwarded: {reason}.\n"
        f"   Fix it by hand (never with a reset that discards work): git -C {shared_root} merge --ff-only origin/main   "
        "(a detached submodule checkout: `git checkout --detach origin/main`),\n"
        "   or set HAMS_ALLOW_STALE_SHARED=1 to run anyway."
    )


# [@ANCHOR: run_linters:real_shared_root]
def real_shared_root(script_path):
    """The hams_shared checkout that holds `script_path`, symlinks resolved.

    The staleness check must examine this checkout, not the repository the script was started from:
    through a hams_open or hams_com `tools` link, `invocation_root` is that repository, and its own
    unpushed or behind commits were reported as a stale hams_shared."""
    return os.path.dirname(os.path.dirname(os.path.realpath(script_path)))


def invocation_root(script_path):
    """The directory the script was started from: the repository for `tools/run_linters.py` and
    for `hams_shared/tools/run_linters.py` alike.

    Started through a repository's `hams_shared` symlink (a worktree whose `fix_worktree_symlinks`
    linked it to the main checkout), `dirname(dirname(script))` is that symlink. Every step
    root, daemon directory and test directory derived from it then pointed into the real
    hams_shared checkout instead of the worktree, so a Rust change in the worktree lay under no
    step's root and `--changed` selected zero steps. The worktree itself is the
    root in that case, exactly as when the script is started through the `tools` link."""
    given = os.path.dirname(os.path.dirname(os.path.abspath(script_path)))
    if os.path.basename(given) == "hams_shared" and os.path.islink(given):
        return os.path.dirname(given)
    return given


# [@ANCHOR: run_linters:steps_without_result]
def steps_without_result(selected_names, results):
    """Names of the selected steps that produced no result, in selection order. A pass stamp says "these steps passed": a step that
    was selected and left no result (a remote host that dropped it, a bug in the executor) is not a pass, and the run must fail
    naming it rather than stamp a list that includes it."""
    have = {r.name for r in results}
    return [name for name in dict.fromkeys(selected_names) if name not in have]


# [@ANCHOR: run_linters:retry_timed_out_steps]
def retry_timed_out_steps(results, steps, execute=None, out=print):
    """Steps that failed by timing out on a saturated machine are rerun ONCE, alone and one at a time, after the whole run.

    `results`: the run's `lint_exec.Result` list; `steps`: the run's steps (the same names; a result with no step is left alone);
    `execute(steps)`: runs steps serially and returns their results (default `lint_exec.execute(steps, 1)`; tests pass a fake).
    A result is a candidate when `lint_exec.timeout_suspect` says so: failed, really run, the machine saturated when it ended and the
    output speaks of a timeout. `HAMS_LINT_TIMEOUT_RETRY=off` disables the retry. Returns (new results list, [step names whose rerun
    passed]). A step that passes on rerun replaces its failed result and is NAMED: the caller records it in the pass stamp
    (`timeout-retried=`) and it is printed here, never silent. A step that fails again keeps its original failure and the report
    says so: with the machine quiet that is a real failure. Cost: one extra run per candidate step, at most; never loops."""
    if os.environ.get("HAMS_LINT_TIMEOUT_RETRY", "on").lower() in ("off", "0", "no", "false"):
        return results, []
    by_name = {step.name: step for step in steps}
    candidates = [index for index, r in enumerate(results) if lint_exec.timeout_suspect(r) and r.name in by_name]
    if not candidates:
        return results, []
    execute = execute or (lambda wanted: lint_exec.execute(wanted, 1))
    results, retried = list(results), []
    for index in candidates:
        failed = results[index]
        out(f"[*] {failed.name} timed out while the machine was saturated ({failed.saturated}): rerunning it once, alone")
        again = execute([by_name[failed.name]])[0]
        if again.returncode == 0:
            results[index] = again
            retried.append(failed.name)
            out(f"[*] {failed.name} passed on its serial rerun: recorded as timeout-retried in the pass stamp")
        else:
            quiet = "" if lint_exec.saturation()[0] else " with the machine quiet"
            out(f"[!] {failed.name} failed again{quiet}: this is a real failure")
    return results, retried


# [@ANCHOR: run_linters:timeout_retried_field]
def timeout_retried_field(names):
    """` timeout-retried=<step>,<step>` for a pass stamp (names with spaces use `_`), or "" for none."""
    return " timeout-retried=" + ",".join("_".join(name.split()) for name in names) if names else ""


# [@ANCHOR: run_linters:baseline_command]
def baseline_command(options, dir_path, repo_root):
    """`--baseline-refresh` / `--baseline-show` / `--baseline-renew`: record, print or renew the known-red baseline of the repository `dir_path` belongs to
    (hams_com, hams_open, or a worktree of hams_shared, key `hams_shared`). Returns an exit code: `lint_baseline.refresh`'s,
    0 for show, 2 when started from the main hams_shared checkout (run it from a worktree of hams_shared, whose combined
    tree is built from origin/main). No lint lock is taken here: the canary's own run takes it like any run."""
    import lint_baseline
    if shared_worktree_root(dir_path, repo_root):
        repo, key, tree_name = dir_path, "hams_shared", lint_batch.SHARED_TREE_NAME
    elif os.path.realpath(dir_path) == real_shared_root(__file__):
        print("--baseline-refresh from the main hams_shared checkout is not supported: run it in a worktree of hams_shared")
        return 2
    else:
        key = os.path.basename(_resolve_main_checkout_root(repo_root).rstrip(os.sep))
        repo, tree_name = dir_path, key
    if options.baseline_show:
        print("\n".join(lint_baseline.show_lines(key)))
        return 0
    if options.baseline_renew:
        try:
            lint_baseline.renew(key, options.baseline_renew, options.baseline_reason, options.baseline_owner)
        except (KeyError, ValueError) as error:
            print("--baseline-renew: %s" % error.args[0])
            return 2
        print("\n".join(lint_baseline.show_lines(key)))
        return 0
    return lint_baseline.refresh(repo, key, tree_name, force=options.baseline_force, remote=options.remote or "auto")


def run_bundle(argv):
    """Remote side of `--remote`: run the steps in a bundle written by lint_remote.dispatch, with this
    machine's paths, under this machine's own run lock and load gate; write a results file."""
    parser = argparse.ArgumentParser(prog="run_linters.py --run-bundle")
    parser.add_argument("--run-bundle", required=True)
    parser.add_argument("--bundle-root", action="append", default=[])
    parser.add_argument("--results-json", required=True)
    parser.add_argument("-j", "--jobs", type=int, default=2)
    parser.add_argument("--no-timing", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=None)
    parser.add_argument("--load-timeout", type=float, default=None)
    options = parser.parse_args(argv)
    with open(options.run_bundle, encoding="utf-8") as handle:
        bundle = json.load(handle)
    roots = dict(item.split("=", 1) for item in options.bundle_root)
    steps = lint_remote.import_bundle(bundle, roots)
    run_ok, run_lock = lint_lock.begin_heavy_run(
        os.environ,
        [step.name for step in steps],
        lint_lock.known_durations(),
        os.path.dirname(options.run_bundle),
        "run_linters.py --run-bundle (remote)",
        lock_timeout=options.lock_timeout,
        load_timeout_override=options.load_timeout,
    )
    if not run_ok:
        return lint_lock.EXIT_TEMPFAIL
    try:
        results = lint_exec.execute(steps, options.jobs, durations=lint_lock.known_durations())
    finally:
        if run_lock:
            run_lock.release()
    with open(options.results_json, "w", encoding="utf-8") as handle:
        json.dump(lint_remote.results_document(bundle["bundle_id"], results), handle)
    if not options.no_timing:
        print(lint_exec.timing_table(results))
    return 1 if any(r.returncode != 0 for r in results) else 0


# tools/test_*.py suites that cannot run on another machine's mirror of the tree (see the "host-bound" step).
HOST_BOUND_TOOL_TESTS = frozenset(
    {
        "test_run_linters_changed_selection.py",
        "test_daemon_family_probe.py",
        "test_odoo_warning_policy.py",
        "test_check_owl_templates.py",
        "test_worktree_hygiene.py",
    }
)


# [@ANCHOR: run_linters:main_orchestrator]
def main():
    lint_remote.prefer_user_toolchain()
    if "--run-bundle" in sys.argv:
        sys.exit(run_bundle(sys.argv[1:]))
    dir_path = invocation_root(__file__)
    repo_root = _resolve_repo_root(dir_path)
    # 0. A stale main hams_shared checkout gives wrong results to every worktree that links to it. Checked first, in
    # seconds, before the sibling layout is built or any step is queued: fast-forwarded when that is safe, else refused.
    if not os.environ.get("HAMS_ALLOW_STALE_SHARED"):
        status, text = ensure_shared_current(real_shared_root(__file__))
        if status == "fast_forwarded":
            print(f"[*] {text}; restarting this run with the current tools", file=sys.stderr)
            if not os.environ.get("HAMS_SHARED_FF_RESTARTED"):
                os.environ["HAMS_SHARED_FF_RESTARTED"] = "1"
                sys.stdout.flush()
                os.execv(sys.executable or "python3", [sys.executable or "python3"] + sys.argv)
        elif status == "refused":
            print(text)
            sys.exit(2)
        elif status == "worktree_behind":
            print(text, file=sys.stderr)
    # A hams_shared worktree is linted inside a layout of its own: this hams_shared as hams_open/hams_shared, hams_com
    # beside it. The main checkouts' submodule is whatever commit they have, so every cross-repository step (anchors,
    # claims, burn list) used to judge the OLD hams_shared and never the branch (#411 and #412).
    layout = None
    if shared_worktree_root(dir_path, repo_root) and not any(
        flag in sys.argv for flag in ("--lint-caches", "--lock-status", "--baseline-refresh", "--baseline-show", "--baseline-renew")
    ):
        import atexit

        main_open = repo_root
        try:
            lint_layout.reap_orphans(main_open)
            layout = lint_layout.build(
                dir_path, main_open, _sibling_checkout(main_open, main_open, "hams_com"),
                out=lambda text: print(text, file=sys.stderr),  # stdout stays clean for --list-json / --list-steps
            )
        except (lint_layout.LayoutError, OSError) as error:
            print(f"🛑 cannot build the sibling layout for this hams_shared tree: {error}", file=sys.stderr)
            sys.exit(2)
        atexit.register(lint_layout.remove, layout)
        repo_root = layout.hams_open

    # 1. Anti-Symlink Mandate
    allowed_symlinks = {
        "hams_shared",
        "tools",
        "docs",
        "AGENTS.md",
        "agents",
        ".agents",
    }
    symlinks_found = [
        f
        for f in os.listdir(dir_path)
        if os.path.islink(os.path.join(dir_path, f)) and f not in allowed_symlinks
    ]
    if symlinks_found:
        print(
            "================================================================================"
        )
        print("🚨 CRITICAL ARCHITECTURE WARNING: NO SYMLINKING 🚨")
        print("Symbolic links detected in the repository root:")
        for s in symlinks_found:
            print(f" - {s}")
        print(
            "This is an ANTI-PATTERN. You are strictly forbidden from symlinking modules"
        )
        print(
            "(e.g., zero_sudo, distributed_redis_cache) from hams_open into hams_com."
        )
        print("You MUST configure and rely on the Odoo --addons-path correctly.")
        print(
            "================================================================================"
        )
        sys.exit(1)

    # 2. Child Directory Mandate
    child_community = os.path.join(dir_path, "hams_open")
    if os.path.isdir(child_community):
        print(
            "================================================================================"
        )
        print("🚨 CRITICAL REPOSITORY STRUCTURE WARNING 🚨")
        print(
            f"hams_open was found as a CHILD of the current repository: {child_community}"
        )
        print("This is an ANTI-PATTERN. hams_open MUST be a SIBLING directory instead.")
        print(
            f"Please move it to: {os.path.abspath(os.path.join(dir_path, '..', 'hams_open'))}"
        )
        print(
            "================================================================================"
        )
        sys.exit(1)

    # 3. Resolve Sibling Dependency
    main_checkout_root = _resolve_main_checkout_root(repo_root)
    community_dir = None
    if not os.path.exists(os.path.join(repo_root, "zero_sudo", "__manifest__.py")):
        sibling_community = _sibling_checkout(repo_root, main_checkout_root, "hams_open")
        if os.path.isdir(sibling_community):
            community_dir = sibling_community
    sibling_dir = community_dir or _sibling_checkout(repo_root, main_checkout_root, "hams_com")

    addons_paths = [
        "/usr/lib/python3/dist-packages/odoo/addons",
        repo_root,
        _sibling_checkout(repo_root, main_checkout_root, "hams_com"),
    ]
    if community_dir:
        addons_paths.append(community_dir)
    addons_path_str = ",".join(addons_paths)

    python_exec = "/usr/bin/python3"
    linters_failed = False

    options = _parse_args(sys.argv[1:])
    if "--no-cache" in sys.argv:
        os.environ["HAMS_LINT_NO_CACHE"] = "1"  # read by lint_cache in the steps and forwarded to the runners
    if options.lint_caches:
        sys.exit(lint_cache.main([options.lint_caches]))
    if options.lock_status:
        print("\n".join(lint_lock.status_lines()))
        sys.exit(0)
    if options.baseline_refresh or options.baseline_show or options.baseline_renew:
        sys.exit(baseline_command(options, dir_path, repo_root))
    wall_start = time.monotonic()
    tools_dir = os.path.join(dir_path, "tools")
    real_shared_dir = real_shared_root(__file__)
    steps = []

    def _add(name, argv, **step_options):
        """Declare one step. `inputs=` names the files the step reads (see lint_inputs());
        it defaults to the whole repo pair with every file suffix, which is always safe."""
        inputs = step_options.pop("inputs", None)
        roots, suffixes = inputs if inputs else _default_inputs(dir_path, sibling_dir, real_shared_dir, repo_root)
        if "env" not in step_options:
            step_options["env"] = checker_env(argv)
        steps.append(
            lint_exec.Step(
                name=name,
                argv=argv,
                roots=roots,
                suffixes=suffixes,
                **step_options,
            )
        )

    def _fail_now(name, message):
        steps.append(lint_exec.Step(name=name, static_output=message, always=True))

    impact_counts = []  # `tools:57of145`, one per narrowed step: the stamp's `impact=` field
    impact_notes = []  # one line per pytest step whose test files were narrowed; printed with the run and counted in the stamp

    def _impact_select(step_name, root, files, also=()):
        """`files` of a pytest step, narrowed under `--changed` to the test files that depend on a changed file (pytest_impact.py); all of them for a full
        run, a run scoped by module names or more changed files than a list can carry. Prints which files were skipped (once the steps are known to run)."""
        if changed_set is None or target_modules_str or len(changed_set) > rust_impact.MAX_PATHS or not files:
            return list(files)
        chosen, skipped_files, why = pytest_impact.select_tests(root, files, changed_set, also)
        if skipped_files:
            impact_notes.append(pytest_impact.describe(step_name, root, chosen, skipped_files, why))
            impact_counts.append(f"{step_name.strip('/').replace('/', '-')}:{len(chosen)}of{len(files)}")
        return chosen

    # 5. Modules Discovery
    target_modules_str = options.modules
    changed_set = None
    run_all = False
    if options.changed is not None:
        # The layout's siblings are pristine clones: only the hams_shared tree under lint has changes (and the copy
        # of each changed file in the layout counts as changed too).
        changed_set, run_all = _collect_changed(
            options.changed,
            None if layout else repo_root,
            None if layout else sibling_dir,
            tools_dir,
        )
        changed_set = lint_layout.mirror_changed(changed_set, layout)
    mod_array = []
    scoped = bool(target_modules_str)
    if changed_set is not None and not target_modules_str and not run_all:
        scoped = True
        mod_array = _changed_modules(changed_set, repo_root, community_dir)
    elif not target_modules_str:
        for item in os.listdir(repo_root):
            mod_path = os.path.join(repo_root, item)
            if os.path.isdir(mod_path) and os.path.isfile(
                os.path.join(mod_path, "__manifest__.py")
            ):
                mod_array.append(item)
    else:
        mod_array = [m.strip() for m in target_modules_str.split(",") if m.strip()]

    # 6. Pre-flight Checks
    # `resolved_mod_paths` also becomes `targets` below (when modules were explicitly named) --
    # `targets` must not be recomputed from scratch as
    # `[os.path.join(repo_root, m) for m in mod_array]`, which would assume every named
    # module lives directly under `repo_root` and ignore the `community_dir`
    # fallback this very loop performs one path resolution earlier. A module that exists only in
    # the sibling repo (e.g. running from `hams_com` and naming a `hams_open`-only module like
    # `zero_sudo`) would get a `targets` entry pointing at a path that doesn't exist at all under
    # `repo_root` -- `os.walk()` on a nonexistent directory yields nothing and raises nothing, so
    # every `targets`-scoped checker (flake8, check_burn_list, verify_anchors,
    # check_manifest_dependencies, check_js_syntax, check_test_tags, check_absolute_paths,
    # check_rabbitmq_pool, check_shebang, check_init_imports) would silently scan zero files for
    # that module and report a clean pass, even though `pre_flight_check.py` (this same loop,
    # correctly, via `mod_path`) was checking the real, correctly-resolved sibling-repo path the
    # whole time. So `targets` reuses this loop's own already-correct `mod_path` resolution instead
    # of recomputing a second, wrong one. A module name that resolves in neither repo now prints
    # an explicit warning and fails the run instead of being silently absent from `targets` with
    # no signal at all (the same "loud, honest failure over a silent no-op" preference applied
    # everywhere else).
    resolved_mod_paths = []
    for mod in mod_array:
        mod_path = _resolve_module_path(mod, repo_root, community_dir)
        if mod_path is None:
            where = f"{repo_root} or {community_dir}" if community_dir else repo_root
            _fail_now(
                f"module-not-found[{mod}]",
                f"❌ Module '{mod}' not found under {where}.",
            )
            continue
        resolved_mod_paths.append(mod_path)

        pre_flight_cmd = [
            python_exec,
            os.path.join(dir_path, "tools", "pre_flight_check.py"),
            "-m",
            mod_path,
            "--addons-path",
            addons_path_str,
        ]
        # The pre-flight step prints a passing run's stdout too (same as every other step):
        # every other step in this file prints a checker's
        # stdout even on a clean (returncode == 0) pass when it's non-empty, so this one must
        # not silently drop a diagnostic pre_flight_check.py prints on
        # success. pre_flight_check.py's own module-tier
        # dormancy notice ("[*] Module-tier architecture check skipped: ... not found") is
        # unconditionally printed to stdout with returncode 0 whenever tier_config.json is
        # absent -- true for every real invocation today, since no tier_config.json exists
        # anywhere in either repo -- and this branch's absence meant that notice never
        # reached a live CI/terminal output, only the module's own capture_output buffer. This matches this file's own
        # established convention for every other step.
        _add(
            f"pre_flight_check[{os.path.basename(mod_path)}]",
            pre_flight_cmd,
            inputs=((mod_path, community_dir or repo_root), None),
        )

    # 7. Flake8
    # The dev box and the apt-provisioned lint machines have it in /usr/bin; a machine provisioned with pip --user
    # (bruce-yoga: ~/.local/bin) has it on the PATH only, and its flake8 step failed there and ran again here.
    flake8_cmd = "/usr/bin/flake8" if os.path.exists("/usr/bin/flake8") else (shutil.which("flake8") or "/usr/bin/flake8")

    targets = resolved_mod_paths if scoped else [repo_root]
    shared_root = shared_worktree_root(dir_path, repo_root)
    outside_modules = []
    if changed_set is not None and not target_modules_str and not run_all:
        outside_modules = changed_outside_modules(changed_set, repo_root, community_dir)

    def scope_for(step_stem, extra_suffixes=None):
        """The scan roots for a per-module checker: the changed modules, or the whole repository when a changed file
        outside every module is one this checker reads (daemons/, ingest/, scripts: a full run scans those trees too).
        A checker that reads only some file types is not widened by a change to another type (a story or a Rust file
        beside a JavaScript fix must not hand the burn list the whole repository)."""
        scan = _scope_for_modules(step_stem, extra_suffixes)
        if shared_root and step_stem in SHARED_WORKTREE_STEPS and (not scoped or shared_changed_for(step_stem)):
            return scan + [shared_root]
        return scan

    def shared_changed_for(step_stem):
        suffixes = STEP_META.get(step_stem, {}).get("suffixes")
        return any(
            path.startswith(shared_root + os.sep)
            and widens_module_scope(path)
            and (suffixes is None or os.path.splitext(path)[1].lower() in suffixes)
            for path in changed_set or ()
        )

    def _scope_for_modules(step_stem, extra_suffixes):
        if not outside_modules:
            return targets
        suffixes = STEP_META.get(step_stem, {}).get("suffixes") or extra_suffixes
        for path in outside_modules:
            if not widens_module_scope(path):
                continue
            if suffixes is None or os.path.splitext(path)[1].lower() in suffixes:
                return [repo_root]
        return targets

    # archive/: frozen, historical snapshots of past pipeline runs, not live
    # source (same exclusion, same reasoning, already established in
    # check_absolute_paths.py's own docstring) -- rewriting a real developer's
    # already-committed one-shot script to satisfy a linter would make the
    # archive inaccurate, not correct.
    #
    # .claude: passing a custom
    # --exclude replaces flake8's own default exclude list entirely (flake8 does
    # NOT append to its defaults unless --extend-exclude is used instead), so
    # without an explicit entry here flake8 descends into
    # ".claude/worktrees/<session>/" -- this project's own standing, documented
    # convention (see check_dependency_cycles.py's identical exclusion, shared by 5 sibling
    # checker scripts) of running agent sessions in isolated git worktrees
    # INSIDE the repo root. A fake .claude/worktrees/<sess>/
    # tree with one file containing a real, unambiguous F401 violation was scanned
    # and reported by flake8 with this exact --exclude value without the entry.
    # Without it, a lint run started while another session's worktree existed
    # could fail (or, depending on E9/F/E402 content, spuriously pass) based on that
    # OTHER session's own in-progress, uncommitted code -- not the real repo state
    # this invocation was actually scoped to check.
    flake8_targets = scope_for("flake8") if shared_root and not scoped else targets
    flake8_roots = tuple(os.path.realpath(r) + os.sep for r in [repo_root] + ([shared_root] if shared_root else []))
    if changed_set is not None and not target_modules_str and not run_all:
        # Changed mode: the changed Python files themselves (flake8 applies --exclude to
        # explicitly named files too), instead of every module directory.
        flake8_targets = sorted(
            path
            for path in changed_set
            if path.endswith(".py") and os.path.isfile(path) and path.startswith(flake8_roots)
        )
    flake8_argv = [
        flake8_cmd,
        *flake8_targets,
        "--exclude=venv,env,.venv,__pycache__,node_modules,target,daemons,archive,.claude",
        "--select=E9,F,E402",
        "--per-file-ignores=__init__.py:F401",
    ]
    if flake8_targets:
        _add(
            "flake8",
            flake8_argv,
            fail_header="❌ Flake8 Violations:",
            print_ok=False,
            missing_msg="❌ Flake8 executable not found.",
            inputs=(tuple(os.path.realpath(t) for t in flake8_targets), frozenset({".py", ".cfg", ".ini", ".toml"})),
        )

    # 8. check_burn_list -- one subprocess call per target, not one batched call with all of
    # `targets` appended (see `_run_per_target_checker`'s own docstring): `check_burn_list.py`'s
    # `directory` argument is `nargs="?"`, which argparse rejects outright given 2+ positional
    # values, so a multi-module scoped run used to crash this step entirely.
    _per_target_steps(_add, python_exec, dir_path, "check_burn_list.py", scope_for("check_burn_list"))

    # 9. verify_anchors -- anchor documentation, tests and cross-references are looked up across
    # the whole repository pair, so in `--changed` mode it always scans the whole repository
    # (about 5 s) and never just the changed modules. Scoped to hams_helpdesk it reported
    # `zero_sudo:persona_login` as undocumented, because the story citing it lives in
    # zero_sudo/docs/stories, outside the scope (2026-10-29, #957, merged with PRMERGE_SKIP_LINT).
    # The sibling repository is named explicitly: verify_anchors would otherwise look for it
    # beside the directory it was started from, and a worktree has no sibling there.
    # Under --changed the Rust checkers check the crates with a changed file and their dependents only (rust_impact.py); a full run, a run of
    # the checker tooling itself, or a list too long to pass checks every crate.
    rust_changed_args = (
        [rust_impact.CHANGED_FLAG] + sorted(changed_set)
        if changed_set is not None and not run_all and len(changed_set) <= rust_impact.MAX_PATHS
        else []
    )
    anchor_targets = [repo_root] if (changed_set is not None and not target_modules_str) else targets
    if anchor_targets == [repo_root] and sibling_dir and os.path.isdir(sibling_dir):
        # Both repositories are scanned as primary, as `verify_anchors.py . ../hams_open` does. Handed one
        # root, the other repository counts as non-primary and its documentation is not credited: a
        # hams_com test that cites a hams_open feature (`# Tests [@ANCHOR: zero_sudo:get_trusted_client_ip]`)
        # was reported as a feature "missing from documentation manuals" although the story is in
        # zero_sudo/docs/stories, and 211 cross-repository references as missing from source (2026-10-30:
        # eleven false findings on a clean main, so no hams_com branch could pass the pre-merge run).
        anchor_targets = [repo_root, os.path.realpath(sibling_dir)]
    if anchor_targets:
        anchor_env = dict(os.environ)
        if sibling_dir and os.path.isdir(sibling_dir):
            sibling_label = "HAMS_OPEN_TREE" if community_dir else "HAMS_COM_TREE"
            anchor_env[sibling_label] = os.path.realpath(sibling_dir)
        _add(
            "verify_anchors",
            [python_exec, os.path.join(dir_path, "tools", "verify_anchors.py")] + anchor_targets,
            env=anchor_env,
        )

    # 10. check_manifest_dependencies -- one call per target, not batched: this script reads
    # only `sys.argv[1]` (see `_run_per_target_checker`'s own docstring) and silently ignores
    # any additional targets appended after it.
    _per_target_steps(_add, python_exec, dir_path, "check_manifest_dependencies.py", scope_for("check_manifest_dependencies"))

    # 11. check_js_syntax
    js_syntax_targets = scope_for("check_js_syntax")
    if js_syntax_targets:
        _add(
            "check_js_syntax",
            [python_exec, os.path.join(dir_path, "tools", "check_js_syntax.py")] + js_syntax_targets,
        )

    # 12. check_test_tags -- one call per target, not batched (sys.argv[1]-only, see
    # `_run_per_target_checker`'s own docstring).
    _per_target_steps(_add, python_exec, dir_path, "check_test_tags.py", scope_for("check_test_tags"))

    # 13. check_absolute_paths -- one call per target, not batched (sys.argv[1]-only, see
    # `_run_per_target_checker`'s own docstring).
    _per_target_steps(_add, python_exec, dir_path, "check_absolute_paths.py", scope_for("check_absolute_paths"))

    # 14. check_rabbitmq_pool -- one call per target, not batched (sys.argv[1]-only, see
    # `_run_per_target_checker`'s own docstring).
    _per_target_steps(_add, python_exec, dir_path, "check_rabbitmq_pool.py", scope_for("check_rabbitmq_pool"))

    # 15. check_shebang -- one call per target, not batched (sys.argv[1]-only, see
    # `_run_per_target_checker`'s own docstring).
    _per_target_steps(_add, python_exec, dir_path, "check_shebang.py", scope_for("check_shebang"))

    # 16. check_summation_bias
    # Hardening, 2026-09-10: pin `cwd` to `repo_root` explicitly rather than inheriting
    # run_linters.py's own ambient process cwd. check_summation_bias.py resolves ITS OWN repo
    # root via `git rev-parse --show-toplevel`, which works correctly from any cwd already
    # inside the right repo (its own docstring documents that this was already fixed once for a
    # different reason) -- so this is not a confirmed live bug, no caller was found that invokes
    # run_linters.py from a cwd outside the repo it's scoped to. But every OTHER step in this
    # file that needs a specific repo root pins it explicitly (as an argv/positional, or via
    # `cwd=`, e.g. step 21's ESLint) rather than trusting whatever cwd happened to be ambient
    # when this process started -- this was the one silent exception. Pinning it here removes
    # that latent inconsistency at zero behavioral cost for every invocation seen in practice.
    _add(
        "check_summation_bias",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_summation_bias.py"),
    ],
        cwd=repo_root,
    )

    # 17. check_skill_integrity
    _add(
        "check_skill_integrity",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_skill_integrity.py"),
    ],
    )

    # 17b. check_claude_hook_registration -- the repository being linted (hams_com, hams_open or hams_shared) registers the
    # signal guard PreToolUse hook in its tracked .claude/settings.json (2026-11-09: an agent in a hams_shared worktree
    # had no guard and killed its own parent).
    _add(
        "check_claude_hook_registration",
        [python_exec, os.path.join(dir_path, "tools", "check_claude_hook_registration.py"), dir_path],
    )

    # 17c. check_backup_encryption_changes -- the change against origin/main adds no cipher setting, stanza creation, backup key generation
    # or secret-file overwrite without an approval entry naming Bruce (docs/APPROVED_BACKUP_ENCRYPTION_CHANGES.md).
    _add(
        "check_backup_encryption_changes",
        [python_exec, os.path.join(dir_path, "tools", "check_backup_encryption_changes.py"), dir_path],
    )

    # 18. check_init_imports -- one call per target, not batched (sys.argv[1]-only, see
    # `_run_per_target_checker`'s own docstring).
    _per_target_steps(_add, python_exec, dir_path, "check_init_imports.py", scope_for("check_init_imports"))

    # 19. check_dependency_cycles
    # Always scans the full repo (not the possibly-scoped `targets`) --
    # a cycle is a property of the whole manifest graph, not any single
    # module, and staying scoped could hide a cycle introduced by a
    # module outside the current target list.
    _add(
        "check_dependency_cycles",
        [python_exec, os.path.join(dir_path, "tools", "check_dependency_cycles.py"), dir_path],
    )

    # 19b. check_test_depends: audits the manifest `test_depends` key (unknown, redundant, self-referential, cyclic entries).
    # Whole-tree for the same reason as step 19.
    _add(
        "check_test_depends",
        [python_exec, os.path.join(dir_path, "tools", "check_test_depends.py"), dir_path],
    )

    # 20. check_self_writeable_field_tests
    # Same reasoning as step 19: this is a property of the whole repo's
    # anchor graph (a SELF_WRITEABLE_FIELDS override in one module can be
    # tested from that module's own tests/ dir only), not the possibly-
    # scoped `targets`.
    _add(
        "check_self_writeable_field_tests",
        [python_exec, os.path.join(dir_path, "tools", "check_self_writeable_field_tests.py"), dir_path],
    )

    # 21. ESLint (JS-side analogue of flake8, config shared from hams_shared/)
    # Flat config restricts scanning to the config's own directory tree, so
    # this must run with cwd set to the common parent of hams_com and
    # hams_open (the workspace root).
    # eslint and eslint.config.js live under the REAL hams_shared directory, which is not
    # necessarily `dir_path` -- `dir_path` is whatever repo run_linters.py was invoked from
    # (hams_com, hams_open, or hams_shared itself, all three valid per AGENTS.md), and only ONE
    # of those three literally IS hams_shared. An earlier version of this comment/code assumed
    # dir_path always equals hams_shared, which broke ESLint entirely (hard, unconditional
    # linters_failed=True) for the other two -- the far more common -- invocation modes:
    # `dir_path/node_modules/.bin/eslint` does not exist when dir_path is a
    # real repo root, only when dir_path is hams_shared itself. So resolve the REAL
    # hams_shared directory via realpath() (which follows the hams_open/tools and hams_com/tools
    # symlinks, unlike a dirname chain off `dir_path`), same technique as in
    # test_odoo_mypy_plugin.py -- correct regardless of which of the three valid roots this
    # script was invoked from.
    _real_tools_dir = os.path.dirname(os.path.realpath(__file__))
    hams_shared_dir = os.path.dirname(_real_tools_dir)
    eslint_config = os.path.join(hams_shared_dir, "eslint.config.js")
    workspace_root = os.path.dirname(os.path.dirname(hams_shared_dir))
    if sibling_dir and os.path.isdir(sibling_dir):
        # A worktree set (`<ws>/hams_open` + `<ws>/hams_com`) keeps both repos under one
        # directory that is not the real hams_shared's grandparent; the common parent of the
        # two repos is the same directory the old formula gave in the normal layout.
        workspace_root = os.path.commonpath([os.path.realpath(repo_root), os.path.realpath(sibling_dir)])
    # The toolchain (node_modules) belongs to the main hams_shared checkout; a worktree of
    # hams_shared has none of its own.
    toolchain = find_eslint_toolchain(hams_shared_dir)
    eslint_targets = scope_for("eslint", frozenset({".js", ".mjs", ".cjs", ".json"}))
    js_inputs = (
        tuple(os.path.realpath(t) for t in eslint_targets)
        + tuple(
            os.path.join(hams_shared_dir, name)
            for name in ("eslint.config.js", "tsconfig.json", "package.json")
        ),
        frozenset({".js", ".mjs", ".cjs", ".json"}),
    )
    if toolchain is None:
        # Not a skip: a JavaScript change cannot be called clean without the linter. The step
        # fails only when JavaScript is in scope (always in a full run, only when a JavaScript or
        # ESLint-config file changed in `--changed` mode), with the message that says why.
        steps.append(
            lint_exec.Step(
                name="eslint",
                static_output=(
                    f"❌ ESLint toolchain not found: no node_modules/.bin/eslint under {hams_shared_dir} "
                    "or the main hams_shared checkout. Run `npm install` in the main hams_shared "
                    "checkout; JavaScript cannot be called clean without it."
                ),
                roots=js_inputs[0],
                suffixes=js_inputs[1],
                gated=True,
            )
        )
    elif eslint_targets:
        # The type-aware rules read a TypeScript project listing the JavaScript files. Its
        # `include` was relative to hams_shared (`../*/static/**`), which is right only when the
        # repositories sit beside the main hams_shared checkout; in a worktree it listed another
        # tree's files and ESLint reported every file as "not found in the provided project".
        # The project is generated per run from the repositories being linted.
        eslint_project = write_eslint_project(hams_shared_dir, [repo_root, sibling_dir])
        eslint_env = eslint_node_env(os.environ)
        eslint_env["HAMS_ESLINT_TSCONFIG"] = eslint_project
        eslint_env["NODE_PATH"] = os.pathsep.join(
            [toolchain["node_modules"]] + ([eslint_env["NODE_PATH"]] if eslint_env.get("NODE_PATH") else [])
        )
        # --changed: the changed scripts and the files that import them, not every file of the changed modules
        # (narrow_eslint_targets; HAMS_LINT_ESLINT=whole keeps the whole directories).
        narrowed = None
        if (
            changed_set is not None
            and not target_modules_str
            and not run_all
            and os.environ.get("HAMS_LINT_ESLINT") != "whole"
        ):
            narrowed = narrow_eslint_targets(
                eslint_targets,
                changed_set,
                [os.path.join(hams_shared_dir, name) for name in ("eslint.config.js", "tsconfig.json", "package.json")],
            )
        _add(
            "eslint",
            [
                toolchain["eslint_bin"],
                "--config",
                eslint_config,
                "--no-error-on-unmatched-pattern",
            ] + (["--no-warn-ignored"] + narrowed if narrowed else eslint_targets),
            cwd=workspace_root,
            env=eslint_env,
            fail_header="❌ ESLint Violations:",
            print_ok=False,
            inputs=js_inputs,
        )

    # 22. check_model_extension_collisions
    # Same reasoning as step 19: a same-_name collision or a cross-module
    # _auto=False extension can be introduced by a module outside the
    # possibly-scoped `targets`, so this always scans the full repo.
    _add(
        "check_model_extension_collisions",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_model_extension_collisions.py"),
        dir_path,
    ],
    )

    # 23. check_untyped_utility_files (ODOO_AWARE_TYPE_CHECKING.md Phase 1)
    # Same reasoning as step 19/20/22: a plain-utility-file call-signature
    # bug can be introduced anywhere in daemons/ or ingest/, not just the
    # possibly-scoped `targets`, so this always scans the full repo's own
    # curated file set rather than `targets`.
    # In `--changed` mode only the changed Python files are checked (mypy runs on one file at a time, as flake8 does here):
    # the whole-tree scan reports every pre-existing finding in a file nobody touched, which no branch could clear.
    untyped_argv = [python_exec, os.path.join(dir_path, "tools", "check_untyped_utility_files.py"), dir_path]
    if changed_set is not None and not target_modules_str and not run_all:
        untyped_argv += ["--only-files"] + sorted(path for path in changed_set if path.endswith(".py") and os.path.isfile(path))
    _add(
        "check_untyped_utility_files",
        untyped_argv,
        print_ok=False,
    )

    # 24. check_pip_audit (CODE_REVIEW_PROCESS.md's Python supply-chain scan,
    # the direct parallel to cargo-deny/cargo-audit on the Rust side).
    # Same reasoning as step 19/20/22/23: a vulnerable dependency can be
    # introduced by any requirements.txt in the repo, not just `targets`.
    _add(
        "check_pip_audit",
        [python_exec, os.path.join(dir_path, "tools", "check_pip_audit.py"), dir_path],
        print_ok=False,
    )

    # 25. check_minified_js_nested_templates (rjsmin compatibility -- see
    # hams_com commit f1f00511 for the real bug this class of check was
    # written to catch). Same reasoning as step 19/20/22/23/24: a bundled
    # JS asset that gets re-minified by Odoo can be declared by any
    # module's manifest, not just the possibly-scoped `targets`, and the
    # vendored/sibling-repo asset it references may live outside
    # `dir_path` entirely, so this always scans the full repo and also
    # searches the sibling repo root for cross-repo asset references.
    _add(
        "check_minified_js_nested_templates",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_minified_js_nested_templates.py"),
        dir_path,
        sibling_dir,
    ],
    )

    # 26. check_external_library_locality -- same reasoning as step 25: a
    # module vendoring its own copy of a library instead of using
    # "external" can be introduced anywhere in the repo, not just the
    # possibly-scoped `targets`, so this always scans the full repo.
    _add(
        "check_external_library_locality",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_external_library_locality.py"),
        dir_path,
    ],
    )

    # 27. hams_shared/tools/ unit test suites -- the test_check_*.py files
    # verifying the checker scripts' OWN logic (test_check_burn_list.py,
    # test_check_dependency_cycles.py, etc.), not the checker scripts
    # themselves (steps 8/10/19/20/22/23/25/26 above already run those
    # against real repo content). Without this step, nothing ever ran
    # these suites at all -- confirmed directly, not assumed, before
    # adding it. A bare `test_*.py` glob with an explicit exclusion set,
    # not a narrower `test_check_*.py` glob: this directory's suite names
    # follow the checker script they cover (test_infrastructure.py,
    # test_run_linters.py, etc.), not all of them start with "check_", so
    # a `test_check_*` glob would silently skip most future suites -- the
    # exact failure this step exists to prevent, recurring by naming
    # accident. tools/test.py and tools/test_mcp_server.py also match a
    # bare `test_*.py` glob, but are Odoo test-runner/MCP-server launcher
    # scripts, not suites -- collecting them would execute their own
    # module-level Odoo-launching code instead of running unit tests, so
    # they're the ones excluded by name. Always the full glob, run once,
    # not scoped to `targets`: a checker's own logic bug isn't a property
    # of any one target module.
    _RUNNER_SCRIPTS_NOT_SUITES = {"test.py", "test_mcp_server.py"}
    tool_test_files = sorted(
        f
        for f in glob.glob(os.path.join(dir_path, "tools", "test_*.py"))
        if os.path.basename(f) not in _RUNNER_SCRIPTS_NOT_SUITES
    )
    # Suites that need THIS checkout or this login session (a real .git, hams_open copied to a scratch
    # directory, the user's systemd bus, the dev box's Odoo/ldap3/Chrome versions): they stay on the
    # machine that started the run, in a step of their own that --remote never sends away
    # (lint_remote.requirements_for). Measured 2026-11-02 on the Odoo test VM: 17 failures, all of them these.
    tool_test_files = _impact_select("tools/", os.path.join(dir_path, "tools"), tool_test_files)
    host_bound = sorted(f for f in tool_test_files if os.path.basename(f) in HOST_BOUND_TOOL_TESTS)
    tool_test_files = [f for f in tool_test_files if f not in host_bound]
    if host_bound:
        _add(
            "pytest tools/[host-bound]",
            [python_exec, "-m", "pytest", "-q"] + host_bound,
            cwd=os.path.join(dir_path, "tools"),
            fail_header="❌ hams_shared/tools/ unit test failures:",
            inputs=((os.path.realpath(os.path.join(dir_path, "tools")),), None),
        )
    # test_odoo_mypy_plugin.py runs a cold mypy with the Odoo plugin for each of its tests (22-61 s each, 10 of them in one
    # class; 474 s in one process at load 9-26, 640 s for the chunk that held it on a lint runner) and is skipped unless the
    # sibling repository is found, which is why the duration table never saw it (0.01 s). The tests are independent (each
    # makes its own scratch directory and mypy cache), so they get a step of their own spread over four workers by test
    # (`--dist load`, not loadfile). HAMS_LINT_MYPY_PLUGIN_SPLIT=0 keeps the file in the chunks.
    mypy_plugin_tests = (
        sorted(f for f in tool_test_files if os.path.basename(f) == "test_odoo_mypy_plugin.py")
        if os.environ.get("HAMS_LINT_MYPY_PLUGIN_SPLIT") != "0"
        else []
    )
    if mypy_plugin_tests:
        tool_test_files = [f for f in tool_test_files if f not in mypy_plugin_tests]
        _add(
            "pytest tools/[mypy-plugin]",
            [python_exec, "-m", "pytest", "-q"]
            + (["-n", "4", "--dist", "load"] if lint_exec.xdist_available(python_exec) else [])
            + mypy_plugin_tests,
            cwd=os.path.join(dir_path, "tools"),
            fail_header="❌ hams_shared/tools/ unit test failures:",
            inputs=((os.path.realpath(os.path.join(dir_path, "tools")),), None),
        )
    if tool_test_files:
        # The suites are independent files: four pytest processes instead of one that takes four minutes on its
        # own. The split is balanced by MEASURED seconds per file (lint_test_durations.json; a file never measured is
        # estimated from its size), not by file length: sizing by length left one chunk at 450-660 s and another at
        # 195 s. HAMS_LINT_CHUNKS=size restores the old split.
        chunk_count = min(4, len(tool_test_files))
        if os.environ.get("HAMS_LINT_CHUNKS") == "size":
            sized = sorted(tool_test_files, key=lambda f: -os.path.getsize(f))
            chunks = [sorted(sized[i::chunk_count]) for i in range(chunk_count)]
        else:
            chunks = lint_test_durations.balanced_chunks(tool_test_files, chunk_count)
        chunk_count = len(chunks)
        for chunk_index, chunk in enumerate(chunks):
            _add(
                f"pytest tools/[{chunk_index + 1}of{chunk_count}]",
                [python_exec, "-m", "pytest", "-q"] + chunk,
                cwd=os.path.join(dir_path, "tools"),
                fail_header="❌ hams_shared/tools/ unit test failures:",
                inputs=((os.path.realpath(os.path.join(dir_path, "tools")),), None),
            )

    # 28. hams_shared/scripts/ unit test suites -- same reasoning as step 27
    # above, for the one other directory of standalone dev-tooling scripts
    # this repo has. Found while adding test_run_headless_chrome.py: without
    # this step, nothing would ever run it (or any future scripts/test_*.py
    # suite) automatically either, the exact gap step 27 already closed for
    # tools/.
    scripts_test_files = _impact_select(
        "scripts/", os.path.join(dir_path, "scripts"), sorted(glob.glob(os.path.join(dir_path, "scripts", "test_*.py")))
    )
    if scripts_test_files:
        _add(
            "pytest scripts/",
            [python_exec, "-m", "pytest", "-q"] + scripts_test_files,
            cwd=os.path.join(dir_path, "scripts"),
            fail_header="❌ hams_shared/scripts/ unit test failures:",
            inputs=((os.path.realpath(os.path.join(dir_path, "scripts")),), None),
        )

    # 29. daemons/ standalone-daemon unit test suites -- same reasoning as
    # steps 27/28, for the one other class of directory this repo has full
    # of test_*.py files nothing ever runs automatically. Unlike tools/ and
    # scripts/ (one shared directory, one shared cwd), daemons/*/test_*.py
    # each need their OWN daemon directory as cwd (for a bare `import main`
    # or `import <daemon>_sync` to resolve), plus PYTHONPATH pointed at
    # daemons/ itself (for `import hams_config`, the shared RPC/download
    # helper every sync daemon imports) -- so this runs pytest once per
    # daemon directory rather than batching every file into one invocation
    # the way steps 27/28 do. Confirmed directly, not assumed, that nothing
    # else runs these: this repo's daemons/ is excluded by name from
    # check_burn_list.py's own directory walk (see
    # docs/proposals/LINTER_POLICY_REVISIT.md), and grepping run_linters.py
    # itself before this step found no other test_*.py discovery under
    # daemons/ at any level. ODOO_URL/DB_NAME are required, no-fallback
    # module-level config in hams_config.py and most daemons' own main.py
    # (this codebase's fail-fast policy) -- every test file already sets
    # dummy values via os.environ.setdefault() before importing, but they're
    # also set here so a test file that forgets to wouldn't silently import-
    # crash this whole step instead of failing its own assertions.
    daemon_test_files = sorted(
        glob.glob(os.path.join(dir_path, "daemons", "test_*.py"))
        + glob.glob(os.path.join(dir_path, "daemons", "*", "test_*.py"))
        + glob.glob(os.path.join(dir_path, "daemons", "*", "tools", "test_*.py"))
        + glob.glob(os.path.join(dir_path, "daemons", "*", "tests", "test_*.py"))
        # hams_simulated_bots/fine_tuning/ is a daemon SUBDIRECTORY with its
        # own separate .venv (torch/transformers/peft -- deliberately not
        # installed alongside the bot's own aiortc/whisper/piper deps), one
        # level deeper than any pattern above reaches. The literal "tests"
        # segment (as opposed to a bare wildcard) keeps this from ever
        # double-matching the existing "tools" pattern above.
        + glob.glob(os.path.join(dir_path, "daemons", "*", "*", "tests", "test_*.py"))
    )
    daemon_test_dirs = sorted({os.path.dirname(f) for f in daemon_test_files})
    daemons_root = os.path.join(dir_path, "daemons")
    daemon_env = dict(os.environ)
    daemon_env.setdefault("ODOO_URL", "http://test-odoo.invalid:8069")
    daemon_env.setdefault("DB_NAME", "hams_test")
    # pdns_sync's own main.py reads these at import time with
    # no fallback -- the exact same dummy defaults provision_environment()
    # already sets via env_vars.setdefault() elsewhere in this file, so this
    # step is self-sufficient rather than depending on inheriting them from
    # a fully-provisioned parent environment.
    daemon_env.setdefault("PDNS_API_URL", "http://powerdns:8081/api/v1/servers/localhost/zones")
    daemon_env.setdefault("PDNS_API_KEY", "secret")
    daemon_env.setdefault("RMQ_PORT", "5672")
    daemon_env.setdefault("RMQ_USER", "guest")
    daemon_env.setdefault("RMQ_PASS", "guest")
    daemon_env["PYTHONPATH"] = daemons_root + os.pathsep + daemon_env.get("PYTHONPATH", "")

    def _venv_python_for(start_dir):
        # A daemon with its own requirements.txt (e.g. hams_simulated_bots,
        # whose deps -- aiortc, av, faster-whisper -- have no business being
        # installed system-wide) provisions a .venv in its own directory or
        # an ancestor between it and daemons_root. Prefer that interpreter
        # over the generic python_exec when one exists, so this step
        # actually exercises what that daemon runs against instead of
        # failing on an import nothing outside its own venv ever provides.
        current = start_dir
        while True:
            candidate = os.path.join(current, ".venv", "bin", "python3")
            if os.path.exists(candidate):
                return candidate
            if current == daemons_root or len(current) <= len(daemons_root):
                return isolated_venv_marker_for(start_dir, daemons_root) or python_exec
            current = os.path.dirname(current)

    # Test directories of ONE daemon that run under the same interpreter are one pytest session
    # (hams_local_relay: its root, tests/ and tools/ were three processes; 25 + 3 + 110 = 138 tests,
    # the same 138 in one session). A daemon's directories under different interpreters
    # (hams_simulated_bots/tests and fine_tuning/tests have separate .venv) stay separate, and so do
    # directories that hold two test files of the same name (pytest would refuse to import both).
    # Different daemons are never merged: each has its own bare `main` module.
    # docs/LINT_STEP_ANALYSIS.md has the evidence.
    loose_daemon_files = tuple(
        os.path.join(daemons_root, name)
        for name in sorted(os.listdir(daemons_root))
        if os.path.isfile(os.path.join(daemons_root, name))
    ) if os.path.isdir(daemons_root) else ()
    daemon_groups = {}
    for test_dir in daemon_test_dirs:
        rel_dir = os.path.relpath(test_dir, daemons_root)
        daemon_dir = os.path.join(daemons_root, rel_dir.split(os.sep)[0])
        daemon_groups.setdefault((daemon_dir, _venv_python_for(test_dir)), []).append(test_dir)

    def _group_dirs(dirs):
        """Split one daemon+interpreter group into the sets that may share a session."""
        if len(dirs) < 2:
            return [[d] for d in dirs]
        names = [
            os.path.basename(f)
            for d in dirs
            for f in daemon_test_files
            if os.path.dirname(f) == d
        ]
        if len(set(names)) != len(names):
            return [[d] for d in dirs]
        return [dirs]

    for (daemon_dir, interpreter), dirs_of_daemon in daemon_groups.items():
        for dirs in _group_dirs(dirs_of_daemon):
            merged = len(dirs) > 1
            files_here = sorted(f for f in daemon_test_files if os.path.dirname(f) in dirs)
            files_here = _impact_select(
                f"daemons/{os.path.relpath(daemon_dir, daemons_root)}", daemon_dir, files_here, also=loose_daemon_files
            )
            if not files_here:
                continue
            # A test_dir one level below its daemon (tools/, tests/) imports its
            # daemon's own main module by bare name (e.g. hams_simulated_bots/
            # tests/test_main.py's `from main import ...`) -- that only
            # resolves with the daemon's own directory on the path too, not
            # just daemons_root.
            this_env = dict(daemon_env)
            if merged:
                run_dir = daemon_dir
                this_env["PYTHONPATH"] = daemon_dir + os.pathsep + this_env["PYTHONPATH"]
                step_name = f"pytest {os.path.relpath(daemon_dir, dir_path)}"
                aliases = tuple(f"pytest {os.path.relpath(d, dir_path)}" for d in sorted(dirs))
            else:
                run_dir = dirs[0]
                this_env["PYTHONPATH"] = os.path.dirname(run_dir) + os.pathsep + this_env["PYTHONPATH"]
                step_name = f"pytest {os.path.relpath(run_dir, dir_path)}"
                aliases = ()
            # A directory listed under "daemon_shards" in lint_test_durations.json (hams_simulated_bots/tests: 540 s
            # in one process) runs as several pytest sessions balanced by recorded seconds per file.
            # HAMS_LINT_DAEMON_SHARDS=0 restores the single session.
            sessions = lint_test_durations.daemon_shards(
                os.path.relpath(run_dir, dir_path), files_here,
                disabled=os.environ.get("HAMS_LINT_DAEMON_SHARDS") == "0",
            )
            for session_index, session_files in enumerate(sessions, 1):
                session_name = step_name if len(sessions) == 1 else f"{step_name}[{session_index}of{len(sessions)}]"
                _add(
                    session_name,
                    pytest_argv_prefix(interpreter, python_exec, dir_path) + session_files,
                    cwd=run_dir,
                    env=this_env,
                    fail_header=f"❌ {step_name[len('pytest '):]} unit test failures:",
                    inputs=((os.path.realpath(daemon_dir),) + loose_daemon_files, None),
                    aliases=aliases if len(sessions) == 1 else (),
                )

    # 30. check_access_csv_group_order -- catches the exact mistake that
    # silently broke the ENTIRE test suite once (not just one module's
    # tests): ham_aprs/__manifest__.py listed security/ir.model.access.csv
    # before the XML file defining the group it references, and Odoo's
    # whole boot died at that module with nothing after it in the -i list
    # ever running. Same reasoning as step 19/20/22/23: a module's own
    # data-list ordering bug can be introduced anywhere, not just the
    # possibly-scoped `targets`, so this always scans the full repo.
    _add(
        "check_access_csv_group_order",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_access_csv_group_order.py"),
        dir_path,
    ],
    )

    # 31. check_module_subpackage_imports -- catches a second, real bug of the same
    # shape as step 30's: a new ham_propagation/models/ subpackage was added (a real, correct
    # _inherit extension of ham.sked) but ham_propagation/__init__.py only ever imported
    # `controllers`, so Odoo's module loader never imported the new file at all -- the new
    # method was genuinely correct Python sitting on disk, invisible to Odoo, and the real
    # failure mode (a view validation error naming the method as "not a valid action") gave no
    # hint the actual cause was a missing top-level import. Same reasoning as step 19/20/22/23/30:
    # always scans the full repo.
    _add(
        "check_module_subpackage_imports",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_module_subpackage_imports.py"),
        dir_path,
    ],
    )

    # 32. check_registry_test_cr_usage -- catches a real, repeatedly-found bug: an
    # `is_test = vars(self.env.registry).get("test_cr") is not None` guard around
    # env.cr.commit() that is always False, since registry.test_cr isn't a real attribute
    # in this installed Odoo version. Seen in ham_relay_bridge (the guarded commit()
    # had literally never been exercised end-to-end by any test) and as the same latent,
    # not-yet-triggered bug in 10 places across user_websites. Same reasoning as
    # step 19/20/22/23/30/31: always scans the full repo.
    _add(
        "check_registry_test_cr_usage",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_registry_test_cr_usage.py"),
        dir_path,
    ],
    )

    # 33. check_xml_comment_double_hyphen -- catches a hard XML syntax error (a literal `--`
    # inside a <!-- --> comment) hit twice in one night writing explanatory security-rule
    # comments in an em-dash-style " -- " aside, both times only caught by a live module-load
    # crash during a real test run. Same reasoning as step 19/20/22/23/30/31/32: always scans
    # the full repo.
    _add(
        "check_xml_comment_double_hyphen",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_xml_comment_double_hyphen.py"),
        dir_path,
    ],
    )

    # 34. check_gdpr_erasure_uses_service_utility -- enforces that every _execute_gdpr_erasure()
    # override deletes records via zero_sudo.security.utils._erase_via_service_account() instead
    # of hand-rolling search()+unlink(). The hand-rolled shape is what hid a real bug in
    # ham_relay_bridge (a service account's own group memberships incidentally matched an
    # unrelated restrictive ir.rule, so its search() silently saw zero records and erasure was a
    # permanent no-op with no error anywhere) -- the utility verifies visibility against ground
    # truth and raises loudly instead of ever repeating that silent failure. Same reasoning as
    # step 19/20/22/23/30/31/32/33: always scans the full repo.
    _add(
        "check_gdpr_erasure_uses_service_utility",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_gdpr_erasure_uses_service_utility.py"),
        dir_path,
    ],
    )

    # check_night_shift_history -- the night-shift log is one markdown file per entry in night_shift_history/ (so that two
    # agents' pull requests never edit the same path). Once that directory exists, the old night_shift_history.md must be
    # the stub and every entry file must be well formed; a stale branch that appends to the old file fails here instead of
    # silently re-creating the conflict. A repository without the directory (hams_open, hams_shared) is not judged.
    _add(
        "check_night_shift_history",
        [
            python_exec,
            os.path.join(dir_path, "tools", "night_shift_history.py"),
            "check",
            dir_path,
        ],
    )

    # check_external_programs_documented -- ADR 0114 decision 5: every external program the relay launches or detects is in
    # the ADR's inventory, and every row that names an upstream repository is in dependency_watch.json and marked watched.
    # Scans the whole checkout (a repository without the relay source, hams_open, has nothing to scan but the watch check).
    _add(
        "check_external_programs_documented",
        [python_exec, os.path.join(dir_path, "tools", "check_external_programs_documented.py"), dir_path],
    )

    # check_anchor_docs_standard -- ADR 0113 step 2: every path of tools/anchor_doc_standard.txt keeps the anchor documentation
    # standard (no stub entries in a listed README, an anchor on every data structure in a listed source path).
    _add(
        "check_anchor_docs_standard",
        [python_exec, os.path.join(dir_path, "tools", "check_anchor_docs_standard.py"), dir_path],
    )

    # 35. check_cargo_deny -- the Rust supply-chain/license/advisory scan
    # check_pip_audit.py's own docstring already describes as its "direct
    # Python parallel," but was never actually wired in here at all: a real
    # gap: running `cargo deny check` by hand for the first
    # time in this codebase's history found 11 real, previously-
    # unremediated advisory/license/unmaintained findings in hams_local_relay
    # alone (an HTTP/2 unbounded-DATA-frame DoS vector among them). Same
    # reasoning as step 19/20/22/23/24/30/31/32/33/34: always scans the full
    # repo, not a possibly-scoped `targets` list.
    _add(
        "check_cargo_deny",
        [python_exec, os.path.join(dir_path, "tools", "check_cargo_deny.py"), dir_path],
    )

    # 36. check_cargo_clippy -- the Rust code-quality scan that is
    # flake8/ESLint's direct parallel on the Rust side, but -- like
    # cargo-deny above -- was never actually wired in here at all: a real
    # gap: running `cargo clippy` by hand for the first time
    # in this codebase's history found real warnings. Same reasoning as step 35: always scans
    # the full repo, not a possibly-scoped `targets` list.
    _add(
        "check_cargo_clippy",
        [python_exec, os.path.join(dir_path, "tools", "check_cargo_clippy.py"), dir_path] + rust_changed_args,
    )

    # 37. check_binary_manifest_consistency -- catches a real drift class seen when
    # bumping kopia/etcd/cloudflared: hams_shared/tools/binary_manifest.json (what
    # dependency_watch.json / check_dependency_releases.py track) and
    # binary_downloader/data/binary_manifest_data.xml (the actual runtime seed data
    # binary_downloader.mixin._download_and_extract() reads via the ORM) hold the same
    # url/checksum data in two different formats with nothing keeping them in sync -- a bump
    # that updates only the tracked JSON copy leaves production silently downloading the old
    # binary forever. Always scans the full repo, not a possibly-scoped `targets` list, for
    # the same reason as step 19/etc: the two files' relationship is a repo-wide property.
    _add(
        "check_binary_manifest_consistency",
        [python_exec, os.path.join(dir_path, "tools", "check_binary_manifest_consistency.py"), dir_path],
    )

    # 38. check_function_test_anchors -- ADR 0090's ratchet: Bruce's own
    # direct decision, 2026-09-04, "institute the CI requirement for new
    # code now, we will do the sweep when we get to that proposal."
    # Doesn't scan `targets` (a possibly-scoped module list) -- the
    # baseline is repo-wide, keyed by `repo_root`'s own basename, matching
    # the `function_test_anchor_baseline_<repo>.json` files generated
    # for both real repos.
    _add(
        "check_function_test_anchors",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_function_test_anchors.py"),
        repo_root,
        "--baseline",
        os.path.join(
            dir_path,
            "tools",
            f"function_test_anchor_baseline_{os.path.basename(main_checkout_root)}.json",
        ),
    ],
    )

    # 38b. check_private_by_default -- the debt side of hams_open/hams_rpc_guard: no rpc_exposure.json may list a public
    # method that is RPC-callable only because it was public when the file was generated (the baselines reached zero and are gone).
    _add(
        "check_private_by_default",
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_private_by_default.py"),
            repo_root,
        ],
    )

    # 38c. check_settings_secrets -- a secret-looking res.config.settings field must be masked by get_values; the unmasked
    # ones are debt in a checked-in baseline that can only shrink (default-deny RPC to-do 2cf07bad, hams1 inventory s.8).
    _add(
        "check_settings_secrets",
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_settings_secrets.py"),
            repo_root,
            "--baseline",
            os.path.join(
                dir_path,
                "tools",
                f"settings_secrets_baseline_{os.path.basename(main_checkout_root)}.json",
            ),
        ],
    )

    # 38d. check_doc_references -- Markdown must not point at files, script flags, ADRs, headings, units, config keys or
    # functions that do not exist. A baseline ratchet like step 38: the existing findings are recorded and only counted;
    # a new one fails. hams_shared's own Markdown is checked with the hams_open run (it is not a repository of its own here).
    _add(
        "check_doc_references",
        [
            python_exec,
            os.path.join(dir_path, "tools", "check_doc_references.py"),
            repo_root,
            "--baseline",
            os.path.join(
                dir_path,
                "tools",
                f"doc_references_baseline_{os.path.basename(main_checkout_root)}.json",
            ),
        ],
    )
    if os.path.basename(main_checkout_root) == "hams_open":
        _add(
            "check_doc_references_shared",
            [
                python_exec,
                os.path.join(dir_path, "tools", "check_doc_references.py"),
                dir_path,
                "--baseline",
                os.path.join(dir_path, "tools", "doc_references_baseline_hams_shared.json"),
            ],
        )

    # 39. check_js_function_test_anchors -- ADR 0090 decision 2's JS
    # sub-track: the same ratchet as step 38, extended to JS via a real
    # acorn AST parse (js_function_scan.cjs), not a regex approximation.
    # Same repo-wide, `targets`-independent baseline convention as step 38.
    _add(
        "check_js_function_test_anchors",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_js_function_test_anchors.py"),
        repo_root,
        "--baseline",
        os.path.join(
            dir_path,
            "tools",
            f"js_function_test_anchor_baseline_{os.path.basename(main_checkout_root)}.json",
        ),
    ],
    )

    # 40. check_rust_function_test_anchors -- ADR 0090 decision 2's Rust
    # sub-track: the same ratchet as steps 38/39, extended to Rust via a
    # real `syn` AST parse (rust_function_scan), not a regex
    # approximation. Same repo-wide, `targets`-independent baseline
    # convention as steps 38/39.
    _add(
        "check_rust_function_test_anchors",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_rust_function_test_anchors.py"),
        repo_root,
        "--baseline",
        os.path.join(
            dir_path,
            "tools",
            f"rust_function_test_anchor_baseline_{os.path.basename(main_checkout_root)}.json",
        ),
    ],
    )

    # 41. hams_com/ingest/ standalone-script unit test suites -- same
    # reasoning as steps 27/28/29 for the one other directory of test_*.py
    # files nothing runs automatically. Confirmed directly, not assumed:
    # ingest/ has no conftest.py, no CI workflow collects it, and steps
    # 27-29 only reach hams_shared/tools, hams_shared/scripts, and
    # daemons/ -- ingest/ is none of those, so its ~170 real tests (built
    # across the ADR 0090 FUNCTION_TEST_ANCHOR_SWEEP.md batches) were
    # guarding nothing until this step existed. ingest/ only exists in
    # hams_com, not hams_open, so this step is a no-op there.
    ingest_dir = os.path.join(repo_root, "ingest")
    ingest_test_files = sorted(glob.glob(os.path.join(ingest_dir, "test_*.py")))
    if ingest_test_files:
        _add(
            "pytest ingest/",
            [python_exec, "-m", "pytest", "-q"] + ingest_test_files,
            cwd=ingest_dir,
            fail_header="❌ ingest/ unit test failures:",
            inputs=((os.path.realpath(ingest_dir),), None),
        )

    # 41b. hams_com/devbox_tools/ unit test suites. Nothing ran them: no step above reaches devbox_tools/, so
    # test_no_external_browser_loads and test_relay_e2e_console_page (which read every tracked file and the console
    # source) went red on main and no lint run noticed (2026-11-07). The inputs stay the default (both repositories,
    # any file) because those two tests read files all over the tree.
    devbox_test_files = sorted(glob.glob(os.path.join(repo_root, "devbox_tools", "test_*.py")))
    if devbox_test_files:
        _add(
            "pytest devbox_tools/",
            [python_exec, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider"] + devbox_test_files,
            cwd=os.path.join(repo_root, "devbox_tools"),
            fail_header="❌ devbox_tools/ unit test failures:",
            # A lint batch holds hams_com in a directory of its own, with no hams_open beside it: the tests that read
            # the other repository look at HAMS_OPEN_TREE first (verify_anchors gets it the same way).
            # `-rs` lists every skipped test with its reason in the output: a lint host without a tool (chromium, nginx) skips
            # the tests that need it, and that must show in the run, not pass silently (2026-11-07).
            env=dict(os.environ, HAMS_OPEN_TREE=os.path.realpath(community_dir)) if community_dir else None,
        )

    # 42. check_owl_templates -- real, standalone Owl QWeb template compile verification via a
    # real headless Chrome. Built 2026-09-07 after a live-instrumented investigation
    # (OFFLINE_HAM_OPERATION.md) found ham_shack.WebShackTemplate had a genuine compile error (a
    # JS numeric separator inside a t-directive expression) that had cost multiple prior sessions
    # real time to root-cause, precisely because Odoo's own runtime error for this failure mode
    # is real but practically undiagnosable (a truncation-prone message with no line/column info).
    # Always scans the full repo, same reasoning as steps 19/20/22/23/30-34/etc.
    _add(
        "check_owl_templates",
        [python_exec, os.path.join(dir_path, "tools", "check_owl_templates.py"), repo_root],
    )

    # 43. check_minified_js_syntax_errors -- general backstop for the whole open-ended defect
    # class check_minified_js_nested_templates.py's step 25 only partially covers: rather than
    # pattern-matching source for one already-known-bad construct, this runs Odoo's own real
    # rjsmin.jsmin() on every bundled JS asset and validates the actual minified output with a
    # real `node --check`, so any future rjsmin miscompilation (not just nested template
    # literals) fails loudly here instead of shipping silently to production. Same reasoning as
    # step 25 for scope: always scans the full repo and the sibling repo root.
    _add(
        "check_minified_js_syntax_errors",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_minified_js_syntax_errors.py"),
        dir_path,
        sibling_dir,
    ],
    )

    # 44. check_hoot_runner_coverage -- a module can register a *.test.js
    # file in web.assets_unit_tests (syntactically valid, bundled, loadable
    # by hoot) with nothing in the Python test suite ever actually
    # executing it via browser_js(). That exact gap let a real, widespread
    # hoot bug (window.fetch reassignment throwing before any assertion
    # ran) sit unnoticed across 4 modules until 2026-09-09. Always scans
    # the full repo and the sibling repo root, same reasoning as step 25.
    _add(
        "check_hoot_runner_coverage",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_hoot_runner_coverage.py"),
        dir_path,
        sibling_dir,
    ],
    )

    _pair_roots = [dir_path] + ([sibling_dir] if sibling_dir else [])

    # 45. check_window_fetch_reassignment -- direct `window.fetch = ...`/
    # `window['fetch'] = ...` reassignment in a *.test.js file throws under
    # the real @odoo/hoot test harness (hoot's mocked window makes
    # window.fetch read-only); the sanctioned pattern is this repo's own
    # mockFetch() helper. Complementary to step 44 (check_hoot_runner_
    # coverage): that one checks a hoot suite is ever actually run at all,
    # this one checks the suite itself doesn't use a pattern that throws
    # before any assertion runs. Takes a single repo_dir argument, so run
    # once per repo rather than once for both like step 44.
    # One process scans both repositories (was one step per repository: two interpreters, two
    # tree walks; docs/LINT_STEP_ANALYSIS.md).
    _add(
        "check_window_fetch_reassignment",
        checker_argv(python_exec, dir_path, "check_window_fetch_reassignment.py", _pair_roots),
        print_ok=False,
    )

    # 46. check_js_test_hook_gating -- ADR 0094: a client-shipped JS file's TEST_*-prefixed
    # postMessage/event branches must be gated behind a real, server-substituted
    # TEST_HOOKS_ENABLED flag (a deliberate, per-deployment ir.config_parameter, never Odoo's
    # own test_enable -- that shape of test/prod branching is banned, see
    # ham_repeater_dir/models/ham_repeater_import.py's own comment). Both real
    # Service Workers (sw.js, shack_sw.js) shipped several such hooks completely unguarded in
    # every production deployment before this ADR. Real AST parse via acorn
    # (js_test_hook_gating_scan.cjs), same split-responsibility pattern as steps 39/40. No
    # baseline/ratchet: a brand-new rule with exactly two known real instances at introduction,
    # both already fixed -- any violation found is real and new, not grandfathered.
    _add(
        "check_js_test_hook_gating",
        [python_exec, os.path.join(dir_path, "tools", "check_js_test_hook_gating.py"), repo_root],
    )

    # 47. check_async_lock_scope -- Rust daemon gate born from a real bug in
    # hams_relay_bridge's telemetry_loop: a tokio::sync::RwLock read
    # guard bound at the top of a loop body with no explicit drop stayed held across a slow
    # network POST (Rust's lexical drop-scope rules don't shorten a guard's lifetime just
    # because the borrow checker's own "last use" analysis ends earlier) -- since
    # tokio::sync::RwLock is write-preferring, that stalled every other reader/writer in the
    # daemon for as long as the POST took. cargo clippy's own `await_holding_lock` lint
    # (step 36) doesn't catch this: it only fires for non-async-aware std::sync guards, never
    # tokio's own async-aware ones. This gate walks every .rs file in both repos looking for
    # exactly that shape. Same two-repo sweep as step 45 -- daemons live under hams_com, but
    # ham_digital_modes (a real Rust crate) lives under hams_open.
    # One process scans both repositories (was one step per repository: two interpreters, two
    # tree walks; docs/LINT_STEP_ANALYSIS.md).
    _add(
        "check_async_lock_scope",
        [python_exec, os.path.join(dir_path, "tools", "check_async_lock_scope.py"), *_pair_roots],
        print_ok=False,
    )

    # check_test_temp_dirs -- Rust test code must not build its own scratch directory under the temporary directory (never removed
    # when the test fails; the temporary directory may be memory). New uses fail; the existing ones are in
    # tools/test_temp_dir_baseline.json. One process scans both repositories.
    _add(
        "check_test_temp_dirs",
        [python_exec, os.path.join(dir_path, "tools", "check_test_temp_dirs.py"), *_pair_roots],
        print_ok=False,
    )

    # 48. check_fake_network_listener -- born alongside step 47, from the same
    # real-network-integration-test work that revealed hams_relay_bridge and dx_firehose had zero
    # tests exercising their own actual WebSocket routing (every prior test either checked
    # extracted pure logic directly or mocked Odoo's own HTTP responses). The pattern
    # (real TcpListener/websockets.serve on an ephemeral port, real connecting client, mock
    # only genuine external boundaries) is now established in both daemons; this gate flags a
    # regression toward the old anti-pattern -- a hand-rolled Fake/Mock/Dummy/Stub type named
    # for a network primitive (Socket/Listener/Stream/Connection/WebSocket/Channel/Transport)
    # in a file with no real bound listener anywhere in it. Same two-repo sweep as step 47.
    # One process scans both repositories (was one step per repository: two interpreters, two
    # tree walks; docs/LINT_STEP_ANALYSIS.md).
    _add(
        "check_fake_network_listener",
        checker_argv(python_exec, dir_path, "check_fake_network_listener.py", _pair_roots),
        print_ok=False,
    )

    # 49. check_int_float_overflow -- born from a real bug in
    # sota_sync.process_sota_csv()'s own _int() helper: int(float(val)) wrapped in
    # `except ValueError` looks like it safely handles any malformed numeric string, but
    # int(float("inf")) raises OverflowError, not ValueError -- Python's float() parses
    # "inf"/"nan"/"-infinity" (case-insensitively) with no exception at all, and only the
    # subsequent int() conversion of that non-finite value fails, with the "wrong" exception
    # type. One bad CSV cell would silently drop every row remaining in the file for that
    # whole sync cycle. This gate walks every .py file in both repos for int(<expr containing
    # a float(...) call>) not guarded by a try/except that would catch OverflowError. Same
    # two-repo sweep as steps 47/48.
    # One process scans both repositories (was one step per repository: two interpreters, two
    # tree walks; docs/LINT_STEP_ANALYSIS.md).
    _add(
        "check_int_float_overflow",
        [python_exec, os.path.join(dir_path, "tools", "check_int_float_overflow.py"), *_pair_roots],
        print_ok=False,
    )

    # 50. check_float_isfinite -- born from roughly fifteen separate real instances of
    # the same bug: float(x)
    # parses "nan"/"inf"/"-infinity" with NO exception at all, so a `try: float(x) except
    # ValueError: ...` block -- which reads as "safely parse this untrusted text" -- silently
    # lets a non-finite value through. This gate flags a float(...) call inside a try/except
    # ValueError with no math.isfinite() call anywhere in the enclosing function. Same two-repo
    # sweep as steps 47/48/49; validated silent on every fixed site after adding
    # float-isfinite-ignore escape hatches to the confirmed-safe remainder.
    # One process scans both repositories (was one step per repository: two interpreters, two
    # tree walks; docs/LINT_STEP_ANALYSIS.md).
    _add(
        "check_float_isfinite",
        [python_exec, os.path.join(dir_path, "tools", "check_float_isfinite.py"), *_pair_roots],
        print_ok=False,
    )

    # check_boundary_hazards -- five shapes of bug the 2026-11-09 bug hunts found by hand, in one pass over every .py/.sh file of both
    # repositories (hams_shared/tools/check_boundary_hazards.py docstring): a credential header sent through urllib with no
    # redirect-refusing handler; a raw command line in a log call; a public @api.model method that acts as a service account and
    # never checks its caller (RPC-callable by any signed-in user); a `gh --json files` listing (capped at 100) treated as complete;
    # gmtime/fromtimestamp of an unchecked outside number. Known, not yet fixed findings sit in boundary_hazards_known.json with an
    # owner and a reason; an entry that no longer matches a finding fails. One process over both roots, like steps 47-50.
    _add(
        "check_boundary_hazards",
        [python_exec, os.path.join(dir_path, "tools", "check_boundary_hazards.py"), *_pair_roots],
        print_ok=False,
    )

    # check_secret_storage -- a secret written to ir.config_parameter (set_param, config_parameter=, an XML data record) or held in a
    # Char/Text/Binary field named like a secret, instead of the secret store (hams_open zero_sudo/daemon/secret_store.py): the web
    # process, a database dump and a settings read over RPC can all read those. Secrets not migrated yet sit in
    # secret_storage_known.json with an owner and a reason; an entry that no longer matches a finding fails. Both roots, one process.
    _add(
        "check_secret_storage",
        [python_exec, os.path.join(dir_path, "tools", "check_secret_storage.py"), *_pair_roots],
        print_ok=False,
    )

    # 51. check_res_config_settings_access -- ADR-0096, born from a real
    # case where saving ANY Settings page could crash with an
    # AccessError: user_websites/security/ir.model.access.csv
    # granted group_user_websites_administrator (not a base.group_system
    # group) full read/write/create/unlink on the ENTIRE shared
    # res.config.settings model -- ir.model.access.csv grants are per
    # (model, group), never per field, so that was never actually scoped to
    # user_websites' own 3 settings fields; it handed that role read/write
    # on every OTHER installed module's settings too (for example
    # distributed_redis_cache's redis_password, cloudflare's
    # cloudflare_api_token). Same reasoning as step 30: always scans the
    # full repo, since a module adding this kind of row can happen anywhere.
    _add(
        "check_res_config_settings_access",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_res_config_settings_access.py"),
        dir_path,
    ],
    )

    # 51b. relay structural tests -- the relay's fast group of source-scanning `cargo test` tests (relay_structural.py): the privilege inventory, the
    # exposure audit, the keying and key-material scans, the demo-lock route table, the manuals rules. Lint ran none of the relay's cargo tests, and three
    # relay merges on 2026-11-08 turned relay main red with tests of exactly this kind. Selected when a file of the relay, of a crate it depends on or of a
    # crate that depends on it changes (`watched_dirs`); the merge queue's combined run selects it the same way. Not cached: the tests read the sources
    # and tables of the whole crate, and a recorded pass would hide a rename.
    relay_dirs = relay_structural.watched_dirs(repo_root)
    if relay_dirs:
        _add(
            "relay structural tests",
            [python_exec, os.path.join(dir_path, "tools", "relay_structural.py"), repo_root],
            inputs=(tuple(relay_dirs), None),
            cacheable=False,
        )

    # 52. check_cargo_fmt -- `cargo fmt -- --check` over the same crates step 36 lints. CI runs
    # this check on only one relay leg, which can start hours after a push; on 2026-09-15 three
    # rustfmt-only fix commits followed tested, clippy-clean relay feature commits. Check only,
    # never a rewrite, since this runs in the shared working tree. Same reasoning as step 35:
    # always scans the full repo, not a possibly-scoped `targets` list.
    _add(
        "check_cargo_fmt",
        [python_exec, os.path.join(dir_path, "tools", "check_cargo_fmt.py"), dir_path] + rust_changed_args,
    )

    # 53. check_claims_freshness -- ADR 0091's own real, named follow-on ("not yet wired into
    # run_linters.py as of this ADR... a real, named follow-on, not forgotten"), wired in
    # as a hard gate after a triage pass brought the real stale-claim backlog to 0 everywhere
    # outside `ingest/`, `ics_forms/`, and `ics_training/`.
    # Those three directories are excluded from the checker's own scan (see
    # check_claims_freshness.py's EXCLUDED_TOP_LEVEL_DIRS), not scoped here, since other
    # sessions have real, active, in-progress work landing throughout them. Always
    # scans `repo_root` (not the possibly-scoped `targets`), same reasoning as step 38: a claim
    # documents one function anywhere in the repo, not a property of any single target module.
    _add(
        "check_claims_freshness",
        [python_exec, os.path.join(dir_path, "tools", "check_claims_freshness.py"), repo_root],
    )

    # 54. check_xml_attribute_position_comment -- a defensive, STYLE-ONLY convention: flags an
    # XML comment placed as a direct sibling of <attribute> elements inside an
    # <xpath ... position="attributes"> block. Originally added (2026-09-28/29/2026-10-01) as a
    # suspected Odoo-core silent-failure trap, but a direct reproduction against Odoo 19's
    # apply_inheritance_specs (2026-10-02, hams_com PR #512 / 551db4c1) disproved that: the
    # attributes branch iterates only <attribute> tags and skips comments, so no attribute is
    # lost. Kept because comments as inheritance-spec siblings are unusual and easy to move
    # before the <xpath>. Always scans the full repo, same reasoning as
    # check_xml_comment_double_hyphen.py immediately above: a view's own structural shape is
    # not a property of any single lint target module.
    _add(
        "check_xml_attribute_position_comment",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_xml_attribute_position_comment.py"),
        dir_path,
    ],
    )

    # 55. check_doc_images -- no documentation page may show a fake, blank or copied placeholder
    # "screenshot" (the AI-generated 'Nexus ERP' New Sales Order form that ~40 manuals carried until
    # 2026-10-11), and every image a manual references must exist. Always scans both repos, same
    # reasoning as check_xml_comment_double_hyphen.py: not a property of any one target module.
    _add(
        "check_doc_images",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_doc_images.py"),
        dir_path,
    ],
    )

    # 56. check_scss_math_functions -- Odoo compiles .scss with libsass, which EVALUATES min()/max()
    # and fails on mixed units (`min(12rem, 100%)`: "Incompatible units"); Odoo then swaps the whole
    # web.assets_frontend bundle for its CSS-error stub, so every page of the site loses its
    # styling (hams.com, 2026-10-11/12). Scans every .scss in BOTH repos regardless of target
    # modules, for the same reason as step 55: a stylesheet's compile result is not a property of
    # any one module (one broken file breaks the shared bundle for all of them).
    scss_roots = [dir_path]
    for sibling_name in ("hams_com", "hams_open"):
        sibling_root = _sibling_checkout(repo_root, main_checkout_root, sibling_name)
        if os.path.isdir(sibling_root) and os.path.realpath(sibling_root) != os.path.realpath(main_checkout_root):
            scss_roots.append(sibling_root)
    _add(
        "check_scss_math_functions",
        [
        python_exec,
        os.path.join(dir_path, "tools", "check_scss_math_functions.py"),
        *scss_roots,
    ],
    )

    steps = _finalise_steps(steps, os.path.basename(main_checkout_root.rstrip(os.sep)))
    skipped = []
    if options.only or options.skip:
        steps = _filter_by_name(steps, options.only, options.skip)
    if changed_set is not None:
        steps, skipped = lint_exec.select_for_changed(steps, changed_set, run_all)
        if not options.list_steps:
            print(
                f"--changed {options.changed}: {len(changed_set)} changed files; running "
                f"{len(steps)} steps, skipping {len(skipped)} whose inputs are unchanged"
                + (" (checker tooling changed: running every step)" if run_all else "")
            )
            if skipped:
                print("   skipped: " + ", ".join(step.name for step, _reason in skipped))
            for note in impact_notes:
                print(note)
    if options.list_json:
        print(json.dumps([lint_exec.step_to_dict(step) for step in steps], indent=1))
        sys.exit(0)
    if options.list_steps:
        for step in steps:
            print(step.name)
        sys.exit(0)

    cache = None
    fingerprints = None
    if options.cache:
        cache = lint_exec.Cache(_cache_path(dir_path))
        fingerprints = lint_exec.InputFingerprints(os.path.realpath(tools_dir))

    pending = [
        step.name
        for step in steps
        if step.argv is not None
        and not (cache is not None and step.cacheable and cache.hit(step.name, fingerprints.step_key(step)) is not None)
    ]
    # --remote: capable steps run on other machines (lint_remote.py); the rest, and anything a machine
    # cannot do, runs here.
    remote_jobs = []
    whole_run = False
    all_steps = list(steps)
    head_repo = stamped_head_repo(repo_root, dir_path, layout)
    head_before = _git_lines(head_repo, "rev-parse", "HEAD")
    # Coalescing batch (ADR 0115): a heavy, plain, committed `--changed` run joins the batch of this repository
    # instead of queueing alone; the leader runs one combined tree, every member gets its own verdict.
    batch_durations = lint_lock.known_durations() if pending else {}
    batch_lock_needed, _batch_why = lint_lock.lock_decision(os.environ, pending, batch_durations)
    # A hams_shared branch worktree (`layout` is set) is batched through its own repository, not the layout's
    # hams_open that `repo_root` names then; its combined tree is a hams_shared worktree (lint_batch.py).
    member_repo = dir_path if layout is not None else repo_root
    member_name = lint_batch.SHARED_TREE_NAME if layout is not None else os.path.basename(main_checkout_root.rstrip(os.sep))
    member_head = _git_lines(member_repo, "rev-parse", "HEAD") if layout is not None else head_before
    status_lines = tree_status_lines(member_repo)
    batch_reason = lint_batch.ineligible_reason(
        changed=options.changed, only=options.only, skip=options.skip, modules=target_modules_str,
        solo=options.solo, bundle=options.run_bundle, environ=os.environ,
        dirty=status_lines is None or bool(status_lines),
        is_shared_checkout=os.path.realpath(dir_path) == real_shared_dir and layout is None,
        lock_needed=batch_lock_needed,
        foreign_shared=layout is None and lint_batch.foreign_shared_checkout(repo_root, main_checkout_root),
        batching_tools=lint_batch.batching_tools_changed(
            lint_batch.changed_paths(member_repo, options.changed, "HEAD")) if layout is not None and options.changed else (),
    )
    if batch_reason is None and member_head:
        branch_lines = _git_lines(member_repo, "rev-parse", "--abbrev-ref", "HEAD")
        branch_name = branch_lines[0] if branch_lines and branch_lines[0] != "HEAD" else member_head[0][:10]
        passthrough = ["-j", str(options.jobs)]
        for flag, value in (("--lock-timeout", options.lock_timeout), ("--load-limit", options.load_limit),
                            ("--load-timeout", options.load_timeout)):
            if value is not None:
                passthrough += [flag, str(value)]
        batch_code = lint_batch.run_member(
            member_repo,
            member_name,
            branch_name,
            member_head[0],
            options.changed,
            pending,
            lint_batch.LintBatch(member_repo, member_name, options.changed, passthrough, write_batch_stamp),
            timeout=options.lock_timeout,
            max_running=lint_batch.batch_limit(os.environ),
            # Steps run `-j` at a time and many have no record: estimate from the known ones, spread over the jobs.
            estimate=lambda names: sum(batch_durations.get(name, 0.0) for name in names) / max(1, options.jobs),
        )
        if batch_code is not None:
            sys.exit(batch_code)
    elif batch_reason and options.changed is not None:
        print(f"[*] lint batch: running alone ({batch_reason})")
    if options.distribute == "off" and options.remote == "auto" and not options.where:
        options.remote = "local"
    if options.remote and options.remote != "local" or options.where:
        if os.path.realpath(dir_path) == real_shared_dir and layout is None:
            print("[*] --remote: not supported when started from a hams_shared checkout; running everything here")
            if options.where:
                sys.exit(0)  # --where never runs anything
        else:
            roots, role = lint_remote.repo_roles(repo_root, sibling_dir, real_shared_dir)
            durations_known = lint_lock.known_durations()
            if options.distribute != "off" and (options.remote or "auto") == "auto":
                takes_lock, _why = lint_lock.lock_decision(os.environ, pending, durations_known)
                busy, busy_why = lint_lock.local_busy(os.environ)
                whole_run = takes_lock and busy
                if whole_run:
                    print(f"[*] --remote: {busy_why}: looking for an idle machine to take the whole run (overflow)")
            steps, remote_jobs = lint_remote.dispatch(
                options.remote or "auto",
                steps,
                pending,
                durations_known,
                {step.name: lint_exec.step_to_dict(step) for step in steps},
                roots,
                role,
                os.cpu_count() or 1,
                os.getloadavg()[0],
                where=options.where,
                whole=whole_run,
                claim=lint_lock.try_claim,
                claimed=lint_lock.claimed,
                odoo_lock=lint_lock.try_odoo_test_lock,
                unfit=lint_remote.load_unfit(),
            )
            if options.where:
                sys.exit(0)
            pending = [name for name in pending if name in {s.name for s in steps}]
    # Heavy runs queue one at a time per machine, then wait for the load to fall (lint_lock.py).
    lock_wait_started = time.monotonic()
    run_ok, run_lock = lint_lock.begin_heavy_run(
        os.environ,
        pending,
        lint_lock.known_durations() if pending else {},
        os.path.realpath(dir_path),
        " ".join(["run_linters.py"] + sys.argv[1:])[:160],
        lock_timeout=options.lock_timeout,
        load_limit_override=options.load_limit,
        load_timeout_override=options.load_timeout,
        residue=bool(remote_jobs) and whole_run,
    )
    if not run_ok:
        sys.exit(lint_lock.EXIT_TEMPFAIL)
    lock_waited = time.monotonic() - lock_wait_started
    if run_lock:
        import atexit

        atexit.register(run_lock.release)
        slot_env = lint_lock.enter_slot(run_lock)
        for step in steps:
            if slot_env and step.env is not None:  # steps whose environment was captured before the slot was known
                step.env = dict(step.env, **slot_env)
    # Longest critical path first, by the median measured duration of each step over every recorded run (not only this checkout's).
    # Extra workers join while the machine is mostly idle (lint_lock's burst rule): one run used about two cores on average.
    # Test steps go through the result cache keyed by the files they really read (lint_trace_cache.py); --no-cache bypasses it.
    trace_cache = lint_trace_cache.open_cache(repo_root, tools_dir, fingerprints.versions() if fingerprints else lint_exec.tool_versions())
    results = lint_exec.execute(steps, options.jobs, cache=cache, fingerprints=fingerprints, durations=batch_durations or lint_lock.known_durations(),
                                extra=lint_exec.elastic_jobs(options.jobs), trace_cache=trace_cache)
    fallback_lock = [run_lock]

    def execute_here(wanted):
        """Steps that come back from a machine (it could not run them, or they failed there and are confirmed
        here) are heavy work on this machine: take the run lock and the load gate for them if this run does
        not hold the lock yet (a run that shipped everything away holds none)."""
        if fallback_lock[0] is None:
            ok, taken = lint_lock.begin_heavy_run(
                os.environ,
                [step.name for step in wanted],
                lint_lock.known_durations(),
                os.path.realpath(dir_path),
                " ".join(["run_linters.py"] + sys.argv[1:])[:160],
                lock_timeout=options.lock_timeout,
                load_limit_override=options.load_limit,
                load_timeout_override=options.load_timeout,
                residue=whole_run,
            )
            if not ok:
                sys.exit(lint_lock.EXIT_TEMPFAIL)
            if taken:
                import atexit

                atexit.register(taken.release)
                fallback_lock[0] = taken
        return lint_exec.execute(wanted, options.jobs, durations=lint_lock.known_durations())

    remote_siblings = []
    extra_results, remote_hosts = lint_remote.collect(
        remote_jobs, all_steps, execute_here, lint_exec.Result, on_unfit=lint_remote.record_unfit, siblings_out=remote_siblings
    )
    results += extra_results
    results, timeout_retried = retry_timed_out_steps(results, all_steps)
    # Known-red baseline (ADR 0117): failures that were already red on main, with the same message, do not block.
    failed_now = {r.name: r.output or "" for r in results if r.returncode != 0}
    baseline_verdict = None
    known_red = ""
    if failed_now:
        import lint_baseline  # not at the top: the remote bundle side (run_bundle) never needs it
    if failed_now and lint_baseline.enabled():
        baseline_verdict = lint_baseline.judge(
            failed_now,
            "hams_shared" if layout is not None else os.path.basename(main_checkout_root.rstrip(os.sep)),
            dir_path if layout is not None else repo_root,
        )
        if baseline_verdict["passes"]:
            known_red = lint_baseline.stamp_field(baseline_verdict["tolerated"], baseline_verdict["main"])
    known_red += timeout_retried_field(timeout_retried)
    if options.results_json:
        with open(options.results_json, "w", encoding="utf-8") as handle:
            document = lint_remote.results_document("local", results)
            document["known_red"] = known_red
            document["timeout_retried"] = timeout_retried
            # The combined run of a batch (lint_batch.py) attributes failures from these.
            document["selected"] = [step.name for step in all_steps]
            document["outputs"] = {r.name: (r.output or "")[-200000:] for r in results if r.returncode != 0}
            json.dump(document, handle)
    wall = time.monotonic() - wall_start
    linters_failed = any(r.returncode != 0 for r in results)
    if baseline_verdict is not None and baseline_verdict["passes"]:
        linters_failed = False
    missing = steps_without_result([step.name for step in all_steps], results)
    if missing:
        # Checked after the known-red baseline: nothing tolerates a step that never reported.
        print(
            f"[!] {len(missing)} selected step(s) produced no result: {', '.join(missing)}. A step that did not report is not a "
            "pass, so this run fails and writes no pass stamp. Cause: the step was dropped between selection and reporting (a "
            "remote host that returned nothing for it, or an executor fault). Next: rerun; if it repeats, run the step alone "
            "with `--only`."
        )
        linters_failed = True
    if options.timing or options.timing_top:
        table = lint_exec.timing_table(results, skipped, wall, top=options.timing_top or None)
        if lock_waited >= 1:
            table += f"\n(the wall clock includes {lock_waited:.0f} s of waiting for the lint run lock and the load gate)"
        print("\n" + table, file=sys.stderr if options.timing_to_stderr else sys.stdout)

    if baseline_verdict is not None and baseline_verdict["tolerated"]:
        print("\n".join(lint_baseline.report_lines(baseline_verdict)))
    elif baseline_verdict is not None and failed_now:
        print("[*] no known-red baseline applies (none recorded for this repository, or it is switched off); "
              "`run_linters.py --baseline-refresh` records main's own failures")
    if not linters_failed and not (options.only or options.skip or target_modules_str):
        if _git_lines(head_repo, "rev-parse", "HEAD") != head_before:
            print("(pass stamp not written: HEAD moved during the run)", file=sys.stderr)
        else:
            _write_pass_stamps(
                stamp_repos(repo_root, tools_dir, dir_path, layout),
                "changed" if changed_set is not None else "full",
                [step.name for step in all_steps],
                remote_hosts,
                known_red,
                remote_siblings,
                impact_counts,
            )

    if linters_failed:
        print("\n🛑 Halting due to linter violations. Please review the output above.")
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
