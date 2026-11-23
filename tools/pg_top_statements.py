#!/usr/bin/env python3
"""Top statements by total time from pg_stat_statements, with the tables each one names.

Run on the database host by the coordinator (read-only):
    python3 tools/pg_top_statements.py [--db hams_prod] [--limit 20] [--table ham_callbook]
It runs one SELECT as the postgres OS user and prints calls, total and mean milliseconds, rows and the normalised query
text ($1 placeholders, so no personal data; utility statements may carry literals and are shown as "<utility>").
Reset the counters only with `sudo -u postgres psql -d hams_prod -c 'SELECT pg_stat_statements_reset()'`.
"""

import argparse
import re
import subprocess
import sys

_QUERY = """
SELECT calls, round(total_exec_time::numeric, 1), round(mean_exec_time::numeric, 3), rows,
       replace(replace(query, E'\\n', ' '), E'\\t', ' ')
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY total_exec_time DESC
LIMIT {limit};
"""
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+\"?([a-z_][a-z0-9_]*)\"?", re.IGNORECASE)
_UTILITY_RE = re.compile(r"^\s*(CREATE|ALTER|DROP|SET|COPY|BEGIN|COMMIT|ROLLBACK|SHOW|VACUUM|ANALYZE|GRANT)\b", re.IGNORECASE)


def tables_in(query):
    """Table names a normalised statement reads or writes, in order of first appearance."""
    seen = []
    for name in _TABLE_RE.findall(query):
        if name not in seen:
            seen.append(name)
    return seen


def parse_rows(output):
    """Rows of the psql -tA output (fields split on |) as dicts; the query text is everything after the fourth |."""
    rows = []
    for line in output.splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        calls, total, mean, nrows, query = parts
        text = "<utility>" if _UTILITY_RE.match(query) else query.strip()
        rows.append({"calls": calls, "total_ms": total, "mean_ms": mean, "rows": nrows, "query": text, "tables": tables_in(query)})
    return rows


def main(argv=None, run=subprocess.run):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="hams_prod")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--table", help="keep only statements that name this table")
    args = ap.parse_args(argv)
    res = run(
        ["sudo", "-n", "-u", "postgres", "psql", "-d", args.db, "-tA", "-c", _QUERY.format(limit=int(args.limit))],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(res.stderr.strip() or "psql failed (is pg_stat_statements created and preloaded?)", file=sys.stderr)
        return 1
    for row in parse_rows(res.stdout):
        if args.table and args.table not in row["tables"]:
            continue
        print(f"{row['calls']} calls  {row['total_ms']} ms total  {row['mean_ms']} ms mean  {row['rows']} rows  [{','.join(row['tables'])}]")
        print(f"    {row['query'][:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
