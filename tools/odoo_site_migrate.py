#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""odoo_site_migrate: copy the website content of an existing Odoo site into a tenant (ADR 0105).

    odoo_site_migrate.py probe  --creds FILE
    odoo_site_migrate.py export --creds FILE --out DIR [--include contacts,mail] [--resume]
    odoo_site_migrate.py verify --out DIR
    odoo_site_migrate.py import --export DIR --target-url URL --target-db DB \
        --target-password-file FILE [--source-domain perens.com ...] [--apply]

The SOURCE is read through a wrapper that can only call read methods (search, search_read, read,
fields_get, search_count): nothing on the source can be written, changed or deleted by this tool.
Requests are rate limited (default one every 0.5 s, backing off on 429/5xx), capped by
--max-requests, and carry the honest User-Agent of ADR 0104.

Credentials file (the tool reads it itself; no credential is ever typed, passed on a command line,
printed or logged), for example ~/.secrets/perens.com/odoo_login.env, mode 0600:

    ODOO_URL=https://perens.com
    ODOO_DB=perens            (optional when the server lists databases or has just one)
    ODOO_LOGIN=bruce@example.com
    ODOO_PASSWORD=...         (or ODOO_API_KEY=...; an API key works in place of the password)

Export: version-tolerant. The server version is detected, every model's own `fields_get` decides
which fields are read (so an Odoo 14 and an Odoo 18 source both work), secret-looking fields are
skipped and reported, and everything lands in DIR as JSON lines with a manifest and SHA-256 sums. It
resumes where it stopped. Import: dry run unless --apply; ids are remapped (idmap.json, saved as it
goes so a second run resumes and creates nothing twice), records are matched to existing ones by
natural keys first, links to the old domain become relative, attachment and blog ids inside content
are rewritten, URLs are kept and a 301 redirect is created wherever Odoo's own URL scheme (blog slugs
carry the record id) forces a change, and a report lists every difference.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import xmlrpc.client
from urllib.parse import urlsplit

TOOL_VERSION = "1.0"
USER_AGENT = "HamsComSyncDaemon/1.0 (+https://crawler.hams.com)"
SECRET_FIELD_RE = re.compile(r"(password|passwd|secret|token|api_?key|private_?key|signature|credential)", re.I)

# The only methods ever sent to the source. Anything else raises before a request is made.
# [@ANCHOR: odoo_site_migrate:read_only_source]
READ_ONLY_METHODS = frozenset({"search", "search_read", "read", "fields_get", "search_count"})

# Runtime and tracking data that is not site content.
EXCLUDED_MODELS = frozenset({"website.visitor", "website.track", "website.lead", "website.page.properties.base"})
BINARY_CAP_MB = 25
# Fields never copied: ids and bookkeeping, and computed values the target recomputes.
SKIP_FIELDS = frozenset(
    {"id", "create_uid", "write_uid", "create_date", "write_date", "display_name", "__last_update",
     "website_url", "website_slug", "website_published_url", "write_date", "access_token"}
)
ATTACHMENT_RES_MODELS = ["ir.ui.view", "blog.post", "blog.blog", "website", "website.page", "website.menu"]


class MigrateError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------
# Credentials and transport
# --------------------------------------------------------------------------------------------


class Credentials:
    def __init__(self, url, db, login, secret):
        self.url, self.db, self.login, self._secret = url.rstrip("/"), db, login, secret

    @property
    def secret(self):
        return self._secret

    def __repr__(self):
        return f"Credentials(url={self.url!r}, db={self.db!r}, login={self.login!r}, secret=<hidden>)"

    __str__ = __repr__


# [@ANCHOR: odoo_site_migrate:load_credentials]
def load_credentials(path):
    """Reads the env-style credentials file. Refuses a file readable by group or others, and a
    non-HTTPS URL for anything but localhost, so the secret never travels in clear text."""
    try:
        info = os.stat(path)
    except OSError as exc:
        raise MigrateError(f"cannot read credentials file {path}: {exc}") from exc
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise MigrateError(f"{path} must not be readable by group or others (chmod 600)")
    values = {}
    with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    url = values.get("ODOO_URL", "")
    secret = values.get("ODOO_API_KEY") or values.get("ODOO_PASSWORD")
    if not url or not values.get("ODOO_LOGIN") or not secret:
        raise MigrateError("credentials file needs ODOO_URL, ODOO_LOGIN and ODOO_PASSWORD or ODOO_API_KEY")
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
        raise MigrateError("ODOO_URL must be https (or localhost)")
    return Credentials(url, values.get("ODOO_DB", ""), values["ODOO_LOGIN"], secret)


class _UATransport(xmlrpc.client.Transport):
    user_agent = USER_AGENT


class _UASafeTransport(xmlrpc.client.SafeTransport):
    user_agent = USER_AGENT


class XmlRpcTransport:
    """XML-RPC (/xmlrpc/2), which every Odoo from 8 to 19 serves. Rate limited with retries."""

    def __init__(self, creds, min_interval=0.5, max_requests=0, sleep=time.sleep, clock=time.monotonic,
                 proxy_factory=None, retries=3):
        self.creds = creds
        self.min_interval = min_interval
        self.max_requests = max_requests
        self.requests = 0
        self._sleep, self._clock = sleep, clock
        self._last = None
        self._retries = retries
        factory = proxy_factory or xmlrpc.client.ServerProxy
        transport = _UASafeTransport() if creds.url.startswith("https") else _UATransport()
        kwargs = {"transport": transport} if proxy_factory is None else {}
        self._common = factory(f"{creds.url}/xmlrpc/2/common", allow_none=True, **kwargs)
        self._object = factory(f"{creds.url}/xmlrpc/2/object", allow_none=True, **kwargs)
        self._uid = None

    def _throttled(self, func, *args):
        for attempt in range(self._retries + 1):
            if self.max_requests and self.requests >= self.max_requests:
                raise MigrateError(f"request cap of {self.max_requests} reached; rerun with --resume")
            if self._last is not None:
                wait = self.min_interval - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait)
            self._last = self._clock()
            self.requests += 1
            try:
                return func(*args)
            except xmlrpc.client.ProtocolError as exc:
                if exc.errcode not in (429, 500, 502, 503, 504) or attempt == self._retries:
                    raise MigrateError(f"HTTP {exc.errcode} from the server") from None
            except (OSError, urllib.error.URLError) as exc:
                if attempt == self._retries:
                    raise MigrateError(f"network error: {exc}") from None
            self._sleep(min(60, 2 ** (attempt + 1)))
        raise MigrateError("unreachable")

    def version(self):
        return self._throttled(self._common.version)

    def login(self):
        db = self.creds.db
        self._uid = self._throttled(self._common.authenticate, db, self.creds.login, self.creds.secret, {})
        if not self._uid:
            raise MigrateError("authentication failed (wrong login, password/API key or database)")
        return self._uid

    def call(self, model, method, args, kwargs=None):
        if self._uid is None:
            self.login()
        return self._throttled(
            self._object.execute_kw, self.creds.db, self._uid, self.creds.secret, model, method, args, kwargs or {}
        )


class ReadOnlySource:
    """Wraps a transport so that only READ_ONLY_METHODS can ever reach the source."""

    def __init__(self, transport):
        self._transport = transport

    def version(self):
        return self._transport.version()

    def call(self, model, method, args, kwargs=None):
        if method not in READ_ONLY_METHODS:
            raise PermissionError(f"refusing to call {method!r} on the source: this tool only reads")
        return self._transport.call(model, method, args, kwargs)


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:  # audit-ignore-path
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def m2o_id(value):
    """The id inside a many2one value as read() returns it ([id, name] or False)."""
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return False


def slugify(text):
    text = re.sub(r"[^\w\s-]", "", (text or "").lower(), flags=re.UNICODE)
    return re.sub(r"[-\s]+", "-", text).strip("-")


# [@ANCHOR: odoo_site_migrate:rewrite_html]
def rewrite_html(html, domains, attachment_map=None, blog_map=None, post_map=None, report=None):
    """Rewrites internal references inside an HTML or QWeb string:

    - absolute links to any of the old `domains` become site-relative;
    - /web/image/<id>... and /web/content/<id>... follow the attachment id map;
    - /blog/<slug>-<id>[/<slug>-<id>] follow the blog and post id maps.
    References whose target was not imported are left alone and counted in `report`."""
    if not html or not isinstance(html, str):
        return html
    attachment_map = attachment_map or {}
    blog_map = blog_map or {}
    post_map = post_map or {}
    report = report if report is not None else {}

    def count(key):
        report[key] = report.get(key, 0) + 1

    for domain in domains:
        pattern = re.compile(r"https?://(?:www\.)?" + re.escape(domain) + r"(?=[/\"'?#\s)]|$)", re.I)
        html, number = pattern.subn("", html)
        report["links_made_relative"] = report.get("links_made_relative", 0) + number

    def attachment(match):
        old = int(match.group(2))
        if old in attachment_map:
            count("attachment_refs_rewritten")
            return f"{match.group(1)}{attachment_map[old]}"
        count("attachment_refs_unresolved")
        return match.group(0)

    html = re.sub(r"(/web/(?:image|content)/(?:ir\.attachment/)?)(\d+)(?=[/?\"'\s)]|$)", attachment, html)

    def blog_link(match):
        slug_b, old_b, tail = match.group(1), int(match.group(2)), match.group(3)
        new_b = blog_map.get(old_b)
        if new_b is None:
            count("blog_refs_unresolved")
            return match.group(0)
        result = f"/blog/{slug_b}-{new_b}"
        post = re.match(r"^/([^/?#\"'\s]*?)-(\d+)(.*)$", tail or "", re.S)
        if post and int(post.group(2)) in post_map:
            result += f"/{post.group(1)}-{post_map[int(post.group(2))]}{post.group(3)}"
            count("blog_refs_rewritten")
        else:
            result += tail or ""
        return result

    html = re.sub(r"/blog/([^/\s\"'?#]*?)-(\d+)((?:/[^\s\"'?#]*)?)", blog_link, html)
    return html


def secret_fields(field_names):
    return sorted(name for name in field_names if SECRET_FIELD_RE.search(name))


# --------------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------------

# model -> (domain, extra fields read although computed)
CORE_MODELS = [
    ("website", [], []),
    ("res.lang", [("active", "=", True)], []),
    ("ir.ui.view", [("website_id", "!=", False)], []),
    ("website.page", [], ["url"]),
    ("website.menu", [], []),
    ("website.rewrite", [], []),
    ("blog.blog", [], ["website_url"]),
    ("blog.tag.category", [], []),
    ("blog.tag", [], []),
    ("blog.post", [], ["website_url"]),
    ("ir.attachment", None, []),
]
OPTIONAL_MODELS = {
    "contacts": [("res.partner", [("user_ids", "=", False), ("email", "!=", False)], [])],
    "mail": [("mail.message", [("model", "=", "blog.post"), ("message_type", "=", "comment")], [])],
}


class Exporter:
    def __init__(self, source, out_dir, include=(), page_size=100, max_file_mb=BINARY_CAP_MB, log=print):
        self.source, self.out, self.include = source, out_dir, tuple(include)
        self.page_size, self.max_file_mb, self.log = page_size, max_file_mb, log
        self.state_path = os.path.join(out_dir, "state.json")
        self.manifest = {"tool": TOOL_VERSION, "models": {}, "files": {}, "skipped": {}}

    # -- small io helpers
    def _write_json(self, name, data):
        path = os.path.join(self.out, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:  # audit-ignore-path
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:  # audit-ignore-path
                return json.load(handle)
        except FileNotFoundError:
            return {"models": {}}

    def fields_of(self, model):
        return self.source.call(model, "fields_get", [], {"attributes": ["type", "relation", "store", "string", "selection"]})

    def model_exists(self, model):
        return bool(self.source.call("ir.model", "search_count", [[("model", "=", model)]]))

    # -- discovery
    def probe(self):
        info = self.source.version()
        installed = self.source.call(
            "ir.module.module", "search_read", [[("state", "=", "installed")]], {"fields": ["name"], "order": "name"}
        )
        names = [row["name"] for row in installed]
        website_models = self.source.call(
            "ir.model", "search_read", [[("model", "=like", "website%"), ("transient", "=", False)]],
            {"fields": ["model"], "order": "model"},
        )
        counts = {}
        for row in website_models + [{"model": m} for m, _d, _e in CORE_MODELS if m.startswith(("blog", "ir.ui", "ir.att"))]:
            model = row["model"]
            if model in counts or model in EXCLUDED_MODELS:
                continue
            try:
                counts[model] = self.source.call(model, "search_count", [[]])
            except Exception as exc:  # a model we cannot read is reported, not fatal
                counts[model] = f"unreadable ({str(exc)[:60]})"
        return {
            "server_version": info.get("server_version"),
            "server_version_info": info.get("server_version_info"),
            "installed_modules": names,
            "website_modules": [n for n in names if n.startswith(("website", "blog", "theme_"))],
            "model_counts": counts,
        }

    def plan_models(self, discovered_extra=True):
        models = [(m, d, e) for m, d, e in CORE_MODELS if self.model_exists(m)]
        for key in self.include:
            for model, domain, extra in OPTIONAL_MODELS.get(key, []):
                if self.model_exists(model):
                    models.append((model, domain, extra))
        if discovered_extra:
            known = {m for m, _d, _e in models}
            rows = self.source.call(
                "ir.model", "search_read", [[("model", "=like", "website%"), ("transient", "=", False)]],
                {"fields": ["model"], "order": "model"},
            )
            for row in rows:
                model = row["model"]
                if model not in known and model not in EXCLUDED_MODELS:
                    models.append((model, [], []))
        return models

    # -- export
    def run(self, resume=False):
        os.makedirs(os.path.join(self.out, "data"), exist_ok=True)
        os.makedirs(os.path.join(self.out, "files"), exist_ok=True)
        state = self._load_state() if resume else {"models": {}}
        if not resume and os.path.exists(os.path.join(self.out, "manifest.json")):
            raise MigrateError(f"{self.out} already holds an export; use --resume or another directory")
        self.manifest.update(self.probe())
        self.manifest["started"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for model, domain, extra in self.plan_models():
            done = state["models"].get(model, {})
            if done.get("done"):
                self.log(f"skip {model} (already exported: {done['count']})")
                self.manifest["models"][model] = done
                continue
            info = self.export_model(model, domain, extra, done)
            state["models"][model] = info
            self.manifest["models"][model] = info
            self._write_json("state.json", state)
        self.finish()
        return self.manifest

    def attachment_domain(self):
        return [
            "&", "&", ("public", "=", True), "!", ("url", "=like", "/web/assets/%"),
            "|", ("website_id", "!=", False), ("res_model", "in", ATTACHMENT_RES_MODELS),
        ]

    def export_model(self, model, domain, extra, previous):
        fields = self.fields_of(model)
        wanted = [
            name for name, meta in fields.items()
            if name not in SKIP_FIELDS and meta.get("store", True) and meta["type"] not in ("binary", "one2many", "properties")
        ]
        skipped_secret = secret_fields(wanted)
        wanted = [n for n in wanted if n not in skipped_secret]
        wanted += [name for name in extra if name in fields and name not in wanted]
        binaries = sorted(n for n, m in fields.items() if m["type"] == "binary" and model != "ir.attachment")
        if skipped_secret or binaries:
            self.manifest["skipped"][model] = {"secret_fields": skipped_secret, "binary_fields": binaries}
        if model == "ir.attachment":
            domain = self.attachment_domain()
        path = os.path.join(self.out, "data", f"{model}.jsonl")
        last_id, count = self._resume_point(path)
        if not previous:
            last_id, count = 0, 0
            open(path, "w", encoding="utf-8").close()  # audit-ignore-path
        while True:
            rows = self.source.call(
                model, "search_read", [list(domain) + [("id", ">", last_id)]],
                {"fields": wanted, "limit": self.page_size, "order": "id asc", "context": {"active_test": False}},
            )
            if not rows:
                break
            with open(path, "a", encoding="utf-8") as handle:  # audit-ignore-path
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
            if model == "ir.attachment":
                for row in rows:
                    self.export_attachment_file(row)
            last_id, count = rows[-1]["id"], count + len(rows)
            self.log(f"{model}: {count} records")
        digest = sha256_file(path)
        self.manifest["files"][f"data/{model}.jsonl"] = digest
        return {"done": True, "count": count, "last_id": last_id, "sha256": digest, "fields": wanted}

    def _resume_point(self, path):
        """Last complete record id and count in an existing JSONL file; drops a torn last line."""
        if not os.path.exists(path):
            return 0, 0
        good, last_id, count = [], 0, 0
        with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    break
                good.append(line)
                last_id, count = record["id"], count + 1
        with open(path, "w", encoding="utf-8") as handle:  # audit-ignore-path
            handle.writelines(good)
        return last_id, count

    def export_attachment_file(self, row):
        if row.get("type") != "binary":
            return
        size = row.get("file_size") or 0
        if size > self.max_file_mb * 1048576:
            self.manifest["skipped"].setdefault("ir.attachment", {}).setdefault("too_large", []).append(row["id"])
            return
        path = os.path.join(self.out, "files", f"att_{row['id']}")
        if os.path.exists(path):
            return
        data = self.source.call("ir.attachment", "read", [[row["id"]]], {"fields": ["datas"]})
        encoded = data[0].get("datas") if data else False
        if not encoded:
            return
        raw = base64.b64decode(encoded)
        with open(path, "wb") as handle:  # audit-ignore-path
            handle.write(raw)
        self.manifest["files"][f"files/att_{row['id']}"] = sha256_bytes(raw)

    def finish(self):
        self.manifest["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._write_json("manifest.json", self.manifest)
        lines = [f"{digest}  {name}" for name, digest in sorted(self.manifest["files"].items())]
        with open(os.path.join(self.out, "CHECKSUMS.sha256"), "w", encoding="utf-8") as handle:  # audit-ignore-path
            handle.write("\n".join(lines) + "\n")


# [@ANCHOR: odoo_site_migrate:verify_export]
def verify_export(out_dir):
    """Re-hashes every file listed in CHECKSUMS.sha256. Returns the list of problems."""
    problems = []
    path = os.path.join(out_dir, "CHECKSUMS.sha256")
    try:
        with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
            entries = [line.rstrip("\n").split("  ", 1) for line in handle if line.strip()]
    except OSError as exc:
        return [f"CHECKSUMS.sha256 unreadable: {exc}"]
    for digest, name in entries:
        full = os.path.join(out_dir, name)
        if not os.path.exists(full):
            problems.append(f"{name} missing")
        elif sha256_file(full) != digest:
            problems.append(f"{name} checksum mismatch")
    return problems


# --------------------------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------------------------


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
        return [json.loads(line) for line in handle if line.strip()]


class Importer:
    """Imports an export directory into a target Odoo through its external API."""

    def __init__(self, export_dir, target, apply=False, source_domains=(), website_map=None, log=print):
        self.dir, self.target, self.apply = export_dir, target, apply
        self.domains = [d.lower() for d in source_domains]
        self.website_map = dict(website_map or {})
        self.log = log
        self.idmap_path = os.path.join(export_dir, "idmap.json")
        self.idmap = self._load_idmap()
        self.report = {"created": {}, "updated": {}, "matched": {}, "dropped_fields": {}, "unsupported": [],
                       "redirects": [], "html": {}, "warnings": []}
        self._target_fields = {}
        self.data = {}

    # -- bookkeeping
    def _load_idmap(self):
        try:
            with open(self.idmap_path, "r", encoding="utf-8") as handle:  # audit-ignore-path
                return json.load(handle)
        except FileNotFoundError:
            return {}

    def _save_idmap(self):
        if not self.apply:
            return
        tmp = self.idmap_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:  # audit-ignore-path
            json.dump(self.idmap, handle, indent=1, sort_keys=True)
        os.replace(tmp, self.idmap_path)

    def mapped(self, model, source_id):
        source_id = m2o_id(source_id)
        if not source_id:
            return False
        return self.idmap.get(model, {}).get(str(source_id), False)

    def _remember(self, model, source_id, target_id):
        self.idmap.setdefault(model, {})[str(source_id)] = target_id

    def _count(self, bucket, model, number=1):
        self.report[bucket][model] = self.report[bucket].get(model, 0) + number

    def fields(self, model):
        if model not in self._target_fields:
            self._target_fields[model] = self.target.call(
                model, "fields_get", [], {"attributes": ["type", "relation", "readonly", "selection", "required"]}
            )
        return self._target_fields[model]

    def has_model(self, model):
        return bool(self.target.call("ir.model", "search_count", [[("model", "=", model)]]))

    def clean(self, model, row, extra_skip=()):
        """The values of `row` the target model has, minus read-only and secret fields; the rest
        are listed in the report as dropped."""
        target = self.fields(model)
        values, dropped = {}, []
        for name, value in row.items():
            if name in SKIP_FIELDS or name in extra_skip:
                continue
            meta = target.get(name)
            if meta is None or (meta.get("readonly") and not meta.get("required")):
                if value not in (False, None, [], ""):
                    dropped.append(name)
                continue
            if meta["type"] == "many2one":
                values[name] = m2o_id(value)
            elif meta["type"] in ("many2many", "one2many"):
                values[name] = value
            elif meta["type"] == "selection" and value:
                allowed = {item[0] for item in meta.get("selection") or []}
                if allowed and value not in allowed:
                    dropped.append(f"{name}={value}")
                    continue
                values[name] = value
            else:
                values[name] = value
        for name in dropped:
            key = f"{model}.{name}"
            self.report["dropped_fields"][key] = self.report["dropped_fields"].get(key, 0) + 1
        return values

    def create_or_match(self, model, source_id, domain, values, update=False):
        """Returns the target id. Matches by `domain` first (natural key). Honest in dry-run: the id
        is a negative placeholder for a record that would be created."""
        existing = self.target.call(model, "search", [domain], {"limit": 1, "context": {"active_test": False}})
        if existing:
            self._remember(model, source_id, existing[0])
            if update and self.apply and values:
                self.target.call(model, "write", [[existing[0]], values])
                self._count("updated", model)
            else:
                self._count("matched", model)
            return existing[0]
        self._count("created", model)
        if not self.apply:
            placeholder = -int(source_id)
            self._remember(model, source_id, placeholder)
            return placeholder
        new_id = self.target.call(model, "create", [values])
        new_id = new_id[0] if isinstance(new_id, list) else new_id
        self._remember(model, source_id, new_id)
        return new_id

    # -- link rewriting
    def rewrite(self, html):
        return rewrite_html(
            html, self.domains,
            {int(k): v for k, v in self.idmap.get("ir.attachment", {}).items() if v > 0},
            {int(k): v for k, v in self.idmap.get("blog.blog", {}).items() if v > 0},
            {int(k): v for k, v in self.idmap.get("blog.post", {}).items() if v > 0},
            self.report["html"],
        )

    # -- the run
    def run(self):
        for model in ("website", "ir.attachment", "ir.ui.view", "website.page", "website.menu", "website.rewrite",
                      "blog.blog", "blog.tag.category", "blog.tag", "blog.post"):
            self.data[model] = read_jsonl(os.path.join(self.dir, "data", f"{model}.jsonl"))
        self.check_languages()
        self.import_websites()
        self.import_attachments()
        self.import_views()
        self.import_pages()
        self.import_menus()
        self.import_rewrites()
        self.import_blog()
        self.rewrite_content()
        self.make_redirects()
        self.note_unsupported()
        self._save_idmap()
        return self.report

    def check_languages(self):
        languages = read_jsonl(os.path.join(self.dir, "data", "res.lang.jsonl"))
        if not languages:
            return
        have = {r["code"] for r in self.target.call("res.lang", "search_read", [[]], {"fields": ["code"]})}
        missing = sorted({lang["code"] for lang in languages} - have)
        if missing:
            self.report["warnings"].append(f"languages not installed in the target (translations skipped): {missing}")

    def target_website_for(self, source_id):
        source_id = m2o_id(source_id)
        if not source_id:
            return False
        if str(source_id) in self.website_map:
            return self.website_map[str(source_id)]
        return self.mapped("website", source_id)

    def import_websites(self):
        sites = self.data["website"]
        target_sites = self.target.call("website", "search", [[]], {"order": "id"})
        if not target_sites:
            raise MigrateError("the target has no website: install the website module first")
        for index, site in enumerate(sites):
            target_id = self.website_map.get(str(site["id"])) or (target_sites[index] if index < len(target_sites) else None)
            if target_id is None:
                self.report["warnings"].append(f"source website {site['id']} has no target website; skipped")
                continue
            self._remember("website", site["id"], target_id)
            values = self.clean("website", site, extra_skip=("domain", "name", "company_id", "default_lang_id", "language_ids"))
            values = {k: v for k, v in values.items() if v not in (False, None)}
            self._count("updated" if self.apply else "matched", "website")
            if self.apply and values:
                self.target.call("website", "write", [[target_id], values])
        self._save_idmap()

    def import_attachments(self):
        done = 0
        for row in self.data["ir.attachment"]:
            path = os.path.join(self.dir, "files", f"att_{row['id']}")
            if row.get("type") == "url" or not os.path.exists(path):
                if row.get("type") != "url":
                    self.report["warnings"].append(f"attachment {row['id']} ({row.get('name')}) has no exported file")
                continue
            values = self.clean("ir.attachment", row, extra_skip=("res_id", "res_model", "res_field", "checksum", "file_size"))
            values["website_id"] = self.target_website_for(row.get("website_id")) or False
            values["public"] = True
            with open(path, "rb") as handle:  # audit-ignore-path
                values["datas"] = base64.b64encode(handle.read()).decode()
            checksum = row.get("checksum")
            domain = [("name", "=", row["name"]), ("public", "=", True)]
            if checksum:
                domain.append(("checksum", "=", checksum))
            self.create_or_match("ir.attachment", row["id"], domain, values)
            done += 1
            if done % 100 == 0:
                self._save_idmap()
        self._save_idmap()

    def view_key_target(self, source_view_id):
        """The target id of the view a source view inherits from, found by key (ids differ)."""
        source = next((v for v in self.data["ir.ui.view"] if v["id"] == m2o_id(source_view_id)), None)
        key = source.get("key") if source else None
        if source is None:
            key = None
        if not key:
            return False
        found = self.target.call("ir.ui.view", "search", [[("key", "=", key)]], {"limit": 1, "order": "website_id desc"})
        return found[0] if found else False

    def import_views(self):
        # Parents first: a view whose parent is in the export comes after it.
        pending = list(self.data["ir.ui.view"])
        order, seen = [], set()
        by_id = {v["id"]: v for v in pending}

        def visit(view):
            if view["id"] in seen:
                return
            seen.add(view["id"])
            parent = by_id.get(m2o_id(view.get("inherit_id")))
            if parent:
                visit(parent)
            order.append(view)

        for view in pending:
            visit(view)
        for view in order:
            website = self.target_website_for(view.get("website_id"))
            values = self.clean("ir.ui.view", view, extra_skip=("inherit_id", "model_data_id", "arch_fs", "arch", "arch_prev"))
            values["website_id"] = website or False
            parent = self.mapped("ir.ui.view", view.get("inherit_id")) or self.view_key_target(view.get("inherit_id"))
            if m2o_id(view.get("inherit_id")) and not parent:
                self.report["warnings"].append(f"view {view.get('key') or view['id']}: parent view not found in the target")
            if parent and parent > 0:
                values["inherit_id"] = parent
            if view.get("arch_db"):
                values["arch_db"] = view["arch_db"]
            domain = [("key", "=", view.get("key")), ("website_id", "=", website or False)] if view.get("key") else [
                ("name", "=", view.get("name")), ("website_id", "=", website or False)]
            self.create_or_match("ir.ui.view", view["id"], domain, values, update=True)
        self._save_idmap()

    def import_pages(self):
        for page in self.data["website.page"]:
            website = self.target_website_for(page.get("website_id"))
            values = self.clean("website.page", page, extra_skip=("view_id", "url"))
            values["url"] = page["url"]
            values["website_id"] = website or False
            view = self.mapped("ir.ui.view", page.get("view_id"))
            if view and view > 0:
                values["view_id"] = view
            self.create_or_match(
                "website.page", page["id"], [("url", "=", page["url"]), ("website_id", "=", website or False)], values,
                update=True,
            )
        self._save_idmap()

    def import_menus(self):
        menus = self.data["website.menu"]
        by_id = {m["id"]: m for m in menus}
        order, seen = [], set()

        def visit(menu):
            if menu["id"] in seen:
                return
            seen.add(menu["id"])
            parent = by_id.get(m2o_id(menu.get("parent_id")))
            if parent:
                visit(parent)
            order.append(menu)

        for menu in menus:
            visit(menu)
        for menu in order:
            website = self.target_website_for(menu.get("website_id"))
            values = self.clean("website.menu", menu, extra_skip=("parent_id", "page_id", "child_id", "parent_path", "mega_menu_content"))
            values["website_id"] = website or False
            parent = self.mapped("website.menu", menu.get("parent_id"))
            if parent and parent > 0:
                values["parent_id"] = parent
            page = self.mapped("website.page", menu.get("page_id"))
            if page and page > 0:
                values["page_id"] = page
            domain = [("name", "=", menu["name"]), ("website_id", "=", website or False)]
            if parent and parent > 0:
                domain.append(("parent_id", "=", parent))
            elif not m2o_id(menu.get("parent_id")):
                domain.append(("parent_id", "=", False))
            self.create_or_match("website.menu", menu["id"], domain, values, update=True)
        self._save_idmap()

    def import_rewrites(self):
        for row in self.data["website.rewrite"]:
            website = self.target_website_for(row.get("website_id"))
            values = self.clean("website.rewrite", row)
            values["website_id"] = website or False
            self.create_or_match(
                "website.rewrite", row["id"], [("url_from", "=", row["url_from"]), ("website_id", "=", website or False)], values
            )
        self._save_idmap()

    def import_blog(self):
        if not self.data["blog.blog"] and not self.data["blog.post"]:
            return
        if not self.has_model("blog.post"):
            self.report["warnings"].append("source has blog content but the target has no blog module (install website_blog)")
            return
        for blog in self.data["blog.blog"]:
            values = self.clean("blog.blog", blog)
            self.create_or_match("blog.blog", blog["id"], [("name", "=", blog["name"])], values, update=True)
        if self.has_model("blog.tag.category"):
            for category in self.data["blog.tag.category"]:
                values = self.clean("blog.tag.category", category)
                self.create_or_match("blog.tag.category", category["id"], [("name", "=", category["name"])], values)
        for tag in self.data["blog.tag"]:
            values = self.clean("blog.tag", tag, extra_skip=("category_id", "post_ids"))
            category = self.mapped("blog.tag.category", tag.get("category_id"))
            if category and category > 0:
                values["category_id"] = category
            self.create_or_match("blog.tag", tag["id"], [("name", "=", tag["name"])], values)
        for post in self.data["blog.post"]:
            blog = self.mapped("blog.blog", post.get("blog_id"))
            values = self.clean("blog.post", post, extra_skip=("blog_id", "tag_ids", "author_id", "website_id"))
            if blog and blog > 0:
                values["blog_id"] = blog
            tags = [self.mapped("blog.tag", t) for t in post.get("tag_ids") or []]
            tags = [t for t in tags if t and t > 0]
            if tags:
                values["tag_ids"] = [(6, 0, tags)]
            values["author_id"] = self.author_for(post)
            self.create_or_match(
                "blog.post", post["id"], [("name", "=", post["name"]), ("blog_id", "=", blog or False)], values, update=True
            )
        self._save_idmap()

    def author_for(self, post):
        """Authors are not users in the target. The post keeps the admin partner as author unless a
        partner with the same name exists; the original author name is listed in the report."""
        author = post.get("author_id")
        if not author:
            return False
        name = author[1] if isinstance(author, (list, tuple)) and len(author) > 1 else None
        if name:
            found = self.target.call("res.partner", "search", [[("name", "=", name)]], {"limit": 1})
            if found:
                return found[0]
            self.report["warnings"].append(f"blog author {name!r} has no partner in the target; post author left to the default")
        return False

    # -- second pass: rewrite internal references now that every id is known
    def rewrite_content(self):
        passes = (("ir.ui.view", "arch_db"), ("blog.post", "content"), ("blog.post", "teaser_manual"),
                  ("blog.post", "cover_properties"), ("website.menu", "mega_menu_content"))
        for model, field in passes:
            for row in self.data.get(model) or read_jsonl(os.path.join(self.dir, "data", f"{model}.jsonl")):
                value = row.get(field)
                new_id = self.mapped(model, row["id"])
                if not value or not new_id or new_id < 0 or field not in self.fields(model):
                    continue
                rewritten = self.rewrite(value)
                if rewritten != value and self.apply:
                    self.target.call(model, "write", [[new_id], {field: rewritten}])
                    self._count("updated", f"{model}.{field}")
                elif rewritten != value:
                    self._count("matched", f"{model}.{field} (would be rewritten)")

    def make_redirects(self):
        """A blog URL carries the record id, so imported posts get new URLs. Old ones redirect."""
        if not self.apply:
            return
        website = next(iter(self.website_map.values()), None) or next(iter(self.idmap.get("website", {}).values()), False)
        for model in ("blog.blog", "blog.post"):
            for row in self.data.get(model, []):
                new_id = self.mapped(model, row["id"])
                old_url = row.get("website_url")
                if not new_id or new_id < 0 or not old_url:
                    continue
                new_url = self.target.call(model, "read", [[new_id]], {"fields": ["website_url"]})[0].get("website_url")
                if not new_url or new_url == old_url:
                    continue
                exists = self.target.call("website.rewrite", "search", [[("url_from", "=", old_url)]], {"limit": 1})
                if exists:
                    continue
                self.target.call("website.rewrite", "create", [{
                    "name": f"migrated {old_url}", "url_from": old_url, "url_to": new_url,
                    "redirect_type": "301", "website_id": website or False,
                }])
                self.report["redirects"].append({"from": old_url, "to": new_url, "code": 301})

    def note_unsupported(self):
        manifest_path = os.path.join(self.dir, "manifest.json")
        if not os.path.exists(manifest_path):
            return
        with open(manifest_path, "r", encoding="utf-8") as handle:  # audit-ignore-path
            manifest = json.load(handle)
        handled = {"website", "ir.attachment", "ir.ui.view", "website.page", "website.menu", "website.rewrite",
                   "blog.blog", "blog.tag.category", "blog.tag", "blog.post", "res.lang"}
        for model, info in sorted(manifest.get("models", {}).items()):
            if model not in handled and info.get("count"):
                self.report["unsupported"].append(f"{model}: {info['count']} record(s) exported, not imported (review by hand)")
        for model, info in manifest.get("skipped", {}).items():
            for key, value in info.items():
                self.report["warnings"].append(f"{model}: skipped {key}: {value}")


def render_report(report, apply):
    lines = [f"{'APPLIED' if apply else 'DRY RUN (nothing was written)'}"]
    for bucket in ("created", "updated", "matched"):
        for model, number in sorted(report[bucket].items()):
            lines.append(f"  {bucket:<8} {model}: {number}")
    if report["html"]:
        lines.append(f"  links: {json.dumps(report['html'], sort_keys=True)}")
    for key, number in sorted(report["dropped_fields"].items()):
        lines.append(f"  dropped field {key} on {number} record(s) (not in the target model)")
    for item in report["redirects"]:
        lines.append(f"  301 {item['from']} -> {item['to']}")
    for item in report["unsupported"]:
        lines.append(f"  UNSUPPORTED {item}")
    for item in report["warnings"]:
        lines.append(f"  WARNING {item}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


def cmd_probe(args, out):
    creds = load_credentials(args.creds)
    transport = XmlRpcTransport(creds, args.min_interval, args.max_requests)
    exporter = Exporter(ReadOnlySource(transport), ".", log=out)
    out(json.dumps(exporter.probe(), indent=2, sort_keys=True))
    return 0


def cmd_export(args, out):
    creds = load_credentials(args.creds)
    transport = XmlRpcTransport(creds, args.min_interval, args.max_requests)
    include = [item for item in (args.include or "").split(",") if item]
    exporter = Exporter(ReadOnlySource(transport), args.out, include, args.page_size, args.max_file_mb, out)
    os.makedirs(args.out, mode=0o700, exist_ok=True)
    manifest = exporter.run(args.resume)
    out(f"exported {sum(m.get('count', 0) for m in manifest['models'].values())} records in "
        f"{len(manifest['models'])} models; {transport.requests} requests")
    return 0


def cmd_verify(args, out):
    problems = verify_export(args.out)
    for problem in problems:
        out(f"PROBLEM {problem}")
    out("export verified" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_import(args, out):
    try:
        with open(args.target_password_file, "r", encoding="utf-8") as handle:  # audit-ignore-path
            secret = handle.read().strip()
    except OSError as exc:
        raise MigrateError(f"cannot read {args.target_password_file}: {exc}") from exc
    creds = Credentials(args.target_url, args.target_db, args.target_login, secret)
    target = XmlRpcTransport(creds, min_interval=0.0)
    website_map = dict(item.split(":", 1) for item in args.website_map or [])
    importer = Importer(args.export, target, args.apply, args.source_domain or [], website_map, out)
    problems = verify_export(args.export)
    if problems:
        raise MigrateError(f"the export is damaged: {problems[:3]}")
    report = importer.run()
    out(render_report(report, args.apply))
    if args.apply:
        with open(os.path.join(args.export, "import_report.json"), "w", encoding="utf-8") as handle:  # audit-ignore-path
            json.dump(report, handle, indent=2, sort_keys=True)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def source_opts(p):
        p.add_argument("--creds", required=True)
        p.add_argument("--min-interval", type=float, default=0.5)
        p.add_argument("--max-requests", type=int, default=0)

    p = sub.add_parser("probe")
    source_opts(p)
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("export")
    source_opts(p)
    p.add_argument("--out", required=True)
    p.add_argument("--include", help="comma list: contacts,mail (default none)")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--max-file-mb", type=int, default=BINARY_CAP_MB)
    p.set_defaults(func=cmd_export)
    p = sub.add_parser("verify")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("import")
    p.add_argument("--export", required=True)
    p.add_argument("--target-url", required=True)
    p.add_argument("--target-db", required=True)
    p.add_argument("--target-login", default="admin")
    p.add_argument("--target-password-file", required=True)
    p.add_argument("--source-domain", action="append", help="old domain(s); links to them become relative")
    p.add_argument("--website-map", action="append", help="SRC_ID:DST_ID (repeatable)")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_import)
    return parser


def main(argv=None, out=print):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args, out)
    except MigrateError as exc:
        out(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
