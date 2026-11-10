#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Enforcement of ADR 0114 decision 5: the external programs the relay uses are inventoried, pinned and watched.

Usage: check_external_programs_documented.py <repo_root>

Reads the inventory tables of `docs/adrs/0114_*.md` and `tools/dependency_watch.json` (both beside this script)
and fails (exit 1) when:
  1. the relay's production Rust code (`daemons/hams_local_relay/src`, test code left out) launches a program by a
     literal name (`Command::new("name")`) that no table of the ADR names;
  2. an installer or release script (`install_*.sh`, `packaging/**`, `tools/*.sh` of the relay) depends on a program
     it detects with `command -v name`, or a Windows script with `Get-Command name`, that no table names;
  2b. a relay file outside `DYNAMIC_LAUNCH_FILES` starts a program with `Command::new(<expression>)`;
  3. a discovery function that locates a program started under a variable name (`find_pat_binary` and the
     Mercury, ardopcf and Direwolf finders) is gone from the source or is not named in the ADR;
  4. a row of an ADR table with a `Watched` column names an `owner/repo` that `dependency_watch.json` lacks, or
     says the program is not watched;
  5. an entry of `dependency_watch.json` names a repo or program the ADR does not mention.
There is no baseline file: findings go to zero and stay at zero (ADR 0090). A repository without the relay source
(hams_open) has nothing to scan in steps 1 to 3 and passes them.
"""

import check_target
import glob
import json
import os
import re
import sys
from tree_files import tree_files

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY_DIR = os.path.join("daemons", "hams_local_relay")

# Dynamic launches: the function that finds the program, and the file it must be defined in (relay-relative).
DISCOVERY_FUNCTIONS = {
    "find_pat_binary": "src/winlink.rs",
    "find_direwolf_binary": "src/direwolf.rs",
    "find_mercury_binary": "src/mercury.rs",
    "find_ardopcf_binary": "src/ardop.rs",
}

# Relay source files that may start a program by an expression (`Command::new(path)`): a path found by a discovery function or a program
# passed in by the caller, which the literal-name scan cannot see. A dynamic launch in any OTHER file is a finding: add the file here
# in the same change that documents the program in ADR 0114.
DYNAMIC_LAUNCH_FILES = frozenset({
    "src/ardop.rs", "src/com0com.rs", "src/direwolf.rs", "src/gps_time.rs", "src/m17_reference.rs", "src/mercury.rs",
    "src/relay_info.rs", "src/secret_store.rs", "src/shared_receiver/frontend_process.rs", "src/updater.rs", "src/winlink.rs",
})

_TEST_FILE = re.compile(r"(?:_tests?|^tests?|^test_[^/]*)\.rs$")
_CFG_TEST = re.compile(r"#\[cfg\(\s*(?:all\(\s*)?test\b[^\]]*\]")
_REPO = re.compile(r"`([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)`")
_LITERAL_LAUNCH = re.compile(r'Command::new\(\s*"([^"\\]+)"\s*\)')
_ANY_LAUNCH = re.compile(r"Command::new\(")
_SHELL_DETECT = re.compile(r"\bcommand\s+-v\s+([A-Za-z][A-Za-z0-9_.+-]*)")
_PS_DETECT = re.compile(r"\bGet-Command\s+['\"]?([A-Za-z][A-Za-z0-9_.+-]*)")


# [@ANCHOR: ext_programs:code_mask]
def code_mask(text):
    """Returns a string of the same length as the Rust `text` in which comments and the inside of string and
    character literals are blanked (newlines kept) so braces and keywords found in it are real code."""
    out = list(text)
    n = len(text)
    i = 0

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            blank(i, j)
            i = j
        elif c in "rbc" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")) and re.match(r'(?:br|cr|r)#*"', text[i : i + 40]):
            # A raw string, also the raw byte (br"...") and raw C (cr"...") forms: no escapes inside, so a backslash
            # before the closing quote must not be read as an escape.
            opening = re.match(r"(?:br|cr|r)(#*)", text[i:])
            hashes = len(opening.group(1))
            start = i + opening.end() + 1
            end = text.find('"' + "#" * hashes, start)
            end = n if end < 0 else end + 1 + hashes
            blank(start, end - 1 - hashes)
            i = end
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        elif c == "'":
            m = re.match(r"'(?:\\.[^']*|[^'\\])'", text[i : i + 12])
            if m:
                blank(i + 1, i + m.end() - 1)
                i += m.end()
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


# [@ANCHOR: ext_programs:test_spans]
def test_spans(masked):
    """Returns the `(start, end)` offsets of every item behind a `#[cfg(test)]` attribute in `masked` (the output of
    `code_mask`): the attribute through the end of the module, function or statement that follows it."""
    spans = []
    for m in _CFG_TEST.finditer(masked):
        i = m.end()
        # Further attributes between the cfg and the item.
        while True:
            a = re.compile(r"\s*#\[").match(masked, i)
            if not a:
                break
            depth, j = 0, a.end() - 1
            while j < len(masked):
                depth += masked[j] == "["
                depth -= masked[j] == "]"
                j += 1
                if depth == 0:
                    break
            i = j
        brace, semi = masked.find("{", i), masked.find(";", i)
        if semi >= 0 and (brace < 0 or semi < brace):
            spans.append((m.start(), semi + 1))
            continue
        if brace < 0:
            spans.append((m.start(), len(masked)))
            continue
        depth, j = 0, brace
        while j < len(masked):
            depth += masked[j] == "{"
            depth -= masked[j] == "}"
            j += 1
            if depth == 0:
                break
        spans.append((m.start(), j))
    return spans


# [@ANCHOR: ext_programs:program_name]
def program_name(literal):
    """The program a `Command::new` literal names: the text after the last `/`, so `"/usr/sbin/ioreg"` is `ioreg` (a program started by absolute
    path, which a PATH search cannot replace, is still the program the ADR must name). A bare name is returned unchanged. Pure."""
    return literal.rsplit("/", 1)[-1]


# [@ANCHOR: ext_programs:rust_launches]
def rust_launches(text):
    """Returns `(literal_names, dynamic_count)` for the Rust source `text`: the set of program names started as
    `Command::new("name")` and the number of `Command::new(<expression>)` calls, both outside comments and
    `#[cfg(test)]` items."""
    masked = code_mask(text)
    spans = test_spans(masked)
    names, dynamic = set(), 0
    for m in _ANY_LAUNCH.finditer(masked):
        if any(a <= m.start() < b for a, b in spans):
            continue
        lit = _LITERAL_LAUNCH.match(text, m.start())
        if lit:
            names.add(program_name(lit.group(1)))
        else:
            dynamic += 1
    return names, dynamic


# [@ANCHOR: ext_programs:script_dependencies]
def script_dependencies(text, powershell=False):
    """Returns the set of program names a shell script (or, with `powershell`, a PowerShell script) tests for with
    `command -v name` (`Get-Command name`), ignoring comment lines."""
    pattern = _PS_DETECT if powershell else _SHELL_DETECT
    found = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        found.update(pattern.findall(line))
    return found


# [@ANCHOR: ext_programs:cells]
def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


# [@ANCHOR: ext_programs:adr_tables]
def adr_tables(adr_text):
    """Parses the markdown tables of the ADR. Returns a list of `(header_cells, rows)`, each row a list of cells."""
    tables, cur = [], None
    for line in adr_text.splitlines():
        if not line.lstrip().startswith("|"):
            cur = None
            continue
        cells = _cells(line)
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
            continue
        if cur is None:
            cur = (cells, [])
            tables.append(cur)
        else:
            cur[1].append(cells)
    return tables


PROGRAM_COLUMNS = frozenset({"program", "programs", "tool", "library"})
# Ordinary English words of a first cell written as prose ("Chrome or Chromium with Playwright", "Debian 12 root file system"): never a program name
# by themselves. A program that really is `file` or `go` must be named in backticks.
PROSE_WORDS = frozenset({"a", "an", "and", "or", "on", "in", "of", "for", "the", "to", "under", "with", "file", "system", "root", "tooling", "toolchain"})


# [@ANCHOR: ext_programs:documented_names]
def documented_names(adr_text):
    """Returns the lower-case program names the ADR tables name: in the columns headed Program, Programs, Tool or Library only, every word
    inside backticks (split at spaces, slashes, commas and semicolons, `.exe` removed) and, when the first column is one of those, the
    words of each row's first cell other than `PROSE_WORDS`. A word in a Role, Notes or Pin column (`and`, `file`, `which`, a commit hash) names nothing."""
    names = set()
    for header, rows in adr_tables(adr_text):
        columns = [k for k, h in enumerate(header) if h.lower() in PROGRAM_COLUMNS]
        for row in rows:
            for k in columns:
                if k >= len(row):
                    continue
                for quoted in re.findall(r"`([^`]+)`", row[k]):
                    for word in re.split(r"[\s/,;]+", quoted):
                        names.add(re.sub(r"\.exe$", "", word.lower()))
            if row and 0 in columns:
                names.update(w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", row[0]) if w.lower() not in PROSE_WORDS)
    return names


# [@ANCHOR: ext_programs:watch_findings]
def watch_findings(adr_text, watch):
    """Returns the findings (strings) about the weekly watch: a table row naming an `owner/repo` that `watch` (the
    parsed `dependency_watch.json`) lacks or that is marked not watched, and a `watch` entry the ADR never mentions."""
    findings = []
    repos = {v["repo"].lower() for v in watch.values() if isinstance(v, dict) and "repo" in v}
    for header, rows in adr_tables(adr_text):
        col = next((k for k, h in enumerate(header) if h.lower() == "watched"), None)
        if col is None:
            continue
        for row in rows:
            repo = _REPO.search(row[0]) if row else None
            if not repo or len(row) <= col:
                continue
            name = repo.group(1)
            if name.lower() not in repos:
                findings.append(f"ADR 0114 lists {name} but dependency_watch.json has no entry for it")
            if not re.sub(r"[*_]", "", row[col]).strip().lower().startswith("yes"):
                findings.append(f"ADR 0114 says {name} is not watched: mark it yes once it is in dependency_watch.json")
    lowered = adr_text.lower()
    names = documented_names(adr_text)
    for key, entry in sorted(watch.items()):
        if not isinstance(entry, dict) or "repo" not in entry:
            continue
        if entry["repo"].lower() not in lowered and key.lower() not in names:
            findings.append(f"dependency_watch.json entry {key} ({entry['repo']}) is not in ADR 0114")
    return findings


# [@ANCHOR: ext_programs:is_test_file]
def _is_test_file(rel):
    base = os.path.basename(rel)
    return bool(_TEST_FILE.search(base)) or "tests" in rel.split(os.sep)


# [@ANCHOR: ext_programs:relay_rust_files]
def _relay_rust_files(relay):
    src = os.path.join(relay, "src")
    for path in tree_files(src, ".rs", ("vendor", "target"), untracked=True):
        rel = os.path.relpath(path, relay)
        if not _is_test_file(rel):
            yield os.path.join(relay, rel), rel


# [@ANCHOR: ext_programs:script_files]
def _script_files(relay):
    found = set()
    for pattern in ("install_*.sh", "packaging/**/*.sh", "packaging/**/postinst", "packaging/**/postrm", "tools/*.sh"):
        found.update(glob.glob(os.path.join(relay, pattern), recursive=True))
    return sorted(f for f in found if not os.path.basename(f).startswith("test_"))


# [@ANCHOR: ext_programs:read]
def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# [@ANCHOR: ext_programs:find_findings]
def find_findings(repo_root, adr_path, watch_path):
    """Runs every check and returns the findings as a list of strings (empty when the tree agrees with the ADR).
    `repo_root` is the checkout that may hold `daemons/hams_local_relay`; `adr_path` and `watch_path` are the
    ADR file and `dependency_watch.json`."""
    adr = _read(adr_path)
    with open(watch_path, "r", encoding="utf-8") as f:
        watch = json.load(f)
    names = documented_names(adr)
    findings = []
    relay = os.path.join(repo_root, RELAY_DIR)
    if os.path.isdir(os.path.join(relay, "src")):
        sources = {}
        for path, rel in _relay_rust_files(relay):
            text = _read(path)
            sources[rel] = text
            literal, dynamic = rust_launches(text)
            if dynamic and rel not in DYNAMIC_LAUNCH_FILES:
                findings.append(f"{RELAY_DIR}/{rel} starts a program by an expression ({dynamic} call(s)); name the program in ADR 0114 and add the file to DYNAMIC_LAUNCH_FILES")
            for name in sorted(literal):
                if re.sub(r"\.exe$", "", name.lower()) not in names:
                    findings.append(f"{RELAY_DIR}/{rel} launches {name!r}, which no table of ADR 0114 names")
        for func, rel in sorted(DISCOVERY_FUNCTIONS.items()):
            if not re.search(r"\bfn\s+" + func + r"\b", sources.get(rel, "")):
                findings.append(f"{RELAY_DIR}/{rel} no longer defines {func}: update DISCOVERY_FUNCTIONS and ADR 0114")
            if func not in adr:
                findings.append(f"ADR 0114 does not name the discovery function {func}")
        for path in _script_files(relay):
            for name in sorted(script_dependencies(_read(path))):
                if name.lower() not in names:
                    findings.append(f"{os.path.relpath(path, repo_root)} needs {name!r}, which no table of ADR 0114 names")
        ps1 = os.path.join(relay, "packaging", "install_windows.ps1")
        if os.path.exists(ps1):
            for name in sorted(script_dependencies(_read(ps1), powershell=True)):
                if re.sub(r"\.exe$", "", name.lower()) not in names:
                    findings.append(f"{os.path.relpath(ps1, repo_root)} needs {name!r}, which no table of ADR 0114 names")
    findings.extend(watch_findings(adr, watch))
    return findings


# [@ANCHOR: ext_programs:main]
def main(argv=None):
    """Command line: `check_external_programs_documented.py <repo_root>`. Prints each finding and returns 1, or
    prints one line and returns 0."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: check_external_programs_documented.py <repo_root>", file=sys.stderr)
        return 2
    adrs = sorted(glob.glob(os.path.join(HERE, "..", "docs", "adrs", "0114_*.md")))
    if not adrs:
        print("[!] ADR 0114 not found beside hams_shared/tools", file=sys.stderr)
        return 2
    target = check_target.require_directory(argv[0], "check_external_programs_documented")
    relay = os.path.join(target, RELAY_DIR)
    # A repository that carries no relay (hams_shared) has nothing to scan here; run by hand on one, that is a failure, not a pass.
    check_target.require_scanned(
        len(list(_relay_rust_files(relay))) if os.path.isdir(os.path.join(relay, "src")) else 0,
        relay,
        "check_external_programs_documented",
        "relay Rust source files",
    )
    findings = find_findings(target, adrs[0], os.path.join(HERE, "dependency_watch.json"))
    if findings:
        print("[!] ADR 0114: the external programs the relay uses are not all inventoried, pinned and watched:")
        for f in findings:
            print(f"    - {f}")
        print(
            "      FIX: add the program to a table of hams_shared/docs/adrs/0114_*.md (and, if we ship or build it, "
            "to tools/dependency_watch.json with Watched = yes), in the same pull request."
        )
        return 1
    print("[+] SUCCESS: every external program the relay launches or detects is in ADR 0114, and every watched one is pinned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
