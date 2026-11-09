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

Python only, for now, matching every other layer of this anchor system's own incremental rollout
(`check_function_test_anchors.py` shipped Python-only under ADR 0090 before its own JS and Rust
sub-tracks followed as separate, later work) -- reuses `check_function_test_anchors.py`'s own
`_function_span` so this check's notion of "the function's own text" never drifts from the
existing ratchet's, the same reasoning that script's own docstring gives for reusing it from
`check_anchor_coverage.py`. JS and Rust claims support are real, named, not-yet-done follow-ons.

No baseline/ratchet is needed the way `check_function_test_anchors.py` needs one: this is a brand
new mechanism with zero pre-existing claims when it ships, so there is no backlog to grandfather --
every claim ever committed should have an accurate hash from the moment it's added.
"""

import argparse
import ast
import hashlib
import io
import os
import re
import subprocess
import sys
import tokenize

import verify_anchors as va  # noqa: E402
from check_function_test_anchors import (  # noqa: E402
    _direct_functions,
    _function_span,
    _is_base_anchor_declaration,
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)
FIELD_RE = re.compile(r"^([a-zA-Z_]+):\s*(.+?)\s*$", re.MULTILINE)

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
        fields[fm.group(1)] = fm.group(2).strip().strip('"').strip("'")
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


def function_hashes_from_text(content, filename="<string>"):
    """Returns `{anchor_name: (normalized_hash, legacy_hash)}` for every function in `content`
    carrying a real BASE anchor declaration (not a Tests/Verified-by/Triggers link to one) within
    its own span. `normalized_hash` is the current `code_hash` definition (comments and
    docstrings ignored, see `normalized_span_text`); `legacy_hash` is the pre-2026-11-09
    definition, the sha256 of the raw span text, kept only so claims written before the change
    can be recognised and migrated (see `migrate_claim_hashes.py`). A file that does not parse
    yields `{}`."""
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError:
        return {}

    lines = content.splitlines()
    comment_columns = None
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
        legacy = hashlib.sha256("\n".join(span_lines).encode("utf-8")).hexdigest()
        normalized = hashlib.sha256(
            normalized_span_text(
                start, end, lines, comment_columns, _docstring_line_ranges(node)
            ).encode("utf-8")
        ).hexdigest()
        for name in names:
            hashes[name] = (normalized, legacy)
    return hashes


def _read_python(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def compute_function_hash(filepath):
    """Returns {anchor_name: sha256_hex} for every function in `filepath` carrying a real BASE
    anchor declaration, hashed with the current comment- and docstring-insensitive definition."""
    content = _read_python(filepath)
    if content is None:
        return {}
    return {
        name: pair[0] for name, pair in function_hashes_from_text(content, filepath).items()
    }


def compute_legacy_function_hash(filepath):
    """Same as `compute_function_hash`, but with the legacy raw-text definition (see
    `function_hashes_from_text`)."""
    content = _read_python(filepath)
    if content is None:
        return {}
    return {
        name: pair[1] for name, pair in function_hashes_from_text(content, filepath).items()
    }


def build_anchor_hash_index(repo_root, legacy=False):
    """Returns {anchor_name: sha256_hex} across every git-tracked .py file in repo_root (the
    legacy raw-text hashes when `legacy` is true). A duplicate anchor name across files is a
    pre-existing verify_anchors.py-level problem, not this script's to resolve -- last one
    scanned wins, matching dict-update semantics elsewhere in this same tool family."""
    index = {}
    compute = compute_legacy_function_hash if legacy else compute_function_hash
    for filepath in _git_tracked_files(repo_root, ".py"):
        index.update(compute(filepath))
    return index


def recorded_hash_matches(recorded, current, legacy_current):
    """True when a claim's recorded `code_hash` value is fresh for an anchor whose current hashes
    are `current` (normalized) and `legacy_current` (raw text, may be None).

    A claim written before 2026-11-09 carries the legacy hash. It is accepted while the function's
    raw text is byte-for-byte what it was (so nothing in flight breaks the moment this lands), and
    `migrate_claim_hashes.py` rewrites it to the normalized form; once the migration has run, the
    legacy branch can be deleted. A legacy hash that no longer matches is stale like any other,
    including after a comment-only edit, which is what the migration's history search resolves."""
    expected = recorded[len("sha256:") :] if recorded.startswith("sha256:") else recorded
    return expected == current or (legacy_current is not None and expected == legacy_current)


def check_claims(repo_root):
    """Returns a list of (claim_path, problem) for every claims/*.md file whose own recorded
    hash doesn't match the anchor's current code, or whose anchor no longer exists at all.

    Thin wrapper over `scan_claims`, kept because it is this module's long-standing entry point.
    """
    return scan_claims(repo_root)[0]


def scan_claims(repo_root):
    """Returns `(problems, retired)`: the same (claim_path, problem) list `check_claims` returns,
    plus the paths of every claim deliberately retired (see COMM_claim_retirement). A retired
    claim is not hash-checked -- its anchor is gone on purpose -- but is reported as a non-failing
    count, so retirements stay visible instead of disappearing."""
    problems = []
    retired = []
    anchor_hashes = None  # computed lazily, only if a claim file actually exists
    legacy_hashes = None

    for claim_path in _git_tracked_files(repo_root, ".md"):
        if f"{os.sep}claims{os.sep}" not in claim_path:
            continue
        # A README.md inside a claims/ directory is a deliberate index/explanation file for the
        # directory itself (e.g. hardware/claims/README.md, which says the directory is out
        # of scope for claims), not a claim -- it carries no frontmatter because it isn't one.
        # Without this exclusion this was a real false positive ("missing required frontmatter")
        # against the real hams_com repo.
        if os.path.basename(claim_path) == "README.md":
            continue
        # The centralized claims store (docs/bug_hunt_claims/<repo>/...) documents code that
        # lives in a DIFFERENT git repo (hams_open/hams_shared claims, centralized into hams_com
        # per the store policy in agents/skills/bug-hunt/SKILL.md) -- this checker's whole
        # freshness model assumes the claim and the anchored code share one repo root
        # (`build_anchor_hash_index` only ever scans `repo_root`'s own git-tracked .py files), so
        # it can never find a centralized claim's real anchor and would otherwise always report
        # it "orphaned". That skill's own docs already name the correct tool for these:
        # `check_claims.py --source-root <other-repo>`. Without this exclusion, running this
        # checker against the real hams_com repo produced over 1100 false-positive "orphaned
        # claim" reports, the large majority of them centralized-store claims for
        # hams_open/hams_shared code.
        rel = os.path.relpath(claim_path, repo_root)
        if rel.split(os.sep)[:2] == ["docs", "bug_hunt_claims"]:
            continue
        # See EXCLUDED_TOP_LEVEL_DIRS's own comment: an exact top-level path-component match,
        # not `startswith`, so a hypothetically-named `ingest_foo/` module (or anything else that
        # merely starts with one of these names) is NOT swept in by accident -- only a claim
        # actually rooted under `ingest/`, `ics_forms/`, or `ics_training/` themselves.
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

        recorded = fields["code_hash"]
        if recorded.strip().lower().startswith("not computable"):
            # Established convention (already in active use across the real repo -- every
            # Rust-anchored claim in daemons/*/claims/, plus claims for genuinely un-anchored
            # Python functions) for a claim this checker structurally cannot verify yet: Python-
            # only anchor hashing (per this file's own docstring, "JS and Rust claims support are
            # real, named, not-yet-done follow-ons") has no way to resolve a Rust/JS anchor, and a
            # function with no real `[@ANCHOR:]` at all has nothing to hash regardless. Without
            # this check, such a claim always fell through to the "anchor not found" branch below
            # and was reported as "orphaned" -- a false positive, and a misleading one (implying
            # the anchor was deleted, when it was simply never Python). In the real hams_com repo, all 344
            # "orphaned claim" findings left once centralized-store claims are excluded used this
            # exact convention, none of them genuinely orphaned.
            continue

        if anchor_hashes is None:
            anchor_hashes = build_anchor_hash_index(repo_root)
            legacy_hashes = build_anchor_hash_index(repo_root, legacy=True)

        anchor = fields["anchor"]
        current = anchor_hashes.get(va._clean(anchor))
        if current is None:
            problems.append(
                (claim_path, f"anchor '{anchor}' not found in any git-tracked .py file -- orphaned claim")
            )
            continue

        expected = recorded[len("sha256:") :] if recorded.startswith("sha256:") else recorded
        if not recorded_hash_matches(recorded, current, legacy_hashes.get(va._clean(anchor))):
            problems.append(
                (
                    claim_path,
                    f"code_hash mismatch for anchor '{anchor}' -- the function's code changed since this "
                    f"claim was last confirmed (comment and docstring edits do not count; recorded sha256:{expected[:12]}..., current "
                    f"sha256:{current[:12]}...); review the claim's own prose against the current "
                    "code and update code_hash once confirmed accurate (or fix the code, if the "
                    "claim was right and the change broke it)",
                )
            )

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
