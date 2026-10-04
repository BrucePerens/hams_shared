#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""static_site_server: a small, read-only static file server for one site's `/static/` tree (ADR 0106).

    static_site_server.py --config /etc/hams-static/perens_com.json

Why this and not nginx: hams1 serves the web through the Cloudflare Tunnel; nginx is installed there but disabled
and was never in the path, and the one planned use of nginx (client-certificate login on auth.hams.com) should not
share a configuration or a failure with a personal site's media. This is about 250 lines of standard library,
bound to loopback, run as its own unprivileged account by `hams-static@<name>.service` (read-only view of the
content, no write path, no capabilities, no outbound network). The tunnel sends `^/static/` of the site's
hostnames to it (tenant spec `static_site`); everything else goes to the Odoo tenant. An nginx server block that
does the same is in hams_com docs/proposals/MULTI_TENANT_ODOO.md (section 18.4) for Bruce to choose instead.

Behaviour (each line is tested over a real socket in test_static_site_server.py):

* GET and HEAD only; anything else is 405 with `Allow`.
* The URL path must start with the configured prefix; the rest is looked up under `<root>/<prefix>`. `..`, NUL,
  backslashes and encoded separators are refused; a symbolic link that leaves the root is refused.
* No directory listings. A directory serves its `index.html` (or `index.htm`) if it has one, else 404; `/dir`
  redirects to `/dir/` so relative links in an index page work.
* Any path segment that starts with a dot (`.git`, `.htaccess`, `.env`) and any file that looks like a secret or a
  backup (`*.sql`, `*.pem`, `*.key`, `wp-config*.php`, `*.log`, `*.bak`...) is a 404: the same names
  `wordpress_to_odoo.py scan` reports, so a file that slipped into the tree is still not served.
* `Accept-Ranges: bytes` and single `Range` requests (206, 416 when unsatisfiable) so video and audio seek;
  `ETag`/`Last-Modified` with 304 on revalidation; `Cache-Control: public, max-age=N` (configurable, default a day).
* `X-Content-Type-Options: nosniff` on everything; content types for .webm, .mkv, .mp3, .mp4, .pdf and the usual
  web types; unknown types are `application/octet-stream`.
"""

import argparse
import email.utils
import fnmatch
import hashlib
import http.server
import json
import mimetypes
import os
import re
import socket
import socketserver
import sys
import urllib.parse

SERVER_NAME = "hams-static"
CHUNK = 256 * 1024
MAX_HEADER_BYTES = 16 * 1024
DEFAULT_CACHE_SECONDS = 86400
# Request/response timeout for one connection (seconds): a stalled client cannot hold a thread forever.
SOCKET_TIMEOUT = 60

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".json": "application/json",
    ".csv": "text/csv; charset=utf-8", ".xml": "application/xml", ".md": "text/plain; charset=utf-8",
    ".pdf": "application/pdf", ".zip": "application/zip", ".gz": "application/gzip",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4", ".ogv": "video/ogg",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
}
# Never served, whatever the tree holds. Kept in step with wordpress_to_odoo.SENSITIVE_NAME_GLOBS (a test checks it).
DENY_GLOBS = [
    ".*", "*.env", "wp-config*.php", "*.sql", "*.sql.gz", "*.sql.bz2", "*.sqlite", "*.db", "*.key", "*.pem", "*.p12",
    "*.pfx", "*.jks", "*.kdbx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "authorized_keys", "known_hosts",
    "*.bak", "*.backup", "*.old", "*.orig", "*.swp", "*~", "debug.log", "error_log", "error.log", "access.log", "*.log",
    "credentials*", "secrets*", "*.csr", "phpinfo.php", "Thumbs.db", "*.php",
]
INDEX_NAMES = ("index.html", "index.htm")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def is_denied_name(name):
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in DENY_GLOBS)


def content_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in CONTENT_TYPES:
        return CONTENT_TYPES[ext]
    guessed = mimetypes.guess_type(path)[0] or ""
    # Anything the table above does not know is a download unless it is plain media.
    return guessed if guessed.startswith(("image/", "audio/", "video/", "font/")) else "application/octet-stream"


# [@ANCHOR: static_site_server:resolve]
# Verified by [@ANCHOR: test_static_site_server:resolve]
def resolve(root, prefix, url_path):
    """Maps a request path to ('file'|'dir'|'redirect'|'missing', filesystem path). Never leaves `root`."""
    if "\x00" in url_path or "\\" in url_path:
        return "missing", None
    decoded = urllib.parse.unquote(url_path)
    if "\x00" in decoded or "\\" in decoded or "//" in url_path.replace("://", ""):
        return "missing", None
    if not decoded.startswith(prefix) and decoded + "/" != prefix:
        return "missing", None
    if decoded + "/" == prefix:
        return "redirect", None
    parts = decoded.split("/")[1:]
    trailing = decoded.endswith("/")
    segments = [p for p in parts if p != ""]
    for segment in segments:
        if segment in (".", "..") or segment.startswith(".") or is_denied_name(segment):
            return "missing", None
    # Only the served subtree counts: a link to the site directory's own bookkeeping (checksum lists) is refused too.
    served = os.path.realpath(os.path.join(root, *[p for p in prefix.split("/") if p]))
    full = os.path.realpath(os.path.join(os.path.realpath(root), *segments))
    if full != served and not full.startswith(served + os.sep):
        return "missing", None
    if os.path.isdir(full):
        if not trailing:
            return "redirect", None
        for name in INDEX_NAMES:
            candidate = os.path.join(full, name)
            if os.path.isfile(candidate):
                return "file", candidate
        return "missing", None
    if os.path.isfile(full):
        if trailing:
            return "missing", None
        return "file", full
    return "missing", None


def parse_range(header, size):
    """Returns (start, end) inclusive, None for 'serve everything', or 'bad' when unsatisfiable."""
    if not header:
        return None
    match = RANGE_RE.match(header.strip())
    if not match:
        return None  # multi-range or an unknown unit: ignore the header, as the standard allows
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        length = int(last)
        if length == 0:
            return "bad"
        return max(0, size - length), size - 1
    start = int(first)
    end = int(last) if last != "" else size - 1
    if start >= size or start > end:
        return "bad"
    return start, min(end, size - 1)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_NAME
    sys_version = ""
    timeout = SOCKET_TIMEOUT

    def version_string(self):
        return SERVER_NAME

    def log_message(self, fmt, *args):  # no access log: journald would hold a record of every visitor
        pass

    def send_common(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_plain(self, status, text="", extra=()):
        body = (text or http.server.BaseHTTPRequestHandler.responses[status][0]).encode()
        self.send_response(status)
        self.send_common()
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_plain(405, extra=[("Allow", "GET, HEAD")])

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        config = self.server.config
        raw = self.path
        if len(raw) > 2048:
            return self.send_plain(414)
        parts = urllib.parse.urlsplit(raw)
        kind, path = resolve(config["root"], config["prefix"], parts.path)
        if kind == "redirect":
            location = parts.path + "/" if not parts.path.endswith("/") else parts.path
            return self.send_plain(301, extra=[("Location", location + (("?" + parts.query) if parts.query else ""))])
        if kind != "file":
            return self.send_plain(404)
        try:
            info = os.stat(path)
            handle = open(path, "rb")  # audit-ignore-path
        except OSError:
            return self.send_plain(404)
        with handle:
            size = info.st_size
            etag = '"%s"' % hashlib.sha1(f"{info.st_size}-{info.st_mtime_ns}".encode()).hexdigest()[:20]
            modified = email.utils.formatdate(info.st_mtime, usegmt=True)
            if self.headers.get("If-None-Match") == etag or (
                    "If-None-Match" not in self.headers and self.headers.get("If-Modified-Since") == modified):
                self.send_response(304)
                self.send_common()
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", f"public, max-age={config['cache_seconds']}")
                self.end_headers()
                return
            wanted = parse_range(self.headers.get("Range"), size)
            if self.headers.get("If-Range") not in (None, etag, modified):
                wanted = None
            if wanted == "bad":
                return self.send_plain(416, extra=[("Content-Range", f"bytes */{size}")])
            if wanted is None:
                status, start, end = 200, 0, size - 1
            else:
                status, (start, end) = 206, wanted
            length = max(0, end - start + 1) if size else 0
            self.send_response(status)
            self.send_common()
            self.send_header("Content-Type", content_type(path))
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", modified)
            self.send_header("Cache-Control", f"public, max-age={config['cache_seconds']}")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD" or not length:
                return
            handle.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = handle.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                self.close_connection = True


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, config):
        self.config = config
        super().__init__(address, Handler)


def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
        raw = json.load(handle)
    unknown = set(raw) - {"root", "prefix", "port", "bind", "cache_seconds"}
    if unknown:
        raise ValueError(f"unknown config key(s): {sorted(unknown)}")
    config = {"bind": "127.0.0.1", "cache_seconds": DEFAULT_CACHE_SECONDS, **raw}
    if config["bind"] not in ("127.0.0.1", "::1"):
        raise ValueError("this server binds to loopback only: the tunnel is the only way in")
    if not config["prefix"].startswith("/") or not config["prefix"].endswith("/"):
        raise ValueError("prefix must start and end with /")
    if not os.path.isdir(config["root"]):
        raise ValueError(f"root {config['root']!r} is not a directory")
    if not isinstance(config["port"], int) or not 1024 <= config["port"] <= 65535:
        raise ValueError("port must be an unprivileged port")
    if not isinstance(config["cache_seconds"], int) or config["cache_seconds"] < 0:
        raise ValueError("cache_seconds must be a non-negative integer")
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    server = Server((config["bind"], config["port"]), config)
    print(f"{SERVER_NAME}: serving {config['root']}{config['prefix']} on {config['bind']}:{config['port']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
