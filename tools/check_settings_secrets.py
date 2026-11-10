#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Settings-secret ratchet: a secret-looking field of `res.config.settings` must be masked by `get_values`.

`res.config.settings.get_values` is a public method of a transient model; before `hams_s3` made its override
`@api.private`, any signed-in caller could read the stored S3 secret back over `call_kw`. A settings field that holds a
credential and is read back by the stock `get_values` (every `config_parameter=` field is) puts the stored value into the
settings form and into every `default_get`. This check finds, by AST, every field of a class that extends
`res.config.settings` whose name, or whose `config_parameter=` key, looks like a secret (secret, token, password,
credential, api key, signing, private key, or a name ending in `_key`) or whose `config_parameter=` is one of the key names
of a production secret (`SEED_PARAMETERS`, from the hams1 inventory, section 5). A field passes when the same class has a
`get_values` override that names the field (it masks or drops it). Every other finding is debt: it must be in the
checked-in baseline (`settings_secrets_baseline_<repo>.json`), and the baseline can only shrink. A new finding not in the
baseline fails the build. Exempt a field that is not a secret with `# settings-secret-ok: <reason>` on its line.

Usage: check_settings_secrets.py <repo_root> [--baseline FILE] [--write-baseline]
"""

import argparse
import ast
import json
import os
import re
import sys

SKIP_DIRS = {".git", "node_modules", "__pycache__", "tmp", "archive", "static", "tests", "migrations"}
SECRET_NAME = re.compile(r"secret|token|passw|credential|api_?key|signing|private_key|(^|_)key$", re.IGNORECASE)
# Names that match SECRET_NAME but are not secrets.
NOT_SECRET = re.compile(r"public_key|reset_password|_key_id$|keyboard|monkey|hotkey", re.IGNORECASE)
SEED_PARAMETERS = frozenset({
    "database.secret", "stripe.secret_key", "stripe.webhook_secret", "gemini.api_key", "ham_relay_bridge.api_key",
    "ham_relay_bridge.source_publish_key", "ham_relay_bridge.turn_secret", "ham_simulated_band.ident_token_secret",
    "form_shield.signing_key", "parking.form_secret", "mail.web_push_vapid_private_key",
})
EXEMPT = "settings-secret-ok:"
DEFAULT_OWNER = "Bruce Perens (mask in get_values or move to the secret store)"
DEFAULT_TRACKING = "night_shift_todo/high/default-deny-rpc-callable-methods-zero-sudo-2cf07bad.md"
FIELD_FACTORIES = {"Char", "Text", "Html", "Binary"}


def _constants(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _extends_settings(cls):
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) \
                and stmt.targets[0].id in ("_name", "_inherit"):
            if "res.config.settings" in _constants(stmt.value):
                return True
    return False


def _is_secret(name, parameter):
    if parameter in SEED_PARAMETERS:
        return True
    return any(SECRET_NAME.search(text) and not NOT_SECRET.search(text) for text in (name, parameter or ""))


def scan_source(source, path="<source>"):
    """`[(key, line)]` for each unmasked secret-looking settings field in `source`; key is `path:Class.field`."""
    tree = ast.parse(source)
    lines = source.splitlines()
    found = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or not _extends_settings(cls):
            continue
        masked = set()
        for stmt in cls.body:
            if isinstance(stmt, ast.FunctionDef) and stmt.name == "get_values":
                masked.update(_constants(stmt))
                masked.update(n.arg for n in ast.walk(stmt) if isinstance(n, ast.keyword) and n.arg)
        for stmt in cls.body:
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Call)):
                continue
            func = stmt.value.func
            if not (isinstance(func, ast.Attribute) and func.attr in FIELD_FACTORIES):
                continue
            name = stmt.targets[0].id
            parameter = None
            for keyword in stmt.value.keywords:
                if keyword.arg == "config_parameter" and isinstance(keyword.value, ast.Constant):
                    parameter = keyword.value.value
            if not _is_secret(name, parameter) or name in masked:
                continue
            span = " ".join(lines[stmt.lineno - 1:getattr(stmt, "end_lineno", stmt.lineno)])
            if EXEMPT in span:
                continue
            found.append(("%s:%s.%s" % (path, cls.name, name), stmt.lineno))
    return found


def scan_repo(repo_root):
    """`{key: line}` for every unmasked secret-looking settings field under `repo_root`."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            full = os.path.join(dirpath, filename)
            try:
                with open(full, encoding="utf-8") as handle:
                    source = handle.read()
                if "res.config.settings" not in source:
                    continue
                for key, line in scan_source(source, os.path.relpath(full, repo_root)):
                    found[key] = line
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
    return found


def load_baseline(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle).get("entries", {})


def compare(found, baseline):
    """`(problems, notes)`: a finding not in the baseline fails; a baseline entry no longer found is a note."""
    problems = ["%s: secret-looking settings field is not masked by get_values and not in the baseline: "
                "mask or drop it in get_values (or add `# settings-secret-ok: <reason>` if it holds no secret)" % key
                for key in sorted(set(found) - set(baseline))]
    notes = ["%s is fixed: remove its baseline entry" % key for key in sorted(set(baseline) - set(found))]
    return problems, notes


def render_baseline(found, previous):
    """Baseline text for `found`; refuses (ValueError) to add an entry that is not already in `previous`."""
    added = sorted(set(found) - set(previous))
    if previous and added:
        raise ValueError("refusing to add baseline entries: %s" % ", ".join(added))
    entries = {key: previous.get(key, {"owner": DEFAULT_OWNER, "tracking": DEFAULT_TRACKING}) for key in sorted(found)}
    return json.dumps({"version": 1, "entries": entries}, indent=2) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("repo_root")
    parser.add_argument("--baseline")
    parser.add_argument("--write-baseline", action="store_true", help="write the baseline (first time) or shrink it")
    args = parser.parse_args(argv)
    found = scan_repo(args.repo_root)
    baseline = load_baseline(args.baseline)
    if args.write_baseline:
        text = render_baseline(found, baseline) if baseline or not found else render_baseline(found, {})
        with open(args.baseline, "w", encoding="utf-8") as handle:
            handle.write(text)
        sys.stdout.write("wrote %s\n" % args.baseline)
        return 0
    sys.stdout.write("secret-looking settings fields not masked by get_values: %d\n" % len(found))
    for key in sorted(found):
        sys.stdout.write("  %s\n" % key)
    problems, notes = compare(found, baseline)
    for note in notes:
        sys.stdout.write("[*] %s\n" % note)
    for problem in problems:
        sys.stdout.write("[!] CI/CD FAILURE: %s\n" % problem)
    if problems:
        return 1
    sys.stdout.write("[+] SUCCESS: no new unmasked secret settings field.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
