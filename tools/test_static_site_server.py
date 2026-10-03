#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for static_site_server.py over a real loopback socket (no mocks of the server)."""

# [@ANCHOR: test_static_site_server:resolve]
# Tests [@ANCHOR: static_site_server:resolve]

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest

import static_site_server as srv
import wordpress_to_odoo as wp


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="static-test-")
        cls.root = os.path.join(cls.tmp, "site")
        files = {
            "static/doc.pdf": b"%PDF-1.4 " + bytes(range(256)) * 4, "static/page.html": b"<p>hi</p>",
            "static/talk.webm": b"W" * 1000, "static/film.mkv": b"M" * 500, "static/song.mp3": b"3" * 300,
            "static/dir/index.html": b"<p>index</p>", "static/empty/readme.txt": b"r", "static/.hidden": b"no",
            "static/.git/config": b"[core]", "static/wp-config.php": b"<?php", "static/dump.sql": b"sql",
            "static/key.pem": b"pem", "static/Notes.LOG": b"log", "static/a b/c d.txt": b"spaces", "secret.txt": b"outside",
            "static/unknown.xyz": b"x",
        }
        for rel, data in files.items():
            path = os.path.join(cls.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        os.symlink(os.path.join(cls.root, "secret.txt"), os.path.join(cls.root, "static", "link.txt"))
        os.symlink("/etc", os.path.join(cls.root, "static", "etc"))
        cls.config = {"root": cls.root, "prefix": "/static/", "port": 0, "bind": "127.0.0.1", "cache_seconds": 600}
        cls.server = srv.Server(("127.0.0.1", 0), cls.config)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, method="GET", headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read()
        finally:
            conn.close()


class ServingTests(ServerCase):
    def test_files_are_served_with_types_cache_and_nosniff(self):
        status, headers, body = self.get("/static/talk.webm")
        self.assertEqual((status, len(body)), (200, 1000))
        self.assertEqual(headers["content-type"], "video/webm")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["cache-control"], "public, max-age=600")
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(self.get("/static/film.mkv")[1]["content-type"], "video/x-matroska")
        self.assertEqual(self.get("/static/song.mp3")[1]["content-type"], "audio/mpeg")
        self.assertEqual(self.get("/static/page.html")[1]["content-type"], "text/html; charset=utf-8")
        self.assertEqual(self.get("/static/unknown.xyz")[1]["content-type"], "application/octet-stream")
        self.assertEqual(self.get("/static/a%20b/c%20d.txt")[2], b"spaces")
        self.assertNotIn("python", headers.get("server", "").lower())

    def test_range_requests_for_video_seeking(self):
        status, headers, body = self.get("/static/talk.webm", headers={"Range": "bytes=10-19"})
        self.assertEqual((status, body), (206, b"W" * 10))
        self.assertEqual(headers["content-range"], "bytes 10-19/1000")
        status, headers, body = self.get("/static/talk.webm", headers={"Range": "bytes=990-"})
        self.assertEqual((status, len(body), headers["content-range"]), (206, 10, "bytes 990-999/1000"))
        status, headers, body = self.get("/static/talk.webm", headers={"Range": "bytes=-5"})
        self.assertEqual((status, len(body), headers["content-range"]), (206, 5, "bytes 995-999/1000"))
        status, headers, _ = self.get("/static/talk.webm", headers={"Range": "bytes=5000-6000"})
        self.assertEqual((status, headers["content-range"]), (416, "bytes */1000"))
        status, _, body = self.get("/static/talk.webm", headers={"Range": "bytes=0-1,5-6"})
        self.assertEqual((status, len(body)), (200, 1000))  # multi-range is ignored, the whole file is sent

    def test_head_and_conditional_requests(self):
        status, headers, body = self.get("/static/doc.pdf", "HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["content-length"], str(len(b"%PDF-1.4 " + bytes(range(256)) * 4)))
        etag = headers["etag"]
        self.assertEqual(self.get("/static/doc.pdf", headers={"If-None-Match": etag})[0], 304)
        self.assertEqual(self.get("/static/doc.pdf", headers={"If-None-Match": '"other"'})[0], 200)
        _, headers, _ = self.get("/static/doc.pdf", headers={"Range": "bytes=0-3", "If-Range": '"stale"'})
        self.assertNotIn("content-range", headers)  # a stale If-Range gets the whole file

    def test_directories_never_list_and_redirect_to_the_slash_form(self):
        status, headers, _ = self.get("/static/dir")
        self.assertEqual((status, headers["location"]), (301, "/static/dir/"))
        self.assertEqual(self.get("/static/dir/")[2], b"<p>index</p>")
        self.assertEqual(self.get("/static/empty/")[0], 404)
        self.assertEqual(self.get("/static/")[0], 404)
        self.assertEqual(self.get("/static")[1]["location"], "/static/")
        self.assertEqual(self.get("/static/page.html/")[0], 404)

    def test_only_get_and_head(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            status, headers, _ = self.get("/static/page.html", method, body=b"x" if method in ("POST", "PUT", "PATCH") else None)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers["allow"], "GET, HEAD")


class RefusalTests(ServerCase):
    def test_dotfiles_secrets_dumps_keys_logs_and_scripts_are_404(self):
        for path in ("/static/.hidden", "/static/.git/config", "/static/wp-config.php", "/static/dump.sql",
                     "/static/key.pem", "/static/Notes.LOG", "/static/%2ehidden", "/static/%2Egit/config"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_traversal_encoded_separators_nul_and_outside_prefix_are_refused(self):
        for path in ("/static/../secret.txt", "/static/%2e%2e/secret.txt", "/static/..%2fsecret.txt",
                     "/static/a/..%5c..%5csecret.txt",
                     "/static/%00", "/secret.txt", "/staticx/page.html", "/static//page.html", "/static/dir/../../secret.txt",
                     "/etc/passwd"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_symbolic_links_that_leave_the_root_are_refused(self):
        self.assertEqual(self.get("/static/link.txt")[0], 404)
        self.assertEqual(self.get("/static/etc/passwd")[0], 404)

    def test_oversized_urls_are_refused(self):
        self.assertEqual(self.get("/static/" + "a" * 3000)[0], 414)


class ResolveTests(unittest.TestCase):
    def test_resolve_never_returns_a_path_outside_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "r")
            os.makedirs(os.path.join(root, "static"))
            for url in ("/static/../x", "/static/%2e%2e/x", "/static/a\\b", "/static/a\x00b"):
                self.assertEqual(srv.resolve(root, "/static/", url), ("missing", None))

    def test_the_server_deny_list_covers_every_name_the_scan_reports(self):
        # A file the scan would flag by name must also never be served.
        samples = {
            ".env": 1, "wp-config.php": 1, "dump.sql": 1, "x.pem": 1, "x.key": 1, "id_rsa": 1, "backup.bak": 1, "debug.log": 1,
            ".htaccess": 1, ".htpasswd": 1, "credentials.json": 1, "x.sqlite": 1, "authorized_keys": 1, "x~": 1,
        }
        for name in samples:
            matched = any(__import__("fnmatch").fnmatch(name, g) or __import__("fnmatch").fnmatch(name.lower(), g.lower())
                          for g in wp.SENSITIVE_NAME_GLOBS)
            self.assertTrue(matched, f"{name} is not in the scan's list (test is out of date)")
            self.assertTrue(srv.is_denied_name(name), f"{name} is flagged by the scan but still served")


class ConfigTests(unittest.TestCase):
    def write(self, tmp, **values):
        config = {"root": tmp, "prefix": "/static/", "port": 18201}
        config.update(values)
        path = os.path.join(tmp, "c.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return path

    def test_good_config_defaults_to_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = srv.load_config(self.write(tmp))
            self.assertEqual((config["bind"], config["cache_seconds"]), ("127.0.0.1", srv.DEFAULT_CACHE_SECONDS))

    def test_non_loopback_bind_bad_prefix_port_and_unknown_keys_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ({"bind": "0.0.0.0"}, {"bind": "10.99.0.1"}, {"prefix": "static"}, {"prefix": "/static"},
                        {"port": 80}, {"port": "x"}, {"cache_seconds": -1}, {"extra": 1}, {"root": os.path.join(tmp, "none")}):
                with self.assertRaises(ValueError, msg=str(bad)):
                    srv.load_config(self.write(tmp, **bad))


if __name__ == "__main__":
    unittest.main()
