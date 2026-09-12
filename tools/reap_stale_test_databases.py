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
  2. Nothing under its on-disk base directory (keyed by OID, real observable evidence -- this
     codebase has no mechanism to record a `created_at` for ad-hoc `createdb` calls) has been
     modified in at least --max-age-hours. Checked as the MOST RECENT mtime across the base
     directory itself AND every file inside it, recursively -- not just the directory's own
     mtime, which (confirmed empirically 2026-09-12) does NOT advance when an existing file's
     contents are merely written to, only when an entry is added/removed/renamed. A long-running
     scratch database in genuine active use (ordinary UPDATE/INSERT into already-created tables)
     survives even if disconnected between queries, because SOME file inside its directory tree
     keeps advancing.

When a database is dropped, this reaper also removes its Odoo filestore directory
(`ODOO_FILESTORE_BASE/<dbname>`) if present -- `dropdb` itself has no concept of the filestore
and leaves it behind, which is what caused a separate ~8.2GB leak found 2026-09-08.
"""

import argparse
import datetime
import logging
import os
import shutil
import subprocess
import sys

_logger = logging.getLogger(__name__)

SCRATCH_DB_PREFIX = "tmp_"

DEFAULT_MAX_AGE_HOURS = 6
PG_DATA_BASE_DIR = "/var/lib/postgresql/17/main/base"

# `dropdb` operates at the PostgreSQL level and has no concept of Odoo's filestore -- only
# Odoo's own `/web/database/manager` "Drop" action (odoo.service.db.exp_drop()) removes the
# matching `filestore/<dbname>` directory alongside the database. Every `dropdb` call bypasses
# that entirely, including this reaper's own, so a dropped scratch database's filestore directory
# was accumulating forever: found 2026-09-08, 149 orphaned directories totaling ~8.2GB on a
# /var partition already at 89% (see hams-devbox-var-partition-small). Since this reaper only
# ever drops SCRATCH_DB_PREFIX-named databases, removing the identically-named filestore
# directory in the same step is safe by the same allowlist reasoning as the database drop itself
# -- a real database's filestore directory can never match the prefix.
ODOO_FILESTORE_BASE = "/var/lib/odoo/.local/share/Odoo/filestore"


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


def _raise(exc):
    raise exc


def _dir_tree_max_mtime(base_dir: str) -> float:
    """The most recent mtime among `base_dir` itself and every file/directory inside it,
    recursively. Raises OSError (propagated, never swallowed) if `base_dir` can't be stat'd, or
    if any part of the walk hits a permission error -- `os.walk`'s own default `onerror` silently
    SKIPS an unreadable subdirectory rather than raising, which would otherwise let this function
    return a plausible-looking but silently-incomplete (too-old) mtime instead of correctly
    falling through to the sudo-based fallback below."""
    max_mtime = os.stat(base_dir).st_mtime
    for root, dirs, files in os.walk(base_dir, onerror=_raise):
        for name in dirs + files:
            mtime = os.stat(os.path.join(root, name)).st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
    return max_mtime


def _sudo_dir_tree_max_mtime(base_dir: str) -> float:
    """Same as `_dir_tree_max_mtime`, via `sudo find` for a directory owned by postgres and
    unreadable from this account."""
    result = subprocess.run(
        ["sudo", "-n", "find", base_dir, "-printf", "%T@\n"],
        check=True,
        capture_output=True,
        text=True,
    )
    mtimes = [
        float(line)  # float-isfinite-ignore: find -printf "%T@" always emits a plain decimal epoch value, not adversarial text
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    if not mtimes:
        raise ValueError(f"`sudo find {base_dir}` produced no output -- directory is empty or gone")
    return max(mtimes)


def _database_age_hours(oid: int) -> float | None:
    """Hours since the most recent observed write anywhere under this database's own OID
    directory.

    Real bug found 2026-09-12, confirmed empirically (a real throwaway `tmp_`-prefixed database,
    real UPDATE/INSERT activity into an already-created table): this used to check ONLY the base
    OID directory's own mtime, not the mtime of anything inside it. A directory's mtime only
    advances when an entry is added/removed/renamed inside it (a new file, e.g. a new table or a
    relation crossing a new segment boundary) -- NOT when an existing file's own contents are
    merely written to, which is standard POSIX semantics, confirmed live on this exact filesystem
    with a plain `stat` before/after an `echo > existing_file` and, more concretely, against a
    real PostgreSQL database: creating a table and inserting 1000 rows advanced the base OID
    directory's mtime once (the table's own relation file being created), but a subsequent
    UPDATE + 1000 more INSERTs into that SAME already-existing table left the base directory's own
    mtime COMPLETELY UNCHANGED, while the relation file's own mtime correctly advanced. This
    module's own docstring claimed "A database still receiving real writes keeps advancing that
    mtime" -- that claim was never actually verified against a real database and was false for
    the common case (ordinary UPDATE/INSERT into pre-existing tables, which is most real Odoo
    test-run activity after initial schema creation). Consequence: a `tmp_` scratch database that
    genuinely had real, valuable write activity recently -- but happened to have zero open
    connections at the moment of this reaper's snapshot check, and whose recent writes didn't
    happen to also create a new table/segment -- would be misjudged as stale by its base
    directory's own frozen mtime and DROPPED, destroying real data. Fixed by taking the most
    recent mtime across the base directory AND every file inside it, recursively, which correctly
    reflects real write activity regardless of whether it happened to also touch the directory's
    own entry list."""
    base_dir = os.path.join(PG_DATA_BASE_DIR, str(oid))
    try:
        mtime = _dir_tree_max_mtime(base_dir)
    except OSError:
        # Directory owned by postgres, unreadable from this account -- fall through to sudo find.
        try:
            mtime = _sudo_dir_tree_max_mtime(base_dir)
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


def _drop_one(datname: str, dry_run: bool = False) -> bool:
    """Drop one scratch database and, only if the drop itself actually succeeded (or the
    database was already gone), remove its Odoo filestore directory too. Returns True if the
    filestore removal step was allowed to proceed, False if it was skipped because `dropdb`
    itself failed.

    Real bug found 2026-09-10: this used to remove the filestore directory unconditionally, with
    no check on whether `dropdb` actually succeeded. `--if-exists` makes `dropdb` exit 0 both
    when it genuinely dropped the database AND when the database was already gone -- but a
    NONZERO exit means the drop was refused (e.g. a new connection raced in between the
    open-connection check in `find_reapable_databases` and this call, or a transient
    `sudo -n`/permission failure) and the database is very much still alive. In that case,
    proceeding to delete its filestore directory anyway would corrupt a real, still-existing
    database's attachments/binary data while leaving the database row itself intact -- strictly
    worse than the leaked-filestore bug this whole filestore-removal step was added to fix (see
    this file's own module docstring), since now the database is broken AND still around to be
    used. This function skips the filestore removal entirely on a nonzero exit and logs it
    clearly instead of silently proceeding."""
    filestore_dir = os.path.join(ODOO_FILESTORE_BASE, datname)
    if dry_run:
        _logger.info("[dry-run] Would drop: %s", datname)
        if os.path.isdir(filestore_dir):
            _logger.info("[dry-run] Would also remove filestore dir: %s", filestore_dir)
        return True

    _logger.info("Dropping: %s", datname)
    result = subprocess.run(
        ["sudo", "-n", "-u", "postgres", "dropdb", "--if-exists", datname],
        check=False,
    )
    if result.returncode != 0:
        _logger.warning(
            "dropdb failed for %s (exit %d) -- leaving its filestore dir alone.",
            datname, result.returncode,
        )
        return False

    # Only reached for a name already verified by the caller to start with SCRATCH_DB_PREFIX,
    # AND only after dropdb itself reported success (or that the database was already gone) --
    # never removes a real, still-existing database's filestore.
    if os.path.isdir(filestore_dir):
        _logger.info("Removing filestore dir: %s", filestore_dir)
        shutil.rmtree(filestore_dir, ignore_errors=True)
    return True


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
        _drop_one(datname, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
