#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
End-of-run clean-up and start-up sweep for test.py runs in slot mode (ADR 0116).

A run in a slot of a machine with more than one slot owns a PostgreSQL database (`<db>_sK`, slot 1: `<db>`),
Odoo's filestore directory of that name, and, on the host, a TMPDIR of its own (`<TMPDIR>/slot-K`). Until this
module existed they were dropped only at the START of the next run in the same slot, so every slot database,
filestore and directory outlived its run, and a run that crashed or was killed left everything behind for good.

The rules, each one a function here:

  * a run MARKS the database it creates with a PostgreSQL comment (`database_marker`): runner name, slot and
    whether `--keep-db` asked to keep it. The comment lives and dies with the database, so there is no second
    record to lose and no file permission to trip over between the users that run test.py;
  * a run removes what it owns when it ends, however it ends (`finish_run`), but only a database whose marker
    names its own slot and is not kept: it never drops a database it did not create;
  * the next run sweeps the leftovers of runs that are gone (`leftovers`, `sweep`): marked databases, the
    databases an older test.py left behind under the slot naming, orphan filestores, slot TMPDIRs. A slot is
    swept only while the sweeper holds that slot's lock, so a database a live run holds is never touched.

Nothing here touches PostgreSQL, the disk or the lock files itself. The callers pass the operations in
(`SweepOperations`, the arguments of `finish_run`), which is what makes the rules testable with real
databases and real directories in test_runner_cleanup.py and with plain functions in the unit tests.
"""

import re
from dataclasses import dataclass

MARKER_PREFIX = "hams_test_runner:v1"
# Never dropped, whatever a comment or a name pattern says.
PROTECTED_DATABASES = frozenset({"hams_prod", "postgres", "template0", "template1"})
DEFAULT_DATABASE = "hams_test"
# Lowest slot number a name pattern recognises: slot 1 uses the bare `--db` name, which a run without slots uses too.
LOWEST_PATTERN_SLOT = 2

_SAFE_DATABASE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
_MARKER_RE = re.compile(
    re.escape(MARKER_PREFIX) + r" slot=(?P<slot>[0-9]+) kept=(?P<kept>[01])\Z"
)
# What an older test.py (ADR 0116, before this module) left behind: `hams_test_s2`, `hams_test_relay_s2`.
_LEGACY_SLOT_DATABASE_RE = re.compile(
    r"^" + re.escape(DEFAULT_DATABASE) + r"[a-z0-9_]*_s(?P<slot>[2-9]|1[0-5])\Z"
)
_SLOT_DIRECTORY_RE = re.compile(r"^slot-(?P<slot>[2-9]|1[0-5])\Z")


# [@ANCHOR: runner_cleanup:database_marker]
def database_marker(slot, kept):
    """The comment test.py puts on a database it creates in slot mode: ``hams_test_runner:v1 slot=2 kept=0``."""
    return f"{MARKER_PREFIX} slot={int(slot)} kept={1 if kept else 0}"


# [@ANCHOR: runner_cleanup:parse_database_marker]
def parse_database_marker(comment):
    """The ``(slot, kept)`` a `database_marker` comment names, or None for any other comment (also None or "")."""
    match = _MARKER_RE.match(comment or "")
    if not match:
        return None
    return int(match.group("slot")), match.group("kept") == "1"


# [@ANCHOR: runner_cleanup:is_droppable_name]
def is_droppable_name(name):
    """True if `name` may ever appear in a DROP DATABASE statement of this module: a plain identifier
    (so it can sit inside double quotes), and not hams_prod, postgres, template0 or template1."""
    return bool(_SAFE_DATABASE_RE.match(name or "")) and name not in PROTECTED_DATABASES


# [@ANCHOR: runner_cleanup:drop_database_sql]
def drop_database_sql(name):
    """``DROP DATABASE IF EXISTS "name" WITH (FORCE);`` (FORCE ends the connections a killed run left open).
    Raises ValueError for a name `is_droppable_name` refuses."""
    if not is_droppable_name(name):
        raise ValueError(f"refusing to build DROP DATABASE for {name!r}")
    return f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);'


# [@ANCHOR: runner_cleanup:comment_database_sql]
def comment_database_sql(name, slot, kept):
    """``COMMENT ON DATABASE "name" IS '<marker>';`` Raises ValueError for a name `is_droppable_name` refuses."""
    if not is_droppable_name(name):
        raise ValueError(f"refusing to build COMMENT ON DATABASE for {name!r}")
    return f"COMMENT ON DATABASE \"{name}\" IS '{database_marker(slot, kept)}';"


# [@ANCHOR: runner_cleanup:owned_slot]
def owned_slot(name, comment):
    """The slot whose run owns database `name`, or None when no run of this runner can be shown to own it.

    A database with a marker comment belongs to the marker's slot (any slot, including 1). A database without
    a marker belongs to slot K only when its name is `hams_test...` plus `_sK` with K from 2 to 15, the naming
    an older test.py used: slot 1's bare name is also the database of a run without slots, so it is never
    guessed. A name `is_droppable_name` refuses is never owned."""
    if not is_droppable_name(name):
        return None
    marker = parse_database_marker(comment)
    if marker is not None:
        return marker[0] if 1 <= marker[0] <= 15 else None
    match = _LEGACY_SLOT_DATABASE_RE.match(name)
    return int(match.group("slot")) if match else None


# [@ANCHOR: runner_cleanup:SlotLeftovers]
@dataclass
class SlotLeftovers:
    """What a run that is gone left behind in one slot; built by `leftovers`, consumed by `sweep`.

    slot: the slot number, 1 to 15.
    databases: names of databases to drop (never kept ones).
    filestores: names of filestore directories to remove: those of the databases above and orphans that match
        the slot naming and have no database.
    tmpdir: True when `<TMPDIR>/slot-K` exists and is to be removed (never set for slot 1).
    """

    slot: int
    databases: list
    filestores: list
    tmpdir: bool


# [@ANCHOR: runner_cleanup:leftovers]
def leftovers(rows, filestore_names, slot_directory_names):
    """Group the leftovers of finished runs by slot.

    rows: ``(database name, comment)`` for every database on the server. filestore_names: the entries of
    Odoo's filestore directory. slot_directory_names: the entries of the base TMPDIR (only ``slot-K`` ones count).
    A kept database (marker ``kept=1``) and its filestore are skipped. Returns a list of SlotLeftovers in slot
    order, empty slots omitted. Pure: it decides, it does not look at which slots are still held; `sweep` does."""
    by_slot = {}

    def entry(slot):
        return by_slot.setdefault(slot, SlotLeftovers(slot, [], [], False))

    kept_names = set()
    existing = set()
    for name, comment in rows:
        existing.add(name)
        slot = owned_slot(name, comment)
        if slot is None:
            continue
        marker = parse_database_marker(comment)
        if marker is not None and marker[1]:
            kept_names.add(name)
            continue
        entry(slot).databases.append(name)
        entry(slot).filestores.append(name)
    for name in filestore_names:
        if name in existing or name in kept_names:
            continue
        match = _LEGACY_SLOT_DATABASE_RE.match(name)
        if match and is_droppable_name(name):
            entry(int(match.group("slot"))).filestores.append(name)
    for name in slot_directory_names:
        match = _SLOT_DIRECTORY_RE.match(name)
        if match:
            entry(int(match.group("slot"))).tmpdir = True
    return [by_slot[slot] for slot in sorted(by_slot)]


# [@ANCHOR: runner_cleanup:SweepOperations]
@dataclass
class SweepOperations:
    """The side effects `sweep` performs, supplied by the caller.

    claim_slot(slot): try to take the slot's lock without waiting; returns a release function, or None when
        another process holds the slot (then nothing of that slot is touched). The slot the sweeper's own run
        holds returns a release function that does nothing.
    drop_database(name), remove_filestore(name), remove_slot_tmpdir(slot): remove one thing; return True if it
        is gone afterwards. They must not raise.
    say(text): report a line.
    """

    claim_slot: object
    drop_database: object
    remove_filestore: object
    remove_slot_tmpdir: object
    say: object


# [@ANCHOR: runner_cleanup:sweep]
def sweep(plan, operations):
    """Remove the leftovers in `plan` (a list of SlotLeftovers) slot by slot, only under that slot's lock.

    A slot whose lock cannot be taken has a live holder: it is skipped and reported, and nothing of it is
    touched. Returns ``{"removed": [...], "skipped": [...], "failed": [...]}`` of human-readable item names
    (``database hams_test_s2``, ``filestore ...``, ``tmpdir slot-2``, ``slot 3``)."""
    outcome = {"removed": [], "skipped": [], "failed": []}
    for slot_plan in plan:
        release = operations.claim_slot(slot_plan.slot)
        if release is None:
            outcome["skipped"].append(f"slot {slot_plan.slot}")
            operations.say(f"[*] Sweep: slot {slot_plan.slot} is held by a live run, leaving its files alone.")
            continue
        try:
            steps = [
                (f"database {name}", operations.drop_database, name) for name in slot_plan.databases
            ]
            steps += [
                (f"filestore {name}", operations.remove_filestore, name) for name in slot_plan.filestores
            ]
            if slot_plan.tmpdir:
                steps.append((f"tmpdir slot-{slot_plan.slot}", operations.remove_slot_tmpdir, slot_plan.slot))
            for label, action, argument in steps:
                if action(argument):
                    outcome["removed"].append(label)
                    operations.say(f"[*] Sweep: removed {label} (slot {slot_plan.slot}, no live run).")
                else:
                    outcome["failed"].append(label)
                    operations.say(f"⚠️  Sweep: could not remove {label}.")
        finally:
            release()
    return outcome


# [@ANCHOR: runner_cleanup:finish_run]
def finish_run(database, slot, keep, read_comment, drop_database, remove_filestore, remove_slot_tmpdir, say):
    """End-of-run clean-up of one run's database, filestore and slot TMPDIR; safe to call twice.

    database: the run's database name; slot: the slot it holds (the caller holds its lock, which is what makes
    the removal safe). keep: --keep-db. read_comment(name) returns the database's comment, or None when the
    database does not exist. drop_database(name) and remove_filestore(name) return True when gone;
    remove_slot_tmpdir() takes no argument and may be None (nothing to remove). say(text) reports a line.

    Returns one of ``"kept"`` (keep, or a kept marker: nothing is removed; the database name is printed),
    ``"dropped"``, ``"absent"`` (no database: only the filestore and the directory are removed), ``"foreign"``
    (a database of that name exists but carries no marker for this slot: it was not made by this run and is left
    alone with a warning), ``"failed"`` (the drop did not work)."""
    comment = read_comment(database)
    marker = parse_database_marker(comment)
    if keep or (marker is not None and marker[0] == slot and marker[1]):
        if comment is not None:
            say(
                f"[*] Database {database} (slot {slot}) KEPT; "
                f"drop it when done: sudo -u postgres dropdb --force {database}"
            )
        return "kept"
    if comment is None:
        remove_filestore(database)
        if remove_slot_tmpdir:
            remove_slot_tmpdir()
        return "absent"
    if marker is None or marker[0] != slot:
        say(
            f"⚠️  Database {database} exists but was not created by a slot-{slot} run of this runner; "
            "leaving it alone."
        )
        return "foreign"
    if not drop_database(database):
        say(f"⚠️  Could not drop database {database}.")
        return "failed"
    say(f"[*] Dropped database {database} (slot {slot}).")
    remove_filestore(database)
    if remove_slot_tmpdir:
        remove_slot_tmpdir()
    return "dropped"
