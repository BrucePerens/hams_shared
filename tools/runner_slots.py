#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Concurrent test slots for test.py (ADR 0116).

test.py has always allowed ONE run per machine: an exclusive flock on
one fixed file. A machine that opts in (HAMS_TEST_SLOTS=N,
``--slots N``, or the one-line file /opt/hams/etc/test_slots) runs up
to N test.py instances at once. Every instance holds one SLOT, a
number from 1 to N, taken with a flock of its own, and everything two
runs would otherwise share is derived from that number here:

  * the lock file: slot 1 is the original lock file itself, slot K the
    same path plus ``.slot-K``;
  * the PostgreSQL database (and with it Odoo's filestore directory);
  * Odoo's HTTP port and the Redis database number, where the run is
    not already inside a private network namespace;
  * the TMPDIR that holds the Chrome profiles, and the patterns that
    kill and clean up that run's Chrome and nothing else;
  * the file and directory names test.py writes into the log
    directory.

Nothing here reads or writes the machine except
``configured_slot_count`` (one file read) and ``broker_listening`` (one
TCP connect). Everything else is a pure function of its arguments.

With no opt-in the slot count is 1, slot 1 is derived, and every value
returned equals what test.py used before this module existed: the same
lock file, database name, port 8075, Redis database 1, file names,
Chrome patterns. That equality is what "default behaviour is
unchanged" means, and test_runner_slots.py asserts it.
"""

import os
import re
import socket
from dataclasses import dataclass

SLOTS_ENV = "HAMS_TEST_SLOTS"
SLOTS_FILE = "/opt/hams/etc/test_slots"
# Redis has 16 numbered databases by default. Database 0 belongs to
# whatever else uses the host's Redis, and slot K uses database K.
MAX_SLOTS = 15
BASE_HTTP_PORT = 8075
BROKER_PORT = 5672
# PostgreSQL truncates identifiers longer than this many bytes.
MAX_DATABASE_NAME_BYTES = 63
DEFAULT_CHROME_PATTERN = "_chrome_odoo"
DEFAULT_CHROME_GLOBS = (
    "/tmp/*_chrome_odoo",
    "/var/tmp/*_chrome_odoo",
)
_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+\Z")
_BROKER_MENTION_RE = re.compile(r"pika|amqp|rabbitmq", re.IGNORECASE)
_SKIPPED_DIRS = frozenset(
    {".git", "__pycache__", "node_modules", "target", "static"}
)
_ERE_SPECIAL = set(".[]()*+?{}|^$\\")


class SlotConfigError(ValueError):
    """A slot count or a slot-derived name that cannot be used."""


# [@ANCHOR: runner_slots:parse_slot_count]
def parse_slot_count(text, source):
    """Validate one slot count; `source` names where it came from."""
    cleaned = str(text).strip()
    if not re.fullmatch(r"[0-9]+", cleaned):
        raise SlotConfigError(
            f"{source}: {cleaned!r} is not a whole number of slots"
        )
    count = int(cleaned)
    if count < 1 or count > MAX_SLOTS:
        raise SlotConfigError(
            f"{source}: {count} is outside 1..{MAX_SLOTS}"
        )
    return count


# [@ANCHOR: runner_slots:configured_slot_count]
def configured_slot_count(
    cli_value=None, environ=None, slots_file=SLOTS_FILE
):
    """How many concurrent runs this machine allows.

    Precedence: the --slots value, then HAMS_TEST_SLOTS, then the
    first line of `slots_file`, then 1. A file that does not exist
    means 1 (the machine has not opted in). A file that exists and
    cannot be read, or any value that is not a whole number in
    1..MAX_SLOTS, raises SlotConfigError or OSError: a typo must not
    quietly turn into one slot or into a different machine-wide
    setting.
    """
    environ = os.environ if environ is None else environ
    if cli_value is not None:
        return parse_slot_count(cli_value, "--slots")
    from_env = environ.get(SLOTS_ENV)
    if from_env not in (None, ""):
        return parse_slot_count(from_env, SLOTS_ENV)
    try:
        with open(slots_file, "r", encoding="utf-8") as handle:
            first_line = handle.readline()
    except FileNotFoundError:
        return 1
    return parse_slot_count(first_line, slots_file)


# [@ANCHOR: runner_slots:slot_lock_path]
def slot_lock_path(base_path, slot):
    """The lock file of `slot`: the base path for slot 1, else
    ``<base>.slot-<slot>``.

    Slot 1 is the original file on purpose. A test.py without slots
    (an older checkout, a machine that has not opted in) takes exactly
    that file, so it and a slotted run exclude each other on slot 1
    instead of sharing its database and ports.
    """
    if slot == 1:
        return base_path
    return f"{base_path}.slot-{slot}"


def slot_suffix(slot):
    """'' for slot 1, else '.slot-K'."""
    return "" if slot == 1 else f".slot-{slot}"


# [@ANCHOR: runner_slots:slot_database_name]
def slot_database_name(database, slot):
    """The PostgreSQL database a run in `slot` uses: `database` itself
    for slot 1, else ``<database>_s<slot>`` ('hams_test_relay_s2').

    Odoo keeps its filestore in a directory named after the database,
    so distinct names also give each slot its own filestore. Raises
    SlotConfigError if the result is longer than PostgreSQL's 63-byte
    identifier limit (the server would truncate it silently and two
    slots could end up on one database).
    """
    if slot == 1:
        return database
    name = f"{database}_s{slot}"
    if len(name.encode("utf-8")) > MAX_DATABASE_NAME_BYTES:
        raise SlotConfigError(
            f"database name {name!r} is longer than "
            f"{MAX_DATABASE_NAME_BYTES} bytes; use a shorter --db"
        )
    return name


# [@ANCHOR: runner_slots:slot_file_name]
def slot_file_name(name, slot):
    """A per-slot name for a file or directory in the log directory:
    unchanged for slot 1, else the slot tag goes before the extension
    ('filtered_test.slot-2.txt', 'coverage_report.slot-2').
    """
    if slot == 1:
        return name
    stem, extension = os.path.splitext(name)
    return f"{stem}{slot_suffix(slot)}{extension}"


def _ere_escape(text):
    return "".join(
        "\\" + char if char in _ERE_SPECIAL else char for char in text
    )


# [@ANCHOR: runner_slots:in_private_network]
def in_private_network(environ):
    """True in the final test process of the full pipeline: test.py's
    own root process sets HAMS_ISOLATED_NS=1 and HAMS_TEST_LOCK_HELD=1
    only after it has created the network namespace. A direct
    ``HAMS_ISOLATED_NS=1`` run sets the first and never the second, so
    it is on the host."""
    return (
        environ.get("HAMS_ISOLATED_NS") == "1"
        and environ.get("HAMS_TEST_LOCK_HELD") == "1"
    )


# [@ANCHOR: runner_slots:SlotResources]
@dataclass(frozen=True)
class SlotResources:
    """What a run in `slot` of `slots` uses that another slot must not.

    slot, slots: this run's slot and the machine's slot count.
    private_network: True inside test.py's own network namespace.
    scoped: True when this run shares the host with other slots (more
        than one slot and no private network): only then are the
        port, Redis database, TMPDIR and Chrome patterns per slot.
    http_port: Odoo's HTTP port.
    redis_db: the Redis database number tests use.
    tmpdir: TMPDIR for this run (None: leave the environment alone).
    chrome_kill_pattern: the `pkill -f` pattern for this run's Chrome.
    chrome_profile_globs: Chrome profile directories to remove at the
        end of the run.
    """

    slot: int
    slots: int
    private_network: bool
    scoped: bool
    http_port: int
    redis_db: int
    tmpdir: object
    chrome_kill_pattern: str
    chrome_profile_globs: tuple


# [@ANCHOR: runner_slots:slot_resources]
def slot_resources(slot, slots, private_network, base_tmpdir):
    """Derive a run's SlotResources.

    `private_network` is True when the run is inside the network
    namespace test.py created (its ports, Redis, RabbitMQ, process
    table and /tmp are private). Then nothing but the slot number and
    the defaults is needed. When it is False (the HAMS_ISOLATED_NS=1
    run on the host) and `slots` > 1, the HTTP port, the Redis
    database, TMPDIR and the Chrome patterns are made per slot.
    `base_tmpdir` is the TMPDIR in force (None if unset).

    Raises SlotConfigError for a slot outside 1..slots, and in the
    scoped case for a missing or oddly spelled TMPDIR, which would
    make the Chrome patterns match nothing or too much.
    """
    if slot < 1 or slot > slots:
        raise SlotConfigError(f"slot {slot} is outside 1..{slots}")
    scoped = slots > 1 and not private_network
    if not scoped:
        return SlotResources(
            slot=slot,
            slots=slots,
            private_network=private_network,
            scoped=False,
            http_port=BASE_HTTP_PORT,
            redis_db=1,
            tmpdir=base_tmpdir,
            chrome_kill_pattern=DEFAULT_CHROME_PATTERN,
            chrome_profile_globs=DEFAULT_CHROME_GLOBS,
        )
    if not base_tmpdir or not _SAFE_PATH_RE.match(base_tmpdir):
        raise SlotConfigError(
            "slots need a TMPDIR of plain path characters, got "
            f"{base_tmpdir!r}"
        )
    tmpdir = base_tmpdir.rstrip("/")
    if slot > 1:
        tmpdir = f"{tmpdir}/slot-{slot}"
    return SlotResources(
        slot=slot,
        slots=slots,
        private_network=private_network,
        scoped=True,
        http_port=BASE_HTTP_PORT + slot - 1,
        redis_db=slot,
        tmpdir=tmpdir,
        chrome_kill_pattern=_ere_escape(tmpdir)
        + "/[^/ ]*_chrome_odoo",
        chrome_profile_globs=(f"{tmpdir}/*_chrome_odoo",),
    )


# [@ANCHOR: runner_slots:broker_listening]
def broker_listening(port=BROKER_PORT, host="127.0.0.1", timeout=0.5):
    """True if something accepts a TCP connection on host:port."""
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def _tree_mentions_broker(directory):
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _SKIPPED_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(
                path, "r", encoding="utf-8", errors="replace"
            ) as handle:
                if _BROKER_MENTION_RE.search(handle.read()):
                    return True
    return False


# [@ANCHOR: runner_slots:modules_mentioning_broker]
def modules_mentioning_broker(module_dirs):
    """The names, sorted, of the modules whose Python source mentions
    RabbitMQ (``pika``, ``amqp`` or ``rabbitmq``, any case).

    `module_dirs` maps a module name to its directory. A mention is
    the test for "this module's tests can use the host broker": it is
    deliberately broad (a comment counts) and looks only at the
    modules named, never at their dependencies, so a module that
    merely depends on one that uses the broker is not counted.
    """
    return sorted(
        name
        for name, directory in module_dirs.items()
        if os.path.isdir(directory)
        and _tree_mentions_broker(directory)
    )


# [@ANCHOR: runner_slots:slot_refusals]
def slot_refusals(resources, pause_on_fail, mcp, broker_users):
    """Why this run cannot use its slot (empty list: it can).

    Slot 1 is never refused: it is what a run has always been. A run
    in a slot above 1 is refused when:

      * --pause-on-fail / HAMS_PAUSE_ON_FAIL: the harness pins Chrome's
        debugging port to 9222 (zero_sudo/tests/common.py), one port
        for the whole host outside a private network;
      * --mcp: the MCP clients are configured for port 8075 and test.py
        serves the derived port in that slot;
      * the run is on the host (no private network) and `broker_users`
        is not empty: those modules use RabbitMQ and one is listening
        on the host, so their queues would be shared with the other
        slots, and a vhost cannot be created as the odoo user. The
        caller passes the modules from ``modules_mentioning_broker``
        only when a RabbitMQ is listening, and [] otherwise. The full
        pipeline starts a private RabbitMQ per run.
    """
    if resources.slot == 1:
        return []
    reasons = []
    if pause_on_fail:
        reasons.append(
            "--pause-on-fail pins Chrome's debugging port 9222, which "
            "only slot 1 may use"
        )
    if mcp:
        reasons.append(
            "--mcp serves the port its clients are configured for "
            f"({BASE_HTTP_PORT}), which only slot 1 uses"
        )
    if not resources.private_network and broker_users:
        reasons.append(
            f"{', '.join(broker_users)} use RabbitMQ and one is "
            f"listening on 127.0.0.1:{BROKER_PORT}; a run on the host "
            "shares it with the other slots. Run these modules in "
            "slot 1, or without HAMS_ISOLATED_NS=1 so test.py starts "
            "a private one"
        )
    return reasons
