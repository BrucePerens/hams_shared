#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""wordpress_to_odoo: convert a mysqldump of a WordPress site into an `odoo_site_migrate` export.

    wordpress_to_odoo.py inventory --sql DUMP.sql.gz [--blog-id 8]
    wordpress_to_odoo.py convert   --sql DUMP.sql.gz --out DIR [--blog-id 8] [--domain perens.com ...]
                                   [--static-root DIR] [--uploads-dir DIR ...] [--blog-name NAME]
    wordpress_to_odoo.py scan      --root DIR [--root DIR ...]
    wordpress_to_odoo.py compare   --sql DUMP.sql.gz --base-url http://127.0.0.1:18101
    wordpress_to_odoo.py finalize-sitemap --export DIR        (after odoo_site_migrate.py import --apply)

Nothing here talks to a network, a database server or WordPress: the input is a file, the output is a
directory in the format `odoo_site_migrate.py import` reads (JSON lines per Odoo model, `files/`,
`manifest.json`, `CHECKSUMS.sha256`), so the dry-run-first, idempotent, id-remapping importer does the
loading and nothing about the load is duplicated here. In addition `convert` writes, next to the export:

    reports/content_report.json     counts, what was dropped and why (the markdown report is built from it)
    reports/sitemap.csv             every old URL -> new URL, with status (kept, redirect, dropped, static)
    reports/missing_references.csv  uploads and /static files that posts and pages reference but the snapshot lacks
    reports/comments_dropped.csv    the comments, without e-mail addresses and IP addresses

Decisions (written down because they change what visitors see):

* Comments are NOT imported (the tenants have no logins). They go to a CSV; they are not part of the export.
* Revisions, auto-drafts, navigation/global-style/template posts and trashed posts are dropped. Draft, pending,
  private and password-protected posts are not published: they are listed in the report and left out unless
  `--include-unpublished` is given (then they arrive as unpublished records).
* Every `<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, form and on* handler is removed from content.
  The perens.com posts carried an injected loader script in every post; it is counted in the report.
* Pages become `website.page` + QWeb `ir.ui.view` records, posts become `blog.post` records. A page keeps its URL
  (hierarchical slugs included). A post cannot keep `/2018/03/03/slug/` because Odoo's blog URL carries a record
  id, so every old permalink (and the `/blog/` prefixed variant) is a 301 `website.rewrite` to the new URL.
  Odoo itself 301s a trailing-slash URL to the slash-less one, so rules exist only for the slash-less forms.
* WordPress's own `wpautop` (blank line -> paragraph) is reproduced, because the stored classic-editor content
  has no `<p>` tags and the original theme only showed paragraphs through that filter. `wptexturize` (curly
  quotes, dashes), smilies and oEmbed (a bare URL on a line becomes an embedded player) are NOT reproduced:
  quotes stay as typed, an embeddable URL stays a plain link. The report counts these.
"""

import argparse
import collections
import csv
import datetime
import fnmatch
import gzip
import hashlib
import html as htmllib
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import odoo_site_migrate as mig  # noqa: E402
from lxml import etree  # noqa: E402
from lxml import html as lxml_html  # noqa: E402

TOOL_VERSION = "1.0"
DEFAULT_BLOG_NAME = "Bruce Perens' Blog"
ROOT_MENU_NAME = "Top Menu for Website 1"  # what Odoo names the first website's root menu
SITE_ID = 1
# WordPress posts had no featured image; Odoo's default blog cover is a half-screen coloured block.
# A compact, plain cover keeps the text on the first screen.
COVER_PROPERTIES = json.dumps({
    "background-image": "none", "background_color_class": "o_cc3", "opacity": "0.2",
    "resize_class": "cover_auto",
}, sort_keys=True)

# Source ids are remapped by the importer. The blog id is large on purpose: the importer rewrites
# `/blog/<slug>-<blog id>/...` anywhere in content, and a small id would also match an external URL such as
# https://example.org/blog/news-1/.
BLOG_ID = 90001

# --------------------------------------------------------------------------------------------
# mysqldump reader
# --------------------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<null>NULL)
      | (?P<hex>0x[0-9A-Fa-f]+)
      | (?P<num>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
      | '(?P<str>(?:[^'\\]|\\.|'')*)'
    )\s*""",
    re.X | re.S,
)
_ESCAPES = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "b": "\b", "Z": "\x1a", "\\": "\\", "'": "'", '"': '"'}
_UNESC = re.compile(r"\\(.)|''", re.S)


def _unescape(raw):
    if "\\" not in raw and "''" not in raw:
        return raw
    return _UNESC.sub(lambda m: "'" if m.group(0) == "''" else _ESCAPES.get(m.group(1), m.group(1)), raw)


# [@ANCHOR: wordpress_to_odoo:read_dump]
# Verified by [@ANCHOR: test_wordpress_to_odoo:read_dump]
def parse_values(text, pos=0):
    """Yields tuples from the text following VALUES in a mysqldump INSERT, until the closing ';'."""
    n = len(text)
    while pos < n:
        while pos < n and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= n or text[pos] == ";":
            return
        if text[pos] != "(":
            raise ValueError(f"unexpected {text[pos:pos + 20]!r} at {pos}")
        pos += 1
        row = []
        while True:
            m = _TOKEN.match(text, pos)
            if not m:
                raise ValueError(f"cannot parse value near {text[pos:pos + 40]!r}")
            pos = m.end()
            if m.group("null"):
                row.append(None)
            elif m.group("num") is not None:
                s = m.group("num")
                row.append(float(s) if any(c in s for c in ".eE") else int(s))
            elif m.group("str") is not None:
                row.append(_unescape(m.group("str")))
            else:
                row.append(bytes.fromhex(m.group("hex")[2:]))
            if text[pos] == ",":
                pos += 1
                continue
            if text[pos] == ")":
                pos += 1
                break
            raise ValueError(f"expected , or ) at {text[pos:pos + 20]!r}")
        yield tuple(row)


def read_dump(path, tables=None):
    """Returns {table: {"columns": [...], "rows": [tuple...]}} for the wanted tables (all when None)."""
    out, cols = {}, {}
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        current = None
        for line in handle:
            if line.startswith("CREATE TABLE `"):
                current = line.split("`")[1]
                cols[current] = []
            elif current and line.startswith("  `"):
                cols[current].append(line.split("`")[1])
            elif line.startswith(") "):
                current = None
            elif line.startswith("INSERT INTO `"):
                table = line.split("`")[1]
                # MariaDB 11 dumps "INSERT INTO `t` VALUES" then one tuple per line; older mysqldump puts
                # everything on one line. The statement ends at a line ending in ";".
                chunks = [line.split(" VALUES", 1)[1]]
                while not chunks[-1].rstrip().endswith(";"):
                    chunks.append(next(handle))
                if tables is not None and table not in tables:
                    continue
                entry = out.setdefault(table, {"columns": cols[table], "rows": []})
                entry["rows"].extend(parse_values("".join(chunks)))
    return out


def php_unserialize(text):
    """Minimal PHP unserialize for options (arrays, strings, ints, floats, bools, null). Objects are refused."""
    data = text.encode("utf-8")
    pos = 0

    def read_until(char):
        nonlocal pos
        end = data.index(char, pos)
        chunk = data[pos:end]
        pos = end + 1
        return chunk.decode()

    def value():
        nonlocal pos
        kind = chr(data[pos])
        if kind == "N":
            pos += 2
            return None
        pos += 2
        if kind == "b":
            return read_until(b";") == "1"
        if kind == "i":
            return int(read_until(b";"))
        if kind == "d":
            return float(read_until(b";"))
        if kind == "s":
            length = int(read_until(b":"))
            pos += 1
            raw = data[pos:pos + length]
            pos += length + 2
            return raw.decode("utf-8", "replace")
        if kind == "a":
            count = int(read_until(b":"))
            pos += 1
            result = {}
            for _ in range(count):
                key = value()
                result[key] = value()
            pos += 1
            return result
        raise ValueError(f"unsupported serialized type {kind!r}")

    try:
        return value()
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------------------------
# WordPress model
# --------------------------------------------------------------------------------------------


def _rows(dump, table):
    entry = dump.get(table)
    if not entry:
        return []
    return [dict(zip(entry["columns"], row)) for row in entry["rows"]]


class WordPressSite:
    """One site of a (multisite) WordPress dump, by blog id (prefix `wp_8_`; blog 1 has prefix `wp_`)."""

    def __init__(self, dump, blog_id=None):
        self.dump = dump
        blogs = _rows(dump, "wp_blogs")
        self.blogs = blogs
        if blog_id is None:
            blog_id = 1
        self.blog_id = blog_id
        self.prefix = "wp_" if blog_id == 1 else f"wp_{blog_id}_"
        self.blog_domain = next((b["domain"] for b in blogs if b["blog_id"] == blog_id), None)
        self.options = {r["option_name"]: r["option_value"] for r in _rows(dump, self.prefix + "options")}
        self.posts = _rows(dump, self.prefix + "posts")
        self.postmeta = collections.defaultdict(dict)
        for r in _rows(dump, self.prefix + "postmeta"):
            self.postmeta[r["post_id"]][r["meta_key"]] = r["meta_value"]
        self.terms = {r["term_id"]: r for r in _rows(dump, self.prefix + "terms")}
        self.taxonomy = {r["term_taxonomy_id"]: r for r in _rows(dump, self.prefix + "term_taxonomy")}
        self.relationships = collections.defaultdict(list)
        for r in _rows(dump, self.prefix + "term_relationships"):
            self.relationships[r["object_id"]].append(r["term_taxonomy_id"])
        self.comments = _rows(dump, self.prefix + "comments")
        self.users = {u["ID"]: u for u in _rows(dump, "wp_users")}
        if not self.posts and not self.options:
            raise ValueError(f"no tables with prefix {self.prefix!r} in the dump")

    def option(self, name, default=""):
        value = self.options.get(name)
        return default if value is None else value

    @property
    def permalink_structure(self):
        return self.option("permalink_structure")

    def author_name(self, user_id):
        user = self.users.get(user_id)
        return user["display_name"] if user else None

    def categories(self, post_id):
        names = []
        for tt in self.relationships.get(post_id, []):
            tax = self.taxonomy.get(tt)
            if tax and tax["taxonomy"] == "category":
                names.append(self.terms[tax["term_id"]]["name"])
        return names

    def tags(self, post_id):
        names = []
        for tt in self.relationships.get(post_id, []):
            tax = self.taxonomy.get(tt)
            if tax and tax["taxonomy"] == "post_tag":
                names.append(self.terms[tax["term_id"]]["name"])
        return names

    def nav_menu_items(self):
        """The top-menu items in order: dicts with title, kind (page/post/custom), object_id, url."""
        locations = php_unserialize(self.option("theme_mods_" + self.option("stylesheet"), "")) or {}
        menu_term = (locations.get("nav_menu_locations") or {}).get("top") if isinstance(locations, dict) else None
        items = []
        for post in self.posts:
            if post["post_type"] != "nav_menu_item" or post["post_status"] != "publish":
                continue
            taxonomy_ids = self.relationships.get(post["ID"], [])
            terms = [self.taxonomy[t]["term_id"] for t in taxonomy_ids if t in self.taxonomy]
            if menu_term is not None and menu_term not in terms:
                continue
            meta = self.postmeta[post["ID"]]
            items.append({
                "id": post["ID"], "title": post["post_title"], "order": post["menu_order"],
                "kind": meta.get("_menu_item_type", ""), "object": meta.get("_menu_item_object", ""),
                "object_id": int(meta.get("_menu_item_object_id") or 0), "url": meta.get("_menu_item_url", ""),
                "parent": int(meta.get("_menu_item_menu_item_parent") or 0),
            })
        return sorted(items, key=lambda i: (i["order"], i["id"]))


# --------------------------------------------------------------------------------------------
# wpautop and shortcodes
# --------------------------------------------------------------------------------------------

_ALLBLOCKS = (
    r"(?:table|thead|tfoot|caption|col|colgroup|tbody|tr|td|th|div|dl|dd|dt|ul|ol|li|pre|form|map|area|"
    r"blockquote|address|math|style|p|h[1-6]|hr|fieldset|legend|section|article|aside|header|footer|hgroup|"
    r"figure|figcaption|details|menu|summary)"
)


# [@ANCHOR: wordpress_to_odoo:wpautop]
# Verified by [@ANCHOR: test_wordpress_to_odoo:wpautop]
def wpautop(text, br=True):
    """A port of WordPress's wpautop() (wp-includes/formatting.php): blank lines become paragraphs and
    single newlines become <br />, except around block-level tags and inside <pre>."""
    if text.strip() == "":
        return ""
    text = text + "\n"
    pre_tags = {}
    if "<pre" in text:
        parts = text.split("</pre>")
        last = parts.pop()
        text = ""
        for index, part in enumerate(parts):
            start = part.find("<pre")
            if start == -1:
                text += part
                continue
            name = f"<pre wp-pre-tag-{index}></pre>"
            pre_tags[name] = part[start:] + "</pre>"
            text += part[:start] + name
        text += last
    text = re.sub(r"<br\s*/?>\s*<br\s*/?>", "\n\n", text)
    text = re.sub(r"(<" + _ALLBLOCKS + r"[\s/>])", r"\n\n\1", text)
    text = re.sub(r"(</" + _ALLBLOCKS + r">)", r"\1\n\n", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # newlines inside a tag (attributes) are not paragraph breaks
    text = re.sub(r"<[^<>]*>", lambda m: m.group(0).replace("\n", " <!-- wpnl --> "), text)
    text = re.sub(r"\n\n+", "\n\n", text)
    pieces = [p for p in re.split(r"\n\s*\n", text) if p != ""]
    text = "".join("<p>" + p.strip("\n") + "</p>\n" for p in pieces)
    text = re.sub(r"<p>\s*</p>", "", text)
    text = re.sub(r"<p>([^<]+)</(div|address|form)>", r"<p>\1</p></\2>", text)
    text = re.sub(r"<p>\s*(</?" + _ALLBLOCKS + r"[^>]*>)\s*</p>", r"\1", text)
    text = re.sub(r"<p>(<li.+?)</p>", r"\1", text)
    text = re.sub(r"<p><blockquote([^>]*)>", r"<blockquote\1><p>", text, flags=re.I)
    text = text.replace("</blockquote></p>", "</p></blockquote>")
    text = re.sub(r"<p>\s*(</?" + _ALLBLOCKS + r"[^>]*>)", r"\1", text)
    text = re.sub(r"(</?" + _ALLBLOCKS + r"[^>]*>)\s*</p>", r"\1", text)
    if br:
        text = re.sub(r"<(script|style|svg|math).*?</\1>", lambda m: m.group(0).replace("\n", "<WPPreserveNewline />"),
                      text, flags=re.S)
        text = text.replace("<br>", "<br />").replace("<br/>", "<br />")
        text = re.sub(r"(?<!<br />)\s*\n", "<br />\n", text)
        text = text.replace("<WPPreserveNewline />", "\n")
    text = re.sub(r"(</?" + _ALLBLOCKS + r"[^>]*>)\s*<br />", r"\1", text)
    text = re.sub(r"<br />(\s*</?(?:p|li|div|dl|dd|dt|th|pre|td|ul|ol)[^>]*>)", r"\1", text)
    text = re.sub(r"\n</p>$", "</p>", text)
    for name, original in pre_tags.items():
        text = text.replace(name, original)
    text = text.replace(" <!-- wpnl --> ", "\n").replace("<!-- wpnl -->", "\n")
    return text


_BLOCK_COMMENT = re.compile(r"<!--\s*/?wp:[^>]*?-->\s*", re.S)
_ATTR_RE = re.compile(r"""([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'\]]+))""")


def shortcode_attrs(text):
    return {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ATTR_RE.finditer(text)}


_CAPTION = re.compile(r"\[caption([^\]]*)\](.*?)\[/caption\]", re.S)
_VIDEO = re.compile(r"\[video([^\]]*)\]\s*(?:\[/video\])?", re.S)
_AUDIO = re.compile(r"\[audio([^\]]*)\]\s*(?:\[/audio\])?", re.S)


def expand_shortcodes(text, stats):
    """The shortcodes WordPress core registers that appear in classic content: caption, video, audio.
    Anything else in square brackets is plain text, exactly as WordPress treats an unregistered shortcode."""
    text = re.sub(r"<p>\s*(\[(?:caption|video|audio)\b.*?(?:\[/(?:caption|video|audio)\]|\]))\s*(?:<br />)?\s*</p>",
                  r"\1", text, flags=re.S)

    def caption(match):
        stats["shortcode_caption"] += 1
        attrs = shortcode_attrs(match.group(1))
        inner = match.group(2).strip()
        image = re.match(r"^(<a\b[^>]*>\s*<img\b[^>]*>\s*</a>|<img\b[^>]*>)\s*(.*)$", inner, re.S)
        if not image:
            return f"<p>{inner}</p>"
        align = attrs.get("align", "alignnone")
        text_part = re.sub(r"<br\s*/?>", " ", image.group(2)).strip()
        return (f'<figure class="wp2odoo-caption wp2odoo-{align}">{image.group(1)}'
                f"<figcaption>{text_part}</figcaption></figure>")

    def video(match):
        stats["shortcode_video"] += 1
        attrs = shortcode_attrs(match.group(1))
        src = attrs.get("src") or attrs.get("mp4") or attrs.get("webm") or attrs.get("ogv") or attrs.get("m4v")
        if not src:
            return ""
        size = "".join(f' {k}="{attrs[k]}"' for k in ("width", "height") if attrs.get(k, "").isdigit())
        return f'<video controls="controls" preload="metadata"{size} src="{htmllib.escape(src, quote=True)}"></video>'

    def audio(match):
        stats["shortcode_audio"] += 1
        attrs = shortcode_attrs(match.group(1))
        src = attrs.get("src") or attrs.get("mp3") or attrs.get("ogg") or attrs.get("wav") or attrs.get("m4a")
        if not src:
            return ""
        return f'<audio controls="controls" preload="none" src="{htmllib.escape(src, quote=True)}"></audio>'

    text = _CAPTION.sub(caption, text)
    text = _VIDEO.sub(video, text)
    return _AUDIO.sub(audio, text)


# --------------------------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------------------------

ALLOWED_TAGS = frozenset(
    "a abbr audio b blockquote br caption cite code col colgroup dd del dfn div dl dt em figcaption figure "
    "h1 h2 h3 h4 h5 h6 hr i img ins kbd li mark ol p pre q s samp small span strong sub sup table tbody td tfoot "
    "th thead tr u ul var video wbr".split()
)
# Elements removed together with everything inside them.
DROP_WITH_CONTENT = frozenset(
    "script style iframe object embed noscript form input button select textarea svg math link meta base "
    "applet template head title frame frameset canvas".split()
)
ALLOWED_ATTRS = {
    "a": {"href", "title", "target", "rel", "id", "name"},
    "img": {"src", "alt", "title", "width", "height", "loading"},
    "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan", "scope"},
    "col": {"span"}, "colgroup": {"span"},
    "ol": {"start", "reversed", "type"},
    "video": {"controls", "preload", "src", "width", "height", "poster"},
    "audio": {"controls", "preload", "src"},
    "blockquote": {"cite"}, "q": {"cite"},
    "abbr": {"title"}, "dfn": {"title"},
}
HEADING_IDS = {f"h{i}" for i in range(1, 7)}
SAFE_URL = re.compile(r"^(?:https?:|mailto:|tel:|ftp:|/(?!/)|#|\.\.?/|[\w%~.-]+(?:/|$))", re.I)
SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_:.-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TRACKING_IMAGE = re.compile(r"cleardot\.gif|/pixel\b|1x1\.(?:gif|png)|spacer\.gif", re.I)
ALIGN_CLASSES = {
    "alignleft": "float-start me-3 mb-2", "alignright": "float-end ms-3 mb-2",
    "aligncenter": "mx-auto d-block text-center", "alignnone": "",
}


def _drop(element):
    """Removes an element and its content, keeping its tail text."""
    parent = element.getparent()
    if parent is None:
        return
    tail = element.tail
    previous = element.getprevious()
    if tail:
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(element)


def _unwrap(element):
    """Replaces an element by its children and text (the tag disappears, the content stays)."""
    parent = element.getparent()
    if parent is None:
        return
    index = parent.index(element)
    previous = element.getprevious()
    lead = element.text or ""
    if lead:
        if previous is not None:
            previous.tail = (previous.tail or "") + lead
        else:
            parent.text = (parent.text or "") + lead
    children = list(element)
    for offset, child in enumerate(children):
        parent.insert(index + offset, child)
    tail = element.tail or ""
    parent.remove(element)
    if tail:
        last = children[-1] if children else previous
        if last is not None:
            last.tail = (last.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail


def unwrap_google_redirect(href):
    """Links pasted from Gmail go through https://www.google.com/url?q=TARGET&sa=D...; use TARGET."""
    parts = urllib.parse.urlsplit(href)
    if parts.hostname in ("www.google.com", "google.com") and parts.path == "/url":
        target = urllib.parse.parse_qs(parts.query).get("q") or urllib.parse.parse_qs(parts.query).get("url")
        if target and target[0].startswith(("http://", "https://")):
            return target[0]
    return href


# [@ANCHOR: wordpress_to_odoo:cleaner]
# Verified by [@ANCHOR: test_wordpress_to_odoo:cleaner]
class Cleaner:
    """Rebuilds HTML from an allowlist. Everything not on a list is dropped or unwrapped."""

    def __init__(self, domains, stats, link_map=None):
        self.domains = [d.lower() for d in domains]
        self.stats = stats
        self.link_map = link_map  # callable(url) -> url (internal link and media rewriting)

    def is_internal(self, href):
        host = urllib.parse.urlsplit(href).hostname
        return bool(host) and host.lower() in self.domains

    def clean_url(self, value, attr):
        value = _CONTROL.sub("", htmllib.unescape(value or "")).strip()
        value = re.sub(r"[\s\x00-\x20]+", " ", value) if " " in value else value
        compact = re.sub(r"[\s\x00-\x20]", "", value)
        if not value:
            return None
        if re.match(r"^[a-z][a-z0-9+.-]*:", compact, re.I) and not re.match(r"^(https?|mailto|tel|ftp):", compact, re.I):
            self.stats["unsafe_urls_removed"] += 1
            return None
        if compact.startswith("//"):
            value = "https:" + compact
        if not SAFE_URL.match(value):
            self.stats["unsafe_urls_removed"] += 1
            return None
        if attr == "href":
            value = unwrap_google_redirect(value)
        return self.link_map(value) if self.link_map else value

    def clean_element(self, element):
        tag = element.tag.lower() if isinstance(element.tag, str) else None
        if tag is None:  # comment, processing instruction
            _drop(element)
            return
        if tag in DROP_WITH_CONTENT:
            self.stats["removed_" + tag] += 1
            _drop(element)
            return
        for child in list(element):
            self.clean_element(child)
        if tag not in ALLOWED_TAGS:
            if tag not in ("html", "body", "article", "header", "section", "footer", "aside", "nav", "main", "font",
                           "center", "o:p", "dir", "tt", "big", "strike"):
                self.stats["unwrapped_" + tag] += 1
            if tag in ("strike",):
                element.tag = "s"
            elif tag in ("tt",):
                element.tag = "code"
            elif tag in ("center",):
                element.tag = "div"
                element.set("class", "text-center")
            else:
                _unwrap(element)
                return
            tag = element.tag
        classes = []
        original_classes = (element.get("class") or "").split()
        original_align = (element.get("align") or "").lower()
        for name in list(element.attrib):
            lower = name.lower()
            keep = ALLOWED_ATTRS.get(tag, set())
            if lower == "id" and tag in HEADING_IDS and SAFE_ID.match(element.get(name) or ""):
                continue
            if lower not in keep:
                del element.attrib[name]
                if lower.startswith("on"):
                    self.stats["event_handlers_removed"] += 1
                elif lower == "style":
                    self.stats["style_attributes_removed"] += 1
                continue
            if lower in ("href", "src", "poster", "cite"):
                cleaned = self.clean_url(element.get(name), lower)
                if cleaned is None:
                    del element.attrib[name]
                else:
                    element.set(name, cleaned)
            elif lower == "id" and not SAFE_ID.match(element.get(name) or ""):
                del element.attrib[name]
            elif lower == "name" and not SAFE_ID.match(element.get(name) or ""):
                del element.attrib[name]
            elif lower in ("width", "height", "colspan", "rowspan", "span", "start") and not re.match(
                    r"^\d{1,5}$", (element.get(name) or "").strip()):
                del element.attrib[name]
        if tag == "a":
            if not element.get("href") and not element.get("id") and not element.get("name"):
                _unwrap(element)
                return
            # A link whose visible text is a URL of the old site: show it without the scheme. The importer
            # turns every absolute link to an old domain into a relative one, text included, and a visible
            # "/static/RV.pdf" is no use to a reader.
            if len(element) == 0 and element.text:
                shown = re.match(r"^\s*https?://((?:[\w-]+\.)*(?:%s)(?:[/?#]\S*)?)\s*$" % "|".join(
                    re.escape(d) for d in self.domains), element.text, re.I)
                if shown:
                    element.text = shown.group(1)
            if element.get("href") and not element.get("href").startswith(("#", "/", "mailto:", "tel:")) \
                    and not self.is_internal(element.get("href")):
                element.set("rel", "noopener noreferrer")
            elif "rel" in element.attrib:
                del element.attrib["rel"]
            if element.get("target") not in (None, "_blank"):
                del element.attrib["target"]
        if tag == "img":
            src = element.get("src") or ""
            if not src or TRACKING_IMAGE.search(src):
                self.stats["tracking_images_removed"] += 1
                _drop(element)
                return
            classes.append("img-fluid")
            for name in original_classes:
                if name in ALIGN_CLASSES and ALIGN_CLASSES[name]:
                    classes.append(ALIGN_CLASSES[name])
            if original_align in ("left", "right") and not any(c in classes for c in ("float-start", "float-end")):
                classes.append("float-start me-3 mb-2" if original_align == "left" else "float-end ms-3 mb-2")
            element.set("loading", "lazy")
            if "alt" not in element.attrib:
                element.set("alt", "")
        elif tag == "figure":
            classes.append("figure")
            for name in original_classes:
                if name.startswith("wp2odoo-") and ALIGN_CLASSES.get(name[8:]):
                    classes.append(ALIGN_CLASSES[name[8:]])
            if "wp-block-image" in original_classes:
                classes.append("text-center")
        elif tag == "figcaption":
            classes.append("figure-caption")
        elif tag == "table":
            classes.extend(["table", "table-bordered", "table-sm"])
        elif tag == "blockquote":
            classes.append("blockquote")
        elif tag == "div" and "text-center" in (element.get("class") or ""):
            classes.append("text-center")
        elif tag in ("video", "audio"):
            classes.append("w-100" if tag == "video" else "")
            element.set("controls", "controls")
        classes = [c for c in " ".join(classes).split() if c]
        if classes:
            element.set("class", " ".join(dict.fromkeys(classes)))
        elif "class" in element.attrib:
            del element.attrib["class"]
        if original_classes:
            self.stats["wordpress_classes_removed"] += sum(1 for c in original_classes if c.startswith(("wp-", "size-", "align")))

    def clean_fragment(self, html_text):
        """HTML text -> list of clean top-level nodes inside a parent element (a <div>)."""
        root = lxml_html.fragment_fromstring(html_text, create_parent="div")
        for child in list(root):
            self.clean_element(child)
        # Empty paragraphs and wrappers left behind
        for element in list(root.iter("p", "div", "span")):
            if (element.text or "").strip() == "" and len(element) == 0 and element is not root and element.get("class") is None:
                _drop(element)
        return root


def escape_text_for_qweb(root):
    """QWeb writes a static text node into the page raw, unescaped. A text node holding `<` is therefore
    live markup. Every `&`, `<` and `>` in text is spelled as the character entity, so the node holds the
    entity text and the browser shows the character. (The same rule as the user_websites arch sanitizer.)"""
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        if node.text:
            node.text = htmllib.escape(node.text, quote=False)
        if node.tail:
            node.tail = htmllib.escape(node.tail, quote=False)


def serialize_children(root, qweb):
    if qweb:
        escape_text_for_qweb(root)
        parts = [root.text or ""]
        parts += [etree.tostring(child, encoding="unicode", method="xml") for child in root]
        return "".join(parts)
    parts = [htmllib.escape(root.text or "", quote=False)]
    parts += [etree.tostring(child, encoding="unicode", method="html") for child in root]
    return "".join(parts)


# --------------------------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------------------------

UPLOAD_PATH = re.compile(r"^/wp-content/uploads/(?:sites/\d+/)?(.+)$")
RESIZED = re.compile(r"^(.*)-\d{1,5}x\d{1,5}(\.[A-Za-z0-9]+)$")
IMAGE_TYPES = ("image/",)
MEDIA_CAP_BYTES = mig.BINARY_CAP_MB * 1048576


class Media:
    """Finds the files posts reference under /wp-content/uploads/ and turns each found file into an attachment."""

    def __init__(self, uploads_dirs, stats):
        self.dirs = [d for d in uploads_dirs if d]
        self.stats = stats
        self.attachments = {}  # relative path -> row (the export row, id assigned in order)
        self.files = {}        # attachment id -> source path
        self.missing = collections.defaultdict(set)  # url path -> {post ids}
        self.variants = collections.defaultdict(set)  # relative path -> every URL path content used for it
        self.next_id = 1

    def find(self, relative):
        """The file for a path relative to the uploads directory, or None. Resized copies (name-300x200.jpg)
        resolve to the original when the original exists; the generated copy is used only when it is all there is."""
        candidates = [relative]
        resized = RESIZED.match(relative)
        if resized:
            candidates.insert(0, resized.group(1) + resized.group(2))
        for base in self.dirs:
            for candidate in candidates:
                for prefix in ("", "sites/4/", "sites/8/"):
                    path = os.path.join(base, prefix + candidate)
                    if os.path.isfile(path):
                        return os.path.realpath(path), candidate
        return None

    def resolve(self, url_path, referrer):
        """Returns the new relative URL for an upload, or None when the snapshot has no such file."""
        match = UPLOAD_PATH.match(urllib.parse.unquote(url_path))
        if not match:
            return None
        found = self.find(match.group(1))
        if not found:
            self.missing[url_path].add(referrer)
            return None
        path, relative = found
        self.variants[relative].add(url_path)
        if relative in self.attachments:
            row = self.attachments[relative]
        else:
            size = os.path.getsize(path)
            if size > MEDIA_CAP_BYTES:
                self.stats["media_too_large"] += 1
                self.missing[url_path].add(referrer)
                return None
            with open(path, "rb") as handle:  # audit-ignore-path
                data = handle.read()
            mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            row = {
                "id": self.next_id, "name": os.path.basename(relative), "type": "binary", "mimetype": mimetype,
                "public": True, "website_id": [SITE_ID, "site"], "file_size": size,
                "checksum": hashlib.sha1(data).hexdigest(), "res_model": False, "res_id": 0,
                "wordpress_path": relative,
            }
            self.attachments[relative] = row
            self.files[row["id"]] = path
            self.next_id += 1
        if row["mimetype"].startswith(IMAGE_TYPES):
            return f"/web/image/{row['id']}"
        return f"/web/content/{row['id']}/{urllib.parse.quote(row['name'])}?download=true"


# --------------------------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------------------------

INJECTED_SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)
SKIPPED_TYPES = {
    "revision": "revision (edit history)", "auto-draft": "auto-draft", "wp_navigation": "block-theme navigation",
    "wp_global_styles": "block-theme styles", "wp_template": "block-theme template",
    "wp_template_part": "block-theme template part", "nav_menu_item": "menu item (rebuilt as an Odoo menu)",
    "customize_changeset": "customizer draft", "oembed_cache": "cache", "attachment": "attachment record",
}
PERMALINK_TOKENS = {
    "%year%": r"(?P<year>\d{4})", "%monthnum%": r"(?P<month>\d{2})", "%day%": r"(?P<day>\d{2})",
    "%hour%": r"\d{2}", "%minute%": r"\d{2}", "%second%": r"\d{2}", "%postname%": r"(?P<slug>[^/]+)",
    "%post_id%": r"(?P<post_id>\d+)", "%category%": r"[^/]+(?:/[^/]+)*", "%author%": r"[^/]+",
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
BARE_URL_LINE = re.compile(r"^\s*(https?://[^\s<>\"]+)\s*$", re.M)
WP_DATE_ZERO = "0000-00-00 00:00:00"


def wp_datetime(value):
    if not value or value == WP_DATE_ZERO:
        return None
    return value


# [@ANCHOR: wordpress_to_odoo:converter]
# Verified by [@ANCHOR: test_wordpress_to_odoo:converter]
class Converter:
    def __init__(self, site, domains=("perens.com",), static_root=None, uploads_dirs=(), blog_name=DEFAULT_BLOG_NAME,
                 include_unpublished=False):
        self.site = site
        self.domains = sorted({d.lower() for d in domains} | {
            urllib.parse.urlsplit(site.option("home")).hostname or "",
            urllib.parse.urlsplit(site.option("siteurl")).hostname or ""} - {""})
        self.static_root = static_root
        self.blog_name = blog_name
        self.include_unpublished = include_unpublished
        self.stats = collections.Counter()
        self.media = Media(uploads_dirs, self.stats)
        self.posts_by_id = {p["ID"]: p for p in site.posts}
        self.unresolved_links = collections.defaultdict(set)
        self.static_refs = collections.defaultdict(set)
        self.external_hosts = collections.Counter()
        self.redirect_plugin = {}  # post id -> target (Quick Page/Post Redirect meta)
        self.items = {"pages": [], "posts": []}
        self.dropped = []
        self.sitemap = []
        self.rewrites = {}
        self.new_url = {}  # post id -> new relative URL
        self.current = None
        self._plan()

    # -- planning: which records exist, with which old and new URLs
    def _page_path(self, post):
        parts, seen, node = [], set(), post
        while node is not None and node["ID"] not in seen:
            seen.add(node["ID"])
            parts.append(urllib.parse.quote(node["post_name"] or str(node["ID"])))
            node = self.posts_by_id.get(node["post_parent"]) if node["post_parent"] else None
        return "/" + "/".join(reversed(parts))

    def _permalink(self, post):
        structure = self.site.permalink_structure
        date = wp_datetime(post["post_date"]) or "1970-01-01 00:00:00"
        year, month, day = date[0:4], date[5:7], date[8:10]
        if not structure:
            return f"/?p={post['ID']}"
        path = structure
        for token, value in (("%year%", year), ("%monthnum%", month), ("%day%", day),
                             ("%postname%", urllib.parse.quote(post["post_name"])), ("%post_id%", str(post["ID"])),
                             ("%hour%", date[11:13]), ("%minute%", date[14:16]), ("%second%", date[17:19])):
            path = path.replace(token, value)
        if "%" in path:
            self.stats["permalink_unsupported_token"] += 1
        return path

    def _permalink_regex(self):
        structure = self.site.permalink_structure
        if not structure:
            return None
        pattern = re.escape(structure)
        for token, regex in PERMALINK_TOKENS.items():
            pattern = pattern.replace(re.escape(token), regex)
        pattern = pattern.rstrip("/") if False else pattern
        return re.compile(r"^(?:/blog)?" + pattern.rstrip("/") + r"/?$")

    def _plan(self):
        site = self.site
        self.permalink_re = self._permalink_regex()
        self.slug_index = {}
        self.old_slugs = {}
        self.skipped_ids = set()
        for post in site.posts:
            meta = site.postmeta.get(post["ID"], {})
            if post["post_type"] not in ("post", "page"):
                reason = SKIPPED_TYPES.get(post["post_type"], f"custom post type {post['post_type']!r}")
                self.stats["skipped_" + post["post_type"]] += 1
                continue
            status = post["post_status"]
            if post["post_type"] == "page" and post["post_name"] == "sample-page" and \
                    _BLOCK_COMMENT.sub("", post["post_content"] or "").lstrip().removeprefix("<p>").startswith(
                        "This is an example page"):
                self.dropped.append({"id": post["ID"], "type": "page", "title": post["post_title"], "status": status,
                                     "reason": "WordPress's own placeholder page"})
                self.stats["skipped_default_content"] += 1
                self.skipped_ids.add(post["ID"])
                continue
            if status == "publish" and not post["post_password"]:
                keep = True
            else:
                keep = self.include_unpublished
                reason = f"status {status}" + (" (password protected)" if post["post_password"] else "")
                self.dropped.append({"id": post["ID"], "type": post["post_type"], "title": post["post_title"],
                                     "status": status, "reason": reason + ("" if not keep else "; imported unpublished")})
            if not keep:
                self.stats["skipped_unpublished"] += 1
                self.skipped_ids.add(post["ID"])
                continue
            if post["post_type"] == "post":
                self.slug_index[post["post_name"]] = post["ID"]
                if meta.get("_wp_old_slug"):
                    self.old_slugs[meta["_wp_old_slug"]] = post["ID"]
            target = meta.get("_pprredirect_url")
            if str(meta.get("_pprredirect_active")) == "1" and target:
                self.redirect_plugin[post["ID"]] = target
        self.page_url = {}
        for post in site.posts:
            if post["post_type"] == "page" and post["ID"] not in self.skipped_ids:
                self.page_url[post["ID"]] = self._page_path(post)

    def post_new_url(self, post):
        slug = mig.slugify(post["post_title"]) or "post"
        return f"/blog/{mig.slugify(self.blog_name) or 'blog'}-{BLOG_ID}/{slug}-{post['ID']}"

    # -- link and media mapping
    def map_url(self, url):
        """Rewrites a URL found in content. Internal permalinks, pages and uploads become the new URLs;
        external URLs are left alone."""
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme and parts.scheme not in ("http", "https"):
            return url
        if host and host not in self.domains:
            self.external_hosts[host] += 1
            return url
        path = parts.path or "/"
        suffix = ("#" + parts.fragment) if parts.fragment else ""
        if path.startswith("/wp-content/uploads/"):
            new = self.media.resolve(path, self.current)
            if new:
                return new + suffix
            self.stats["upload_refs_missing"] += 1
            return path + (("?" + parts.query) if parts.query and "wp-nocache" not in parts.query else "") + suffix
        if path.startswith("/static/") or path == "/static":
            self.static_refs[urllib.parse.unquote(path)].add(self.current)
            return path + suffix
        target = self.lookup_internal(path)
        if target:
            return target + suffix
        if path == "/" or (host and path in ("", "/")):
            return "/" + suffix
        if not host and not parts.path:  # a pure fragment or query
            return url
        self.unresolved_links[path].add(self.current)
        return path + suffix

    def lookup_internal(self, path):
        decoded = urllib.parse.unquote(path)
        if self.permalink_re:
            match = self.permalink_re.match(path) or self.permalink_re.match(decoded)
            if match and "slug" in match.groupdict():
                slug = urllib.parse.unquote(match.group("slug"))
                post_id = self.slug_index.get(slug) or self.old_slugs.get(slug)
                if post_id:
                    return self.post_new_url(self.posts_by_id[post_id])
            if match and "post_id" in match.groupdict():
                post = self.posts_by_id.get(int(match.group("post_id")))
                if post and post["post_type"] == "post":
                    return self.post_new_url(post)
        trimmed = path.rstrip("/") or "/"
        for page_id, url in self.page_url.items():
            if url == trimmed or urllib.parse.unquote(url) == urllib.parse.unquote(trimmed):
                post = self.posts_by_id[page_id]
                if page_id in self.redirect_plugin:
                    return self._redirect_target(self.redirect_plugin[page_id])
                return url
        return None

    def _redirect_target(self, target):
        parts = urllib.parse.urlsplit(target)
        if (parts.hostname or "").lower() in self.domains:
            return parts.path + (("?" + parts.query) if parts.query else "")
        return target

    # -- content
    def convert_content(self, post, qweb):
        self.current = post["ID"]
        raw = post["post_content"] or ""
        injected = INJECTED_SCRIPT.findall(raw)
        if injected:
            self.stats["injected_scripts_removed"] += len(injected)
            self.stats["posts_with_scripts"] += 1
        raw = INJECTED_SCRIPT.sub("", raw)
        has_blocks = "<!-- wp:" in raw
        if has_blocks:
            self.stats["gutenberg_posts"] += 1
        raw = _BLOCK_COMMENT.sub("", raw)
        for url in BARE_URL_LINE.findall(raw):
            if re.search(r"youtube\.com|youtu\.be|vimeo\.com|twitter\.com|soundcloud\.com|flickr\.com", url):
                self.stats["oembed_urls_left_as_links"] += 1
        raw = BARE_URL_LINE.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', raw)
        stats = collections.Counter()
        text = expand_shortcodes(wpautop(raw), stats)
        for key, value in stats.items():
            self.stats[key] += value
        cleaner = Cleaner(self.domains, self.stats, self.map_url)
        root = cleaner.clean_fragment(text)
        for element in root.iter("img"):
            src = element.get("src") or ""
            if src.startswith("/wp-content/uploads/"):
                self.stats["images_without_file"] += 1
        return serialize_children(root, qweb)

    def convert_all(self):
        site = self.site
        data = {m: [] for m in ("website", "ir.ui.view", "website.page", "website.menu", "website.rewrite", "blog.blog",
                                "blog.tag", "blog.post", "ir.attachment")}
        website_row = {"id": SITE_ID, "name": site.option("blogname"), "homepage_url": "/blog"}
        data["website"].append(website_row)
        data["blog.blog"].append({"id": BLOG_ID, "name": self.blog_name, "subtitle": site.option("blogdescription") or False})
        pages = [p for p in site.posts if p["post_type"] == "page"]
        posts = [p for p in site.posts if p["post_type"] == "post"]
        redirects = []  # (old_path, new_target, source description)

        for post in sorted(pages, key=lambda p: p["ID"]):
            if post["ID"] not in self.page_url:
                if post["ID"] in self.skipped_ids and post["post_status"] == "publish":
                    note = "WordPress's own placeholder page"
                else:
                    note = f"page not published ({post['post_status']})"
                self.sitemap.append({"old_url": self.page_url_old(post), "new_url": "", "status": "dropped",
                                     "note": note, "kind": "page", "id": post["ID"]})
                continue
            url = self.page_url[post["ID"]]
            old = url + "/"
            if post["ID"] in self.redirect_plugin:
                target = self._redirect_target(self.redirect_plugin[post["ID"]])
                self.stats["pages_redirected_by_plugin"] += 1
                redirects.append((url, target))
                self.sitemap.append({"old_url": old, "new_url": target, "status": "redirect-301", "kind": "page",
                                     "id": post["ID"], "note": "WordPress redirect plugin: the page was never shown"})
                continue
            body = self.convert_content(post, qweb=True)
            title = post["post_title"] or url.strip("/")
            arch = self.page_arch(post, title, body)
            view_id = post["ID"]
            data["ir.ui.view"].append({
                "id": view_id, "name": title, "key": f"website.wp_{post['ID']}_{mig.slugify(post['post_name']) or post['ID']}",
                "type": "qweb", "arch_db": arch, "website_id": [SITE_ID, "site"], "active": True, "mode": "primary",
            })
            data["website.page"].append({
                "id": post["ID"], "url": urllib.parse.unquote(url), "view_id": [view_id, title], "is_published": True,
                "website_id": [SITE_ID, "site"], "website_indexed": True,
                "date_publish": wp_datetime(post["post_date_gmt"]) or wp_datetime(post["post_date"]),
            })
            if urllib.parse.unquote(url) != url:
                redirects.append((url, urllib.parse.unquote(url)))
            self.stats["pages"] += 1
            if not post["post_content"].strip():
                self.stats["pages_empty"] += 1
            self.sitemap.append({"old_url": old, "new_url": urllib.parse.unquote(url), "status": "kept", "kind": "page",
                                 "id": post["ID"], "note": "same path; the trailing-slash form redirects"})
            self.items["pages"].append({"id": post["ID"], "title": title, "url": url})

        for post in sorted(posts, key=lambda p: (p["post_date"], p["ID"])):
            published = post["post_status"] == "publish" and not post["post_password"]
            old = self._permalink(post)
            if not published and not self.include_unpublished:
                self.sitemap.append({"old_url": old, "new_url": "", "status": "dropped", "kind": "post", "id": post["ID"],
                                     "note": f"not published ({post['post_status']})"})
                continue
            if post["ID"] in self.redirect_plugin:
                target = self._redirect_target(self.redirect_plugin[post["ID"]])
                self.stats["posts_redirected_by_plugin"] += 1
                redirects.extend(self._permalink_variants(old, target))
                self.sitemap.append({"old_url": old, "new_url": target, "status": "redirect-301", "kind": "post",
                                     "id": post["ID"], "note": "WordPress redirect plugin: the post was never shown"})
                continue
            body = self.convert_content(post, qweb=False)
            if not (post["post_title"] or "").strip() and not _normal_text(lxml_html.fragment_fromstring(
                    body or "<p></p>", create_parent="div").text_content()):
                self.stats["skipped_empty_posts"] += 1
                self.dropped.append({"id": post["ID"], "type": "post", "title": "", "status": post["post_status"],
                                     "reason": "published but has no title and no text"})
                self.sitemap.append({"old_url": old, "new_url": "", "status": "dropped", "kind": "post", "id": post["ID"],
                                     "note": "empty untitled post"})
                continue
            new_url = self.post_new_url(post)
            self.new_url[post["ID"]] = new_url
            date = wp_datetime(post["post_date_gmt"]) or wp_datetime(post["post_date"])
            data["blog.post"].append({
                "id": post["ID"], "name": post["post_title"] or f"Post {post['ID']}", "blog_id": [BLOG_ID, self.blog_name],
                "content": body, "is_published": published, "website_published": published, "post_date": date,
                "published_date": date if published else False, "author_id": [post["post_author"], "Bruce Perens"],
                "tag_ids": [], "website_id": [SITE_ID, "site"], "cover_properties": COVER_PROPERTIES,
            })
            redirects.extend(self._permalink_variants(old, new_url))
            self.stats["posts"] += 1
            self.sitemap.append({"old_url": old, "new_url": new_url, "status": "redirect-301", "kind": "post", "id": post["ID"],
                                 "note": "Odoo blog URLs carry the record id"})
            self.items["posts"].append({"id": post["ID"], "title": post["post_title"], "date": date, "old": old})
        self.stats["posts_with_comments_open"] = sum(1 for p in posts if p["comment_status"] == "open")

        # Menu
        data["website.menu"].append({"id": 1, "name": ROOT_MENU_NAME, "url": "#", "parent_id": False, "sequence": 0,
                                     "website_id": [SITE_ID, "site"]})
        for index, item in enumerate(self.site.nav_menu_items()):
            url, title = None, item["title"]
            self.current = f"menu{item['id']}"
            if item["kind"] == "post_type" and item["object_id"] in self.posts_by_id:
                target = self.posts_by_id[item["object_id"]]
                title = title or target["post_title"]
                if item["object_id"] in self.redirect_plugin:
                    url = self._redirect_target(self.redirect_plugin[item["object_id"]])
                elif target["post_type"] == "page" and item["object_id"] in self.page_url:
                    url = urllib.parse.unquote(self.page_url[item["object_id"]])
                elif item["object_id"] in self.new_url:
                    url = self.new_url[item["object_id"]]
            elif item["kind"] == "custom":
                url = self.map_url(item["url"])
            if not url:
                self.dropped.append({"id": item["id"], "type": "menu item", "title": title, "status": "publish",
                                     "reason": "its target is not imported"})
                continue
            data["website.menu"].append({
                "id": item["id"], "name": title or "Link", "url": url, "parent_id": [1, ROOT_MENU_NAME],
                "sequence": 100 + 10 * index, "website_id": [SITE_ID, "site"], "new_window": False,
            })
            self.stats["menu_items"] += 1

        # WordPress's site feed lives at /feed/; Odoo's blog feed is /blog/<blog>/feed.
        blog_path = f"/blog/{mig.slugify(self.blog_name) or 'blog'}-{BLOG_ID}"
        redirects.append(("/feed", f"{blog_path}/feed"))
        # Redirects: old permalinks, trailing-slash variants, the old upload paths
        seen = set()
        for old, new in redirects:
            if old in seen or old == new:
                continue
            seen.add(old)
            data["website.rewrite"].append({
                "id": len(data["website.rewrite"]) + 1, "name": f"wordpress {old}"[:100], "url_from": old, "url_to": new,
                "redirect_type": "301", "website_id": [SITE_ID, "site"],
            })
        for relative, row in sorted(self.media.attachments.items(), key=lambda kv: kv[1]["id"]):
            for variant in sorted({f"/wp-content/uploads/sites/4/{relative}", f"/wp-content/uploads/sites/8/{relative}",
                                   f"/wp-content/uploads/{relative}"} | set(self.media.variants.get(relative, ()))):
                target = f"/web/image/{row['id']}" if row["mimetype"].startswith("image/") else \
                    f"/web/content/{row['id']}/{urllib.parse.quote(row['name'])}?download=true"
                data["website.rewrite"].append({
                    "id": len(data["website.rewrite"]) + 1, "name": f"wordpress upload {relative}"[:100],
                    "url_from": variant, "url_to": target, "redirect_type": "301", "website_id": [SITE_ID, "site"],
                })
            data["ir.attachment"].append({k: v for k, v in row.items() if k not in ("wordpress_path", "mimetype")})
        self.stats["redirects"] = len(data["website.rewrite"])
        self.stats["attachments"] = len(data["ir.attachment"])
        return data

    def page_url_old(self, post):
        return self._page_path(post) + "/"

    def _permalink_variants(self, old, new):
        # Odoo answers any URL with a trailing slash by a 301 to the same URL without it, before redirect rules
        # apply (measured: /zzz/ -> /zzz), so only the slash-less forms need rules.
        bare = old.rstrip("/")
        return [(bare, new), ("/blog" + bare, new)]

    def page_arch(self, post, title, body):
        name = htmllib.escape(title, quote=True)
        key_slug = f"wp_{post['ID']}_{mig.slugify(post['post_name']) or post['ID']}"
        heading = htmllib.escape(title, quote=False)
        heading = htmllib.escape(heading, quote=False)  # QWeb text is written raw: escape twice (see escape_text_for_qweb)
        return (
            f'<t name="{name}" t-name="website.{key_slug}"><t t-call="website.layout">'
            f'<t t-set="pageName" t-value="\'{key_slug}\'"/>'
            f'<div id="wrap" class="oe_structure oe_empty"><section class="s_text_block pt24 pb48">'
            f'<div class="container"><div class="row"><div class="col-lg-10 offset-lg-1">'
            f"<h1>{heading}</h1>{body}"
            f"</div></div></div></section></div></t></t>"
        )


# --------------------------------------------------------------------------------------------
# Safety check used by the tests and by `convert` before it writes anything
# --------------------------------------------------------------------------------------------

FORBIDDEN_TAGS = {"script", "iframe", "object", "embed", "base", "style", "form", "link", "meta", "svg"}


# [@ANCHOR: wordpress_to_odoo:check_arch_safe]
# Verified by [@ANCHOR: test_wordpress_to_odoo:check_arch_safe]
def check_arch_safe(arch, qweb=True):
    """Returns a list of problems: the page-arch sanitizer's rules of hams_open/user_websites
    (`_sanitize_user_arch`) applied to our own output, so an export can never contain what that sanitizer
    would strip: script/iframe/object/embed/base, on* handlers, javascript:/data:/vbscript: URLs, t-* directives
    other than the fixed page frame, and any text node holding `<`."""
    problems = []
    parser = etree.XMLParser(recover=False)
    try:
        root = etree.fromstring(f"<root>{arch}</root>", parser=parser)
    except etree.XMLSyntaxError as exc:
        return [f"not well-formed XML: {exc}"]
    for element in root.iter():
        if not isinstance(element.tag, str):
            problems.append("comment or processing instruction")
            continue
        tag = element.tag.lower()
        if ":" in tag:
            problems.append(f"namespaced element {tag}")
        if tag in FORBIDDEN_TAGS:
            problems.append(f"forbidden element <{tag}>")
        for name, value in element.attrib.items():
            lowered = name.lower()
            if lowered.startswith("on"):
                problems.append(f"event handler {name}")
            if lowered.startswith("t-") and not (tag == "t" and lowered in ("t-name", "t-call", "t-set", "t-value")):
                problems.append(f"QWeb directive {name} on <{tag}>")
            if lowered == "style":
                problems.append("style attribute")
            if lowered in ("href", "src", "poster", "cite", "action", "formaction", "data", "xlink:href") and re.match(
                    r"^\s*(javascript|data|vbscript)\s*:", re.sub(r"[\x00-\x20]", "", value), re.I):
                problems.append(f"dangerous URL in {name}")
        for text in (element.text, element.tail):
            if text and "<" in text:
                problems.append(f"raw '<' in text near {text[:40]!r}")
    return problems


def check_html_safe(content):
    """The same rules for a blog post's HTML (no QWeb directives at all, text may not hold a raw `<`)."""
    try:
        root = lxml_html.fragment_fromstring(content, create_parent="div")
    except etree.ParserError:
        return []
    problems = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            problems.append("comment")
            continue
        tag = element.tag.lower()
        if tag in FORBIDDEN_TAGS:
            problems.append(f"forbidden element <{tag}>")
        for name, value in element.attrib.items():
            lowered = name.lower()
            if lowered.startswith(("on", "t-")) or lowered == "style":
                problems.append(f"forbidden attribute {name}")
            if lowered in ("href", "src", "poster") and re.match(
                    r"^\s*(javascript|data|vbscript)\s*:", re.sub(r"[\x00-\x20]", "", value), re.I):
                problems.append(f"dangerous URL in {name}")
    return problems


# --------------------------------------------------------------------------------------------
# Rendered-page comparison
# --------------------------------------------------------------------------------------------


def _normal_text(text):
    text = htmllib.unescape(text or "").replace("\xa0", " ")
    text = re.sub(r"https?://((?:www|new)\.)?perens\.com", "perens.com", text)
    return re.sub(r"\s+", " ", text).strip()


def reference_text(converter, post):
    """The visible text the original theme would have shown for a post: the stored content after WordPress's
    own content filters (wpautop, the caption/video shortcodes), without scripts, as plain text."""
    raw = INJECTED_SCRIPT.sub("", post["post_content"] or "")
    raw = _BLOCK_COMMENT.sub("", raw)
    text = expand_shortcodes(wpautop(raw), collections.Counter())
    root = lxml_html.fragment_fromstring(text, create_parent="div")
    for element in list(root.iter("script", "style", "iframe", "form", "object", "embed")):
        _drop(element)
    return _normal_text(root.text_content())


def page_text_from_html(document, title_hint=None):
    root = lxml_html.fromstring(document)
    nodes = root.xpath("//main") or [root]
    for element in list(nodes[0].iter("script", "style", "noscript")):
        _drop(element)
    return _normal_text(nodes[0].text_content())


def compare_rendered(sql_path, base_url, blog_id=8, domains=("perens.com", "www.perens.com", "new.perens.com"),
                     fetch=None, log=print, uploads_dirs=(), blog_name=DEFAULT_BLOG_NAME):
    """Fetches every converted page and post from a running Odoo (posts through their OLD permalink, so the
    redirects are exercised too) and checks that the text the original would have shown is on the page.
    Returns a list of problems. Read-only: GET requests only."""
    import difflib
    import urllib.request

    site = WordPressSite(read_dump(sql_path), blog_id)
    converter = Converter(site, domains, None, uploads_dirs, blog_name)
    data = converter.convert_all()
    posts_by_id = converter.posts_by_id

    def default_fetch(url):
        request = urllib.request.Request(url, headers={"User-Agent": "wordpress_to_odoo-compare/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8", "replace"), response.geturl()
        except urllib.error.HTTPError as exc:
            return exc.code, "", url

    fetch = fetch or default_fetch
    problems, checked = [], 0
    targets = []
    for page in data["website.page"]:
        targets.append((page["id"], page["url"], "page"))
    for item in converter.items["posts"]:
        targets.append((item["id"], item["old"], "post"))
    for ident, path, kind in targets:
        status, body, final = fetch(base_url.rstrip("/") + path)
        if status != 200:
            problems.append(f"{kind} {ident} {path}: HTTP {status}")
            continue
        expected = reference_text(converter, posts_by_id[ident])
        actual = page_text_from_html(body)
        checked += 1
        if expected and expected not in actual:
            # Tracking pixels and removed elements change nothing visible; use a ratio for the remainder.
            matcher = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
            covered = sum(b.size for b in matcher.get_matching_blocks()) / max(1, len(expected))
            if covered < 0.995:
                problems.append(f"{kind} {ident} {path}: only {covered:.1%} of the original text is on the page")
        if kind == "post" and urllib.parse.urlsplit(final).path == urllib.parse.urlsplit(path).path:
            problems.append(f"post {ident}: {path} did not redirect to the new URL")
    log(f"compared {checked} of {len(targets)} pages and posts; {len(problems)} problem(s)")
    return problems


# --------------------------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------------------------

SECRET_OPTION = re.compile(r"(api_?key|passw|secret|token|salt|private|credential|auth_key)", re.I)
IGNORED_OPTION = re.compile(r"^(_transient|_site_transient|cron$|rewrite_rules$|wp_.*user_roles$)")


def inventory(site, blogs_note=True):
    """Counts and facts about one WordPress site, read from the dump only. No content is printed."""
    types = collections.Counter((p["post_type"], p["post_status"]) for p in site.posts)
    kept = [p for p in site.posts if p["post_type"] in ("post", "page")]
    blocks, shortcodes, tags = collections.Counter(), collections.Counter(), collections.Counter()
    emails, phones = {}, {}
    scripts = 0
    for p in kept:
        text = p["post_content"] or ""
        scripts += len(INJECTED_SCRIPT.findall(text))
        text = INJECTED_SCRIPT.sub("", text)
        blocks.update(re.findall(r"<!-- wp:([\w/-]+)", text))
        shortcodes.update(re.findall(r"\[(caption|video|audio|gallery|playlist|embed|contact-form-7|gravityform)\b", text))
        tags.update(t.lower() for t in re.findall(r"<(\w+)", text))
        found = set(EMAIL_RE.findall(text))
        if found:
            emails[p["ID"]] = sorted(found)
        found = set(PHONE_RE.findall(text))
        if found:
            phones[p["ID"]] = sorted(found)
    comments = site.comments
    secrets = sorted(name for name, value in site.options.items()
                     if value and SECRET_OPTION.search(name) and not IGNORED_OPTION.match(name))
    widgets = []
    sidebars = php_unserialize(site.option("sidebars_widgets", "")) or {}
    blocks_widget = php_unserialize(site.option("widget_block", "")) or {}
    for area, ids in (sidebars.items() if isinstance(sidebars, dict) else []):
        if isinstance(ids, dict):
            for key, widget_id in ids.items():
                widgets.append({"area": area, "id": widget_id})
    widget_block_types = collections.Counter()
    for entry in blocks_widget.values() if isinstance(blocks_widget, dict) else []:
        if isinstance(entry, dict):
            widget_block_types.update(re.findall(r"<!-- wp:([\w/-]+)", entry.get("content", "")))
    return {
        "site": {
            "blog_id": site.blog_id, "domain": site.blog_domain, "title": site.option("blogname"),
            "tagline": site.option("blogdescription"), "home": site.option("home"), "siteurl": site.option("siteurl"),
            "permalink_structure": site.permalink_structure, "theme": site.option("stylesheet"),
            "template": site.option("template"), "active_plugins": php_unserialize(site.option("active_plugins", "")) or {},
            "site_icon": site.option("site_icon"), "show_on_front": site.option("show_on_front"),
            "users_can_register": site.option("users_can_register"),
        },
        "other_sites_in_dump": [{"blog_id": b["blog_id"], "domain": b["domain"]}
                                for b in site.blogs if b["blog_id"] != site.blog_id],
        "post_types": {f"{t}/{s}": n for (t, s), n in sorted(types.items())},
        "published_posts": sum(1 for p in kept if p["post_type"] == "post" and p["post_status"] == "publish"),
        "published_pages": sum(1 for p in kept if p["post_type"] == "page" and p["post_status"] == "publish"),
        "categories": {r["name"]: r["count"] for r in site.taxonomy_rows("category")},
        "tags": {r["name"]: r["count"] for r in site.taxonomy_rows("post_tag")},
        "post_years": dict(sorted(collections.Counter(p["post_date"][:4] for p in kept if p["post_type"] == "post").items())),
        "authors": sorted({site.author_name(p["post_author"]) or f"(user {p['post_author']})" for p in kept}),
        "comments": {
            "total": len(comments),
            "by_approval": dict(collections.Counter(str(c["comment_approved"]) for c in comments)),
            "by_type": dict(collections.Counter(c["comment_type"] or "comment" for c in comments)),
        },
        "gutenberg_blocks": dict(blocks), "shortcodes_in_use": dict(shortcodes),
        "html_tags": dict(tags.most_common(40)),
        "injected_scripts": scripts,
        "posts_with_email_addresses": {str(k): len(v) for k, v in emails.items()},
        "posts_with_phone_numbers": {str(k): len(v) for k, v in phones.items()},
        "nav_menu": [{"id": i["id"], "title": i["title"], "object_id": i["object_id"], "url": i["url"]}
                     for i in site.nav_menu_items()],
        "widgets": widgets, "widget_block_types": dict(widget_block_types),
        "secret_looking_options_not_copied": secrets,
        "redirect_plugin_posts": sorted(
            pid for pid, meta in site.postmeta.items() if str(meta.get("_pprredirect_active")) == "1"),
    }


def _taxonomy_rows(self, kind):
    rows = []
    for row in self.taxonomy.values():
        if row["taxonomy"] == kind:
            rows.append({"name": self.terms[row["term_id"]]["name"], "count": row["count"]})
    return rows


WordPressSite.taxonomy_rows = _taxonomy_rows


# --------------------------------------------------------------------------------------------
# Scan for files that must never be served or imported
# --------------------------------------------------------------------------------------------

SENSITIVE_NAME_GLOBS = [
    ".env", ".env.*", "*.env", "wp-config.php", "wp-config*.php", "*.sql", "*.sql.gz", "*.sql.bz2", "*.sqlite", "*.db",
    "*.key", "*.pem", "*.p12", "*.pfx", "*.jks", "*.kdbx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "authorized_keys", "known_hosts", "*.bak", "*.backup", "*.old", "*.orig", "*.swp", "*~", "debug.log",
    "error_log", "error.log", "access.log", "*.log", ".htpasswd", ".htaccess", ".DS_Store", "Thumbs.db",
    "credentials*", "secrets*", "*.crt.key", "*.csr", "phpinfo.php", "*.git-credentials", ".npmrc", ".netrc",
]
SENSITIVE_DIRS = {".git", ".svn", ".hg", ".idea", ".vscode", "node_modules", "__MACOSX", ".ssh", ".aws", "backup",
                  "backups", "upgrade-temp-backup"}
ARCHIVE_GLOBS = ["*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.tar.bz2", "*.7z", "*.rar"]
ARCHIVE_HINT = re.compile(r"(backup|dump|export|db|database|wp-content|site)", re.I)
TEXT_EXTENSIONS = {".txt", ".html", ".htm", ".js", ".json", ".md", ".yml", ".yaml", ".conf", ".cfg", ".ini", ".php",
                   ".py", ".sh", ".xml", ".csv", ".env", ".cnf", ".toml", ".properties", ""}
CONTENT_RULES = [
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |)PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack/github/stripe token",
     re.compile(r"\b(?:xox[abp]-[0-9A-Za-z-]{10,}|gh[pousr]_[A-Za-z0-9]{30,}|sk_live_[0-9A-Za-z]{16,})")),
    ("quoted credential assignment", re.compile(
        r"(?i)\b(?:db_)?(?:password|passwd|secret|api_?key|auth_?token|access_?token)\b\s*[:=]\s*['\"][^'\"\s$<{]{8,}['\"]")),
    ("DB_PASSWORD define", re.compile(r"define\(\s*['\"](?:DB_PASSWORD|AUTH_KEY|SECURE_AUTH_KEY|LOGGED_IN_KEY|NONCE_KEY)['\"]")),
]
SCAN_SIZE_LIMIT = 2 * 1024 * 1024


# [@ANCHOR: wordpress_to_odoo:scan_tree]
# Verified by [@ANCHOR: test_wordpress_to_odoo:scan_tree]
def scan_tree(roots, content=True):
    """Walks directories and lists what looks like a secret or a private file. Reports paths and rule names,
    never the matching text. Returns a list of {path, rule, kind, size}."""
    findings = []
    for root in roots:
        for directory, dirnames, filenames in os.walk(root):
            for name in list(dirnames):
                if name in SENSITIVE_DIRS:
                    findings.append({"path": os.path.join(directory, name), "rule": f"directory {name}",
                                     "kind": "name", "size": 0})
            for name in filenames:
                path = os.path.join(directory, name)
                try:
                    size = os.lstat(path).st_size
                except OSError:
                    continue
                rule = next((g for g in SENSITIVE_NAME_GLOBS
                             if fnmatch.fnmatch(name, g) or fnmatch.fnmatch(name.lower(), g.lower())), None)
                if rule is None and any(fnmatch.fnmatch(name.lower(), g) for g in ARCHIVE_GLOBS) and ARCHIVE_HINT.search(name):
                    rule = "archive that looks like a backup"
                if rule is None and any(part in SENSITIVE_DIRS for part in os.path.relpath(path, root).split(os.sep)[:-1]):
                    rule = "inside a sensitive directory"
                if rule:
                    findings.append({"path": path, "rule": rule, "kind": "name", "size": size})
                    continue
                ext = os.path.splitext(name)[1].lower()
                if content and ext in TEXT_EXTENSIONS and 0 < size <= SCAN_SIZE_LIMIT and not os.path.islink(path):
                    try:
                        with open(path, "r", encoding="utf-8", errors="replace") as handle:  # audit-ignore-path
                            text = handle.read()
                    except OSError:
                        continue
                    for label, pattern in CONTENT_RULES:
                        match = pattern.search(text)
                        if match:
                            findings.append({"path": path, "rule": label, "kind": "content", "size": size,
                                             "line": text.count("\n", 0, match.start()) + 1})
    return findings


def static_refs_missing(static_root, refs):
    """Which /static/... URLs the content references that the snapshot lacks. A directory counts when it has an index."""
    missing = []
    for url_path in sorted(refs):
        relative = url_path.lstrip("/")
        full = os.path.normpath(os.path.join(static_root, relative))
        if not full.startswith(os.path.normpath(static_root) + os.sep):
            missing.append(url_path)
            continue
        if os.path.isfile(full):
            continue
        if os.path.isdir(full) and any(os.path.isfile(os.path.join(full, n)) for n in ("index.html", "index.htm")):
            continue
        missing.append(url_path)
    return missing


# --------------------------------------------------------------------------------------------
# Export directory
# --------------------------------------------------------------------------------------------


def write_export(out_dir, data, media, converter, report):
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    for sub in ("data", "files", "reports"):
        os.makedirs(os.path.join(out_dir, sub), mode=0o700, exist_ok=True)
    if os.path.exists(os.path.join(out_dir, "manifest.json")):
        raise mig.MigrateError(f"{out_dir} already holds an export; use another directory")
    manifest = {"tool": TOOL_VERSION, "source": "wordpress", "models": {}, "files": {}, "skipped": {}}
    for model, rows in data.items():
        path = os.path.join(out_dir, "data", f"{model}.jsonl")
        with open(path, "w", encoding="utf-8") as handle:  # audit-ignore-path
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        digest = mig.sha256_file(path)
        manifest["files"][f"data/{model}.jsonl"] = digest
        manifest["models"][model] = {"done": True, "count": len(rows), "last_id": rows[-1]["id"] if rows else 0,
                                     "sha256": digest, "fields": sorted({k for r in rows for k in r})}
    for att_id, source in sorted(media.files.items()):
        target = os.path.join(out_dir, "files", f"att_{att_id}")
        with open(source, "rb") as src, open(target, "wb") as dst:  # audit-ignore-path
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        manifest["files"][f"files/att_{att_id}"] = mig.sha256_file(target)
    manifest["finished"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest["converter"] = report.get("counts", {})
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as handle:  # audit-ignore-path
        json.dump(manifest, handle, indent=2, sort_keys=True)
    lines = [f"{digest}  {name}" for name, digest in sorted(manifest["files"].items())]
    with open(os.path.join(out_dir, "CHECKSUMS.sha256"), "w", encoding="utf-8") as handle:  # audit-ignore-path
        handle.write("\n".join(lines) + "\n")
    return manifest


def write_csv(path, fieldnames, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:  # audit-ignore-path
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def export_comments_csv(site, path):
    """The comments, kept for the record. E-mail addresses, IP addresses, user agents and author URLs are left out."""
    posts = {p["ID"]: p for p in site.posts}
    rows = []
    for c in site.comments:
        post = posts.get(c["comment_post_ID"], {})
        rows.append({
            "comment_id": c["comment_ID"], "post_id": c["comment_post_ID"], "post_slug": post.get("post_name", ""),
            "date": c["comment_date"], "author": c["comment_author"], "type": c["comment_type"] or "comment",
            "approved": c["comment_approved"], "parent": c["comment_parent"], "text": c["comment_content"],
        })
    write_csv(path, ["comment_id", "post_id", "post_slug", "date", "author", "type", "approved", "parent", "text"], rows)
    return len(rows)


def convert(sql_path, out_dir, blog_id=8, domains=("perens.com", "www.perens.com", "new.perens.com"), static_root=None,
            uploads_dirs=(), blog_name=DEFAULT_BLOG_NAME, include_unpublished=False, log=print):
    dump = read_dump(sql_path)
    site = WordPressSite(dump, blog_id)
    converter = Converter(site, domains, static_root, uploads_dirs, blog_name, include_unpublished)
    data = converter.convert_all()
    problems = []
    arch_by_view = {v["id"]: v["arch_db"] for v in data["ir.ui.view"]}
    for view_id, arch in arch_by_view.items():
        for issue in check_arch_safe(arch):
            problems.append(f"page {view_id}: {issue}")
    for post in data["blog.post"]:
        for issue in check_html_safe(post["content"]):
            problems.append(f"post {post['id']}: {issue}")
    if problems:
        raise mig.MigrateError("converted content failed the safety check: " + "; ".join(problems[:10]))
    comments = len(site.comments)
    static_missing = static_refs_missing(static_root, converter.static_refs) if static_root else []
    missing_rows = []
    for url, ids in sorted(converter.media.missing.items()):
        missing_rows.append({"kind": "upload", "url": url, "referenced_by": " ".join(map(str, sorted(map(str, ids))))})
    for url in static_missing:
        missing_rows.append({"kind": "static", "url": url,
                             "referenced_by": " ".join(sorted(map(str, converter.static_refs[url])))})
    report = {
        "counts": dict(converter.stats),
        "inventory": inventory(site),
        "dropped": converter.dropped,
        "comments_dropped": comments,
        "unresolved_internal_links": {k: sorted(map(str, v)) for k, v in sorted(converter.unresolved_links.items())},
        "static_references": len(converter.static_refs),
        "static_references_missing": static_missing,
        "uploads_missing": sorted(converter.media.missing),
        "external_hosts_top": dict(converter.external_hosts.most_common(25)),
    }
    manifest = write_export(out_dir, data, converter.media, converter, report)
    reports = os.path.join(out_dir, "reports")
    with open(os.path.join(reports, "content_report.json"), "w", encoding="utf-8") as handle:  # audit-ignore-path
        json.dump(report, handle, indent=2, sort_keys=True, default=str)
    write_csv(os.path.join(reports, "sitemap.csv"), ["kind", "id", "old_url", "new_url", "status", "note"], converter.sitemap)
    for ref in sorted(converter.static_refs):
        status = "missing" if ref in static_missing else "kept"
        converter.sitemap.append({"kind": "static", "id": "", "old_url": ref, "new_url": ref, "status": status,
                                  "note": "served read-only from the static file server"})
    write_csv(os.path.join(reports, "sitemap.csv"), ["kind", "id", "old_url", "new_url", "status", "note"], converter.sitemap)
    write_csv(os.path.join(reports, "missing_references.csv"), ["kind", "url", "referenced_by"], missing_rows)
    export_comments_csv(site, os.path.join(reports, "comments_dropped.csv"))
    log(f"pages {converter.stats['pages']}, posts {converter.stats['posts']}, attachments {converter.stats['attachments']}, "
        f"redirects {converter.stats['redirects']}, menu items {converter.stats['menu_items']}")
    log(f"injected scripts removed: {converter.stats['injected_scripts_removed']}; comments dropped: {comments}")
    log(f"missing uploads: {len(converter.media.missing)}; missing static files: {len(static_missing)}")
    return report, manifest


def finalize_sitemap(export_dir):
    """After `odoo_site_migrate.py import --apply`: rewrites the new URLs of reports/sitemap.csv (written with the
    source ids) through the importer's idmap.json, into reports/sitemap_final.csv. Returns the number of rows."""
    with open(os.path.join(export_dir, "idmap.json"), "r", encoding="utf-8") as handle:  # audit-ignore-path
        idmap = json.load(handle)

    def table(model):
        return {int(k): v for k, v in idmap.get(model, {}).items() if v > 0}

    sitemap = os.path.join(export_dir, "reports", "sitemap.csv")
    with open(sitemap, "r", encoding="utf-8", newline="") as handle:  # audit-ignore-path
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row["new_url"]:
            row["new_url"] = mig.rewrite_html(row["new_url"], [], table("ir.attachment"), table("blog.blog"),
                                              table("blog.post"))
    write_csv(os.path.join(export_dir, "reports", "sitemap_final.csv"),
              ["kind", "id", "old_url", "new_url", "status", "note"], rows)
    return len(rows)


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory", help="print counts and facts about the site")
    inv.add_argument("--sql", required=True)
    inv.add_argument("--blog-id", type=int, default=8)
    conv = sub.add_parser("convert", help="write an odoo_site_migrate export directory")
    conv.add_argument("--sql", required=True)
    conv.add_argument("--out", required=True)
    conv.add_argument("--blog-id", type=int, default=8)
    conv.add_argument("--domain", action="append", help="old host names (default perens.com, www., new.)")
    conv.add_argument("--static-root", help="directory that holds `static/` (checks /static/ references)")
    conv.add_argument("--uploads-dir", action="append", default=[], help="a wp-content/uploads directory (repeatable)")
    conv.add_argument("--blog-name", default=DEFAULT_BLOG_NAME)
    conv.add_argument("--include-unpublished", action="store_true")
    cmp_ = sub.add_parser("compare", help="GET every converted page and post from a running Odoo and compare its text")
    cmp_.add_argument("--sql", required=True)
    cmp_.add_argument("--base-url", required=True)
    cmp_.add_argument("--blog-id", type=int, default=8)
    cmp_.add_argument("--uploads-dir", action="append", default=[])
    cmp_.add_argument("--blog-name", default=DEFAULT_BLOG_NAME)
    fin = sub.add_parser("finalize-sitemap", help="after the import: put the real new URLs into reports/sitemap_final.csv")
    fin.add_argument("--export", required=True)
    scan = sub.add_parser("scan", help="list secret-looking and private files under directories")
    scan.add_argument("--root", action="append", required=True)
    scan.add_argument("--no-content", action="store_true")
    return parser


def main(argv=None, out=print):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inventory":
            site = WordPressSite(read_dump(args.sql), args.blog_id)
            out(json.dumps(inventory(site), indent=2, sort_keys=True, default=str))
        elif args.command == "convert":
            domains = args.domain or ["perens.com", "www.perens.com", "new.perens.com"]
            convert(args.sql, args.out, args.blog_id, domains, args.static_root, args.uploads_dir, args.blog_name,
                    args.include_unpublished, out)
        elif args.command == "finalize-sitemap":
            out(f"{finalize_sitemap(args.export)} row(s) written to {args.export}/reports/sitemap_final.csv")
        elif args.command == "compare":
            problems = compare_rendered(args.sql, args.base_url, args.blog_id, uploads_dirs=args.uploads_dir,
                                        blog_name=args.blog_name, log=out)
            for item in problems:
                out("DIFF " + item)
            return 1 if problems else 0
        else:
            findings = scan_tree(args.root, content=not args.no_content)
            for item in findings:
                out(f"{item['kind']:<8} {item['rule']:<34} {item['path']}" + (f":{item['line']}" if "line" in item else ""))
            out(f"{len(findings)} finding(s)")
            return 1 if findings else 0
    except (mig.MigrateError, ValueError, OSError) as exc:
        out(f"error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
