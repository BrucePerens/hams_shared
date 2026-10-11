#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Documentation Image Checker
-------------------------------------------------------------------------------------------
Every module's `data/documentation.html` (and `data/*.html` pages, a daemon's `manuals/*.html`, and a module's
`README.md`) is end-user or developer documentation. Until 2026-10-11 about 40 of them showed
AI-generated mock-up "screenshots" of fictional products (a "Nexus ERP" New Sales Order form, a
"GeoTrack Pro" map, a generic purchase-order list, a "NexusFlow" settings page) under captions
like "Form View Screenshot". They were never our UI. Bruce: "A lot of the documentation still
has the 'new sales order' photo as an example of its user interface."

This checker fails when a documentation page references an image that:

  1. has a file name that module scaffolding used for those placeholders
     (`form_screenshot.png`, `list_screenshot.png`, `map_screenshot.png`);
  2. has the exact bytes (MD5) of a known fake, blank or wrong-content image (DENYLIST below);
  3. does not exist in the repository (a broken image in a published manual); or
  4. is the same file shown by the documentation of three or more different modules (a copied
     placeholder has no other explanation; a real screenshot belongs to one module's UI).

A genuine screenshot of hams.com's own UI is always accepted. Add a new known-bad image's MD5 to
DENYLIST in the same change that deletes the file. There is no escape-hatch comment.

Usage: check_doc_images.py <repo_root>
"""

import hashlib
import os
import re
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".claude", "vendor", "target", "radae"}
BAD_NAMES = {"form_screenshot.png", "list_screenshot.png", "map_screenshot.png"}
MAX_MODULES_PER_IMAGE = 2

# md5 -> what the image is. Fake or misleading images found 2026-10-11.
DENYLIST = {
    "c6e2aaa999ba289c9efa6c7f4819a3c7": "AI mock-up of a fictional 'Nexus ERP' New Sales Order form",
    "832d6c4710853df0f33365b6593b9d4f": "AI mock-up of a fictional 'GeoTrack Pro' asset map",
    "6bb133f1d22810c422308f667c3d5da9": "AI mock-up of a generic ERP Purchase Orders list",
    "1b7956796090d2db21fe52d625c9212a": "AI mock-up of a fictional 'NexusFlow' settings page",
    "d51ec08df9bb62d0db1e41868273a35e": "AI mock-up of a generic 'Optimize SEO' dialog",
    "a4d894574683568076c714f9fbc21696": "AI mock-up of a restaurant-finder map on a tablet",
    "dbb9270d53e900c62a10c20b7eaf374c": "AI-generated photo of a fictional radio, not the website widget",
    "1df6c6691e3b1d53d4d450118473c014": "blank white page",
    "e1fb9eeefc3623ad8eab78d98e4fe5b6": "login page, captioned as the portal home screen",
    "85d14c919706d0f83a6834af543fa474": "404 error page, captioned as the community directory",
    "e27986899363e29df32266a474820e16": "AI-generated stock image of a NOC control room (pager_duty, never referenced)",
    "75e261dcf9f9ed9b60ef5796c4d322fe": "a Knowledge manual page, captioned as the User Websites dashboard",
}

_IMG_TAG = re.compile(r"<img\b[^>]*?>", re.DOTALL | re.IGNORECASE)
_SRC = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MD_IMG = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")


def _is_doc_file(rel_path):
    parts = rel_path.split(os.sep)
    name = parts[-1]
    if len(parts) == 2 and name == "README.md":
        return True
    if len(parts) >= 3 and parts[-2] == "data" and name.endswith(".html"):
        return True
    return len(parts) >= 3 and parts[-2] == "manuals" and name.endswith(".html")


def _image_refs(text, is_markdown):
    refs = []
    for tag in _IMG_TAG.findall(text):
        match = _SRC.search(tag)
        if match:
            refs.append(match.group(1))
    if is_markdown:
        refs.extend(_MD_IMG.findall(text))
    return refs


def _resolve(ref, doc_path, repo_roots):
    """Return the image file for `ref`, or None. `/module/static/...` is looked up in every repo
    root (a module's docs can show an image of a module in the sibling repository)."""
    ref = ref.split("#")[0].split("?")[0]
    if ref.startswith("/"):
        for root in repo_roots:
            candidate = os.path.join(root, ref.lstrip("/"))
            if os.path.isfile(candidate):
                return candidate
        return None
    candidate = os.path.normpath(os.path.join(os.path.dirname(doc_path), ref))
    return candidate if os.path.isfile(candidate) else None


def _md5(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_repo_roots(given_path):
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        given_path = os.path.dirname(given_path)
    roots = [given_path]
    sibling_name = "hams_open" if os.path.basename(given_path) != "hams_open" else "hams_com"
    sibling = os.path.abspath(os.path.join(given_path, "..", sibling_name))
    if os.path.isdir(sibling) and os.path.isfile(os.path.join(sibling, "AGENTS.md")):
        roots.append(sibling)
    return roots


def check_doc_images(repo_root, extra_roots=()):
    violations = []
    repo_roots = [repo_root] + [r for r in extra_roots if r != repo_root]
    modules_by_hash = {}
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in files:
            path = os.path.join(root, filename)
            rel = os.path.relpath(path, repo_root)
            if not _is_doc_file(rel):
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except (UnicodeDecodeError, OSError) as error:
                print(f"Warning: could not read {path}: {error}")
                continue
            module = rel.split(os.sep)[0]
            for ref in (r.strip() for r in _image_refs(text, filename.endswith(".md"))):
                if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", ref, re.IGNORECASE):
                    continue
                base = os.path.basename(ref.split("?")[0])
                if base in BAD_NAMES:
                    violations.append(
                        f"{rel}: image '{ref}' is a scaffolding placeholder name "
                        f"({base}); show a real screenshot of this module's own UI or no image."
                    )
                    continue
                image = _resolve(ref, path, repo_roots)
                if image is None:
                    violations.append(f"{rel}: image '{ref[:100]}' does not exist.")
                    continue
                digest = _md5(image)
                if digest in DENYLIST:
                    violations.append(
                        f"{rel}: image '{ref}' is {DENYLIST[digest]} (md5 {digest}); "
                        f"documentation must never show a fake UI."
                    )
                    continue
                modules_by_hash.setdefault(digest, {}).setdefault(module, set()).add(ref)
    for digest, modules in sorted(modules_by_hash.items()):
        if len(modules) > MAX_MODULES_PER_IMAGE:
            names = ", ".join(sorted(modules))
            violations.append(
                f"Identical image (md5 {digest}) is shown by {len(modules)} modules' "
                f"documentation ({names}): a copied placeholder, not a screenshot of each "
                f"module's own UI."
            )
    return violations


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_doc_images.py <repo_root>")
        sys.exit(2)
    roots = _resolve_repo_roots(sys.argv[1])
    found = []
    for scan_root in roots:
        found.extend(check_doc_images(scan_root, roots))
    for line in found:
        print(line)
    sys.exit(1 if found else 0)
