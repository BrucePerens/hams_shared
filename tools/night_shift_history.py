#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Night-shift history: one file per entry
---------------------------------------
The completed-work log of the night-shift agents used to be a single file, `night_shift_history.md`, edited at the top by
every agent, so every pull request conflicted with every other one. It is now a directory, `night_shift_history/`, with ONE
FILE PER ENTRY. A new entry is a new file whose name no other agent will pick, so two branches never touch the same path.

    python3 hams_shared/tools/night_shift_history.py latest -n 5        # the five newest entries, newest first
    python3 hams_shared/tools/night_shift_history.py latest --titles    # one line per entry: file name and heading
    python3 hams_shared/tools/night_shift_history.py new --title "Short title" <<'EOF'
    - what was wrong, what changed, the evidence
    EOF                                                                   # creates the file, prints its path
    python3 hams_shared/tools/night_shift_history.py check [ROOT]       # lint: the layout and every entry file

`check` is also the linter step `check_night_shift_history` in run_linters.py. It accepts BOTH layouts so the rollout can be
done in two steps: a repository without `night_shift_history/` is the old single-file layout and is not judged; once the
directory exists, `night_shift_history.md` must be exactly STUB_TEXT and `night_shift_history_archive/` must be gone, so a
stale branch that appends to the old file fails loudly instead of re-creating the conflict. Full reference: README.md in
this directory, "Night-shift history: one file per entry".
"""

import argparse
import datetime
import os
import re
import secrets
import subprocess
import sys

DIRECTORY = "night_shift_history"
OLD_FILE = "night_shift_history.md"
OLD_ARCHIVE = "night_shift_history_archive"
DIRECTORY_README = "README.md"

# [@ANCHOR: tools:night_shift_history_stub_text]
# The only content `night_shift_history.md` may have once `night_shift_history/` exists.
STUB_TEXT = (
    "# Night-shift history has moved\n"
    "\n"
    "The history is no longer one file. Every entry is its own file in the directory `night_shift_history/`, so that two\n"
    "agents adding an entry never edit the same path and their pull requests do not conflict. The older entries that used to\n"
    "be in `night_shift_history_archive/part_NN/` are in the same directory.\n"
    "\n"
    "- Read the newest entries: `python3 hams_shared/tools/night_shift_history.py latest -n 5`\n"
    "- Add an entry: `python3 hams_shared/tools/night_shift_history.py new --title \"Short title\"` with the text on standard input\n"
    "  (it creates `night_shift_history/<date>-<time>-<slug>-<random>.md` and prints the path). Never edit this file.\n"
    "- Check entries: `python3 hams_shared/tools/night_shift_history.py check`\n"
    "- Search: `grep -r PATTERN night_shift_history/`\n"
    "\n"
    "The layout and the entry format are described in `night_shift_history/README.md`. A branch that was started before the\n"
    "move and still appends to this file will conflict here and fail the `check_night_shift_history` linter: copy its entry\n"
    "into a new file with the command above and restore this file from `main`.\n"
)

# [@ANCHOR: tools:night_shift_history_entry_format]
# File name: YYYY-MM-DD-HHMMSS-<slug>-<6 hex>.md. Names sort in time order, so `ls` is the history.
NAME_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d\d-\d\d)-(?P<time>\d{6})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<rand>[0-9a-f]{6})\.md$"
)
# First line of an entry written on or after STRICT_FROM: "### YYYY-MM-DD -- title".
STRICT_HEADING = re.compile(r"^### (?P<date>\d{4}-\d\d-\d\d) -- (?P<title>\S.*)$")
# Entries dated on or before this day were migrated from the single file, whose headings follow many conventions.
LEGACY_THROUGH = "2026-11-08"
SLUG_MAX = 60

_DATE_IN_TEXT = re.compile(r"\d{4}-\d\d-\d\d")
_HEADING_LINE = re.compile(r"^(#{1,6}) ")
_FENCE_LINE = re.compile(r"^ {0,3}(```|~~~)")
_DATED_H3 = re.compile(r"^### \d{4}-\d\d-\d\d")


# [@ANCHOR: tools:night_shift_history_split_entries]
def split_entries(text):
    """Cut `text` into (preamble, [entry, ...]) so that preamble + "".join(entries) == text, exactly.

    An entry starts at a `## ` heading, or a `### ` heading that begins with a date, outside a code fence; a heading line
    directly under a heading line of the same level is the wrapped rest of that heading, not a new entry. Every other
    heading (`# `, `### Verification`) is body. Full entry: README.md in this directory."""
    lines = text.splitlines(keepends=True)
    starts = []
    in_fence = False
    previous_heading_level = None
    for index, line in enumerate(lines):
        if _FENCE_LINE.match(line):
            in_fence = not in_fence
        if in_fence:
            previous_heading_level = None
            continue
        match = _HEADING_LINE.match(line)
        if not match:
            previous_heading_level = None
            continue
        level = match.group(1)
        if previous_heading_level == level:
            continue
        previous_heading_level = level
        if level == "##" or (level == "###" and _DATED_H3.match(line)):
            starts.append(index)
    if not starts:
        return text, []
    bounds = starts + [len(lines)]
    preamble = "".join(lines[: starts[0]])
    return preamble, ["".join(lines[bounds[i]: bounds[i + 1]]) for i in range(len(starts))]


# [@ANCHOR: tools:night_shift_history_heading_text]
def heading_text(entry):
    """The heading of an entry as one line: its heading line(s) without the leading `#` marks, joined by a space."""
    lines = entry.splitlines()
    match = _HEADING_LINE.match(lines[0]) if lines else None
    if not match:
        return ""
    marks = match.group(0)
    parts = []
    for line in lines:
        if not line.startswith(marks):
            break
        parts.append(line[len(marks):].strip())
    return " ".join(parts)


# [@ANCHOR: tools:night_shift_history_slug]
def slugify(title):
    """A file-name slug for `title`: lower case ASCII letters and digits joined by single hyphens, at most SLUG_MAX
    characters, cut at a hyphen. A leading date and punctuation are dropped. Returns "entry" when nothing is left."""
    title = _DATE_IN_TEXT.sub("", title, count=1) if re.match(r"^\s*\d{4}-\d\d-\d\d", title) else title
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(slug) > SLUG_MAX:
        cut = slug[:SLUG_MAX]
        slug = cut[: cut.rfind("-")] if "-" in cut[SLUG_MAX // 2:] else cut
        slug = slug.strip("-")
    return slug or "entry"


# [@ANCHOR: tools:night_shift_history_entry_problems]
def entry_problems(name, text):
    """Everything wrong with one entry file, as a list of messages (empty = well formed).

    `name` is the file name, `text` its contents. See README.md in this directory for the rules and which of them apply
    only to entries dated after LEGACY_THROUGH."""
    match = NAME_PATTERN.match(name)
    if not match:
        return [f"{name}: the name must be YYYY-MM-DD-HHMMSS-<slug>-<6 hex>.md (lower case; the slug is letters, digits, hyphens)"]
    problems = []
    try:
        date = datetime.date.fromisoformat(match.group("date"))
    except ValueError:
        return [f"{name}: {match.group('date')} is not a calendar date"]
    strict = match.group("date") > LEGACY_THROUGH
    if strict:
        clock = match.group("time")
        if int(clock[:2]) > 23 or int(clock[2:4]) > 59 or int(clock[4:]) > 59:
            problems.append(f"{name}: {clock} is not a time of day (HHMMSS)")
    if "\r" in text or "\0" in text:
        problems.append(f"{name}: contains a carriage return or NUL byte")
    if not text.endswith("\n") or text.endswith("\n\n"):
        problems.append(f"{name}: must end with exactly one newline")
    preamble, entries = split_entries(text)
    if preamble.strip():
        problems.append(f"{name}: text before the entry heading (the first line must be the heading)")
    if len(entries) != 1:
        problems.append(
            f"{name}: holds {len(entries)} entries (a `## ` heading or a dated `### ` heading); one file is one entry"
        )
    if strict:
        lines = text.split("\n")
        heading = STRICT_HEADING.match(lines[0])
        if not heading:
            problems.append(f"{name}: the first line must be `### {date.isoformat()} -- <title>`")
        elif heading.group("date") != match.group("date"):
            problems.append(f"{name}: the heading date {heading.group('date')} differs from the file name date")
        if len(lines) < 3 or lines[1] != "" or not lines[2].strip():
            problems.append(f"{name}: the heading must be followed by a blank line and then the text of the entry")
    return problems


# [@ANCHOR: tools:night_shift_history_check_tree]
def check_tree(root):
    """Problems with the night-shift history under repository `root`, as a list of messages (empty = fine).

    Without `root/night_shift_history/` it is the old single-file layout and nothing is judged. With it: the old file must be
    exactly STUB_TEXT, the archive directory must be gone, and every entry file must pass entry_problems()."""
    directory = os.path.join(root, DIRECTORY)
    if not os.path.isdir(directory):
        return []
    problems = []
    old_file = os.path.join(root, OLD_FILE)
    try:
        with open(old_file, encoding="utf-8") as handle:
            old_text = handle.read()
    except OSError:
        old_text = None
    if old_text != STUB_TEXT:
        problems.append(
            f"{OLD_FILE}: must be the stub that points to {DIRECTORY}/ and nothing else; the history is one file per entry now. "
            f"A branch that appends here is stale: put its entry in a new file ("
            f"python3 hams_shared/tools/night_shift_history.py new --title ...) and restore {OLD_FILE} from main"
        )
    if os.path.exists(os.path.join(root, OLD_ARCHIVE)):
        problems.append(f"{OLD_ARCHIVE}/: the archive moved into {DIRECTORY}/; remove it")
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if name == DIRECTORY_README and os.path.isfile(path):
            continue
        if not os.path.isfile(path) or os.path.islink(path):
            problems.append(f"{DIRECTORY}/{name}: only regular entry files belong in this directory")
            continue
        try:
            with open(path, encoding="utf-8", newline="") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as error:
            problems.append(f"{DIRECTORY}/{name}: cannot be read as UTF-8 text ({error})")
            continue
        problems.extend(f"{DIRECTORY}/{problem}" for problem in entry_problems(name, text))
    return problems


# [@ANCHOR: tools:night_shift_history_latest]
def latest_entries(root, count):
    """The `count` newest entries under `root` as a list of (label, text), newest first.

    Reads `night_shift_history/` (newest = last file name) or, in the old layout, the top of `night_shift_history.md`.
    Raises FileNotFoundError when neither exists and ValueError when `count` is below 1."""
    if count < 1:
        raise ValueError("count must be at least 1")
    directory = os.path.join(root, DIRECTORY)
    if os.path.isdir(directory):
        names = sorted(n for n in os.listdir(directory) if NAME_PATTERN.match(n))
        chosen = list(reversed(names[-count:]))
        result = []
        for name in chosen:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                result.append((name, handle.read()))
        return result
    old_file = os.path.join(root, OLD_FILE)
    with open(old_file, encoding="utf-8") as handle:
        _, entries = split_entries(handle.read())
    return [(f"{OLD_FILE} (entry {i + 1})", entry) for i, entry in enumerate(entries[:count])]


# [@ANCHOR: tools:night_shift_history_new_entry]
def new_entry(root, title, body, now=None, date=None):
    """Create a new entry file under `root/night_shift_history/` and return its path.

    `title` is the heading text, `body` the entry text (may be multi-line; a trailing newline is added). The name is the
    local date and time, the slug of the title and six random hex digits. `date` (YYYY-MM-DD) overrides the date in the
    heading and the name. Raises ValueError when the entry would not pass entry_problems(), and FileNotFoundError when
    the repository still has the single-file layout."""
    directory = os.path.join(root, DIRECTORY)
    if not os.path.isdir(directory):
        raise FileNotFoundError(
            f"{directory} does not exist: this checkout still has the single-file layout, add the entry at the top of {OLD_FILE}"
        )
    now = now or datetime.datetime.now()
    day = date or now.date().isoformat()
    title = " ".join(title.split())
    text = f"### {day} -- {title}\n\n{body.strip()}\n"
    for _ in range(100):
        name = f"{day}-{now.strftime('%H%M%S')}-{slugify(title)}-{secrets.token_hex(3)}.md"
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            break
    problems = entry_problems(name, text)
    if problems:
        raise ValueError("; ".join(problems))
    with open(path, "x", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


# [@ANCHOR: tools:night_shift_history_repository_root]
def _repository_root(explicit):
    if explicit:
        return explicit
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else os.getcwd()


# [@ANCHOR: tools:night_shift_history_main]
def main(argv=None):
    """Command line: `latest`, `new` and `check` (see the module docstring). Returns the exit status: 0 success, 1 `check`
    found problems or `new`/`latest` could not do the work, 2 bad usage (argparse)."""
    parser = argparse.ArgumentParser(prog="night_shift_history.py", description="Night-shift history entries, one file each.")
    sub = parser.add_subparsers(dest="command", required=True)
    latest = sub.add_parser("latest", help="print the newest entries, newest first")
    latest.add_argument("-n", type=int, default=5, help="how many entries (default 5)")
    latest.add_argument("--titles", action="store_true", help="print only the file name and heading of each")
    latest.add_argument("--root", help="repository root (default: the git top level of the current directory)")
    new = sub.add_parser("new", help="create a new entry file; the text is read from --body-file or standard input")
    new.add_argument("--title", required=True, help="heading text, without the date")
    new.add_argument("--body-file", help="read the entry text from this file (default: standard input)")
    new.add_argument("--date", help="YYYY-MM-DD for the heading and file name (default: today)")
    new.add_argument("--root", help="repository root (default: the git top level of the current directory)")
    check = sub.add_parser("check", help="lint the layout and every entry file")
    check.add_argument("root", nargs="?", help="repository root (default: the git top level of the current directory)")
    args = parser.parse_args(argv)
    root = _repository_root(getattr(args, "root", None))
    if args.command == "check":
        problems = check_tree(root)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    try:
        if args.command == "latest":
            for label, text in latest_entries(root, args.n):
                if args.titles:
                    print(f"{label}\t{heading_text(text)}")
                else:
                    print(f"<!-- {label} -->\n{text}")
            return 0
        if args.body_file:
            with open(args.body_file, encoding="utf-8") as handle:
                body = handle.read()
        else:
            body = sys.stdin.read()
        print(os.path.relpath(new_entry(root, args.title, body, date=args.date), root))
        return 0
    except (OSError, ValueError) as error:
        print(f"night_shift_history.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
