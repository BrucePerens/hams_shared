#!/usr/bin/env python3
"""
Scans the codebase for Python string literals containing raw XML/HTML tags
or suspicious empty strings that may indicate tags stripped by LLM UIs.
"""
import os
import re
import ast
import logging

logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
_logger = logging.getLogger(__name__)

def scan_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        tree = ast.parse(content, filename=filepath)
    except Exception as e:  # audit-ignore-catch-all
        _logger.warning("Failed to parse %s: %s", filepath, e)
        return False

    vulnerabilities = []

    class StringScanner(ast.NodeVisitor):
        def check_string(self, s, lineno):
            # 1. Look for raw un-escaped HTML/XML tags (e.g., <record>, <group>)
            # We ignore < and > with spaces (e.g. math operators like x < y)
            if re.search(r"<[a-zA-Z/][^>]*>", s):
                vulnerabilities.append((lineno, "RAW XML TAG (Vulnerable to UI Stripping)", s.strip()))

            # 2. Look for suspiciously stripped UI tour anchors or empty strings
            if re.search(r"using\s+['\"]['\"]\s+if", s) or re.search(r"\(\s*\)\s+and\s+templates", s):
                vulnerabilities.append((lineno, "SUSPICIOUS EMPTY STRING (Likely stripped comment/tag)", s.strip()))

        def visit_Constant(self, node):
            if isinstance(node.value, str):
                self.check_string(node.value, node.lineno)
            self.generic_visit(node)

        def visit_JoinedStr(self, node):
            parts = []
            for val in node.values:
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    parts.append(val.value)
                elif isinstance(val, ast.FormattedValue):
                    parts.append("{...}")
            full_str = "".join(parts)
            self.check_string(full_str, node.lineno)
            self.generic_visit(node)

    StringScanner().visit(tree)

    if vulnerabilities:
        print(f"\n[!] UI Stripping Vulnerabilities detected in {filepath}:")
        for lineno, vtype, text in vulnerabilities:
            # Truncate for display
            snippet = text.replace('\n', ' ')
            snippet = snippet[:120] + "..." if len(snippet) > 120 else snippet
            print(f"  Line {lineno} | {vtype}: {snippet}")
        return True
    return False

def main():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    print(f"[*] Scanning codebase at {base_dir} for LLM UI stripping vulnerabilities...")

    scanned_count = 0
    vulnerable_files = 0

    for root, dirs, files in os.walk(base_dir):
        # Ignore virtual environments, node modules, and caches
        dirs[:] = [d for d in dirs if d not in ("venv", "node_modules", "__pycache__", ".git")]
        for file in files:
            if file.endswith(".py"):
                filepath = os.path.join(root, file)
                if scan_file(filepath):
                    vulnerable_files += 1
                scanned_count += 1

    print(f"\n[*] Scan complete. Checked {scanned_count} Python files.")
    if vulnerable_files > 0:
        print(f"[!] Found {vulnerable_files} file(s) requiring hex-escape immunization (\\x3c / \\x3e).")
    else:
        print("[*] No vulnerabilities found. Python strings are immunized.")

if __name__ == "__main__":
    main()
