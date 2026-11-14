"""Find the test_*.py files no other lint step collects, so a new suite cannot be left unrun.

run_linters.py has dedicated steps for hams_shared/tools, scripts/, devbox_tools/, ingest/ and daemons/ (COLLECTED_BY_OTHER_STEPS), and Odoo
runs <module>/tests. Every other test_*.py is run by `pytest <directory>/` steps (one per directory, cwd = that directory), except the
directories in BASELINE, each with the reason it cannot run on a plain lint host.
"""
import os
import re

SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "vendor", "target", "__pycache__", "tmp", "scratch"})

# Relative (posix) paths of test files that another step already collects: the same globs as run_linters.py steps 27-29, 41, 41b.
COLLECTED_BY_OTHER_STEPS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(tools|scripts)/[^/]+$",
        r"^(devbox_tools|ingest)/[^/]+$",
        r"^daemons/(?:[^/]+/)?[^/]+$",
        r"^daemons/[^/]+/(?:tools|tests)/[^/]+$",
        r"^daemons/[^/]+/[^/]+/tests/[^/]+$",
    )
)

# file or directory (relative, posix; a directory covers everything below it) -> why no step runs it.
BASELINE = {
    "hardware/test_generate_netlist.py": "imports skidl (KiCad netlist generator), not installed on lint hosts",
    "daemons/shack_console/tools/stt_accuracy": "imports av, which only the hams_simulated_bots venv has",
    "docs/Clubs/scripts": "test_normalize.py and test_regions.py are pipeline scripts that open the clubs database, not tests",
    "hams_shared/tools/odoo_type_stubs": "type stubs of Odoo's own test modules, not tests",
    "hams_shared/tools/restoration_system/e2e": "HEAVY: needs passwordless sudo, debootstrap, QEMU/KVM and the network; run by run_heavy.py",
    "distributed_redis_cache/scripts/test_provision_cache_manager_db_role.py": "needs a live PostgreSQL (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE)",
    "daemons/bench_mcp/src/glasgow/test_ops_harness.py": "a command-line harness for ops.py (takes ENTRY APPLET OP JSON), not a test suite",
    "daemons/cloudflared/component-tests": "vendored upstream cloudflared (Go) tests that need its Go build tree and a live Cloudflare edge",
}


def _baseline_reason(rel):
    for prefix, reason in BASELINE.items():
        if rel == prefix or rel.startswith(prefix + "/"):
            return reason
    return None


def _is_module_test(root, rel):
    """An Odoo addon's <module>/tests/test_*.py: Odoo runs it."""
    parts = rel.split("/")
    return len(parts) == 3 and parts[1] == "tests" and os.path.isfile(os.path.join(root, parts[0], "__manifest__.py"))


def find_uncollected(root):
    """({relative directory: [test file names]} for the directories to run, same for the baselined ones)."""
    run, baselined = {}, {}
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(current, d)))
        rel_dir = os.path.relpath(current, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in sorted(files):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            shared_rel = rel[len("hams_shared/"):] if rel.startswith("hams_shared/") else rel  # hams_open holds hams_shared as a real directory
            if any(p.match(shared_rel) for p in COLLECTED_BY_OTHER_STEPS) or _is_module_test(root, rel):
                continue
            target = baselined if _baseline_reason(rel) else run
            target.setdefault(rel_dir, []).append(name)
    return run, baselined
