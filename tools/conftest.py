# SPDX-License-Identifier: AGPL-3.0-or-later
"""pytest configuration for the tests in hams_shared/tools.

Hypothesis fails a test whose single example takes longer than its default deadline (200 ms). That is a timing check, not
a correctness one, and the lint runs share the machine with other agents' runs: a test that takes 1 ms alone failed with
"Unreliable test timings: 251 ms, then 0.92 ms" at load 14-23 (test_check_async_lock_scope.py, 2026-11-03). No example of these
tests is meant to be slow, so the deadline is off; the properties are still checked in full.
"""

try:
    from hypothesis import settings
except ImportError:  # hypothesis is only needed by the tests that use it
    settings = None

if settings is not None:
    settings.register_profile("hams_lint", deadline=None)
    settings.load_profile("hams_lint")


import os

import pytest


@pytest.fixture(autouse=True)
def _no_sibling_tree_override(monkeypatch):
    """A test never sees the caller's `HAMS_COM_TREE` / `HAMS_OPEN_TREE`.

    `lint_exec.sibling_repo_root` takes those variables ahead of every directory it would find, so a developer who
    exported one to run the anchor scan made 24 tests of the cargo, pip-audit and dependency-cycle checkers fail (they
    build a throw-away repository and expect no sibling; the real hams_open was found instead, 2026-11-07). A test that
    needs the override sets it itself, after this fixture."""
    for name in ("HAMS_COM_TREE", "HAMS_OPEN_TREE"):
        if name in os.environ:
            monkeypatch.delenv(name)
