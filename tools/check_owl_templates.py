#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Real, standalone Owl QWeb template compile-checker.

Built 2026-09-07 after a real, live-instrumented investigation (OFFLINE_HAM_OPERATION.md,
ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md) finally found `ham_shack.WebShackTemplate` had a genuine
compile error -- `ham_shack.web_shack` never mounted for weeks/several prior debugging sessions
specifically because Odoo's own runtime error reporting for this failure mode is real but
practically useless: the thrown `OwlError`'s message embeds the *entire* generated JS source (tens
of KB), which (a) gets silently truncated by Chrome DevTools Protocol's own console-message preview
limit before it ever reaches a test log, and (b) V8's own `SyntaxError` from a dynamically
`new Function()`-compiled string carries no line/column information at all ("Invalid or unexpected
token", full stop) -- there is no way to find the actual broken template from that error alone.
Per Bruce's own direct instruction, this is an always-on linter, not a one-off debugging script:
wired into `run_linters.py` as a required gate, so a template that fails to compile is caught at
commit time, in a clear, non-truncated, per-template report -- not discovered by a component
silently never mounting in production.

**Why a real headless browser, not a hand-rolled DOM shim**: Owl (`owl.js`) touches real DOM APIs
(`DOMParser`, `Element`, `Node`) at module-load time, not just at call time -- a first attempt at
loading it in plain Node with global shims failed immediately ("Cannot read properties of
undefined"). A linter that could produce a wrong (false-negative OR false-positive) compile result
because of an incomplete hand-written DOM shim is worse than no linter at all here; headless Chrome
is the same real runtime every actual user's browser uses, so a genuine compile failure/success here
is the real answer, not an approximation.

**How it works**: launches a real headless Chrome via CDP (same technique this session's own live
investigation already used), loads the real `owl.js` bundle by evaluating its source directly (no
script tag, no CORS/file:// friction), then for each `static/src/xml/*.xml` file in a module,
extracts every `[t-name]` element's raw outerHTML via a real `DOMParser`, registers it with a fresh
`owl.App` instance via `addTemplates()`, and calls `getTemplate(name)` for each -- the *exact* same
call `ComponentNode`'s own constructor makes at real mount time (confirmed by reading
`interaction_service.js`/`colibri.js`/Owl's own `owl.js` source directly this session, not assumed).
The full, untruncated error (if any) comes back as the direct return value of one `Runtime.evaluate`
call with `returnByValue=True` -- never printed through `console.*`, so none of the truncation/
ordering problems that made the live investigation so hard apply here.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

import requests
import websockets

_OWL_JS_PATH = (
    "/usr/lib/python3/dist-packages/odoo/addons/web/static/lib/owl/owl.js"
)


def find_owl_template_files(target_dir):
    """Yields every `static/src/xml/*.xml` file under `target_dir` -- this codebase's own
    convention for where Owl component templates (as opposed to Odoo view/data XML) live.

    Matches on the directory's own trailing path COMPONENTS (["static", "src", "xml"]), never a
    plain substring test -- a substring test on "/static/src/xml" would also match an unrelated
    directory merely named e.g. "static/src/xml_export/" (this function's own pre-fix version had
    exactly this bug: its "cheap prefilter" computed os.path.join(os.sep, "static", os.sep,
    "src", os.sep, "xml"), intending "/static/src/xml", but os.path.join resets to the last
    absolute-path argument it sees -- every os.sep argument here IS absolute -- so the real value
    was just "/xml", making the substring check match almost any path and short-circuit past the
    real, precise component check below it)."""
    for root, dirs, files in os.walk(target_dir):
        # Dot-directories -- critically ".claude/worktrees/<session>/", this project's own
        # standing convention for running concurrent bug-hunt dispatches in isolated git
        # worktrees INSIDE the repo root -- are never real template locations. Without this, a
        # concurrently running session's own worktree gets scanned as if it were part of the real
        # repo: confirmed live while reviewing this exact file -- 40 of 60 real-repo template
        # files found were duplicates from two other concurrently active sessions' own worktrees,
        # meaning this checker was spending roughly two-thirds of its real headless-Chrome
        # compile work re-checking other sessions' own copies of the same templates.
        dirs[:] = [
            d
            for d in dirs
            if d not in ("node_modules", "__pycache__", ".git", "target")
            and not d.startswith(".")
        ]
        parts = root.replace("\\", "/").split("/")
        if parts[-3:] != ["static", "src", "xml"]:
            continue
        for f in files:
            if f.endswith(".xml"):
                yield os.path.join(root, f)


async def _cdp_call(ws, msg_id, method, params=None):
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == msg_id:
            return msg


async def check_templates_via_cdp(xml_file_contents, port):
    """`xml_file_contents`: list of (filepath, raw_xml_text). Returns a list of dicts:
    {filepath, template_name, ok, error} for every `[t-name]` template found across all files."""
    targets = requests.get(f"http://localhost:{port}/json").json()
    page = next(t for t in targets if t.get("type") == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
        with open(_OWL_JS_PATH, "r", encoding="utf-8") as f:
            owl_src = f.read()
        await _cdp_call(ws, 1, "Runtime.evaluate", {"expression": owl_src})

        results = []
        for idx, (filepath, xml_text) in enumerate(xml_file_contents):
            script = f"""
            (function() {{
                const xmlText = {json.dumps(xml_text)};
                const app = new owl.App(class extends owl.Component {{}}, {{}});
                let names;
                try {{
                    const doc = new DOMParser().parseFromString(xmlText, "text/xml");
                    const parserError = doc.querySelector("parsererror");
                    if (parserError) {{
                        return [{{template_name: null, ok: false, error: "XML parse error: " + parserError.textContent}}];
                    }}
                    names = Array.from(doc.querySelectorAll("[t-name]")).map(el => el.getAttribute("t-name"));
                    app.addTemplates(doc);
                }} catch (e) {{
                    return [{{template_name: null, ok: false, error: "addTemplates() threw: " + e.message}}];
                }}
                const out = [];
                for (const name of names) {{
                    try {{
                        app.getTemplate(name);
                        out.push({{template_name: name, ok: true, error: null}});
                    }} catch (e) {{
                        out.push({{template_name: name, ok: false, error: e.message}});
                    }}
                }}
                return out;
            }})()
            """
            resp = await _cdp_call(
                ws,
                100 + idx,
                "Runtime.evaluate",
                {"expression": script, "returnByValue": True, "awaitPromise": False},
            )
            result = resp.get("result", {})
            exc = result.get("exceptionDetails")
            if exc:
                results.append(
                    {
                        "filepath": filepath,
                        "template_name": None,
                        "ok": False,
                        "error": "Evaluation itself threw: "
                        + exc.get("exception", {}).get("description", str(exc)),
                    }
                )
                continue
            for entry in result.get("result", {}).get("value", []):
                results.append({"filepath": filepath, **entry})
        return results


def _launch_headless_chrome(port, user_data_dir, log_file):
    chrome_bin = None
    for candidate in ("google-chrome", "chromium", "chromium-browser"):
        from shutil import which

        if which(candidate):
            chrome_bin = candidate
            break
    if not chrome_bin:
        raise RuntimeError("No Chrome/Chromium binary found on PATH")
    return subprocess.Popen(
        [
            chrome_bin,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            # This tool never plays or captures real audio (it only compiles Owl
            # templates), but headless Chrome's audio service still probes the
            # real system output device on launch unless muted -- confirmed
            # directly on this box: without --mute-audio, launching headless
            # Chrome here pushed PipeWire's real audio sinks aside in favor of
            # a "dummy" device, exactly the failure this codebase's test.py
            # already guards against at every one of its own Chrome launch
            # sites. Match that established, working convention rather than
            # reinventing a narrower flag set.
            "--mute-audio",
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            "--disable-background-networking",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "about:blank",
        ],
        stdout=log_file,
        stderr=log_file,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument("--port", type=int, default=9333)
    args = parser.parse_args()

    if not os.path.exists(_OWL_JS_PATH):
        print(f"[!] SKIPPED: Owl bundle not found at {_OWL_JS_PATH} (not an Odoo box?)")
        return 0

    target_dir = os.path.abspath(args.directory)
    xml_files = sorted(find_owl_template_files(target_dir))
    if not xml_files:
        print("[*] No static/src/xml/*.xml template files found -- nothing to check.")
        return 0

    xml_file_contents = []
    for fp in xml_files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                xml_file_contents.append((fp, f.read()))
        except OSError as e:
            print(f"  ❌ ERROR: Could not read {fp}: {e}")

    import tempfile

    user_data_dir = tempfile.mkdtemp(prefix="owl_template_check_")
    log_path = os.path.join(user_data_dir, "chrome.log")
    log_file = open(log_path, "wb")
    try:
        proc = _launch_headless_chrome(args.port, user_data_dir, log_file)
    except RuntimeError as e:
        print(f"[!] ERROR: {e} -- cannot verify Owl templates without a real browser.")
        return 1
    try:
        for _ in range(50):
            try:
                requests.get(f"http://localhost:{args.port}/json", timeout=0.2)
                break
            except requests.exceptions.ConnectionError:
                time.sleep(0.1)
        else:
            log_file.flush()
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                chrome_log = f.read()
            print("[!] ERROR: headless Chrome never exposed its devtools port")
            if chrome_log.strip():
                print(f"    Chrome's own stdout/stderr:\n{chrome_log}")
            return 1

        results = asyncio.run(check_templates_via_cdp(xml_file_contents, args.port))
    finally:
        log_file.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    failures = [r for r in results if not r["ok"]]
    checked_names = sum(1 for r in results if r.get("template_name"))
    if failures:
        print(f"[!] CI/CD FAILURE: {len(failures)} Owl template(s) failed to compile:")
        for r in failures:
            print(f"    - {r['filepath']}: template '{r.get('template_name')}'")
            print(f"      {r['error']}")
        return 1

    print(
        f"[+] SUCCESS: {checked_names} Owl template(s) across {len(xml_files)} file(s) all "
        "compile cleanly."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
