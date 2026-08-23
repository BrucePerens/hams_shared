#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Odoo Manifest Dependency Cycle Detector
----------------------------------------
Odoo's module loader cannot install a manifest dependency graph that
contains a cycle -- it only fails at install time, as a crash, once
someone actually tries to install the affected modules together. This
walks every __manifest__.py's 'depends' list (across both hams_com and
hams_open, since real deployments install modules from both repos into
the same registry) and reports any cycle statically, before it ever
reaches an install.

If a genuine cycle is unavoidable (two modules that legitimately need
each other's functionality), do NOT declare a hard 'depends' entry for
the side that would close the loop. Instead:
  1. List the other module in a 'depends_cycle' key in __manifest__.py
     (a plain list; Odoo itself ignores unknown manifest keys, so this
     is purely documentation plus a contract this tooling reads).
  2. At the actual point of use, call
     zero_sudo.security.utils._resolve_dependency_cycle(dependency_module)
     -- it verifies the calling module actually declared the dependency
     in 'depends_cycle' (raising UserError immediately if it didn't, or
     if the calling module can't be identified at all -- an unverifiable
     declaration is a bug, not a "dependency missing" case), then checks
     whether the dependency is really installed via the registry (no DB
     query, no ACL needed), and either returns a bool for callers that
     can degrade gracefully or raises UserError for callers that pass
     required=True and would rather fail loudly on a missing dependency
     too.
See ham_onboarding/__manifest__.py's 'depends_cycle' entry for
ham_testing (get_auth_signup_qcontext's CAPTCHA integration) for a
worked example of this pattern end to end.

This script also validates every 'depends_cycle' entry is honest: it
must not duplicate a real 'depends' entry (redundant), and hard-depending
on it instead must actually close a cycle (otherwise there was no reason
to avoid a real 'depends' entry, and 'depends_cycle' is being used as an
undocumented escape hatch instead of what it's for).
"""

import ast
import os
import sys


def _find_sibling_repo(repo_root):
    """Mirrors run_linters.py's sibling-repo resolution so the graph this
    script builds is complete regardless of which repo it's invoked from."""
    repo_root = os.path.abspath(repo_root)
    for sibling_name in ("hams_open", "hams_com"):
        if os.path.basename(repo_root) == sibling_name:
            continue
        candidate = os.path.abspath(os.path.join(repo_root, "..", sibling_name))
        if not os.path.isdir(candidate):
            continue
        has_a_module = any(
            os.path.isfile(os.path.join(candidate, d, "__manifest__.py"))
            for d in os.listdir(candidate)
            if os.path.isdir(os.path.join(candidate, d))
        )
        if has_a_module:
            return candidate
    return None


def _build_graph(roots):
    graph = {}
    depends_cycle = {}
    for repo_root in roots:
        for root, dirs, files in os.walk(repo_root):
            if "radae" in dirs:
                dirs.remove("radae")
            dirs[:] = [
                d
                for d in dirs
                if d not in ("node_modules", "__pycache__", ".git", "daemons", "tools")
            ]
            if "__manifest__.py" not in files:
                continue
            mod = os.path.basename(root)
            path = os.path.join(root, "__manifest__.py")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=path)
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == "depends":
                            graph[mod] = [
                                e.value for e in value.elts if isinstance(e, ast.Constant)
                            ]
                        elif isinstance(key, ast.Constant) and key.value == "depends_cycle":
                            depends_cycle[mod] = [
                                e.value for e in value.elts if isinstance(e, ast.Constant)
                            ]
                    if mod in graph:
                        break
    return graph, depends_cycle


def _reachable(graph, start, target):
    """Is `target` reachable from `start` by following hard 'depends' edges?"""
    seen = set()
    stack = list(graph.get(start, []))
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))
    return False


def _find_cycles(graph):
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {m: WHITE for m in graph}
    cycles = []

    def dfs(node, stack):
        color[node] = GRAY
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue  # core Odoo module or optional external dep, not in our graph
            if color.get(dep, WHITE) == GRAY:
                cycles.append(stack[stack.index(dep):] + [dep])
            elif color.get(dep, WHITE) == WHITE:
                dfs(dep, stack)
        stack.pop()
        color[node] = BLACK

    for m in list(graph):
        if color[m] == WHITE:
            dfs(m, [])
    return cycles


def _check_depends_cycle_entries(graph, depends_cycle):
    errors = []
    for mod, entries in depends_cycle.items():
        for dep in entries:
            if dep in graph.get(mod, []):
                errors.append(
                    f"❌ ERROR: REDUNDANT depends_cycle: {mod}'s 'depends_cycle' lists "
                    f"'{dep}', which is ALSO in its real 'depends' -- remove it from "
                    "depends_cycle, the hard dependency already covers it."
                )
                continue
            if dep not in graph:
                errors.append(
                    f"❌ ERROR: UNKNOWN depends_cycle target: {mod}'s 'depends_cycle' "
                    f"lists '{dep}', which is not a module in this dependency graph "
                    "(typo, or the module doesn't exist)."
                )
                continue
            # A depends_cycle entry is only justified if hard-depending on it
            # would actually close a cycle -- i.e. the target can already
            # reach mod via real 'depends' edges.
            if not _reachable(graph, dep, mod):
                errors.append(
                    f"❌ ERROR: UNJUSTIFIED depends_cycle: {mod} lists '{dep}' in "
                    "'depends_cycle', but adding it as a real 'depends' entry would "
                    "NOT create a cycle -- use a real 'depends' entry instead. "
                    "depends_cycle is only for cases Odoo genuinely cannot install."
                )
    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: check_dependency_cycles.py <repo_root>")
        sys.exit(1)

    repo_root = os.path.abspath(sys.argv[1])
    roots = [repo_root]
    sibling = _find_sibling_repo(repo_root)
    if sibling:
        roots.append(sibling)

    graph, depends_cycle = _build_graph(roots)
    cycles = _find_cycles(graph)
    entry_errors = _check_depends_cycle_entries(graph, depends_cycle)

    total_errors = 0

    if cycles:
        seen = set()
        for cycle in cycles:
            key = tuple(sorted(cycle))
            if key in seen:
                continue
            seen.add(key)
            print("❌ ERROR: CIRCULAR MANIFEST DEPENDENCY: " + " -> ".join(cycle))
            print(
                "  Odoo cannot install this graph. If both directions are "
                "genuinely needed, remove one side's hard 'depends' entry "
                "and use a 'depends_cycle' manifest key instead, verified "
                "at runtime via zero_sudo.security.utils._resolve_dependency_cycle() "
                "-- see this script's module docstring."
            )
        total_errors += len(seen)

    for err in entry_errors:
        print(err)
    total_errors += len(entry_errors)

    if total_errors:
        print(f"Total Errors (Dependency Cycles): {total_errors}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
