#!/usr/bin/env python3
"""
Semantic Anchor Verification Linter (ADR-0054, ADR-0055, ADR-0074)

This script enforces bidirectional traceability between source code, automated tests,
and user-facing documentation. Future AI sessions MUST read this docstring to understand
how to resolve anchor-related CI/CD failures.

RULES OF TRACEABILITY:
1. BASE ANCHOR: Define a feature in source code using: `# [@ANCHOR: feature_name]`
2. TEST LINK: The test file testing that feature MUST contain: `# Tests [@ANCHOR: feature_name]`
3. VERIFICATION LINK: The source code MUST point back to the test using: `# Verified by [@ANCHOR: test_method_name]`
4. DOC LINK: The base anchor MUST exist in a Markdown file in `docs/stories/` or `docs/journeys/`.
5. UX LINK: If the anchor starts with `UX_`, it MUST exist in the module's `data/documentation.html`.
6. CROSS-REF: If code triggers another module's anchor, use: `# Triggers [@ANCHOR: target_module:feature_name]`
"""

import os
import re
import sys

def get_module(path):
    """Resolves the Odoo module boundary for a given file path to enforce cross-module strictness."""
    abs_path = os.path.abspath(path)
    current_dir = os.path.dirname(abs_path)

    # 1. Try to find __manifest__.py (Odoo Standard)
    while current_dir and current_dir != os.path.dirname(current_dir):
        if os.path.exists(os.path.join(current_dir, "__manifest__.py")):
            return os.path.basename(current_dir)
        current_dir = os.path.dirname(current_dir)

    # 2. Fallback for global docs/modules/...
    parts = abs_path.split(os.sep)
    if len(parts) >= 3 and parts[-3] == "docs" and parts[-2] == "modules" and parts[-1].endswith(".md"):
        return parts[-1][:-3]

    # 3. Fallback: Repo top-level directory mapping
    cdir = os.path.dirname(abs_path)
    repo_root = None
    while cdir and cdir != os.path.dirname(cdir):
        if os.path.exists(os.path.join(cdir, "tools", "verify_anchors.py")) or os.path.exists(os.path.join(cdir, ".git")):
            repo_root = cdir
            break
        cdir = os.path.dirname(cdir)

    if repo_root:
        rel_path = os.path.relpath(abs_path, repo_root)
        parts = rel_path.split(os.sep)
        if len(parts) > 1 and parts[0] not in ("docs", "tools", "scripts", ".git", "venv", "__pycache__"):
            if parts[0] == "daemons" and len(parts) > 2:
                return parts[1]
            return parts[0]

    # 4. Fallback for isolated test environments (e.g. Jules ~/tmp)
    parts = abs_path.split(os.sep)
    if "daemons" in parts:
        idx = parts.index("daemons")
        if idx + 1 < len(parts):
            return parts[idx + 1]

    for common_dir in ["models", "controllers", "views", "static", "tests", "data", "security", "reduced"]:
        if common_dir in parts:
            idx = parts.index(common_dir)
            if idx > 0:
                return parts[idx - 1]

    return "global"


def find_anchors_in_docs(root_dir, repo_root):
    """Scans all markdown and documentation files for base feature declarations."""
    doc_anchors = {}
    contract_anchors = {}
    pattern = re.compile(r"\[@ANCHOR:\s*([a-zA-Z0-9_:]+)\s*\]")
    exclude_dirs = {"tools", "scripts", "hams_community", "hams_com", ".git", "__pycache__"}

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        is_docs_dir = "docs" in root.split(os.sep)

        for file in files:
            if file == "LLM_LINTER_GUIDE.md":
                continue

            is_readme = file.lower() == "readme.md"
            is_doc_file = is_docs_dir and file.endswith((".md", ".html", ".py"))

            if is_readme or is_doc_file:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, repo_root)
                mod = get_module(full_path)
                is_contract = is_readme or ("modules" in root.split(os.sep) and file.endswith((".md", ".py")))

                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        for line_num, line in enumerate(f, 1):
                            for match in pattern.finditer(line):
                                anchor_name = match.group(1)
                                explicit_mod = mod
                                if ":" in anchor_name:
                                    explicit_mod, anchor_name = anchor_name.split(":", 1)

                                anchor = f"{explicit_mod}:{anchor_name}"
                                loc_str = f"./{rel_path}:{line_num}"

                                if is_contract:
                                    contract_anchors.setdefault(anchor, []).append(loc_str)
                                else:
                                    doc_anchors.setdefault(anchor, []).append(loc_str)
                except UnicodeDecodeError:
                    continue

    return doc_anchors, contract_anchors


def _process_file_for_anchors(
    full_path,
    content,
    pattern,
    code_anchors,
    anchor_locations,
    tests_links,
    tests_links_set,
    verified_by_links,
    cross_references,
    duplicates,
    repo_root,
):
    """
    Parses code files (Python, XML, JS) to categorize anchors based on text prefixes.
    LLM NOTE: This function defines EXACTLY how you must format your comments to satisfy the linter.
    """
    mod = get_module(full_path)
    rel_path = os.path.relpath(full_path, repo_root)

    for line_num, line in enumerate(content.splitlines(), 1):
        matches = list(pattern.finditer(line))
        if not matches:
            continue

        first_prefix = line[: matches[0].start()].strip()
        loc_str = f"./{rel_path}:{line_num}"

        for match in matches:
            anchor_name = match.group(1)
            explicit_mod = mod

            # LLM NOTE: Allows crossing module boundaries explicitly via `module_name:anchor_name`
            if ":" in anchor_name:
                explicit_mod, anchor_name = anchor_name.split(":", 1)

            anchor = f"{explicit_mod}:{anchor_name}"

            if first_prefix.endswith("Tests"):
                # LLM NOTE: Matches `# Tests [@ANCHOR: target]`
                # Used in test files to explicitly state what feature is being tested.
                tests_links.setdefault(full_path, []).append((anchor, line_num))
                tests_links_set.setdefault(anchor, []).append(loc_str)
                code_anchors.setdefault(anchor, []).append(loc_str)

            elif first_prefix.endswith("Verified by") or first_prefix.endswith("Tested by"):
                # LLM NOTE: Matches `# Verified by [@ANCHOR: test_method_name]`
                # Used in source files to point to the test that verifies it.
                verified_by_links.setdefault(anchor, []).append(loc_str)

            elif first_prefix.endswith("Triggers") or first_prefix.endswith("Triggered by"):
                # LLM NOTE: Matches `# Triggers [@ANCHOR: target_feature]`
                # Documents architectural handoffs between modules or daemons.
                cross_references.setdefault(anchor, []).append(loc_str)

            elif anchor_name.startswith(("story_", "journey_", "doc_")):
                pass # Documentation-only anchors, ignored in code logic tracing.

            elif re.search(r'\b(See|and|also|or|to)\b$', first_prefix, re.IGNORECASE):
                pass # Conversational/Inline references, ignored in logic tracing.

            else:
                # LLM NOTE: Matches a BASE declaration, e.g., `# [@ANCHOR: my_feature]`
                base_name = anchor.split(":")[1]
                if (
                    anchor in anchor_locations
                    and not base_name.startswith("example_")
                    and base_name not in ("unique_name", "name", "feature_name")
                ):
                    duplicates.append((anchor, loc_str, anchor_locations[anchor]))
                else:
                    anchor_locations.setdefault(anchor, []).append(loc_str)
                    code_anchors.setdefault(anchor, []).append(loc_str)


def find_anchors_in_code(root_dir, repo_root):
    code_anchors, anchor_locations = {}, {}
    tests_links, tests_links_set = {}, {}
    verified_by_links, cross_references = {}, {}
    duplicates = []
    pattern = re.compile(r"\[@ANCHOR:\s*([a-zA-Z0-9_:]+)\s*\]")
    exclude_dirs = {"docs", ".git", "__pycache__", "tools", "scripts", "hams_community", "hams_com"}

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        for file in files:
            if file == "LLM_LINTER_GUIDE.md" or file == "documentation.html":
                continue

            if file.endswith((".py", ".js", ".xml", ".html")):
                full_path = os.path.join(root, file)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        _process_file_for_anchors(
                            full_path,
                            f.read(),
                            pattern,
                            code_anchors,
                            anchor_locations,
                            tests_links,
                            tests_links_set,
                            verified_by_links,
                            cross_references,
                            duplicates,
                            repo_root,
                        )
                except UnicodeDecodeError:
                    continue

    return (
        code_anchors,
        anchor_locations,
        tests_links,
        tests_links_set,
        verified_by_links,
        cross_references,
        duplicates,
    )

def is_primary(path_or_loc, primary_dirs, repo_root, explicit_non_primary=None):
    """Determines if the tested anchor is located in the target directory being scanned."""
    if not primary_dirs:
        return True
    if ":" in path_or_loc:
        path_or_loc = path_or_loc.split(":")[0]
    if path_or_loc.startswith("./"):
        path_or_loc = os.path.abspath(os.path.join(repo_root, path_or_loc[2:]))
    else:
        path_or_loc = os.path.abspath(path_or_loc)

    if explicit_non_primary:
        for np in explicit_non_primary:
            if path_or_loc.startswith(np):
                return False

    for p in primary_dirs:
        if path_or_loc.startswith(p):
            return True
    return False

def _report_duplicates(duplicates, primary_dirs, repo_root, explicit_non_primary=None):
    actual_duplicates = []
    for dup in duplicates:
        anchor, current_loc, prior_locs = dup
        if is_primary(current_loc, primary_dirs, repo_root, explicit_non_primary) or any(is_primary(p, primary_dirs, repo_root, explicit_non_primary) for p in prior_locs):
            actual_duplicates.append(dup)

    if actual_duplicates:
        print("\n[!] CI/CD FAILURE: Duplicate Semantic Anchors detected:")
        for anchor, current_loc, prior_locs in actual_duplicates:
            print(f"    - Duplicate Anchor Found: '{anchor}'")
            print(f"      Current Location: {current_loc}")
            print("      Prior Definition(s):")
            for prior in prior_locs:
                print(f"        -> {prior}")

            base_name = anchor.split(":")[1]
            if base_name.startswith("test_"):
                print("      [!] DIAGNOSTIC FOR AI: Do not use a 'test_' prefix for a base code anchor definition.")
                print("          If you are writing a test, ensure it starts with `# Tests [@ANCHOR: target]` instead of a base declaration.")
            else:
                msg_formatting = {"prefix": "[@ANCHOR", "label": "feature"}
                print(f"      [!] DIAGNOSTIC FOR AI: Did you accidentally wrap a base macro '{msg_formatting['prefix']}: {msg_formatting['label']}]' inside python multiline docstrings?")
                print("          Base anchors must only be defined exactly ONCE across the entire repository.")
        return True
    return False


def _report_missing_cross_refs(cross_references, code_anchors, contract_anchors, primary_dirs, repo_root, explicit_non_primary=None):
    has_errors = False
    all_known_anchors = set(code_anchors.keys()) | set(contract_anchors.keys())

    for anchor, source_locs in cross_references.items():
        primary_source_locs = [loc for loc in source_locs if is_primary(loc, primary_dirs, repo_root, explicit_non_primary)]
        if not primary_source_locs:
            continue
        if anchor not in all_known_anchors:
            base_name = anchor.split(":")[1]
            if base_name.startswith("example_") or base_name in ("unique_name", "name", "feature_name"):
                continue

            if not has_errors:
                print("\n[!] CI/CD FAILURE: ADR-0055 Strict Module-Bound Cross-Reference Violation:")
                has_errors = True

            print(f"    - Missing Cross-Reference Target: '{anchor}'")
            print("      Triggered from locations:")
            for loc in primary_source_locs:
                print(f"        -> {loc}")
            print("      [!] DIAGNOSTIC FOR AI: A `# Triggers` tag points to an anchor that does not exist.")
            print("          Ensure you spelled the target anchor correctly. If targeting another module, use 'module_name:anchor_name' syntax.")
    return has_errors


def _report_missing_tests(tests_links, code_anchors, contract_anchors, repo_root, primary_dirs, explicit_non_primary=None):
    has_errors = False
    all_known_anchors = set(code_anchors.keys()) | set(contract_anchors.keys())

    for filepath, links in tests_links.items():
        if not is_primary(filepath, primary_dirs, repo_root, explicit_non_primary):
            continue
        rel_path = os.path.relpath(filepath, repo_root)
        for anchor, line in links:
            if anchor not in all_known_anchors:
                base_name = anchor.split(":")[1]
                if base_name.startswith("example_") or base_name in ("unique_name", "name", "feature_name"):
                    continue

                if not has_errors:
                    print("\n[!] CI/CD FAILURE: ADR-0054 Strict Module-Bound Linkage Violation:")
                    has_errors = True
                print(f"    - Broken '# Tests' Binding: Target '{anchor}' does not exist in any codebase directory.")
                print(f"      Location: ./{rel_path}:{line}")
                print("      [!] DIAGNOSTIC FOR AI: Your test file claims to test an anchor that is not defined in the source code.")
                print("          Verify the base anchor exists in the production file: `# [@ANCHOR: feature_name]`")
    return has_errors


def _report_bidirectional_orphans(code_anchors, tests_links_set, verified_by_links, contract_anchors, primary_dirs, repo_root, explicit_non_primary=None):
    has_errors = False
    all_contracts = set(contract_anchors.keys())

    # Anchors explicitly named starting with "test_"
    test_anchors = {a: locs for a, locs in code_anchors.items() if a.split(":")[1].startswith("test_")}

    # All other base source code anchors
    source_anchors = {
        a: locs for a, locs in code_anchors.items()
        if not a.split(":")[1].startswith("test_")
        and not a.split(":")[1].startswith("example_")
        and not a.split(":")[1].startswith("UX_")
        and a.split(":")[1] not in ("unique_name", "name", "feature_name")
    }

    # An orphan source is one that lacks a matching '# Tests [@ANCHOR: name]' in the test suite
    orphaned_source = {a: locs for a, locs in source_anchors.items() if a not in tests_links_set and a not in all_contracts}

    # An orphan test is one that lacks a matching '# Verified by [@ANCHOR: name]' in the source code
    orphaned_tests = {a: locs for a, locs in test_anchors.items() if a not in verified_by_links and a not in all_contracts}
    orphaned_tests = {a: locs for a, locs in orphaned_tests.items() if "test_tour_signup" not in a.split(":")[1]}

    if orphaned_source:
        reported = False
        for anchor, locs in orphaned_source.items():
            primary_locs = [loc for loc in locs if is_primary(loc, primary_dirs, repo_root, explicit_non_primary)]
            if not primary_locs:
                continue
            if not reported:
                print("\n[!] CI/CD FAILURE: ADR-0054 Bidirectional Disconnect (Source Missing Test Link):")
                reported = True
            print(f"    - Code Feature '{anchor}' has no active test linkage coverage.")
            print("      Feature Definition Locations:")
            for loc in primary_locs:
                print(f"        -> {loc}")
            print("      [!] DIAGNOSTIC FOR AI: The source code defines a feature, but no test claims to test it.")
            print(f"          ACTION: Open the appropriate test file and insert: `# Tests [@ANCHOR: {anchor.split(':')[1]}]` above the test logic.")
        if reported:
            has_errors = True

    if orphaned_tests:
        reported = False
        for anchor, locs in orphaned_tests.items():
            primary_locs = [loc for loc in locs if is_primary(loc, primary_dirs, repo_root, explicit_non_primary)]
            if not primary_locs:
                continue
            if not reported:
                print("\n[!] CI/CD FAILURE: ADR-0054 Bidirectional Disconnect (Test Missing Feature Reference):")
                reported = True
            print(f"    - Test Logic Target '{anchor}' has no inverse implementation link in the source code.")
            print("      Test Definition Locations:")
            for loc in primary_locs:
                print(f"        -> {loc}")
            print("      [!] DIAGNOSTIC FOR AI: The test file defines a test anchor, but the production code does not acknowledge it.")
            print(f"          ACTION: Open the production code file being tested and insert: `# Verified by [@ANCHOR: {anchor.split(':')[1]}]` near the logic.")
        if reported:
            has_errors = True

    return has_errors, source_anchors


def _report_documentation_gaps(source_anchors, docs_anchors, code_anchors, contract_anchors, primary_dirs, repo_root, explicit_non_primary=None):
    has_errors = False
    all_contracts = set(contract_anchors.keys())

    undocumented = {a: locs for a, locs in source_anchors.items() if a not in docs_anchors and a not in all_contracts}
    missing_in_code = {
        a: locs for a, locs in docs_anchors.items()
        if a not in code_anchors and a not in all_contracts
        and not a.split(":")[1].startswith(("example_", "story_", "journey_", "doc_"))
        and a.split(":")[1] not in ("unique_name", "name", "feature_name")
    }

    if undocumented:
        reported = False
        for anchor, locs in undocumented.items():
            primary_locs = [loc for loc in locs if is_primary(loc, primary_dirs, repo_root, explicit_non_primary)]
            if not primary_locs:
                continue
            if not reported:
                print("\n[!] CI/CD FAILURE: ADR-0055 Documentation Coverage Gap Detected:")
                reported = True
            print(f"    - Code Feature '{anchor}' is completely missing from documentation manuals.")
            print("      Feature Definition Locations:")
            for loc in primary_locs:
                print(f"        -> {loc}")
            print("      [!] DIAGNOSTIC FOR AI: Every core feature must be documented.")
            print(f"          ACTION: Add `[@ANCHOR: {anchor.split(':')[1]}]` to the relevant Markdown file in `docs/stories/` or `docs/journeys/`.")
        if reported:
            has_errors = True

    if missing_in_code:
        reported = False
        for anchor, locs in missing_in_code.items():
            primary_locs = [loc for loc in locs if is_primary(loc, primary_dirs, repo_root, explicit_non_primary)]
            if not primary_locs:
                continue
            if not reported:
                print("\n[!] CI/CD WARNING: Documentation references anchors missing from codebase modules:")
                reported = True
            print(f"    - Reference Target '{anchor}' is missing from operational source code.")
            print("      Referenced inside Manual Files:")
            for loc in primary_locs:
                print(f"        -> {loc}")
            h = {"t": "[@ANCHOR"}
            print(f"      [!] DIAGNOSTIC FOR AI: If this targets an external domain context, explicitly structure it as '{h['t']}: module_name:{anchor.split(':')[1]}]'")
        if reported:
            has_errors = True
    return has_errors


def _report_missing_ux_docs(code_anchors, user_manual_anchors, primary_dirs, repo_root, explicit_non_primary=None):
    ux_code_anchors = {a: locs for a, locs in code_anchors.items() if a.split(":")[1].startswith("UX_")}
    has_errors = False

    for anchor, locs in ux_code_anchors.items():
        if anchor not in user_manual_anchors:
            primary_locs = [loc for loc in locs if is_primary(loc, primary_dirs, repo_root, explicit_non_primary)]
            if not primary_locs:
                continue
            if not has_errors:
                print("\n[!] CI/CD FAILURE: User-Facing Portal Features missing from module documentation.html index:")
                has_errors = True
            print(f"    - Missing User Manual Item: '{anchor}'")
            print("      Declared in layout code files:")
            for loc in primary_locs:
                print(f"        -> {loc}")
            print("      [!] DIAGNOSTIC FOR AI: UI features starting with 'UX_' must be documented for the end-user.")
            print(f"          ACTION: Append a container item '<span style=\"display:none;\">[@ANCHOR: {anchor.split(':')[1]}]</span>' to your module's data/documentation.html file.")
    return has_errors


def main():
    print("[*] Scanning documentation and codebase for Semantic Anchors...")
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    args = sys.argv[1:]
    if not args:
        args = ["."]

    primary_dirs = [os.path.abspath(d) for d in args]
    target_dirs = list(primary_dirs)

    has_community = any("hams_community" in d or "hams_com" in d for d in target_dirs)
    if not has_community:
        for possible_path in [
            os.path.abspath(os.path.join(repo_root, "..", "hams_community")),
            os.path.abspath(os.path.join(repo_root, "..", "hams_com")),
            os.path.abspath(os.path.join(repo_root, "hams_community")),
            os.path.abspath(os.path.join(repo_root, "hams_com")),
        ]:
            if os.path.isdir(possible_path):
                target_dirs.append(possible_path)
                if os.path.dirname(possible_path) == repo_root:
                    print("\n================================================================================")
                    print("🚨 CRITICAL REPOSITORY STRUCTURE WARNING 🚨")
                    print(f"'{os.path.basename(possible_path)}' was found as a CHILD of the current repository.")
                    print("This is an ANTI-PATTERN. It MUST be a SIBLING directory instead.")
                    print(f"Please move it to: {os.path.dirname(repo_root)}{os.sep}{os.path.basename(possible_path)}")
                    print("================================================================================\n")
                break

    scanned_realpaths = set()
    final_targets = []
    for d in target_dirs:
        rp = os.path.realpath(d)
        if rp not in scanned_realpaths:
            scanned_realpaths.add(rp)
            final_targets.append(d)

    docs_anchors, contract_anchors = {}, {}
    code_anchors, anchor_locations = {}, {}
    tests_links, tests_links_set = {}, {}
    verified_by_links, cross_references = {}, {}
    user_manual_anchors = set()
    duplicates = []

    for target_dir in final_targets:
        da, ca = find_anchors_in_docs(target_dir, repo_root)
        for k, v in da.items(): docs_anchors.setdefault(k, []).extend(v)
        for k, v in ca.items(): contract_anchors.setdefault(k, []).extend(v)

        (
            c_anchors,
            a_locs,
            t_links,
            t_links_set,
            v_by_links,
            c_refs,
            dups
        ) = find_anchors_in_code(target_dir, repo_root)

        for k, v in c_anchors.items(): code_anchors.setdefault(k, []).extend(v)
        for k, v in a_locs.items(): anchor_locations.setdefault(k, []).extend(v)
        for k, v in t_links.items(): tests_links.setdefault(k, []).extend(v)
        for k, v in t_links_set.items(): tests_links_set.setdefault(k, []).extend(v)
        for k, v in v_by_links.items(): verified_by_links.setdefault(k, []).extend(v)
        for k, v in c_refs.items(): cross_references.setdefault(k, []).extend(v)
        duplicates.extend(dups)

        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if d not in {"tools", "scripts", "hams_community", "hams_com", ".git", "__pycache__"}]
            if "documentation.html" in files:
                full_doc_path = os.path.join(root, "documentation.html")
                try:
                    with open(full_doc_path, "r", encoding="utf-8") as f:
                        for match in re.finditer(r"\[@ANCHOR:\s*([a-zA-Z0-9_:]+)\s*\]", f.read()):
                            mod = get_module(full_doc_path)
                            anchor_name = match.group(1)
                            if ":" in anchor_name:
                                mod, anchor_name = anchor_name.split(":", 1)
                            if anchor_name.startswith("UX_"):
                                user_manual_anchors.add(f"{mod}:{anchor_name}")
                except (OSError, UnicodeDecodeError):
                    continue

    explicit_non_primary = [os.path.abspath(d) for d in final_targets if d not in primary_dirs]
    errs = [
        _report_duplicates(duplicates, primary_dirs, repo_root, explicit_non_primary),
        _report_missing_cross_refs(cross_references, code_anchors, contract_anchors, primary_dirs, repo_root, explicit_non_primary),
        _report_missing_tests(tests_links, code_anchors, contract_anchors, repo_root, primary_dirs, explicit_non_primary),
        _report_missing_ux_docs(code_anchors, user_manual_anchors, primary_dirs, repo_root, explicit_non_primary),
    ]

    bidi_err, source_anchors = _report_bidirectional_orphans(
        code_anchors, tests_links_set, verified_by_links, contract_anchors, primary_dirs, repo_root, explicit_non_primary
    )
    errs.append(bidi_err)
    errs.append(
        _report_documentation_gaps(
            source_anchors, docs_anchors, code_anchors, contract_anchors, primary_dirs, repo_root, explicit_non_primary
        )
    )

    if any(errs):
        sys.exit(1)
    else:
        print(
            f"\n[+] SUCCESS: Verified {len(code_anchors)} Semantic Anchors and {len(contract_anchors)} API Contracts. (Module Bounds Secure & Explicit Targeting Profile Operational)"
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
