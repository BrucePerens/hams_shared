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

PRODUCT_ROOTS = [os.path.join(HERE, "..", ".."), os.path.join(HERE, "..", "..", "..", "hams_com")]
SKIPPED_DIRS = {".git", "node_modules", "target", "tests", "static", "__pycache__", "venv", ".venv", "vendor", "docs",
                "site-packages", "tools", "devbox_tools", "hams_shared"}
# Calls that block on the network (or a peer) forever unless given a timeout.
CLIENTS = {
    "requests": {"get", "post", "put", "delete", "head", "request", "patch"},
    "httpx": {"get", "post", "put", "delete", "head", "request", "patch", "Client", "AsyncClient"},
    "urllib.request": {"urlopen"},
    "smtplib": {"SMTP", "SMTP_SSL"},
    "imaplib": {"IMAP4", "IMAP4_SSL"},
    "poplib": {"POP3", "POP3_SSL"},
}


def product_findings(path):
    """Daemon, model and controller code (the tools directories are covered above): an outbound call with no timeout,
    or a function that opens a socket and never sets a timeout on it (hunt 2026-11-20)."""
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = ast.unparse(node.func.value)
            keywords = {keyword.arg for keyword in node.keywords}
            if node.func.attr in CLIENTS.get(owner, ()) and not keywords & {"timeout", None}:
                found.append("%s:%d %s.%s" % (path, node.lineno, owner, node.func.attr))
            if owner == "socket" and node.func.attr == "create_connection" and len(node.args) < 2 and "timeout" not in keywords:
                found.append("%s:%d socket.create_connection" % (path, node.lineno))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            text = ast.get_source_segment(source, node) or ""
            if "socket.socket(" in text and "timeout" not in text and not any(word in text for word in ("bind(", "listen(", "fileno=")):  # servers, not callers
                found.append("%s:%d %s opens a socket without a timeout" % (path, node.lineno, node.name))
    return found


class NetworkTimeoutTests(unittest.TestCase):
    def test_product_code_outbound_calls_and_sockets_name_a_timeout(self):
        missing = []
        for root in PRODUCT_ROOTS:
            for directory, subdirs, names in os.walk(root):
                subdirs[:] = [d for d in subdirs if d not in SKIPPED_DIRS]
                for name in names:
                    if name.endswith(".py") and not name.startswith("test_"):
                        missing += product_findings(os.path.join(directory, name))
        self.assertEqual(missing, [])

    def test_network_git_gh_and_requests_calls_name_a_timeout(self):
        missing = []
        for name in sorted(os.listdir(HERE)):
            if name.endswith(".py") and not name.startswith("test_"):
                missing += findings(os.path.join(HERE, name))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
