"""Advisory lint tier (Bruce, 2026-11-23): documentation-format and claim-freshness checks are reported but never block.

An advisory step still runs and its failure is printed as `ADVISORY FAIL: <step> ...`, listed in the round's SUMMARY.md and recorded in the
`--results-json` document, but it does not fail run_linters, a merge_pr check or the round (GO stays GO). Real anchor breakage (verify_anchors),
syntax/type/test/security checks, flake8, mypy and fmt are NOT advisory. The round manager dispatches a batch fix for what the report lists.
"""

# [@ANCHOR: lint_advisory_steps]
# Verified by [@ANCHOR: test_advisory_failure_does_not_block_but_blocking_failure_does]
ADVISORY_STEPS = frozenset({
    "check_claims_freshness",  # ADR 0091: stale or orphaned function claims, claim hash mismatch
    "check_anchor_docs_standard",  # ADR 0113: word minimums and wording/format of anchor documentation
})

MARK = "ADVISORY FAIL"


def is_advisory(name):
    return name in ADVISORY_STEPS


# [@ANCHOR: lint_advisory_split]
# Verified by [@ANCHOR: test_advisory_failure_does_not_block_but_blocking_failure_does]
def split_failures(results):
    """(blocking failed results, advisory failed results) of step results that have `.name` and `.returncode`."""
    blocking, advisory = [], []
    for result in results:
        if result.returncode != 0:
            (advisory if is_advisory(result.name) else blocking).append(result)
    return blocking, advisory


def report_lines(advisory):
    """One `ADVISORY FAIL: <step>: <first output line>` line per advisory failure (does not block)."""
    lines = []
    for result in advisory:
        first = next((l.strip() for l in (result.output or "").splitlines() if l.strip()), "")
        lines.append("%s: %s: %s" % (MARK, result.name, first[:200]))
    return lines
