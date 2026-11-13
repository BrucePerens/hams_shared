#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Function-Level Claims Freshness Check (ADR 0091)

A "claim," per ADR 0091, is a short markdown file at `<module>/claims/<anchor_name>.md`
documenting precisely and falsifiably what one anchored function guarantees -- the same
falsifiable-claim discipline used to re-verify patent specifications (see
`hams_shared/agents/skills/bug-hunt/SKILL.md`), applied to ordinary
functions rather than patent claims. A claim file's own YAML frontmatter records `anchor` (the
base anchor name the claim documents, matching a real `# [@ANCHOR: name]` in source) and
`code_hash` (a sha256 of the anchored function's own source span, as it existed the last time a
human or AI actually confirmed the claim's own prose still matches the code).

This script does not, and cannot, verify that a claim's *prose* is still true -- that needs a
reader, human or AI, actually comparing the two. What it verifies mechanically, cheaply, and
without any semantic understanding at all: whether the anchored function's *source text* has
changed since the claim was last confirmed. A hash mismatch does not mean the claim is wrong -- it
means nobody has looked since the code changed, which is exactly the condition that let real
patent-specification claims drift out of sync with the code they described (a cross-document or
cross-section self-contradiction that nobody re-checked).

Hash algorithms (see the table above `ALGORITHMS`): a claim records `hash_algorithm: v2`, the token
stream of the anchored item per language (Python, Rust, JavaScript, XML/QWeb, shell, SQL; see
`claim_normalizers.py`), so comments and formatting never stale it. A claim with no such field is
legacy: its algorithm is chosen from its own date (front matter `date:`, else the commit that last
wrote the claim file, never the file's mtime). A legacy claim whose exact hash no longer matches is
not stale for a comment-only or format-only change: the check loads the function as of the claim's
commit (`git show`), computes v2 over that text and over the current text, and passes it when the
two are equal; verdicts are cached in `~/.cache/hams_claims/` (`$HAMS_CLAIM_CACHE_DIR`). No
repo-wide re-hash is ever needed, so old claims are never rewritten just to survive a tool change.

No baseline/ratchet is needed the way `check_function_test_anchors.py` needs one: this is a brand
new mechanism with zero pre-existing claims when it ships, so there is no backlog to grandfather --
every claim ever committed should have an accurate hash from the moment it's added.
"""

import argparse
import ast
import collections
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tokenize

import claim_normalizers as cn  # noqa: E402
import verify_anchors as va  # noqa: E402
from check_function_test_anchors import (  # noqa: E402
    _direct_functions,
    _function_span,
    _is_base_anchor_declaration,
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)
FIELD_RE = re.compile(r"^([a-zA-Z_]+):[ \t]*(.+?)[ \t]*$", re.MULTILINE)

# Wired into run_linters.py as a hard-blocking gate, with these three top-level directories
# excluded for now: `ingest/`, `ics_forms/`, and `ics_training/` all have other sessions' real,
# active, in-progress work landing continuously, and re-checking their claims before that work
# settles would flag transient churn, not a real regression. Every other stale-claim finding
# outside these three was triaged to 0 before the gate was turned on. A hardcoded exclusion
# set, not a CLI flag: matches the existing convention this tool family already uses for the identical
# "live, in-progress pipeline directory" situation --
# check_absolute_paths.py's own `ignore_dirs` already excludes `ics_training/` for the same
# reason. Remove this once that other session's work lands and settles, and a fresh
# check_claims_freshness.py run is confirmed clean there too (not assumed).
EXCLUDED_TOP_LEVEL_DIRS = {"ingest", "ics_forms", "ics_training"}


class GitListingError(RuntimeError):
    """`git ls-files` could not list the tree (not a repository, git missing, dubious ownership). Raised, never swallowed into an
    empty list: an empty list reads as "no claims, all fresh" and the gate printed SUCCESS for a tree it had not looked at."""


def _git_tracked_files(repo_root, suffix):
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "ls-files", f"*{suffix}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        raise GitListingError(f"cannot list the files of {repo_root}: {str(detail).strip()}") from error
    return [
        os.path.join(repo_root, line)
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def parse_claim_frontmatter(content):
    """Returns {field: value} from a claim file's own YAML-lite frontmatter, or None if the
    file has no `---`-delimited frontmatter block at all. Deliberately not a real YAML parser --
    this format is two flat scalar fields (`anchor`, `code_hash`), not a dependency worth adding."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return None
    fields = {}
    for fm in FIELD_RE.finditer(m.group(1)):
        value = re.sub(r"\s+#.*$", "", fm.group(2))  # a trailing YAML comment (the retirement example in check_retirement uses one)
        fields[fm.group(1)] = value.strip().strip('"').strip("'")
    return fields


# [@ANCHOR: check_claims_freshness:COMM_claim_retirement]
def check_retirement(fields, claim_path, search_roots=()):
    """Classify a claim's retirement. Returns `(is_retired, problem_or_None)`.

    A claim documents one anchored function. When that function is deliberately deleted or
    renamed, the claim is kept on purpose -- it holds the original finding, often a high-severity
    security one -- but its `anchor:` can no longer resolve, so every freshness checker reported
    it as an "orphaned claim" forever. Three real claims were in exactly that state, against one
    genuine finding, and a gate that can never go green stops being read: the real orphan hiding
    among the permanent ones stops being noticed. There was also no way to fix it from inside the
    claim, because dropping `anchor:` only trades the error for "missing required frontmatter".

    The convention, therefore, is a `retired:` date plus a statement of WHICH kind of retirement
    it is, because a reviewer must be able to tell a deliberate retirement from an accidental
    orphan:

        retired: 2026-09-15
        superseded_by: _create_odoo_role_if_missing.md   # renamed or moved: name the live claim
        retired_reason: deleted                          # or: the code is simply gone

    Exactly one of the two is required. `retired:` alone is refused, so this cannot become a
    one-line way to silence a real orphan -- the author has to say which case it is, and when
    they say "superseded", the successor is verified to exist and to not itself be retired, so a
    typo or a retirement chain cannot quietly hide a claim forever.

    `superseded_by` is resolved relative to the retired claim's own directory first (the common
    case is a renamed function's sibling claim), then relative to each of `search_roots` (for a
    successor that moved to another module's claims directory).
    """
    if "retired" not in fields:
        return False, None

    superseded = fields.get("superseded_by")
    reason = fields.get("retired_reason")
    if not superseded and not reason:
        return True, (
            "'retired:' with neither 'superseded_by:' nor 'retired_reason:' -- a retirement must "
            "say which kind it is, so it can't be used to silence a genuinely orphaned claim; "
            "name the live successor claim, or give a reason such as 'retired_reason: deleted'"
        )

    if superseded:
        resolved, tried = _resolve_superseded_by(claim_path, superseded, search_roots)
        if resolved is None:
            return True, (
                f"superseded_by '{superseded}' names no existing claim file (looked in: "
                f"{', '.join(tried)}) -- a typo here hides this claim forever"
            )
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                successor = parse_claim_frontmatter(f.read())
        except (OSError, UnicodeDecodeError):
            successor = None
        if successor is not None and "retired" in successor:
            return True, (
                f"superseded_by '{superseded}' is itself retired -- point at the live claim that "
                "documents the current code, not at another retired one"
            )

    return True, None


def _resolve_superseded_by(claim_path, value, search_roots):
    """Returns `(resolved_path_or_None, paths_tried)` for a `superseded_by:` value."""
    tried = []
    candidates = [os.path.join(os.path.dirname(claim_path), value)]
    candidates.extend(os.path.join(root, value) for root in search_roots)
    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        tried.append(normalized)
        if os.path.isfile(normalized):
            return normalized, tried
    return None, tried


def _comment_columns(content):
    """Returns {1-based line number: column where a real `#` COMMENT token starts}, found with
    `tokenize` so a `#` inside a string literal is never mistaken for a comment."""
    columns = {}
    for tok in tokenize.generate_tokens(io.StringIO(content).readline):
        if tok.type == tokenize.COMMENT:
            columns[tok.start[0]] = tok.start[1]
    return columns


def _docstring_line_ranges(node):
    """Returns [(first_line, last_line)] for the docstring of `node` and of every function and
    class nested in it. A docstring that shares its line with other code is left alone (it is not
    removable as whole lines), which only ever makes the hash more sensitive, never less."""
    ranges = []
    for sub in ast.walk(node):
        if not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        first = sub.body[0] if sub.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ranges.append((first.lineno, first.end_lineno))
    return ranges


def normalized_span_text(span_start, span_end, lines, comment_columns, docstring_ranges):
    """The text `code_hash` is computed from: lines `span_start..span_end` (1-based, inclusive)
    with every `#` comment removed, every docstring of the function (and of nested functions and
    classes) removed, trailing whitespace stripped and blank lines dropped.

    WHY: a claim is stale when the function's behaviour may have changed. A reworded comment or
    docstring cannot change behaviour, yet hashing the raw text made all 27 user_websites claims
    stale after one night of comment-rewording pull requests (2026-11-09). Indentation and every
    code token are kept, so any real edit (including a changed string literal, which can carry
    behaviour) still changes the hash. Text-based on purpose: unlike hashing `ast.dump`, the
    result does not depend on the Python version that parses it, so the same hash comes out on
    the dev box and on every remote lint host."""
    dropped = set()
    for first, last in docstring_ranges:
        if span_start <= first and last <= span_end:
            dropped.update(range(first, last + 1))
    out = []
    for number in range(span_start, min(span_end, len(lines)) + 1):
        if number in dropped:
            continue
        line = lines[number - 1]
        column = comment_columns.get(number)
        if column is not None:
            line = line[:column]
        line = line.rstrip()
        if line:
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# Hash algorithms (a claim's own date, never the file's mtime, says which one it was written with)
# ---------------------------------------------------------------------------------------------

# v1   sha256 of the function's raw source text (every claim written before 2026-11-09).
# v1b  sha256 of the text with `#` comments, docstrings, blank lines and trailing whitespace removed
#      (hams_shared #540, 2026-11-09): Python only, still sensitive to formatting.
# v2   sha256 of the code's TOKEN STREAM, per language (`claim_normalizers.py`): comments and
#      formatting never reach it; Python, Rust, JavaScript, XML/QWeb, shell and SQL.
# A claim records `hash_algorithm: v2`. A claim without the field is legacy: the table below maps
# the date the claim was written (front matter `date:` when present, else the date of the commit
# that last wrote the claim file, `git log`) to the algorithm it was written with.
ALGORITHMS = ("v1", "v1b", "v2")
CURRENT_ALGORITHM = "v2"
ALGORITHM_BY_DATE = (
    ("0000-00-00", "v1"),
    ("2026-11-09", "v1b"),
    ("2026-11-10", "v2"),
)
HISTORY_DEPTH = 300

Hashes = collections.namedtuple("Hashes", "v2 v1b v1")
AnchorInfo = collections.namedtuple("AnchorInfo", "root path hashes")

SOURCE_SUFFIXES = (".py", ".rs", ".js", ".mjs", ".cjs", ".xml", ".sh", ".bash", ".sql")


def algorithm_for_date(date_text):
    """The algorithm a legacy claim written on `date_text` (YYYY-MM-DD..., or None for 'now') used."""
    if not date_text:
        return CURRENT_ALGORITHM
    chosen = ALGORITHM_BY_DATE[0][1]
    for start, algorithm in ALGORITHM_BY_DATE:
        if date_text[:10] >= start:
            chosen = algorithm
    return chosen


def function_hashes_from_text(content, filename="<string>"):
    """Returns `{anchor_name: Hashes}` for every Python function in `content` carrying a real BASE
    anchor declaration (not a Tests/Verified-by/Triggers link to one) within its own span (the
    AST span including decorators and the comment lines above them). `Hashes` is
    `(v2, v1b, v1)`, see the table above. A file that does not parse yields `{}`."""
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError:
        return {}

    lines = content.splitlines()
    comment_columns = None
    tokens = None
    hashes = {}
    for _qualname, node in _direct_functions(tree.body, []):
        start, end = _function_span(node, lines)
        span_lines = lines[start - 1 : min(end, len(lines))]
        names = []
        for i, line in enumerate(span_lines):
            prev_line = span_lines[i - 1] if i > 0 else ""
            for anchor_match in va.ANCHOR_PATTERN.finditer(line):
                if _is_base_anchor_declaration(line, anchor_match, prev_line):
                    names.append(va._clean(anchor_match.group(1)))
        if not names:
            continue
        if comment_columns is None:
            comment_columns = _comment_columns(content)
        try:
            if tokens is None:
                tokens = cn.PythonTokens(content)
            v2 = hashlib.sha256(tokens.normalize(start, end).encode("utf-8")).hexdigest()
        except cn.NormalizeError:
            continue
        v1 = hashlib.sha256("\n".join(span_lines).encode("utf-8")).hexdigest()
        v1b = hashlib.sha256(
            normalized_span_text(
                start, end, lines, comment_columns, _docstring_line_ranges(node)
            ).encode("utf-8")
        ).hexdigest()
        for name in names:
            hashes[name] = Hashes(v2, v1b, v1)
    return hashes


def anchor_hashes_from_text(content, path, wanted=None):
    """`{anchor_name: Hashes}` for the anchored items of one source text, whatever its language
    (by `path`'s suffix); `{}` for an unknown language or a text that cannot be tokenized."""
    language = cn.language_for_path(path)
    if language is None or "[@ANCHOR" not in content:
        return {}
    if language == "python":
        return function_hashes_from_text(content, path)
    try:
        return {
            name: Hashes(h, None, None)
            for name, h in cn.anchored_hashes(content, language, wanted).items()
        }
    except cn.NormalizeError:
        return {}


def _read_text(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def compute_function_hash(filepath):
    """Returns {anchor_name: sha256_hex} for every anchored item in `filepath` (any supported
    language), hashed with the current algorithm (v2: the token stream)."""
    content = _read_text(filepath)
    if content is None:
        return {}
    return {name: h.v2 for name, h in anchor_hashes_from_text(content, filepath).items()}


def build_anchor_index(repo_root, wanted=None):
    """Returns {anchor_name: AnchorInfo(root, path, Hashes)} across every git-tracked source file
    (Python, Rust, JavaScript, XML/QWeb, shell, SQL) of `repo_root`. `wanted` (a set of cleaned
    anchor names) skips every file that declares none of them, so a run that has 2,400 claims to
    check does not tokenize the files nobody claims. A duplicate anchor name across files is a
    pre-existing verify_anchors.py-level problem, not this script's to resolve: last scanned wins."""
    index = {}
    for suffix in SOURCE_SUFFIXES:
        for filepath in _git_tracked_files(repo_root, suffix):
            content = _read_text(filepath)
            if content is None or "[@ANCHOR" not in content:
                continue
            if wanted is not None:
                names = {va._clean(m.group(1)) for m in va.ANCHOR_PATTERN.finditer(content)}
                if not names & wanted:
                    continue
            for name, hashes in anchor_hashes_from_text(content, filepath, wanted).items():
                index[name] = AnchorInfo(repo_root, filepath, hashes)
    return index


def build_anchor_hash_index(repo_root):
    """`{anchor_name: v2 sha256 hex}`; see `build_anchor_index`."""
    return {name: info.hashes.v2 for name, info in build_anchor_index(repo_root).items()}


def _strip_prefix(recorded):
    recorded = recorded.strip()
    return recorded[len("sha256:") :] if recorded.startswith("sha256:") else recorded


def recorded_hash_matches(recorded, current):
    """Exact comparison of a recorded hash with the current `Hashes` (any algorithm)."""
    return _strip_prefix(recorded) in [h for h in current if h]


def _git_out(root, *args):
    result = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


class FreshnessContext:
    """Per-run state of the legacy-claim history check: dates of claim commits, and a persistent
    cache of verdicts (`~/.cache/hams_claims/history_verdicts.json`, or `$HAMS_CLAIM_CACHE_DIR`).
    A verdict is keyed by (recorded hash, anchor, current v2 hash, claim commit time, file), which
    fully determines it, because the revisions it reads are all older than the claim commit."""

    def __init__(self, cache_dir=None, depth=HISTORY_DEPTH):
        self.depth = depth
        self.cache_dir = cache_dir or os.environ.get("HAMS_CLAIM_CACHE_DIR") or os.path.join(
            os.path.expanduser("~"), ".cache", "hams_claims"
        )
        self.cache_path = os.path.join(self.cache_dir, "history_verdicts.json")
        self._cache = None
        self._dirty = False
        self._times = {}
        self.history_lookups = 0  # revisions walked (not served from the cache), for tests

    def _load(self):
        if self._cache is None:
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def save(self):
        if not self._dirty:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = f"{self.cache_path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f)
            os.replace(tmp, self.cache_path)
        except OSError:
            pass  # a cache that cannot be written only makes the next run slower
        self._dirty = False

    def claim_time(self, claim_path):
        """(unix time, ISO date) of the commit that last wrote the claim file, or (None, None)."""
        if claim_path not in self._times:
            out = _git_out(
                os.path.dirname(claim_path) or ".", "log", "-1", "--format=%ct %cI", "--", os.path.basename(claim_path)
            )
            parts = (out or "").split()
            self._times[claim_path] = (int(parts[0]), parts[1][:10]) if len(parts) == 2 else (None, None)
        return self._times[claim_path]

    def claim_date(self, fields, claim_path):
        """The claim's own date: front matter `date:` when it is a date, else its commit's date."""
        declared = str(fields.get("date", ""))
        if re.match(r"\d{4}-\d\d-\d\d", declared):
            return declared[:10]
        return self.claim_time(claim_path)[1]

    def history_verdict(self, claim_path, anchor, info, recorded, date_algorithm):
        """("fresh" | "stale" | "unknown", detail). Finds the newest revision of the anchor's file,
        not newer than the claim's commit, whose anchored item carries the recorded hash (under any
        algorithm, the date's own first), and compares the current v2 hash with that revision's:
        equal means only comments or formatting changed since the claim was written."""
        ts, _date = self.claim_time(claim_path)
        if ts is None:
            return "unknown", "the claim file has no commit, so it cannot be dated"
        rel = os.path.relpath(info.path, info.root)
        key = hashlib.sha256(
            "|".join([recorded, anchor, info.hashes.v2, str(ts), rel]).encode("utf-8")
        ).hexdigest()
        cache = self._load()
        if key in cache:
            return cache[key][0], cache[key][1]
        listing = _git_out(info.root, "log", f"-n{self.depth}", "--format=%H %ct", "--", rel)
        if not listing:
            return "unknown", f"no git history for {rel} in {info.root}"
        order = [date_algorithm] + [a for a in ALGORITHMS if a != date_algorithm]
        verdict = None
        for line in listing.splitlines():
            rev, _, ctime = line.partition(" ")
            if not ctime.isdigit() or int(ctime) > ts:
                continue
            self.history_lookups += 1
            text = _git_out(info.root, "show", f"{rev}:{rel}")
            if text is None:
                continue
            old = anchor_hashes_from_text(text, rel, {anchor}).get(anchor)
            if old is None:
                continue
            if any(getattr(old, a) == recorded for a in order if getattr(old, a)):
                if old.v2 == info.hashes.v2:
                    verdict = ("fresh", f"only comments or formatting changed since {rev[:10]}")
                else:
                    verdict = ("stale", f"the code changed since {rev[:10]}")
                break
        if verdict is None:
            return "unknown", (
                f"the recorded hash matches no revision of {rel} up to the claim's commit "
                f"(searched the newest {self.depth} revisions), so history cannot say what changed"
            )
        cache[key] = list(verdict)
        self._dirty = True
        return verdict


def claim_hash_problem(fields, claim_path, anchor, info, context):
    """None when the claim's recorded hash is fresh for `info` (an `AnchorInfo`), else the text of
    the problem. A claim declaring `hash_algorithm: v2` compares exactly (v2 already ignores
    comments and formatting). A legacy claim (no field, or v1 / v1b) is fresh when its hash equals
    one of the current hashes, and otherwise gets the history check
    (`FreshnessContext.history_verdict`): comment-only and format-only changes since the claim was
    written keep it fresh, a code change makes it stale, and when the history cannot be found the
    exact comparison's mismatch stands, with the reason."""
    recorded = _strip_prefix(fields["code_hash"])
    declared = fields.get("hash_algorithm")
    if declared is not None and declared not in ALGORITHMS:
        return f"unknown hash_algorithm '{declared}' (known: {', '.join(ALGORITHMS)})"
    current = info.hashes
    if declared == "v2":
        if recorded == current.v2:
            return None
        why = "the anchored code changed since this claim was last confirmed"
    else:
        if recorded_hash_matches(recorded, current):
            return None
        date = context.claim_date(fields, claim_path)
        algorithm = declared or algorithm_for_date(date)
        status, detail = context.history_verdict(claim_path, anchor, info, recorded, algorithm)
        if status == "fresh":
            return None
        if status == "stale":
            why = f"{detail} (written with hash algorithm {algorithm})"
        else:
            why = (
                f"the exact comparison failed and the history check could not decide: {detail}; "
                f"treated as stale (written with hash algorithm {algorithm})"
            )
    return (
        f"code_hash mismatch for anchor '{anchor}' -- {why}; comment, docstring and formatting edits do not count "
        f"(recorded sha256:{recorded[:12]}..., current sha256:{current.v2[:12]}... under {CURRENT_ALGORITHM}); "
        "review the claim's own prose against the current code and rewrite the claim with write_claim.py "
        "once confirmed accurate (or fix the code, if the claim was right and the change broke it)"
    )


def check_claims(repo_root):
    """Returns a list of (claim_path, problem) for every claims/*.md file whose own recorded
    hash doesn't match the anchor's current code, or whose anchor no longer exists at all.

    Thin wrapper over `scan_claims`, kept because it is this module's long-standing entry point.
    """
    return scan_claims(repo_root)[0]


def scan_claims(repo_root, context=None):
    """Returns `(problems, retired)`: the same (claim_path, problem) list `check_claims` returns,
    plus the paths of every claim deliberately retired (see COMM_claim_retirement). A retired
    claim is not hash-checked -- its anchor is gone on purpose -- but is reported as a non-failing
    count, so retirements stay visible instead of disappearing."""
    problems = []
    retired = []
    pending = []  # (claim_path, fields, anchor) claims that carry a real hash
    context = context or FreshnessContext()

    for claim_path in _git_tracked_files(repo_root, ".md"):
        if f"{os.sep}claims{os.sep}" not in claim_path:
            continue
        # A README.md inside a claims/ directory is a deliberate index/explanation file for the
        # directory itself, not a claim -- it carries no frontmatter because it isn't one.
        if os.path.basename(claim_path) == "README.md":
            continue
        # The centralized claims store (docs/bug_hunt_claims/<repo>/...) documents code that
        # lives in a DIFFERENT git repo; this checker's freshness model assumes the claim and the
        # anchored code share one repo root, so it would report every one "orphaned". That skill's
        # own docs name the correct tool for these: `check_claims.py --source-root <other-repo>`.
        rel = os.path.relpath(claim_path, repo_root)
        if rel.split(os.sep)[:2] == ["docs", "bug_hunt_claims"]:
            continue
        # See EXCLUDED_TOP_LEVEL_DIRS's own comment: an exact top-level path-component match.
        if rel.split(os.sep)[0] in EXCLUDED_TOP_LEVEL_DIRS:
            continue
        try:
            with open(claim_path, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as error:
            # An unreadable claim is a failure, not a claim that is skipped: skipping made a damaged file read as fresh.
            problems.append((claim_path, f"claim file cannot be read: {error}"))
            continue
        fields = parse_claim_frontmatter(content)
        if fields is None or "anchor" not in fields or "code_hash" not in fields:
            problems.append(
                (claim_path, "missing required frontmatter fields 'anchor' and/or 'code_hash'")
            )
            continue

        # Checked before the hash, deliberately: a retired claim's anchor is gone on purpose, so
        # every check below it would report a permanent, unfixable failure. See
        # COMM_claim_retirement.
        is_retired, retirement_problem = check_retirement(fields, claim_path, (repo_root,))
        if is_retired:
            retired.append(claim_path)
            if retirement_problem:
                problems.append((claim_path, retirement_problem))
            continue

        if fields["code_hash"].strip().lower().startswith("not computable"):
            # A declared tooling gap (a claim written before the per-language normalizers, or an
            # anchor-less function): not a freshness problem. write_claim.py now records a real
            # hash for every language it can anchor.
            continue
        pending.append((claim_path, fields, fields["anchor"]))

    if pending:
        wanted = {va._clean(anchor) for _p, _f, anchor in pending}
        index = build_anchor_index(repo_root, wanted)
        for claim_path, fields, anchor in pending:
            info = index.get(va._clean(anchor))
            if info is None:
                problems.append(
                    (claim_path, f"anchor '{anchor}' not found in any git-tracked source file -- orphaned claim")
                )
                continue
            problem = claim_hash_problem(fields, claim_path, anchor, info, context)
            if problem:
                problems.append((claim_path, problem))
        context.save()
    return problems, retired


# Source suffixes `--unclaimed` scans for implementation anchors. A claim documents one anchored
# *function* (ADR 0091), so only languages with functions are listed -- the same three
# `check_function_test_anchors.py` and its JS/Rust siblings cover. XML/HTML anchors mark views,
# records and templates, which nobody writes a function claim for.
UNCLAIMED_SCAN_SUFFIXES = (".py", ".js", ".rs")

# Path components that make a file a test file. A base anchor declared in one is a test anchor
# (the target of a `# Verified by [@ANCHOR: ...]` link), not an implementation anchor.
_TEST_DIR_NAMES = {"tests", "test", "tour", "tours"}


def _is_test_file(rel_path):
    parts = rel_path.split(os.sep)
    if any(part in _TEST_DIR_NAMES for part in parts[:-1]):
        return True
    base = parts[-1]
    return (
        base.startswith("test_")
        or base.endswith(("_test.py", "_tests.py", ".test.js", ".rs.test"))
        or base in ("tests.rs", "conftest.py")
    )


def _anchor_scan_excluded(rel_path):
    """Mirrors `verify_anchors.find_anchors_in_code`'s own directory exclusions (build output,
    `docs/`, `tools/`, `scripts/`, the nested-checkout names), including its exception for real
    code islands that happen to live under `docs/`."""
    for island in va.CODE_ISLANDS_UNDER_DOCS:
        if rel_path.startswith(island.rstrip(os.sep) + os.sep):
            return False
    excluded = va.ANCHOR_SCAN_EXCLUDE_DIRS | {
        "docs", "tools", "scripts", "hams_community", "hams_com", "radae",
    }
    return any(part in excluded for part in rel_path.split(os.sep)[:-1])


def implementation_anchors_in_file(full_path, content, repo_root):
    """Returns `[(module, name, line_num)]` for every base anchor declaration in `content`.

    Classification is delegated to `verify_anchors._process_file_for_anchors` itself (with
    throwaway accumulators) rather than reimplemented, so `Tests`/`Verified by`/`Triggers` links,
    `doc_`/`story_`/`journey_` documentation anchors, conversational "see [@ANCHOR: ...]"
    mentions and wrapped-comment references are all excluded by exactly the rules the anchor
    linter itself uses. `module` is the anchor's explicit `module:` prefix when it has one, else
    the file's own module (`verify_anchors.get_module`)."""
    anchor_locations = {}
    duplicates = []
    va._process_file_for_anchors(
        full_path, content, va.ANCHOR_PATTERN, {}, anchor_locations, {}, {}, {}, {}, {},
        duplicates, {}, repo_root,
    )
    found = []
    seen = set()
    entries = [(anchor, loc) for anchor, locs in anchor_locations.items() for loc in locs]
    entries += [(anchor, loc) for anchor, loc, _prior in duplicates]
    for anchor, loc in entries:
        module, name = anchor.split(":", 1)
        if (module, name) in seen:
            continue
        seen.add((module, name))
        found.append((module, name, int(loc.rsplit(":", 1)[1])))
    return found


def _claim_keys(claim_path, fields, module):
    """Returns the set of `(module, cleaned_base_name)` keys one claim file satisfies.

    A claim is matched both by its file name (`<module>/claims/<anchor_name>.md`, the ADR 0091
    layout) and by its frontmatter `anchor:` value, because the two do not always agree in the
    real repos (e.g. `add_incident_note.md` documents `pager_duty:mcp_add_incident_note_tool`).
    An `anchor:` value can carry its own `module:` prefix, and a few claims document several
    anchors joined with `+` or `,`. `module=None` means "any module" (the centralized store)."""
    keys = {(module, va._clean(os.path.splitext(os.path.basename(claim_path))[0]))}
    for raw in re.split(r"[+,\s]+", (fields or {}).get("anchor", "")):
        if not raw:
            continue
        if ":" in raw:
            prefix, base = raw.split(":", 1)
            keys.add((module, va._clean(base)))
            if module is not None:
                keys.add((prefix, va._clean(base)))
        else:
            keys.add((module, va._clean(raw)))
    return keys


def _read_claim_fields(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_claim_frontmatter(f.read())
    except (OSError, UnicodeDecodeError):
        return None


def build_claim_index(repo_root, store_roots=()):
    """Returns the set of `(module, base_name)` keys covered by a claim file.

    Every git-tracked `claims/*.md` in `repo_root` counts for the module `verify_anchors.get_module`
    assigns its own path (so `ham_shack/claims/x.md` covers `ham_shack` anchors, and
    `daemons/hams_local_relay/claims/x.md` covers `hams_local_relay` ones). Each of `store_roots`
    is a centralized claims store (hams_com's `docs/bug_hunt_claims/<repo>/`, which holds the
    claims for hams_open and hams_shared code): its claims are matched by base name in any module,
    since the store's directory layout mirrors source paths rather than module names. Retired
    claims still count -- a retirement is a deliberate decision about that anchor."""
    keys = set()
    for claim_path in _git_tracked_files(repo_root, ".md"):
        if f"{os.sep}claims{os.sep}" not in claim_path or os.path.basename(claim_path) == "README.md":
            continue
        rel = os.path.relpath(claim_path, repo_root)
        if rel.split(os.sep)[:2] == ["docs", "bug_hunt_claims"]:
            continue
        keys |= _claim_keys(claim_path, _read_claim_fields(claim_path), va.get_module(claim_path))
    for store in store_roots:
        for dirpath, _dirs, files in os.walk(store):
            if os.path.basename(dirpath) != "claims":
                continue
            for name in files:
                if not name.endswith(".md") or name == "README.md":
                    continue
                claim_path = os.path.join(dirpath, name)
                keys |= _claim_keys(claim_path, _read_claim_fields(claim_path), None)
    return keys


# [@ANCHOR: check_claims_freshness:COMM_unclaimed_anchors]
def find_unclaimed_anchors(repo_root, store_roots=()):
    """Returns `[(module, anchor_name, "rel/path:line")]`, sorted, for every implementation anchor
    under `repo_root` that no claim file covers (see `build_claim_index`).

    The freshness check above only ever looks at claims that exist: an anchored function nobody
    wrote a claim for is invisible to it. Two real night-shift to-dos (three CSP anchors, four
    ham_init helpers) were exactly that gap, found by hand. This is the inventory side: it never
    fails a build, it lists.

    Known over-report: a "cluster" claim that documents several functions in its prose but names
    only one in `anchor:` (e.g. hams_local_relay's `cw_rate_estimator_accessors_cluster.md`)
    covers just that one anchor here -- there is no machine-readable field for the others.
    Directories `verify_anchors.py` itself skips (`tools/`, `scripts/`, `docs/`) are skipped too."""
    claimed = build_claim_index(repo_root, store_roots)
    unclaimed = []
    for full_path in _git_tracked_files(repo_root, ""):
        if not full_path.endswith(UNCLAIMED_SCAN_SUFFIXES) or os.path.islink(full_path):
            continue
        rel = os.path.relpath(full_path, repo_root)
        if _is_test_file(rel) or _anchor_scan_excluded(rel):
            continue
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for module, name, line_num in implementation_anchors_in_file(full_path, content, repo_root):
            base = va._clean(name)
            if (module, base) in claimed or (None, base) in claimed:
                continue
            unclaimed.append((module, name, f"{rel}:{line_num}"))
    return sorted(unclaimed)


def _print_unclaimed(unclaimed):
    by_module = {}
    for module, name, loc in unclaimed:
        by_module.setdefault(module, []).append((name, loc))
    for module in sorted(by_module):
        print(f"[*] {module}: {len(by_module[module])} unclaimed anchor(s)")
        for name, loc in by_module[module]:
            print(f"    - {module}:{name}  ({loc})")
    print(
        f"[*] TOTAL: {len(unclaimed)} implementation anchor(s) in {len(by_module)} module(s) have "
        "no claim file (informational, not a failure)."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument(
        "--unclaimed",
        action="store_true",
        help="instead of checking existing claims, list every implementation anchor that has no "
        "claim file under its module's claims/ directory (read-only, always exits 0)",
    )
    parser.add_argument(
        "--claims-store",
        action="append",
        default=[],
        metavar="DIR",
        help="with --unclaimed: a centralized claims store whose claims also count, matched by "
        "anchor name (e.g. hams_com/docs/bug_hunt_claims/hams_open for hams_open code); repeatable",
    )
    args = parser.parse_args()

    repo_root = os.path.abspath(args.directory)
    # A path that isn't a directory walks nothing and would otherwise print SUCCESS: a run from
    # the wrong working directory with a module name that didn't resolve would look like the
    # stale claims had been fixed.
    if not os.path.isdir(repo_root):
        print(f"[!] ERROR: {args.directory!r} is not a directory (resolved to {repo_root}).")
        return 2
    if args.unclaimed:
        for store in args.claims_store:
            if not os.path.isdir(store):
                print(f"[!] ERROR: --claims-store {store!r} is not a directory.")
                return 2
        try:
            unclaimed = find_unclaimed_anchors(repo_root, [os.path.abspath(d) for d in args.claims_store])
        except GitListingError as error:
            print(f"[!] ERROR: {error}.")
            return 2
        _print_unclaimed(unclaimed)
        return 0
    try:
        problems, retired = scan_claims(repo_root)
    except GitListingError as error:
        print(f"[!] ERROR: {error}. Nothing was checked, so this is not a pass.")
        return 2

    if retired:
        print(
            f"[*] {len(retired)} retired claim(s) skipped (see COMM_claim_retirement) -- kept as "
            "historical record, not checked against current code:"
        )
        for claim_path in retired:
            print(f"    - {os.path.relpath(claim_path, repo_root)}")

    if problems:
        print("[!] CI/CD FAILURE: Stale or Orphaned Function Claims Detected (ADR 0091):")
        for claim_path, problem in problems:
            rel = os.path.relpath(claim_path, repo_root)
            print(f"    - {rel}: {problem}")
        return 1

    print("[+] SUCCESS: All function claims match their anchored code's current hash.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
