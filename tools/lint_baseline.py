#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Known-red baseline of the lint run (ADR 0117, docs/runbooks/LINT_MERGE_QUEUE.md).

A failure that is already on the main branch is not caused by the branch under test. A "canary" run
(`run_linters.py --baseline-refresh`) lints a clean `origin/main` and records every failing step and failing test
here. A normal branch run then compares its own failures with that record: an item that is in the record with the
same failure signature is reported loudly as "known red on main" and does not block; any other failure (a new
item, or a recorded test failing with a different message) blocks as before. The pass stamp lists what it
tolerated, so nothing is hidden.

The record is a machine-local file under `~/.cache/hams-lint/baseline/<repository>.json`, written only from a real
run of the linters on a clean main tree. The reasons it is not a file in the repository are in ADR 0117.
"""

import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time

import batch_queue

VERSION = 1
ENV_SWITCH = "HAMS_LINT_BASELINE"
MAX_AGE_ENV = "HAMS_LINT_BASELINE_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 3.0
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
UNASSIGNED = "unassigned"
MAX_MESSAGE = 240
MAX_STAMP_ITEMS = 12

PYTEST_LINE = re.compile(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$")
UNITTEST_LINE = re.compile(r"^(FAIL|ERROR): (\S+) \(([\w.]+)\)")
CARGO_LINE = re.compile(r"^test (\S+) \.\.\. FAILED$")
PROGRESS_LINE = re.compile(r"^\s*[.sFExX]+\s*(\[\s*\d+%\])?\s*$")
VOLATILE = [
    (re.compile(r"/[^\s'\"):,]+"), "<path>"),
    (re.compile(r"<path>:\d+"), "<path>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds|min)\b"), "<time>"),
    (re.compile(r"\s+"), " "),
]

# [@ANCHOR: lint_baseline:BaselineFile]
# The baseline document, one JSON file per repository: {"version": 1, "repo": name, "main": sha of the main commit
# the canary linted, "created": ISO time, "items": {item_id: {"step": step base name, "test": test id or "",
# "signature": 12 hex digits, "message": short text, "since": the oldest main commit at which this item was seen
# red with this signature, "first_seen_time": ISO local time the item was first recorded, "owner": who fixes it
# (default "unassigned"), optional "renewed": {"time", "reason"}}}. An item id is `<step>::<test id>` for a failing test and `<step>` for a step that failed
# with no test named. See docs/runbooks/LINT_MERGE_QUEUE_ANCHORS.md.


# [@ANCHOR: lint_baseline:enabled]
def enabled(environ=None):
    """True unless `HAMS_LINT_BASELINE` is `off`/`0`. `--no-baseline` sets it to `off` for the run."""
    environ = os.environ if environ is None else environ
    return environ.get(ENV_SWITCH, "on").lower() not in ("off", "0", "no", "false")


# [@ANCHOR: lint_baseline:baseline_dir]
def baseline_dir(environ=None):
    """Directory holding the baseline files: `$XDG_CACHE_HOME` (else `~/.cache`) `/hams-lint/baseline`."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hams-lint", "baseline")


# [@ANCHOR: lint_baseline:baseline_path]
def baseline_path(key, environ=None):
    """The baseline file of repository `key` (`hams_com`, `hams_open`, `hams_shared`)."""
    return os.path.join(baseline_dir(environ), key + ".json")


# [@ANCHOR: lint_baseline:normalize_message]
def normalize_message(text):
    """A failure message with what changes from run to run removed: absolute paths, addresses, long hex
    strings (ids, hashes), durations and whitespace runs. Other digits stay, so `3 != 4` and `3 != 5` differ.
    Returns the stripped string."""
    for pattern, replacement in VOLATILE:
        text = pattern.sub(replacement, text)
    return text.strip()


def _signature(text):
    return hashlib.sha1(normalize_message(text).encode("utf-8", "replace")).hexdigest()[:12]


def _base(step_name):
    """Step name without its shard or option suffix: `pytest tools/[2of4]` -> `pytest tools/`."""
    return step_name.split("[")[0].strip()


# [@ANCHOR: lint_baseline:max_age_days]
def max_age_days(environ=None):
    """How many days an item may stay tolerated: `HAMS_LINT_BASELINE_MAX_AGE_DAYS` (a finite number above 0) or 3. A bad value
    raises ValueError: a limit that quietly became "forever" would defeat it."""
    environ = os.environ if environ is None else environ
    text = environ.get(MAX_AGE_ENV, "").strip()
    if not text:
        return DEFAULT_MAX_AGE_DAYS
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("%s=%r is not a finite number of days above 0" % (MAX_AGE_ENV, text))
    return value


# [@ANCHOR: lint_baseline:item_age_days]
def item_age_days(item, created=None, now=time.time):
    """Days an item has been known red: since its last renewal, else since `first_seen_time`, else (an item recorded before
    the field existed) since the document's `created` time `created`. Returns a float, 0 when no time is known or it is unreadable."""
    renewed = item.get("renewed") or {}
    text = renewed.get("time") or item.get("first_seen_time") or created
    try:
        started = time.mktime(time.strptime(str(text), TIME_FORMAT))
    except (ValueError, OverflowError):
        return 0.0
    return max(0.0, (now() - started) / 86400.0)


def _age_text(days):
    return "%.1f days" % days if days >= 1 else "%.0f hours" % (days * 24)


def _item(step, test, message):
    return {"step": step, "test": test, "signature": _signature(message),
            "message": normalize_message(message)[:MAX_MESSAGE]}


# [@ANCHOR: lint_baseline:failure_items]
def failure_items(step_name, output):
    """The failures a failed step reports, as {item_id: {"step", "test", "signature", "message"}}.

    pytest `FAILED id - msg` / `ERROR id - msg` lines, unittest `FAIL: test (module.Class)` blocks and cargo
    `test name ... FAILED` lines each give one item per test, identified without the shard suffix of the step
    (the shard a file lands in changes when files are added). A step that names no test gives one item,
    `<step>`, whose signature covers the sorted set of its normalized output lines (progress dots and blank lines
    left out): the same findings give the same signature, one more or one different finding changes it."""
    step = _base(step_name)
    lines = str(output or "").splitlines()
    items = {}
    for index, line in enumerate(lines):
        found = PYTEST_LINE.match(line)
        if found:
            items["%s::%s" % (step, found.group(2).strip())] = _item(step, found.group(2).strip(), found.group(3) or "")
            continue
        found = CARGO_LINE.match(line)
        if found:
            items["%s::%s" % (step, found.group(1))] = _item(step, found.group(1), "")
            continue
        found = UNITTEST_LINE.match(line)
        if found:
            test = found.group(3) if found.group(3).endswith("." + found.group(2)) else "%s.%s" % (found.group(3), found.group(2))
            message = ""
            for follow in lines[index + 1:]:
                if follow.startswith(("=====", "Ran ")):
                    break
                if follow.strip() and not follow.startswith("-----"):
                    message = follow.strip()
            items["%s::%s" % (step, test)] = _item(step, test, message)
    if items:
        return items
    kept = sorted({normalize_message(line) for line in lines
                   if line.strip() and not PROGRESS_LINE.match(line) and "bringing up nodes" not in line})
    first = next((line.strip() for line in lines if re.search(r"error|fail|violation|✗|\U0001f6d1", line, re.I)), "")
    items[step] = {"step": step, "test": "", "signature": _signature("\n".join(kept)),
                   "message": normalize_message(first)[:MAX_MESSAGE]}
    return items


# [@ANCHOR: lint_baseline:items_from_failures]
def items_from_failures(failed):
    """Merge `failure_items` of every failed step. `failed` is {step name: output}. Returns {item_id: item}."""
    items = {}
    for name, output in failed.items():
        items.update(failure_items(name, output))
    return items


# [@ANCHOR: lint_baseline:read_baseline]
def read_baseline(key, environ=None):
    """The baseline document of `key`, or None when there is none or it is unreadable or of another version."""
    try:
        with open(baseline_path(key, environ), encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("version") != VERSION or not isinstance(doc.get("items"), dict):
        return None
    return doc


# [@ANCHOR: lint_baseline:write_baseline]
def write_baseline(key, main, items, environ=None, previous=None, now=time.time):
    """Write the baseline of `key` for main commit `main` (atomically). `items` is {item_id: item}; an item that
    `previous` (the old document) already has with the same signature keeps its `since`, `first_seen_time`, `owner` and
    `renewed`; a new one gets `since=main`, `first_seen_time` now and owner `unassigned`. Returns the document.
    Raises OSError when the file cannot be written."""
    old = (previous or {}).get("items", {})
    stamp = time.strftime(TIME_FORMAT, time.localtime(now()))
    stored = {}
    for item_id, item in items.items():
        kept = old.get(item_id)
        same = bool(kept) and kept.get("signature") == item["signature"]
        record = dict(item, since=kept.get("since") if same and kept.get("since") else main,
                      first_seen_time=(kept.get("first_seen_time") or (previous or {}).get("created") or stamp) if same else stamp,
                      owner=(kept.get("owner") or UNASSIGNED) if same else UNASSIGNED)
        if same and kept.get("renewed"):
            record["renewed"] = kept["renewed"]
        stored[item_id] = record
    doc = {"version": VERSION, "repo": key, "main": main, "created": stamp, "items": stored}
    _store(doc, key, environ)
    return doc


def _store(doc, key, environ=None):
    directory = baseline_dir(environ)
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=directory, prefix=key + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(doc, out, indent=1, sort_keys=True)
        os.replace(temp, baseline_path(key, environ))
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


# [@ANCHOR: lint_baseline:renew]
def renew(key, item_id, reason, owner=None, environ=None, now=time.time):
    """Keep tolerating `item_id` for another `max_age_days` window, with a stated `reason` (and optionally a new `owner`).
    Records `renewed: {time, reason}` on the item, atomically. Returns the item. Raises ValueError for an empty reason and
    KeyError when there is no baseline or no such item (the message lists nothing else: use `--baseline-show`)."""
    if not str(reason or "").strip():
        raise ValueError("a renewal needs a reason")
    doc = read_baseline(key, environ)
    if doc is None or item_id not in doc["items"]:
        raise KeyError("no known-red item %r in the baseline of %s" % (item_id, key))
    item = doc["items"][item_id]
    item["renewed"] = {"time": time.strftime(TIME_FORMAT, time.localtime(now())), "reason": str(reason).strip()}
    if owner:
        item["owner"] = owner
    _store(doc, key, environ)
    return item


def _contains(repo, commit, head):
    """True when `head` has `commit` in its history (or is it)."""
    if not commit:
        return False
    result = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", commit, head],
                            capture_output=True, text=True)
    return result.returncode == 0


# [@ANCHOR: lint_baseline:classify]
def classify(items, doc, repo=None, head="HEAD", contains=_contains, now=time.time, environ=None):
    """Sort this run's failure `items` against baseline `doc`.

    Returns (tolerated, blocking): `tolerated` is {item_id: baseline item} for items recorded with the same
    signature whose `since` commit is in `head`'s history (the branch contains the red main, so the failure is not
    its own); `blocking` is {item_id: reason} for everything else: "new" (not in the baseline), "changed" (recorded,
    but the message differs: the branch changed how it fails), "since <sha> is not in your tree: rebase", or "known red for N
    days": an item older than `max_age_days` (counted from its last renewal) is no longer tolerated, whatever the branch.
    Each tolerated item is returned as a copy with `age_days`. With no document nothing is tolerated."""
    tolerated, blocking = {}, {}
    limit = max_age_days(environ)
    recorded = (doc or {}).get("items", {})
    for item_id, item in items.items():
        old = recorded.get(item_id)
        if old is None:
            blocking[item_id] = "new: not failing on main"
        elif old.get("signature") != item["signature"]:
            blocking[item_id] = "changed: fails differently from main (main: %s)" % (old.get("message") or "no message")[:120]
        elif repo is not None and not contains(repo, old.get("since"), head):
            blocking[item_id] = "main was red since %s, which is not in your tree: rebase on origin/main" % str(old.get("since"))[:10]
        else:
            age = item_age_days(old, (doc or {}).get("created"), now)
            if age > limit:
                blocking[item_id] = ("known red for %s (limit %g days, owner %s): fix it, or renew it with a reason: "
                                     "run_linters.py --baseline-renew '%s' --reason TEXT" % (
                                         _age_text(age), limit, old.get("owner") or UNASSIGNED, item_id))
            else:
                tolerated[item_id] = dict(old, age_days=age)
    return tolerated, blocking


# [@ANCHOR: lint_baseline:judge]
def judge(failed, key, repo, head="HEAD", environ=None, now=time.time):
    """The decision of one run. `failed` is {step name: output} of every failed step. Returns a dict:
    {"tolerated": {id: item}, "blocking": {id: reason}, "items": {id: item}, "main": baseline's main sha or None,
    "passes": bool}. `passes` is True only when there is at least one failure, all of them tolerated. No failures,
    a disabled switch or a missing baseline all give passes=False with nothing tolerated (the caller keeps its own
    verdict then)."""
    items = items_from_failures(failed)
    doc = read_baseline(key, environ) if enabled(environ) else None
    if not items or doc is None:
        return {"tolerated": {}, "blocking": {i: "new: no baseline" for i in items}, "items": items,
                "main": None, "passes": False}
    tolerated, blocking = classify(items, doc, repo, head, now=now, environ=environ)
    return {"tolerated": tolerated, "blocking": blocking, "items": items, "main": doc.get("main"),
            "passes": bool(tolerated) and not blocking}


# [@ANCHOR: lint_baseline:report_lines]
def report_lines(verdict):
    """Printable lines for a run with known-red items: a loud banner listing each tolerated item (and, when
    something still blocks, which items are new or changed)."""
    lines = []
    tolerated, blocking = verdict["tolerated"], verdict["blocking"]
    if tolerated:
        lines.append("")
        lines.append("=== KNOWN RED ON MAIN (baseline of %s): %d failure(s) tolerated, not caused by this branch ===" % (
            str(verdict.get("main") or "?")[:10], len(tolerated)))
        for item_id, item in sorted(tolerated.items()):
            lines.append("  known red: %s   (known red for %s, owner %s; red on main since %s)  %s" % (
                item_id, _age_text(item.get("age_days", 0.0)), item.get("owner") or UNASSIGNED,
                str(item.get("since"))[:10], item.get("message", "")[:100]))
    if blocking:
        lines.append("")
        lines.append("=== %d failure(s) that are NOT known red on main (these block) ===" % len(blocking))
        for item_id, reason in sorted(blocking.items()):
            lines.append("  BLOCKS: %s  [%s]" % (item_id, reason))
    return lines


# [@ANCHOR: lint_baseline:stamp_field]
def stamp_field(tolerated, main):
    """` known_red=<id>(<age>;<owner>),... baseline=<main 12 hex>` for a pass stamp, or "" when nothing was tolerated. Spaces
    in an id become `_`; the age is `item["age_days"]` as `3.1d`; more than `MAX_STAMP_ITEMS` ids end with `+N`. The pass
    stamp thereby records what it tolerated, for how long, and whose it is."""
    if not tolerated:
        return ""
    ids = ["%s(%.1fd;%s)" % ("_".join(item_id.split()), item.get("age_days", 0.0),
                             "_".join(str(item.get("owner") or UNASSIGNED).split()))
           for item_id, item in sorted(tolerated.items())]
    shown = ids[:MAX_STAMP_ITEMS]
    if len(ids) > MAX_STAMP_ITEMS:
        shown.append("+%d" % (len(ids) - MAX_STAMP_ITEMS))
    return " known_red=%s baseline=%s" % (",".join(shown), str(main or "?")[:12])


# [@ANCHOR: lint_baseline:refresh]
def refresh(repo, key, tree_name, base="origin/main", force=False, environ=None, out=print, remote="auto",
            command=None, fetch=True, prepare=None):
    """The canary: lint a clean `base` and record what fails. Returns an exit code: 0 recorded (or already
    current), 75 the run could not finish (the old baseline is kept), 2 setup problem.

    `repo`: a git checkout of the repository; `key`: baseline name; `tree_name`: directory name of the temporary tree
    (not `hams_shared`: lint treats that name as a link). Steps: fetch, return at once when the baseline already
    belongs to this commit (unless `force`), take a non-blocking lock so only one canary runs per repository, build a
    clean worktree of `base` (no branch merged), run `tools/run_linters.py --solo --no-baseline --remote <remote>` there
    (the full run, no cache), record the failing items from `--results-json`, remove the tree. A run that gave up
    waiting for the lint lock (75) or reported no steps leaves the old baseline alone. Side effects: a worktree
    under ~/workspace/tmp for the duration of the run, the baseline file. `command(tree, results_json)` and `prepare(tree)`
    are for tests."""
    import lint_batch
    environ = dict(os.environ if environ is None else environ)
    if fetch:
        subprocess.run(["git", "-C", repo, "fetch", "-q", "origin"], capture_output=True, text=True)
    head = subprocess.run(["git", "-C", repo, "rev-parse", base], capture_output=True, text=True).stdout.strip()
    if not head:
        out("baseline: cannot resolve %s in %s" % (base, repo))
        return 2
    previous = read_baseline(key, environ)
    if previous and previous.get("main") == head and not force:
        out("baseline: %s is current for %s (%d known-red item(s))" % (key, head[:10], len(previous["items"])))
        return 0
    os.makedirs(baseline_dir(environ), exist_ok=True)
    lock = open(os.path.join(baseline_dir(environ), key + ".lock"), "w")  # noqa: SIM115 - held until return
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            out("baseline: another canary run for %s is in progress; not starting a second" % key)
            return 0
        batch_queue.reap_orphan_trees(repo, "canary", tree_name, environ=environ)
        parent = batch_queue.tree_parent("canary", tree_name, environ=environ)
        dest = os.path.join(parent, tree_name)
        results = os.path.join(parent, "results.json")
        try:
            built = batch_queue.build_combined_tree(repo, head, [], dest)
            if built["head"] is None:
                out("baseline: cannot create the tree: %s" % built.get("error", ""))
                return 2
            if prepare is not None:
                prepare(dest)
            else:
                fixer = os.path.join(dest, "fix_worktree_symlinks.py")
                if os.path.isfile(fixer):
                    subprocess.run([sys.executable or "python3", fixer], cwd=dest, capture_output=True, text=True)
            argv = (command(dest, results) if command else
                    ["nice", "-n", "10", sys.executable or "python3", os.path.join(dest, "tools", "run_linters.py"),
                     "--solo", "--no-baseline", "--remote", remote, "--results-json", results])
            run_env = dict(environ, **{ENV_SWITCH: "off"})
            out("baseline: linting a clean %s of %s (%s)" % (base, key, head[:10]))
            code = subprocess.run(argv, cwd=dest, env=run_env).returncode
            selected, failed = lint_batch.read_results(results)
            if code == 75 or not selected:
                out("baseline: the canary run did not finish (exit %d, %d steps reported): old baseline kept" % (code, len(selected)))
                return 75
            doc = write_baseline(key, head, items_from_failures(failed), environ, previous)
            out("baseline: %s recorded for %s: %d known-red item(s)" % (key, head[:10], len(doc["items"])))
            for item_id, item in sorted(doc["items"].items()):
                out("  known red: %s  %s" % (item_id, item["message"][:100]))
            return 0
        finally:
            batch_queue.remove_tree(repo, dest)
    finally:
        lock.close()


# [@ANCHOR: lint_baseline:show_lines]
def show_lines(key, environ=None, now=time.time):
    """Lines describing the baseline of `key` (`run_linters.py --baseline-show`), the oldest item first: age, owner, a renewal
    reason if any, whether it is already past the limit, and the message."""
    doc = read_baseline(key, environ)
    if doc is None:
        return ["baseline: none recorded for %s (run: run_linters.py --baseline-refresh)" % key]
    limit = max_age_days(environ)
    rows = sorted(((item_age_days(item, doc.get("created"), now), item_id, item) for item_id, item in doc["items"].items()),
                  key=lambda row: (-row[0], row[1]))
    lines = ["baseline of %s: main %s, recorded %s, %d known-red item(s), tolerated for at most %g days" % (
        key, doc.get("main", "?")[:10], doc.get("created", "?"), len(doc["items"]), limit)]
    for age, item_id, item in rows:
        renewed = item.get("renewed")
        lines.append("  %s  known red for %s%s, owner %s, since %s%s  %s" % (
            item_id, _age_text(age), " (EXPIRED: blocks)" if age > limit else "", item.get("owner") or UNASSIGNED,
            str(item.get("since"))[:10], ", renewed: %s" % renewed["reason"] if renewed else "", item.get("message", "")[:100]))
    return lines
