#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""site_compare: compare the saved HTML of an old site with the same paths served by its replacement.

    site_compare.py --original-dir DIR --new-url http://127.0.0.1:18102 --host postopen.org \
        --paths /,/about-post-open,/press [--report FILE]

DIR holds one file per path, named as `name_for_path` does (`/` is ROOT.html, `/documents/license` is
documents__license.html), saved earlier by the operator. The new site is read with ordinary anonymous GETs
(one per second at most, honest User-Agent) and nothing is written to it. For each path the tool compares

- the HTTP status,
- the text of the page body (the `<main>` element), word for word after whitespace is collapsed,
- the images of the body (host and cache-busting query removed) and whether each one answers,
- the links of the body,
- the title, the meta description and the robots meta,
- the navigation entries and the footer text (reported separately: they are theme, not content).

The result is a Markdown table and a list of differences. Exit status 1 if any body differs.
"""

import argparse
import difflib
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit

USER_AGENT = "HamsComSyncDaemon/1.0 (+https://crawler.hams.com)"
SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
VOID = {"br", "img", "meta", "link", "hr", "input", "source", "area", "base", "col", "embed", "param", "track", "wbr"}


def name_for_path(path):
    name = path.strip("/").replace("/", "__")
    return name or "ROOT"


class _Page(HTMLParser):
    """Collects the parts of an Odoo page that matter for a content comparison."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta = {}
        self.stack = []
        self.text = {"main": [], "nav": [], "footer": []}
        self.images, self.links = [], []
        self.nav_items = []
        self._in_title = False
        self._skip = 0
        self._region = None
        self._region_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            name = attrs.get("name") or attrs.get("property")
            if name:
                self.meta[name.lower()] = attrs.get("content") or ""
        region = None
        if tag == "main":
            region = "main"
        elif tag == "footer":
            region = "footer"
        elif tag == "nav" and attrs.get("aria-label") in ("Main", "Mobile"):
            region = "nav"
        if self._region is None and region:
            self._region, self._region_depth = region, 0
        if self._region is not None and tag not in VOID:
            self._region_depth += 1
        if self._region == "main":
            if tag == "img" and attrs.get("src"):
                self.images.append(attrs["src"])
            if tag == "a" and attrs.get("href"):
                self.links.append(attrs["href"])
        if tag == "a" and self._region == "nav" and attrs.get("href"):
            self.nav_items.append(attrs["href"])
        if tag in ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"):
            self._add_text(" ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if self._region is not None and tag not in VOID:
            self._region_depth -= 1
            if self._region_depth <= 0:
                self._region = None

    def _add_text(self, data):
        if self._region and not self._skip:
            self.text[self._region].append(data)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        self._add_text(data)


def parse_page(html):
    page = _Page()
    page.feed(html)
    words = {key: re.sub(r"\s+", " ", " ".join(value)).strip() for key, value in page.text.items()}
    return {
        "title": re.sub(r"\s+", " ", page.title).strip(),
        "description": page.meta.get("description", ""),
        "robots": page.meta.get("robots", ""),
        "main": words["main"], "nav": words["nav"], "footer": words["footer"],
        "images": [normalize_url(src) for src in page.images],
        "links": [normalize_url(href) for href in page.links],
        "nav_items": [normalize_url(href) for href in page.nav_items],
    }


def normalize_url(url, hosts=()):
    """A comparable form: no scheme or host of the site itself, no cache-busting query."""
    parts = urlsplit(url)
    path = parts.path or "/"
    query = re.sub(r"(^|&)unique=[^&]*", "", parts.query).strip("&")
    local = not parts.netloc or parts.netloc.lower().removeprefix("www.") in {h.lower().removeprefix("www.") for h in hosts}
    if local or not parts.scheme:
        return path + (f"?{query}" if query else "")
    return url


def diff_words(old, new, limit=12):
    """Short, readable differences between two texts: (removed, added) word runs."""
    a, b = old.split(), new.split()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        out.append((" ".join(a[i1:i2])[:120], " ".join(b[j1:j2])[:120]))
        if len(out) >= limit:
            break
    return out


# [@ANCHOR: site_compare:compare_pages]
# Verified by [@ANCHOR: test_site_compare:compare_pages]
def compare_pages(old_html, new_html, hosts=()):
    """Differences between two renderings of one page, as a dict of lists (all empty means identical)."""
    old, new = parse_page(old_html), parse_page(new_html)
    for page in (old, new):
        page["images"] = [normalize_url(i, hosts) for i in page["images"]]
        page["links"] = [normalize_url(i, hosts) for i in page["links"]]
        page["nav_items"] = [normalize_url(i, hosts) for i in page["nav_items"]]
    result = {"text": [], "images": [], "links": [], "meta": [], "nav": [], "footer": []}
    if old["main"] != new["main"]:
        result["text"] = diff_words(old["main"], new["main"])
    for kind in ("images", "links"):
        missing = [item for item in old[kind] if item not in new[kind]]
        added = [item for item in new[kind] if item not in old[kind]]
        result[kind] = [f"missing {m}" for m in missing] + [f"added {a}" for a in added]
    for key in ("title", "description", "robots"):
        if old[key] != new[key]:
            result["meta"].append(f"{key}: {old[key]!r} -> {new[key]!r}")
    gone = sorted(set(old["nav_items"]) - set(new["nav_items"]))
    came = sorted(set(new["nav_items"]) - set(old["nav_items"]))
    if gone or came:
        result["nav"] = [f"navigation: missing {gone} added {came}"]
    if old["footer"] != new["footer"]:
        result["footer"] = diff_words(old["footer"], new["footer"], 4)
    return result


class Fetcher:
    def __init__(self, base, host=None, interval=1.0, sleep=time.sleep, opener=None):
        self.base, self.host, self.interval, self._sleep = base.rstrip("/"), host, interval, sleep
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._last = 0.0

    def get(self, path, method="GET"):
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        headers = {"User-Agent": USER_AGENT}
        if self.host:
            headers["Host"] = self.host
        request = urllib.request.Request(self.base + path, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=30) as response:
                return response.status, response.read() if method == "GET" else b"", dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() if method == "GET" else b"", dict(exc.headers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def run(original_dir, fetcher, paths, hosts=(), out=print):
    """Returns (rows, problems): rows for the Markdown table, problems = pages whose body differs."""
    rows, problems = [], 0
    for path in paths:
        name = name_for_path(path)
        try:
            with open(f"{original_dir}/{name}.html", "r", encoding="utf-8", errors="replace") as handle:  # audit-ignore-path
                old_html = handle.read()
        except OSError:
            rows.append((path, "no saved original", "-", "-", "-"))
            continue
        status, body, _headers = fetcher.get(path)
        new_html = body.decode("utf-8", "replace") if status == 200 else ""
        if status != 200:
            rows.append((path, f"new site answers {status}", "-", "-", "-"))
            problems += 1
            continue
        diff = compare_pages(old_html, new_html, hosts)
        broken = []
        for image in sorted(set(parse_page(new_html)["images"])):
            if image.startswith("/") and fetcher.get(image, "HEAD")[0] >= 400:
                broken.append(image)
        same_text = "same" if not diff["text"] else f"{len(diff['text'])} difference(s)"
        images = "all answer" if not broken else f"{len(broken)} broken"
        meta = "same" if not diff["meta"] else "; ".join(diff["meta"])
        rows.append((path, "200", same_text, images + ("; " + ", ".join(diff["images"][:2]) if diff["images"] else ""), meta))
        if diff["text"] or broken:
            problems += 1
        for removed, added in diff["text"]:
            out(f"  {path}: text -{removed!r} +{added!r}")
        for item in diff["links"] + diff["nav"]:
            out(f"  {path}: {item}")
        for item in broken:
            out(f"  {path}: image does not answer: {item}")
    return rows, problems


def render_table(rows):
    lines = ["| Path | New status | Body text | Images | Title / description / robots |", "|---|---|---|---|---|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("|", "/") for cell in row) + " |")
    return "\n".join(lines)


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--original-dir", required=True)
    parser.add_argument("--new-url", required=True)
    parser.add_argument("--host", help="Host header to send (the old site's name)")
    parser.add_argument("--paths", required=True, help="comma separated list of paths")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--report", help="write the Markdown table to this file")
    args = parser.parse_args(argv)
    host = args.host
    paths = [p for p in args.paths.split(",") if p]
    rows, problems = run(args.original_dir, Fetcher(args.new_url, host, args.interval), paths, [host] if host else (), out)
    table = render_table(rows)
    out(table)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:  # audit-ignore-path
            handle.write(table + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
