#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Known-red baseline and flake handling for test.py (ADR 0118).

A test run used to be red when any test failed, whoever caused it. That forced agents to hand-debug whole-system
failures before anything else could land. This module is the pure part of the replacement policy; `test.py` calls it
after Odoo's run and prints what it returns:

* a failing test that is listed in a tracked baseline file is "known red, not new": it is named in the summary, with
  its age and owner, and does not turn the run red; a baselined test that passes again is named so its entry gets
  removed; an entry older than `HAMS_KNOWN_RED_MAX_AGE_DAYS` (default 3) is no longer tolerated, unless it was renewed
  with a reason (ADR 0118, amendment of 2026-11-08);
* any other failing test is rerun ONCE, in isolation (fresh database, same installed modules, only that test);
  passing on the rerun is a flake (counted per test in a small JSON file), failing again is a real failure;
* a setup or class error is never rerun: it is reported as such, and is red unless baselined;
* a failure the log parser could not name (Odoo counted more failures than were found) is red.

Nothing is hidden: every tolerated test, flake and removable entry appears in the run summary.
The reference is the "Known-red baseline and flakes" section of tools/README.md.
"""

import argparse
import calendar
import dataclasses
import fcntl
import json
import os
import re
import sys
import tempfile
import time

BASELINE_FILE_NAME = "known_red_baseline.json"
BASELINE_ENV = "HAMS_KNOWN_RED_BASELINE"
FLAKE_FILE_ENV = "HAMS_TEST_FLAKE_FILE"
FLAKE_THRESHOLD_ENV = "HAMS_FLAKE_FIX_ME_THRESHOLD"
DEFAULT_FLAKE_THRESHOLD = 3
MAX_AGE_ENV = "HAMS_KNOWN_RED_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 3.0
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_RERUN_TESTS = 25
FLAKE_FILE_NAME = "test_flakes.json"
BASELINE_VERSION = 1
RESULTS_VERSION = 1

_TEST_FAILURE_LINE = re.compile(
    r"\b(?:ERROR|CRITICAL)\s+\S+\s+(?P<logger>[\w.]+): (?P<flavour>FAIL|ERROR): (?P<desc>.+?)\s*$"
)
_CLASS_LEVEL_DESC = re.compile(
    r"^(?P<method>setUpClass|tearDownClass|setUpModule|tearDownModule) \((?P<path>[\w.]+)\)$"
)
_TEST_DESC = re.compile(r"^(?:Subtest )?(?P<cls>[A-Za-z_]\w*)\.(?P<method>\w+)(?: .*)?$")
_STARTING_LINE = re.compile(
    r"\sINFO\s+\S+\s+odoo\.addons\.(?P<module>[A-Za-z0-9_]+)[\w.]*: Starting (?P<cls>[A-Za-z_]\w*)\.(?P<method>\w+) \.\.\."
)
_ADDON_LOGGER = re.compile(r"^odoo\.addons\.(?P<module>[A-Za-z0-9_]+)(?:\.|$)")
_ODOO_RESULT = re.compile(
    r"odoo\.tests\.result:\s*(?P<failed>\d+) failed, (?P<errors>\d+) error\(s\) of (?P<total>\d+) tests"
)
_TEST_ID = re.compile(r"^/?(?P<module>[A-Za-z0-9_]+):(?P<cls>[A-Za-z_]\w*)\.(?P<method>\w+)$")


class BaselineError(ValueError):
    """A baseline or flake file that cannot be trusted; the run stops rather than guess."""


# [@ANCHOR: known_red:max_age_days]
def max_age_days(environ=None):
    """How many days a baseline entry is tolerated: `HAMS_KNOWN_RED_MAX_AGE_DAYS` (a finite number above 0) or 3. A bad value
    raises `BaselineError`: a limit that quietly became "forever" would defeat it."""
    environ = os.environ if environ is None else environ
    text = environ.get(MAX_AGE_ENV, "").strip()
    if not text:
        return DEFAULT_MAX_AGE_DAYS
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not value > 0 or value == float("inf"):
        raise BaselineError("%s=%r is not a finite number of days above 0" % (MAX_AGE_ENV, text))
    return value


# [@ANCHOR: known_red:age_text]
def age_text(days):
    """`3.1 days`, or `5 hours` below one day: how long a baseline entry has been known red. Pure."""
    return "%.1f days" % days if days >= 1 else "%.0f hours" % (days * 24)


def _epoch(text):
    return calendar.timegm(time.strptime(text, TIME_FORMAT))


# [@ANCHOR: known_red:FailureRecord]
@dataclasses.dataclass(frozen=True)
class FailureRecord:
    """One failing test as found in Odoo's log: `test_id` is `<module>:<Class>.<method>` (a class-level error uses
    the method `setUpClass`, `tearDownClass`, ...), `flavour` is Odoo's `FAIL` or `ERROR`, `setup` is True for a
    class- or module-level error."""

    test_id: str
    flavour: str
    setup: bool


# [@ANCHOR: known_red:parse_failure_line]
def parse_failure_line(line):
    """The `FailureRecord` named by one Odoo log line, or None for any other line."""
    match = _TEST_FAILURE_LINE.search(line)
    if not match:
        return None
    desc = match.group("desc")
    class_level = _CLASS_LEVEL_DESC.match(desc)
    if class_level:
        found = _ADDON_LOGGER.match(class_level.group("path"))
        if not found:
            return None
        path = class_level.group("path").split(".")
        return FailureRecord(
            "%s:%s.%s" % (found.group("module"), path[-1], class_level.group("method")), match.group("flavour"), True
        )
    logger = _ADDON_LOGGER.match(match.group("logger"))
    named = _TEST_DESC.match(desc)
    if not logger or not named:
        return None
    return FailureRecord(
        "%s:%s.%s" % (logger.group("module"), named.group("cls"), named.group("method")), match.group("flavour"), False
    )


# [@ANCHOR: known_red:parse_started_line]
def parse_started_line(line):
    """The test id (`<module>:<Class>.<method>`) of an Odoo "Starting Class.method ..." line, or None."""
    match = _STARTING_LINE.search(line)
    if not match:
        return None
    return "%s:%s.%s" % (match.group("module"), match.group("cls"), match.group("method"))


# [@ANCHOR: known_red:RunObservation]
class RunObservation:
    """What one Odoo process said about its tests, built line by line with `feed`.

    It also carries the attributes `test.py`'s `run_cmd` sets on its line consumer, so a rerun can use it directly
    as the consumer without the report-writing side effects of the full `FailureExtractor`."""

    mcp_mode = False

    def __init__(self):
        self.failures = []
        self.started = set()
        self.odoo_result_lines = []
        self.cpu_limit_line = None
        self.signal_line = None
        self.child_exit_status = None

    def feed(self, line):
        """Reads one log line (ANSI colour already removed)."""
        record = parse_failure_line(line)
        if record is not None:
            self.failures.append(record)
            return
        started = parse_started_line(line)
        if started is not None:
            self.started.add(started)
            return
        result = _ODOO_RESULT.search(line)
        if result:
            self.odoo_result_lines.append(
                {k: int(result.group(k)) for k in ("failed", "errors", "total")} | {"raw": line.strip()}
            )

    def process_line(self, line):
        """`run_cmd`'s consumer interface."""
        self.feed(re.sub(r"\x1b\[[0-9;]*m", "", line))

    def append_diagnostic(self, text):
        """`run_cmd`'s consumer interface; a rerun keeps no diagnostics."""

    def counted(self):
        """Failures plus errors Odoo itself counted, summed over every `odoo.tests.result` line."""
        return sum(r["failed"] + r["errors"] for r in self.odoo_result_lines)

    def failed_ids(self):
        return {f.test_id for f in self.failures}


# [@ANCHOR: known_red:BaselineEntry]
@dataclasses.dataclass(frozen=True)
class BaselineEntry:
    """One line of the known-red baseline: `test_id` (`<module>:<Class>.<method>`), `first_seen` (commit that first
    showed the failure), `first_seen_time` (UTC `YYYY-MM-DDTHH:MM:SSZ`), `owner` (who fixes it), `reason` (why it is
    red), `source` (file it was read from) and `renewed` (None, or `(time, reason)` of the last renewal)."""

    test_id: str
    first_seen: str
    owner: str
    reason: str
    source: str = ""
    first_seen_time: str = ""
    renewed: tuple = None

    def age_days(self, now=time.time):
        """Days since the last renewal, else since `first_seen_time`. Never negative."""
        started = self.renewed[0] if self.renewed else self.first_seen_time
        return max(0.0, (now() - _epoch(started)) / 86400.0)


# [@ANCHOR: known_red:baseline_paths]
def baseline_paths(base_dir, environ=None):
    """The baseline files to read: `HAMS_KNOWN_RED_BASELINE` (an os.pathsep list) when set, else
    `<base_dir>/known_red_baseline.json`, the same file of a sibling `hams_open`, and `tools/known_red_baseline.json`
    beside this module. Only existing files are returned."""
    environ = os.environ if environ is None else environ
    if environ.get(BASELINE_ENV):
        candidates = [p for p in environ[BASELINE_ENV].split(os.pathsep) if p]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base_dir, BASELINE_FILE_NAME),
            os.path.join(base_dir, "hams_open", BASELINE_FILE_NAME),
            os.path.join(base_dir, "..", "hams_open", BASELINE_FILE_NAME),
            os.path.join(here, BASELINE_FILE_NAME),
        ]
    found, seen = [], set()
    for path in candidates:
        real = os.path.realpath(path)
        if real not in seen and os.path.isfile(real):
            seen.add(real)
            found.append(real)
    return found


# [@ANCHOR: known_red:load_baseline]
def load_baseline(paths):
    """Reads baseline files into `{test_id: BaselineEntry}`. Raises `BaselineError` for unreadable JSON, a wrong
    version, a missing or empty field (`test_id`, `first_seen`, `first_seen_time` in `YYYY-MM-DDTHH:MM:SSZ`, `owner`,
    `reason`), a malformed `renewed` (`{"time", "reason"}`, optional), a malformed test id, or the same id twice: a
    baseline that is wrong must stop the run, never silently tolerate or drop a test."""
    entries = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            raise BaselineError("%s: cannot read the baseline: %s" % (path, error)) from error
        if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION or not isinstance(
            data.get("entries"), list
        ):
            raise BaselineError("%s: expected {\"version\": %d, \"entries\": [...]}" % (path, BASELINE_VERSION))
        for raw in data["entries"]:
            names = ("test_id", "first_seen", "first_seen_time", "owner", "reason")
            fields = {k: raw.get(k) for k in names} if isinstance(raw, dict) else {}
            if not fields or not all(isinstance(v, str) and v.strip() for v in fields.values()):
                raise BaselineError("%s: an entry needs non-empty %s: %r" % (path, ", ".join(names), raw))
            renewed = raw.get("renewed")
            try:
                _epoch(fields["first_seen_time"])
                if renewed is not None:
                    if not isinstance(renewed, dict) or not str(renewed.get("reason", "")).strip():
                        raise ValueError("a renewal needs a reason")
                    _epoch(renewed.get("time", ""))
            except (ValueError, TypeError) as error:
                raise BaselineError("%s: bad time or renewal in %r (times are YYYY-MM-DDTHH:MM:SSZ): %s" % (path, raw, error)) from error
            found = _TEST_ID.match(fields["test_id"])
            if not found:
                raise BaselineError("%s: %r is not <module>:<Class>.<method>" % (path, fields["test_id"]))
            test_id = "%s:%s.%s" % found.group("module", "cls", "method")
            if test_id in entries:
                raise BaselineError("%s: %s is listed twice (also in %s)" % (path, test_id, entries[test_id].source))
            entries[test_id] = BaselineEntry(
                test_id, fields["first_seen"], fields["owner"], fields["reason"], path, fields["first_seen_time"],
                (renewed["time"], renewed["reason"]) if renewed else None,
            )
    return entries


# [@ANCHOR: known_red:flake_threshold]
def flake_threshold(environ=None):
    """The flake count above which a test is listed as "fix me": `HAMS_FLAKE_FIX_ME_THRESHOLD` (a whole number, at
    least 1) or 3. A bad value raises `BaselineError`."""
    environ = os.environ if environ is None else environ
    text = environ.get(FLAKE_THRESHOLD_ENV)
    if text in (None, ""):
        return DEFAULT_FLAKE_THRESHOLD
    if not re.fullmatch(r"[0-9]+", text.strip()) or int(text) < 1:
        raise BaselineError("%s=%r is not a whole number of at least 1" % (FLAKE_THRESHOLD_ENV, text))
    return int(text)


def _read_flakes(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise BaselineError("%s: cannot read the flake counters: %s" % (path, error)) from error
    if not isinstance(data, dict) or not isinstance(data.get("tests"), dict):
        raise BaselineError("%s: expected {\"version\": 1, \"tests\": {...}}" % path)
    return data["tests"]


# [@ANCHOR: known_red:load_flakes]
def load_flakes(path):
    """`{test_id: {"count", "first", "last", "last_commit"}}` from the flake file; `{}` when it does not exist yet.
    Raises `BaselineError` for a file that exists and is not valid."""
    return _read_flakes(path)


# [@ANCHOR: known_red:record_flakes]
def record_flakes(path, test_ids, commit, now=None):
    """Adds one to the counter of every id in `test_ids` and returns the updated `{test_id: entry}` for those ids.

    The read-modify-write holds an exclusive flock on `<path>.lock` and replaces the file atomically, so runs in
    several slots may call it at once without losing a count."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if now is None else now
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path + ".lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tests = _read_flakes(path)
        updated = {}
        for test_id in sorted(set(test_ids)):
            entry = dict(tests.get(test_id) or {"count": 0, "first": now})
            entry.update(count=int(entry.get("count", 0)) + 1, last=now, last_commit=commit or "")
            tests[test_id] = updated[test_id] = entry
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=".flakes-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump({"version": 1, "tests": tests}, out, indent=1, sort_keys=True)
                out.write("\n")
            os.chmod(temporary, 0o666)
            os.replace(temporary, path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
    return updated


# [@ANCHOR: known_red:Classification]
@dataclasses.dataclass
class Classification:
    """How the first Odoo run's failures divide: `known_red` (`{id: BaselineEntry}` tolerated), `new` (ids to rerun),
    `setup_new` (new class-level errors, never rerun), `unparsed` (failures Odoo counted that no log line named),
    `now_passing` (baselined ids that ran and passed), `expired` (`{id: BaselineEntry}` baselined failures older than the
    age limit: no longer tolerated, never rerun)."""

    known_red: dict
    new: list
    setup_new: list
    unparsed: int
    now_passing: list
    expired: dict = dataclasses.field(default_factory=dict)


# [@ANCHOR: known_red:classify]
def classify(observation, baseline, max_age=None, now=time.time):
    """Splits `observation.failures` using `baseline` (`{id: BaselineEntry}`) into a `Classification`. A baselined failure
    whose entry is older than `max_age` days (`max_age_days()` when None; the age counts from the last renewal) goes to
    `expired`, not `known_red`."""
    failed = observation.failed_ids()
    limit = max_age_days() if max_age is None else max_age
    listed = {i: baseline[i] for i in sorted(failed) if i in baseline}
    expired = {i: e for i, e in listed.items() if e.age_days(now) > limit}
    known = {i: e for i, e in listed.items() if i not in expired}
    fresh = [f for f in observation.failures if f.test_id not in baseline]
    seen, new, setup_new = set(), [], []
    for record in fresh:
        if record.test_id in seen:
            continue
        seen.add(record.test_id)
        (setup_new if record.setup else new).append(record.test_id)
    unparsed = max(0, observation.counted() - len(observation.failures))
    passing = sorted(i for i in baseline if i in observation.started and i not in failed)
    return Classification(known, new, setup_new, unparsed, passing, expired)


# [@ANCHOR: known_red:rerun_tags]
def rerun_tags(test_ids):
    """Odoo's `--test-tags` selector for exactly these ids: `/<module>:<Class>.<method>,...`."""
    return ",".join("/" + i for i in sorted(test_ids))


# [@ANCHOR: known_red:PolicyOutcome]
@dataclasses.dataclass
class PolicyOutcome:
    """The final verdict of the policy: `run_green` (True when every failure is tolerated), `known_red`,
    `flakes` (`{id: flake entry}`), `failed_again`, `setup_new`, `not_rerun` (`{id: reason}`), `unparsed`,
    `now_passing`, `fix_me` (`{id: entry}` of flakes above the threshold), `rerun_ran`, `expired` (`{id: BaselineEntry}`
    over the age limit: red) and `clock` (the time source the ages are read from) and `limit` (the age limit in days)."""

    run_green: bool
    known_red: dict
    flakes: dict
    failed_again: list
    setup_new: list
    not_rerun: dict
    unparsed: int
    now_passing: list
    fix_me: dict
    rerun_ran: bool
    expired: dict = dataclasses.field(default_factory=dict)
    clock: object = time.time
    limit: float = DEFAULT_MAX_AGE_DAYS

    def results(self, commit="", counts=None):
        """The machine-readable form written to `test_results.json` (version 1)."""
        return {
            "version": RESULTS_VERSION,
            "commit": commit,
            "run_green": self.run_green,
            "counts": counts or {},
            "known_red": {
                i: {"owner": e.owner, "reason": e.reason, "first_seen": e.first_seen, "first_seen_time": e.first_seen_time,
                    "age_days": round(e.age_days(self.clock), 2)}
                for i, e in self.known_red.items()
            },
            "expired": {i: {"owner": e.owner, "age_days": round(e.age_days(self.clock), 2)} for i, e in self.expired.items()},
            "flakes": self.flakes,
            "new_failures": sorted(self.failed_again) + sorted(self.setup_new) + sorted(self.not_rerun) + sorted(self.expired),
            "setup_errors": sorted(self.setup_new),
            "unparsed": self.unparsed,
            "now_passing": self.now_passing,
            "fix_me": sorted(self.fix_me),
        }

    def summary_lines(self):
        """The text printed under the run's headline; every tolerated test is named."""
        lines = []
        if self.known_red:
            lines.append("Known red, not new (%d; tolerated for at most %g days, listed in the baseline):" % (
                len(self.known_red), self.limit))
            for test_id, entry in self.known_red.items():
                lines.append("  - %s  known red for %s, owner %s, first seen %s: %s" % (
                    test_id, age_text(entry.age_days(self.clock)), entry.owner, entry.first_seen[:12], entry.reason))
        for test_id, entry in self.expired.items():
            lines.append("EXPIRED known red (%s, over the %g day limit, owner %s): %s. Fix it, or renew it with a reason: "
                         "python3 hams_shared/tools/known_red.py renew %s --reason TEXT" % (
                             age_text(entry.age_days(self.clock)), self.limit, entry.owner, test_id, test_id))
        for test_id in self.now_passing:
            lines.append("Known red now PASSING, remove its baseline entry: %s" % test_id)
        if self.flakes:
            lines.append("Flaky (failed, passed when rerun alone; tolerated, counted in the flake file):")
            for test_id, entry in sorted(self.flakes.items()):
                lines.append("  - %s  flake count %d" % (test_id, entry["count"]))
        if self.fix_me:
            lines.append("FIX ME (flake count above the threshold):")
            for test_id, entry in sorted(self.fix_me.items()):
                lines.append("  - %s  %d flakes, last %s" % (test_id, entry["count"], entry.get("last", "?")))
        for test_id in self.failed_again:
            lines.append("NEW FAILURE (failed again when rerun alone): %s" % test_id)
        for test_id in self.setup_new:
            lines.append("NEW SETUP/CLASS ERROR (never rerun): %s" % test_id)
        for test_id, reason in sorted(self.not_rerun.items()):
            lines.append("NEW FAILURE (not rerun: %s): %s" % (reason, test_id))
        if self.unparsed:
            lines.append("NEW FAILURE x%d: Odoo counted failures that no log line named." % self.unparsed)
        return lines


# [@ANCHOR: known_red:decide]
def decide(first, baseline, rerun, flake_path, commit, threshold, modules=None, now=None, max_rerun=MAX_RERUN_TESTS,
           max_age=None, clock=time.time):
    """Applies the policy to the first run and returns a `PolicyOutcome`.

    `first`: `RunObservation` of the whole run. `baseline`: `{id: BaselineEntry}`. `rerun(test_ids)`: called at most
    once with the ids to rerun alone, returns a `RunObservation` of that rerun. `flake_path`: the flake file.
    `commit`: the commit under test. `threshold`: see `flake_threshold`. `modules`: the modules under test (limits
    the fix-me listing to them; None lists all). Nothing is rerun when no new failure exists, when more than
    `max_rerun` tests are new (a systemic failure), or when a failure could not be named; those new failures stay red.
    `max_age`: days a baseline entry is tolerated (`max_age_days()` when None); an entry past it, counted from its last renewal
    by `clock` (default `time.time`), is not tolerated and is never rerun: the run is red and the summary says how to renew it."""
    limit = max_age_days() if max_age is None else max_age
    cls = classify(first, baseline, limit, clock)
    not_rerun, flakes, failed_again, rerun_ran = {}, {}, [], False
    if cls.new:
        if len(cls.new) > max_rerun:
            not_rerun = {i: "more than %d new failures: systemic" % max_rerun for i in cls.new}
        elif cls.unparsed:
            not_rerun = {i: "some failures could not be named" for i in cls.new}
        else:
            again = rerun(list(cls.new))
            rerun_ran = True
            after = again.failed_ids()
            verdict = bool(again.odoo_result_lines)
            passed = [i for i in cls.new if verdict and i in again.started and i not in after]
            failed_again = [i for i in cls.new if i not in passed]
            if passed:
                flakes = record_flakes(flake_path, passed, commit, now)
    counts = load_flakes(flake_path)
    fix_me = {
        i: e for i, e in counts.items() if e.get("count", 0) > threshold and (modules is None or i.split(":")[0] in modules)
    }
    green = not (failed_again or cls.setup_new or not_rerun or cls.unparsed or cls.expired)
    return PolicyOutcome(
        green, cls.known_red, flakes, failed_again, cls.setup_new, not_rerun, cls.unparsed, cls.now_passing, fix_me, rerun_ran,
        cls.expired, clock, limit,
    )


# [@ANCHOR: known_red:raw_outcome]
def raw_outcome(first):
    """The outcome for `--raw-failures`: no baseline, no rerun, every failure reported as new and red. Used by
    tools that need Odoo's untouched list of failing tests (the module bisection)."""
    ids = []
    setup = []
    for record in first.failures:
        target = setup if record.setup else ids
        if record.test_id not in target:
            target.append(record.test_id)
    return PolicyOutcome(
        False, {}, {}, ids, setup, {}, max(0, first.counted() - len(first.failures)), [], {}, False
    )


# [@ANCHOR: known_red:git_head]
def git_head(directory):
    """The full commit id checked out in `directory`, or "" when git cannot say (never raises)."""
    import subprocess

    try:
        done = subprocess.run(
            ["git", "-c", "safe.directory=" + directory, "-C", directory, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


# [@ANCHOR: known_red:write_results]
def write_results(path, outcome, commit, observation):
    """Writes `outcome.results(...)` as JSON to `path` (atomically; the directory must exist). `observation` supplies
    the counts: `failed`, `errors`, `total` summed over its `odoo.tests.result` lines."""
    counts = {
        k: sum(r[k] for r in observation.odoo_result_lines) for k in ("failed", "errors", "total")
    }
    data = outcome.results(commit, counts)
    handle, temporary = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".results-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=1, sort_keys=True)
        out.write("\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


# [@ANCHOR: known_red:show_lines]
def show_lines(baseline, max_age=None, clock=time.time):
    """The baseline as text, oldest entry first: id, age, whether it is `EXPIRED: fails the run`, owner, first-seen commit,
    a renewal reason if any, and the reason it is red. `baseline` is `{id: BaselineEntry}`; `max_age` days (default
    `max_age_days()`). `known_red.py show`. An empty baseline gives one line saying so. Pure."""
    limit = max_age_days() if max_age is None else max_age
    if not baseline:
        return ["known-red baseline: empty (no tolerated test)"]
    rows = sorted(((e.age_days(clock), i, e) for i, e in baseline.items()), key=lambda r: (-r[0], r[1]))
    lines = ["known-red baseline: %d tolerated test(s), tolerated for at most %g days" % (len(rows), limit)]
    for age, test_id, entry in rows:
        lines.append("  %s  known red for %s%s, owner %s, first seen %s%s: %s" % (
            test_id, age_text(age), " (EXPIRED: fails the run)" if age > limit else "", entry.owner, entry.first_seen[:12],
            ", renewed: %s" % entry.renewed[1] if entry.renewed else "", entry.reason))
    return lines


# [@ANCHOR: known_red:renew_entry]
def renew_entry(baseline, test_id, reason, owner=None, now=time.time):
    """Renews `test_id` for another age window: rewrites the file that holds it (`entry.source`) with
    `renewed: {"time", "reason"}` (and `owner` when given), atomically, keeping every other field and entry. Returns the path.
    Raises `BaselineError` for an empty reason or an id that is not in `baseline`."""
    if not str(reason or "").strip():
        raise BaselineError("a renewal needs a reason")
    entry = baseline.get(test_id)
    if entry is None:
        raise BaselineError("%s is not in the known-red baseline" % test_id)
    with open(entry.source, encoding="utf-8") as handle:
        data = json.load(handle)
    for raw in data["entries"]:
        if raw["test_id"].lstrip("/") == test_id:
            raw["renewed"] = {"time": time.strftime(TIME_FORMAT, time.gmtime(now())), "reason": str(reason).strip()}
            if owner:
                raw["owner"] = owner
    directory = os.path.dirname(entry.source)
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".baseline-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=1)
        out.write("\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, entry.source)
    return entry.source


# [@ANCHOR: known_red:main]
def main(argv=None, out=print):
    """`python3 tools/known_red.py show|renew`: `show [--base-dir DIR]` lists the baseline oldest first; `renew TEST_ID --reason TEXT
    [--owner NAME] [--base-dir DIR]` keeps tolerating an entry for another window. Files are found as `baseline_paths(base_dir)` does
    (default the working directory). Returns 0, or 2 with the cause for a bad baseline, empty reason or unknown id."""
    parser = argparse.ArgumentParser(description="The known-red baseline of test.py (ADR 0118)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "renew"):
        command = sub.add_parser(name)
        command.add_argument("--base-dir", default=os.getcwd())
        if name == "renew":
            command.add_argument("test_id")
            command.add_argument("--reason", required=True)
            command.add_argument("--owner")
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline(baseline_paths(args.base_dir))
        if args.command == "renew":
            out("renewed %s in %s" % (args.test_id, renew_entry(baseline, args.test_id, args.reason, args.owner)))
            baseline = load_baseline(baseline_paths(args.base_dir))
        for line in show_lines(baseline):
            out(line)
    except BaselineError as error:
        out("known_red: %s" % error)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
