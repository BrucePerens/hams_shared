"""The tools that talk to GitHub or another host name a timeout, so a hung server stops the tool instead of hanging it
(timeout audit 2026-11-12: a hung git fetch would hang the half-hourly worktree hygiene unit and the lint batches)."""
import ast
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NETWORK_GIT = {"fetch", "clone", "ls-remote", "push", "pull"}
# Helpers that put a timeout on every command they run themselves.
BOUNDED_HELPERS = {"_git"}


def literal_strings(node):
    return [e.value for e in getattr(node, "elts", []) if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def findings(path):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {keyword.arg for keyword in node.keywords}
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        owner = function.value.id if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name) else ""
        if owner == "requests" and name in {"get", "post", "put", "delete", "head", "request"} and "timeout" not in keywords:
            found.append("%s:%d requests.%s" % (os.path.basename(path), node.lineno, name))
        if owner == "subprocess" and name in {"run", "check_output", "check_call", "call"} and "timeout" not in keywords:
            words = literal_strings(node.args[0]) if node.args else []
            if (words[:1] == ["git"] and NETWORK_GIT & set(words)) or words[:1] == ["gh"]:
                found.append("%s:%d %s" % (os.path.basename(path), node.lineno, " ".join(words[:4])))
    return found


class NetworkTimeoutTests(unittest.TestCase):
    def test_network_git_gh_and_requests_calls_name_a_timeout(self):
        missing = []
        for name in sorted(os.listdir(HERE)):
            if name.endswith(".py") and not name.startswith("test_"):
                missing += findings(os.path.join(HERE, name))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
