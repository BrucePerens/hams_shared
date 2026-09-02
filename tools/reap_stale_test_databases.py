#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Drops ad-hoc scoped-test PostgreSQL databases that outlived the test run that created them.

Every scoped-test invocation in this codebase's own established convention (see test.py's own
AI DIRECTIVE header, and night_shift_todo.md) creates a throwaway database via
`sudo -u postgres createdb`, runs one `odoo --test-tags ...` pass against it, then drops it via
`sudo -u postgres dropdb --if-exists`. That cleanup step only runs if the test run reaches it --
a killed tool call, a crashed test process, or an interrupted session skips it and leaves the
database behind permanently. Found 2026-09-02: 87 such leaked databases had accumulated on one
dev box, consuming several GB of a nearly-full /var partition. No amount of care at the
*creation* site fixes this, because the failure mode is specifically "the process that would
clean up never got to run" -- the only fix robust against every failure mode is a reaper that
doesn't depend on the creator's own cleanup path succeeding at all.

**Naming convention, load-bearing, not cosmetic**: this reaper only ever considers a database
whose name starts with SCRATCH_DB_PREFIX ("tmp_") -- it is an opt-in allowlist by construction,
the safe direction for a destructive default. Every ad-hoc scoped-test database created anywhere
in this codebase's own conventions (manual `createdb` invocations, this project's own dev-session
test runs) MUST use this prefix from now on (e.g. `tmp_night_verify8`, `tmp_ses_webhook_test2`).
A name-pattern *denylist*
("test", "verify", timestamp suffixes, ...) was considered and rejected: it's exactly backwards
for a destructive operation, since a real database that happened to match the pattern would be
silently destroyed while a scratch database with an unexpected name would silently survive.
Real databases (hams_dev, hams_com, ...) can never accidentally match this prefix, so this
reaper structurally cannot touch them regardless of what future databases get added.

A candidate is only actually dropped once both of these hold:
  1. It currently has zero connections in pg_stat_activity ("open" check) -- never drop a
     database a test run is still actively using, no matter its age.
  2. Its on-disk base directory (keyed by OID, real observable evidence -- this codebase has no
     mechanism to record a `created_at` for ad-hoc `createdb` calls) hasn't been modified in at
     least --max-age-hours. A database still receiving real writes keeps advancing that mtime,
     so a long-running scratch database in genuine active use survives even if disconnected
     between queries.
"""

import argparse
import datetime
import logging
import os
import subprocess
import sys

_logger = logging.getLogger(__name__)

SCRATCH_DB_PREFIX = "tmp_"

DEFAULT_MAX_AGE_HOURS = 6
PG_DATA_BASE_DIR = "/var/lib/postgresql/17/main/base"


def _run_psql(sql: str) -> str:
    result = subprocess.run(
        ["sudo", "-n", "-u", "postgres", "psql", "-tAc", sql],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _list_scratch_databases() -> dict[str, int]:
    """Returns {datname: oid} for every database matching the reserved scratch-db prefix."""
    out = _run_psql("SELECT datname, oid FROM pg_database ORDER BY datname;")
    candidates = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        datname, oid = line.rsplit("|", 1)
        if not datname.startswith(SCRATCH_DB_PREFIX):
            continue
        candidates[datname] = int(oid)
    return candidates


def _open_database_names() -> set[str]:
    """Databases with at least one live connection right now -- never reap these."""
    out = _run_psql("SELECT DISTINCT datname FROM pg_stat_activity WHERE datname IS NOT NULL;")
    return {line.strip() for line in out.splitlines() if line.strip()}


def _database_age_hours(oid: int) -> float | None:
    base_dir = os.path.join(PG_DATA_BASE_DIR, str(oid))
    try:
        mtime = os.stat(base_dir).st_mtime
    except OSError:
        # Directory owned by postgres, unreadable from this account -- fall through to sudo stat.
        try:
            result = subprocess.run(
                ["sudo", "-n", "stat", "-c", "%Y", base_dir],
                check=True,
                capture_output=True,
                text=True,
            )
            mtime = float(result.stdout.strip())
        except (subprocess.CalledProcessError, ValueError):
            return None
    age = datetime.datetime.now().timestamp() - mtime
    return age / 3600.0


def find_reapable_databases(max_age_hours: float) -> list[str]:
    candidates = _list_scratch_databases()
    open_dbs = _open_database_names()
    reapable = []
    for datname, oid in candidates.items():
        if datname in open_dbs:
            _logger.info("Skipping %s: open (has an active connection right now).", datname)
            continue
        age_hours = _database_age_hours(oid)
        if age_hours is None:
            _logger.warning("Skipping %s: could not determine age (unreadable base dir).", datname)
            continue
        if age_hours < max_age_hours:
            _logger.info(
                "Skipping %s: only %.1fh old (threshold %.1fh).", datname, age_hours, max_age_hours
            )
            continue
        _logger.info("Reapable: %s (%.1fh old, not open).", datname, age_hours)
        reapable.append(datname)
    return reapable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help=f"Only reap scratch databases whose base directory hasn't been touched in this many hours (default {DEFAULT_MAX_AGE_HOURS}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List what would be dropped without dropping it."
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    reapable = find_reapable_databases(args.max_age_hours)
    if not reapable:
        _logger.info("Nothing to reap.")
        return

    for datname in reapable:
        if args.dry_run:
            _logger.info("[dry-run] Would drop: %s", datname)
            continue
        _logger.info("Dropping: %s", datname)
        subprocess.run(
            ["sudo", "-n", "-u", "postgres", "dropdb", "--if-exists", datname],
            check=False,
        )


if __name__ == "__main__":
    sys.exit(main())
