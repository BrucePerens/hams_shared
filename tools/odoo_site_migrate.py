#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""odoo_site_migrate: copy the website content of an existing Odoo site into a tenant (ADR 0106).

    odoo_site_migrate.py probe  --creds FILE
    odoo_site_migrate.py export --creds FILE --out DIR [--include contacts,mail] [--resume]
    odoo_site_migrate.py restore   --dump FILE --db mig_NAME          (offline snapshot: scratch database)
    odoo_site_migrate.py inventory --snapshot-db mig_NAME --filestore DIR
    odoo_site_migrate.py export    --snapshot-db mig_NAME --filestore DIR --out DIR
    odoo_site_migrate.py verify --out DIR
    odoo_site_migrate.py import --export DIR --target-url URL --target-db DB \
        --target-password-file FILE [--source-domain perens.com ...] [--apply]
    odoo_site_migrate.py lockdown --target-url URL --target-db DB --target-password-file FILE [--apply]

The source is either a live server (--creds) or an OFFLINE SNAPSHOT (--snapshot-db: a restored pg_dump and
the filestore directory; odoo_snapshot_source.py answers the same five read methods over SQL), so a site
whose server is not to be contacted again can still be exported. The export holds only the site owner's
content: website-owned pages and menus, the QWeb of pages, the owner's own views, switches of header and
footer options, the site's SCSS (colours, fonts), blogs, public files. Copies of module and theme views
that Odoo made for the website (untouched ones) are not exported: the target's own modules make them.

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
import unicodedata
import urllib.error
import urllib.request
from xml.etree import ElementTree
import xmlrpc.client
from urllib.parse import urlsplit

TOOL_VERSION = "1.0"
PUBLICATION_FIELDS = ("is_published", "website_published", "date_publish", "published_date")
USER_AGENT = "HamsComSyncDaemon/1.0 (+https://crawler.hams.com)"
SECRET_FIELD_RE = re.compile(r"(password|passwd|secret|token|api_?key|private_?key|signature|credential)", re.I)

# The only methods ever sent to the source. Anything else raises before a request is made.
# [@ANCHOR: odoo_site_migrate:read_only_source]
# Verified by [@ANCHOR: test_odoo_site_migrate:read_only_source]
READ_ONLY_METHODS = frozenset({"search", "search_read", "read", "fields_get", "search_count"})

# Runtime and tracking data that is not site content.
EXCLUDED_MODELS = frozenset({"website.visitor", "website.track", "website.lead", "website.page.properties.base"})
BINARY_CAP_MB = 25
# Fields never copied: ids and bookkeeping, and computed values the target recomputes.
SKIP_FIELDS = frozenset(
    {"id", "create_uid", "write_uid", "create_date", "write_date", "display_name", "__last_update",
     "website_url", "website_slug", "website_published_url", "write_date", "access_token"}
)
# Binary fields copied as files (site identity); every other binary field is skipped and reported.
BINARY_COPY = {"website": ["logo", "favicon", "social_default_image"]}
NOISY_BINARIES = {("res.lang", "flag_image"), ("blog.post", "author_avatar")}
ATTACHMENT_RES_MODELS = ["ir.ui.view", "blog.post", "blog.blog", "website", "website.page", "website.menu"]


# What a site's database holds besides its content. Counted in the inventory, never exported.
NOT_CONTENT_MODELS = (
    "res.users", "res.partner", "mail.message", "mail.mail", "mail.template", "ir.mail_server", "fetchmail.server",
    "ir.cron", "payment.provider", "payment.method", "product.template", "gamification.badge", "crm.lead",
    "project.project", "project.task", "forum.forum", "forum.post", "event.event", "slide.channel",
    "mailing.mailing", "survey.survey", "website.visitor", "website.track", "calendar.event",
)
TRACKING_FIELD_RE = re.compile(r"(analytics|plausible|search_console|maps|pixel|gtm|recaptcha|turnstile|hotjar|matomo)", re.I)
# Tracking identifiers that are public by design are recorded with their value; the rest only as "set".
PUBLIC_TRACKING = frozenset({"google_analytics_key", "google_search_console", "plausible_site"})
WEBSITE_INVENTORY_FIELDS = (
    "name", "domain", "homepage_url", "auto_redirect_lang", "cookies_bar", "robots_txt", "custom_code_head",
    "custom_code_footer", "auth_signup_uninvited", "cdn_activated", "block_third_party_domains",
    "social_twitter", "social_facebook", "social_github", "social_linkedin", "social_youtube",
    "social_instagram", "social_tiktok",
)


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
# Verified by [@ANCHOR: test_odoo_site_migrate:load_credentials]
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


class _ThrottledTransport:
    """Rate limit, request cap and retries shared by the two transports. A subclass raises
    xmlrpc.client.ProtocolError for an HTTP status worth retrying (429, 500, 502, 503, 504)."""

    def __init__(self, creds, min_interval=0.5, max_requests=0, sleep=time.sleep, clock=time.monotonic, retries=3):
        self.creds = creds
        self.min_interval = min_interval
        self.max_requests = max_requests
        self.requests = 0
        self._sleep, self._clock = sleep, clock
        self._last = None
        self._retries = retries
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


class XmlRpcTransport(_ThrottledTransport):
    """XML-RPC (/xmlrpc/2), which every Odoo from 8 to 19 serves (deprecated in 19). Rate limited with retries."""

    def __init__(self, creds, min_interval=0.5, max_requests=0, sleep=time.sleep, clock=time.monotonic,
                 proxy_factory=None, retries=3):
        super().__init__(creds, min_interval, max_requests, sleep, clock, retries)
        factory = proxy_factory or xmlrpc.client.ServerProxy
        transport = _UASafeTransport() if creds.url.startswith("https") else _UATransport()
        kwargs = {"transport": transport} if proxy_factory is None else {}
        self._common = factory(f"{creds.url}/xmlrpc/2/common", allow_none=True, **kwargs)
        self._object = factory(f"{creds.url}/xmlrpc/2/object", allow_none=True, **kwargs)

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


# JSON-2 (POST /json/2/<model>/<method>, Odoo 19+) takes named parameters, not execute_kw's positional
# ones. For every method the tools call: does it act on records (`ids`), and the names of its positional
# parameters in order. A method not listed here is refused, never guessed at.
JSON2_SIGNATURES = {
    "search": (False, ("domain", "offset", "limit", "order")),
    "search_read": (False, ("domain", "fields", "offset", "limit", "order")),
    "search_count": (False, ("domain", "limit")),
    "read": (True, ("fields", "load")),
    "fields_get": (False, ("allfields", "attributes")),
    "create": (False, ("vals_list",)),
    "write": (True, ("vals",)),
    "unlink": (True, ()),
    "save_asset": (False, ("url", "bundle", "content", "file_type")),
}


def json2_request(model, method, args, kwargs=None):
    """(path, body) of the JSON-2 request equivalent to execute_kw(model, method, args, kwargs), and whether a
    single-dict `create` was wrapped in a list (so the caller unwraps the one id)."""
    if method not in JSON2_SIGNATURES:
        raise MigrateError(f"JSON-2 transport: no parameter names known for {model}.{method}")
    takes_ids, names = JSON2_SIGNATURES[method]
    args = list(args)
    body = {}
    if takes_ids:
        if not args:
            raise MigrateError(f"{model}.{method} needs the record ids as its first argument")
        body["ids"] = list(args.pop(0))
    if len(args) > len(names):
        raise MigrateError(f"{model}.{method}: too many positional arguments for the JSON-2 transport")
    body.update(zip(names, args))
    for key, value in (kwargs or {}).items():
        if key in body:
            raise MigrateError(f"{model}.{method}: {key!r} given both positionally and by name")
        body[key] = value
    unwrap = False
    if method == "create" and isinstance(body.get("vals_list"), dict):
        body["vals_list"] = [body["vals_list"]]
        unwrap = True
    return f"/json/2/{model}/{method}", body, unwrap


def _urllib_http(url, headers, data, timeout=120):
    """(status, body bytes); a GET when `data` is None, else a POST. An HTTP error status is returned, not raised."""
    request = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # audit-ignore-outbound-fetch
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class Json2Transport(_ThrottledTransport):
    """JSON-2 (Odoo 19+): `Authorization: bearer <API key>`, one POST per call. The secret of the credentials
    must be an API key (JSON-2 refuses a password). A server-side error is raised as xmlrpc.client.Fault, so
    callers written for XML-RPC keep working. Use it for a target Odoo 19 or later; an older source needs XML-RPC."""

    def __init__(self, creds, min_interval=0.5, max_requests=0, sleep=time.sleep, clock=time.monotonic,
                 post=None, retries=3):
        super().__init__(creds, min_interval, max_requests, sleep, clock, retries)
        self._http = post or _urllib_http

    def _send(self, path, body=None):
        """POST `body` as JSON to `path` (a GET of the unauthenticated /web/version when `body` is None)."""
        headers = {"User-Agent": USER_AGENT}
        if body is not None:
            headers.update({"Content-Type": "application/json", "Authorization": f"bearer {self.creds.secret}"})
            if self.creds.db:
                headers["X-Odoo-Database"] = self.creds.db
        data = None if body is None else json.dumps(body).encode()
        status, raw = self._http(self.creds.url + path, headers, data)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            payload = None
        if status == 401 and body is not None:
            raise MigrateError("authentication failed (JSON-2 needs a valid API key, and the right database)")
        error_body = isinstance(payload, dict) and "message" in payload
        if status in (429, 502, 503, 504) or (status == 500 and not error_body):
            raise xmlrpc.client.ProtocolError(self.creds.url + path, status, "retry", {})
        if status >= 400:
            message = payload.get("message") if error_body else (raw or b"")[:200].decode("utf-8", "replace")
            raise xmlrpc.client.Fault(status, str(message))
        return payload

    def version(self):
        payload = self._throttled(self._send, "/web/version") or {}
        return {"server_version": payload.get("version"), "server_version_info": payload.get("version_info")}

    def login(self):
        context = self._throttled(self._send, "/json/2/res.users/context_get", {})
        self._uid = (context or {}).get("uid")
        if not self._uid:
            raise MigrateError("authentication failed (the API key did not identify a user)")
        return self._uid

    def call(self, model, method, args, kwargs=None):
        if self._uid is None:
            self.login()
        path, body, unwrap = json2_request(model, method, args, kwargs)
        result = self._throttled(self._send, path, body)
        return result[0] if unwrap and isinstance(result, list) and len(result) == 1 else result


def make_transport(kind, creds, **kwargs):
    """`xmlrpc` (any Odoo 8 to 19, password or API key) or `json2` (a target Odoo 19+, API key)."""
    if kind == "json2":
        return Json2Transport(creds, **kwargs)
    if kind == "xmlrpc":
        return XmlRpcTransport(creds, **kwargs)
    raise MigrateError(f"unknown transport {kind!r} (xmlrpc or json2)")


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
    """Odoo's own slugify (odoo.tools.misc): ASCII by decomposition, every run of other characters one
    hyphen, so "Bruce's Blog" is bruce-s-blog."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[\W_]+", "-", text).strip("-").lower()


# [@ANCHOR: odoo_site_migrate:rewrite_html]
# Verified by [@ANCHOR: test_odoo_site_migrate:rewrite_html]
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
        # Only inside an attribute value or a CSS url(): the visible text of a page may quote the address
        # (a licence citing https://postopen.org/zero-contract/) and must stay as written.
        pattern = re.compile(r"(?<=[\"'(])https?://(?:www\.)?" + re.escape(domain) + r"(?=[/\"'?#\s)]|$)", re.I)
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
        post = re.match(r"^/([^/?#\"'\s]*?)-(\d+)(?=[/?#\"'\s)]|$)(.*)$", tail or "", re.S)
        if post and int(post.group(2)) in post_map:
            result += f"/{post.group(1)}-{post_map[int(post.group(2))]}{post.group(3)}"
            count("blog_refs_rewritten")
        else:
            result += tail or ""
        return result

    # The id is the digits at the END of the slug segment: `-(\d+)` must be followed by a separator, or
    # a slug such as "my-2018-trip-7" would be read as id 2018.
    html = re.sub(r"/blog/([^/\s\"'?#]*?)-(\d+)(?=[/?#\"'\s)]|$)((?:/[^\s\"'?#]*)?)", blog_link, html)

    def bare_blog(match):
        new_b = blog_map.get(int(match.group(1)))
        if new_b is None:
            count("blog_refs_unresolved")
            return match.group(0)
        count("blog_refs_rewritten")
        return f"/blog/{new_b}"

    # Odoo also serves a blog by its bare id (/blog/3): the menu entry of a blog is of that form.
    html = re.sub(r"/blog/(\d+)(?=[/?#\"'\s)]|$)", bare_blog, html)
    return html


# Routes that exist only while a module is installed. A menu entry pointing at one of them is skipped
# when the target lacks the module (the page would answer 404).
MODULE_ROUTES = {
    "/forum": "website_forum", "/event": "website_event", "/events": "website_event", "/slides": "website_slides",
    "/shop": "website_sale", "/livechat": "website_livechat", "/groups": "website_mail_group",
    "/jobs": "website_hr_recruitment", "/profile": "website_profile", "/helpdesk": "website_helpdesk",
}
DEFAULT_FORMS_NOTICE = "This website has no contact form."


def module_for_url(url):
    """The module that provides a menu URL such as /forum or /slides/all, or None for a page or blog."""
    match = re.match(r"^(/[A-Za-z0-9_-]+)", url or "")
    return MODULE_ROUTES.get(match.group(1)) if match else None


# [@ANCHOR: odoo_site_migrate:neutralize_forms]
# Verified by [@ANCHOR: test_odoo_site_migrate:neutralize_forms]
def neutralize_forms(arch, notice=DEFAULT_FORMS_NOTICE):
    """Replaces every website form in a QWeb arch (`s_website_form` sections, and any <form> posting to
    /website/form/) by a static text block. A tenant has no outgoing mail, so a visitor's message would
    be accepted and never delivered. Returns (new_arch, number_replaced); an arch with no form comes
    back untouched, byte for byte."""
    if not arch or not isinstance(arch, str) or ("s_website_form" not in arch and "/website/form" not in arch):
        return arch, 0
    try:
        root = ElementTree.fromstring(arch.encode("utf-8"))
    except ElementTree.ParseError:
        return arch, 0
    parents = {child: parent for parent in root.iter() for child in parent}

    def is_form_section(node):
        return node.tag == "section" and "s_website_form" in (node.get("class") or "").split()

    def posts_to_form(node):
        return node.tag == "form" and (node.get("action") or "").startswith("/website/form")

    targets = []
    for node in root.iter():
        if is_form_section(node):
            targets.append(node)
        elif posts_to_form(node):
            holder = parents.get(node)
            chain = []
            while holder is not None:
                chain.append(holder)
                holder = parents.get(holder)
            if not any(is_form_section(item) for item in chain):
                targets.append(node)
    count = 0
    for node in targets:
        parent = parents.get(node)
        if parent is None:
            continue
        index = list(parent).index(node)
        block = ElementTree.Element("section", {
            "class": "s_text_block pt24 pb24 o_no_contact_form", "data-snippet": "s_text_block", "data-name": "No contact form"})
        container = ElementTree.SubElement(block, "div", {"class": "container"})
        ElementTree.SubElement(container, "p").text = notice
        block.tail = node.tail
        parent.remove(node)
        parent.insert(index, block)
        count += 1
    if not count:
        return arch, 0
    return ElementTree.tostring(root, encoding="unicode"), count


def secret_fields(field_names):
    return sorted(name for name in field_names if SECRET_FIELD_RE.search(name))


def normalize_arch(arch):
    """A canonical string for an XML/QWeb arch, ignoring comments, attribute order and whitespace
    between tags, so two copies of the same stock view compare equal."""
    if not arch or not isinstance(arch, str):
        return ""
    try:
        root = ElementTree.fromstring(arch.encode("utf-8"))
    except ElementTree.ParseError:
        return re.sub(r"\s+", " ", arch).strip()

    def squash(value):
        return re.sub(r"\s+", " ", value or "").strip()

    def walk(node):
        attrs = ",".join(k + "=" + squash(v) for k, v in sorted(node.attrib.items()))
        inner = "".join(walk(child) for child in node)
        return f"<{node.tag} {attrs}>{squash(node.text)}{inner}</{node.tag}>{squash(node.tail)}"

    return walk(root)


# [@ANCHOR: odoo_site_migrate:classify_view]
# Verified by [@ANCHOR: test_odoo_site_migrate:classify_view]
def classify_view(view, generic, page_view_ids):
    """What a website-specific view is, to decide whether it is the site owner's content.

    page        the QWeb of a website.page: content, always exported
    theme       a copy a theme module made for the website at install: the target gets its own when the
                same theme is installed there (and not exporting it stops an old theme's markup from
                overriding a newer theme)
    stock_copy  an untouched copy of a module view (Odoo copies a view for a website when anything about
                it is switched): identical to the generic view, so the target already has it
    toggle      same arch as the generic view but a different `active` flag: a header or footer
                layout option the owner switched on or off; imported as that switch, not as old markup
    customized  a module view whose markup the owner edited: exported, applied if the target accepts it
    custom      a view with no module counterpart (a view the owner wrote): exported
    """
    if view.get("id") in page_view_ids:
        return "page"
    key = view.get("key") or ""
    if key.split(".", 1)[0].startswith("theme_"):
        return "theme"
    base = generic.get(key) if key else None
    if base is None:
        return "custom"
    if normalize_arch(view.get("arch_db")) == normalize_arch(base.get("arch_db")):
        return "stock_copy" if bool(view.get("active")) == bool(base.get("active")) else "toggle"
    return "customized"


# --------------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------------

# model -> (domain, extra fields read although computed)
CORE_MODELS = [
    ("website", [], []),
    ("res.lang", [("active", "=", True)], []),
    ("ir.ui.view", [("website_id", "!=", False)], []),
    # Site-owned custom SCSS (colour palette, fonts) is an ir.asset pointing at a /_custom/ attachment.
    ("ir.asset", [("website_id", "!=", False), ("path", "=like", "/_custom/%")], []),
    # Only website-owned pages and menus: the generic ones (no website) are module data that the target's
    # own modules create, and importing them would overwrite the target's stock pages with an old version.
    ("website.page", [("website_id", "!=", False)], ["url"]),
    ("website.menu", [("website_id", "!=", False)], []),
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
        self._view_classes = {}
        self._page_views = None

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
    def discover_website_models(self):
        """Concrete (stored) website* models. Abstract mixins have no table and are refused by the
        server; `abstract` exists on ir.model from Odoo 16, older servers are filtered by name."""
        has_abstract = bool(self.source.call("ir.model.fields", "search_count",
                                             [[("model", "=", "ir.model"), ("name", "=", "abstract")]]))
        domain = [("model", "=like", "website%"), ("transient", "=", False)]
        if has_abstract:
            domain.append(("abstract", "=", False))
        rows = self.source.call("ir.model", "search_read", [domain], {"fields": ["model"], "order": "model"})
        return [r["model"] for r in rows if not r["model"].endswith((".mixin", ".metadata"))]

    def probe(self):
        info = self.source.version()
        installed = self.source.call(
            "ir.module.module", "search_read", [[("state", "=", "installed")]], {"fields": ["name"], "order": "name"}
        )
        names = [row["name"] for row in installed]
        website_models = self.discover_website_models()
        counts = {}
        for model in website_models + [m for m, _d, _e in CORE_MODELS if m.startswith(("blog", "ir.ui", "ir.att"))]:
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

    def inventory(self):
        """A human-readable description of what makes up the public site and what does not. Read only."""
        info = self.probe()
        installed = info["installed_modules"]
        rows = {}

        def fetch(model, fields, domain=(), **extra):
            if not self.model_exists(model):
                return []
            have = self.fields_of(model)
            wanted = [f for f in fields if f in have]
            try:
                return self.source.call(model, "search_read", [list(domain)], dict(
                    {"fields": wanted, "order": "id asc", "context": {"active_test": False}}, **extra))
            except xmlrpc.client.Fault as exc:
                return [{"error": str(exc)[:80]}]

        websites = fetch("website", list(WEBSITE_INVENTORY_FIELDS) + [
            f for f in self.fields_of("website") if TRACKING_FIELD_RE.search(f)] + ["theme_id", "default_lang_id"])
        site_rows = []
        for site in websites:
            tracking = {}
            for name, value in site.items():
                if TRACKING_FIELD_RE.search(name) and value:
                    tracking[name] = value if name in PUBLIC_TRACKING else "set (value not recorded)"
            site_rows.append({
                "id": site.get("id"), "name": site.get("name"), "domain": site.get("domain"),
                "homepage_url": site.get("homepage_url") or "", "theme": (site.get("theme_id") or [0, ""])[1] if site.get("theme_id") else "",
                "cookies_bar": bool(site.get("cookies_bar")), "auto_redirect_lang": bool(site.get("auto_redirect_lang")),
                "signup_scope": site.get("auth_signup_uninvited"), "robots_txt_custom": bool(site.get("robots_txt")),
                "custom_code_head_chars": len(site.get("custom_code_head") or ""),
                "custom_code_footer_chars": len(site.get("custom_code_footer") or ""),
                "social": {k: v for k, v in site.items() if k.startswith("social_") and v}, "tracking_ids": tracking,
            })
        rows["websites"] = site_rows
        pages = fetch("website.page", ["url", "is_published", "website_indexed", "view_id", "website_id"], [("website_id", "!=", False)])
        rows["pages"] = [{"url": p.get("url"), "published": p.get("is_published"), "indexed": p.get("website_indexed")} for p in pages]
        rows["menus"] = [{"name": m.get("name"), "url": m.get("url"), "parent": m2o_id(m.get("parent_id")) or None}
                         for m in fetch("website.menu", ["name", "url", "parent_id", "website_id"], [("website_id", "!=", False)])]
        rows["blogs"] = [b.get("name") for b in fetch("blog.blog", ["name"])]
        rows["blog_posts"] = [{"name": b.get("name"), "published": b.get("is_published")} for b in fetch("blog.post", ["name", "is_published"])]
        rows["languages"] = [lang.get("code") for lang in fetch("res.lang", ["code"], [("active", "=", True)])]
        rows["redirects"] = [{"from": r.get("url_from"), "to": r.get("url_to")} for r in fetch("website.rewrite", ["url_from", "url_to"])]
        rows["themes"] = [n for n in installed if n.startswith("theme_")]
        rows["custom_assets"] = [a.get("path") for a in fetch("ir.asset", ["path", "website_id"], [("website_id", "!=", False), ("path", "=like", "/_custom/%")])]
        not_content = {}
        for model in NOT_CONTENT_MODELS:
            if self.model_exists(model):
                try:
                    not_content[model] = self.source.call(model, "search_count", [[]], {"context": {"active_test": False}})
                except xmlrpc.client.Fault:
                    not_content[model] = "unreadable"
        rows["not_content_counts"] = not_content
        rows["server_version"] = info["server_version"]
        rows["installed_module_count"] = len(installed)
        rows["website_modules"] = info["website_modules"]
        return rows

    def plan_models(self, discovered_extra=True):
        models = [(m, d, e) for m, d, e in CORE_MODELS if self.model_exists(m)]
        for key in self.include:
            for model, domain, extra in OPTIONAL_MODELS.get(key, []):
                if self.model_exists(model):
                    models.append((model, domain, extra))
        if discovered_extra:
            known = {m for m, _d, _e in models}
            for model in self.discover_website_models():
                if model not in known and model not in EXCLUDED_MODELS:
                    models.append((model, "discovered", []))
        return models

    # -- export
    def run(self, resume=False):
        os.makedirs(os.path.join(self.out, "data"), exist_ok=True)
        os.makedirs(os.path.join(self.out, "files"), exist_ok=True)
        state = self._load_state() if resume else {"models": {}}
        if not resume and os.path.exists(os.path.join(self.out, "manifest.json")):
            raise MigrateError(f"{self.out} already holds an export; use --resume or another directory")
        self.manifest.update(self.probe())
        self._write_json("inventory.json", self.inventory())
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
        # Public binary content of the site, and the /_custom/ SCSS files of the site's design. `url`
        # attachments only redirect to a module's static image: they are not content.
        return [
            "|", ("url", "=like", "/_custom/%"),
            "&", "&", "&", ("public", "=", True), ("type", "!=", "url"), "!", ("url", "=like", "/web/assets/%"),
            "|", ("website_id", "!=", False), ("res_model", "in", ATTACHMENT_RES_MODELS),
        ]

    def shipped_ids(self, model):
        """Ids of records the installed modules created themselves (they have an external id from a
        module): default data, not the site owner's content."""
        rows = self.source.call(
            "ir.model.data", "search_read", [[("model", "=", model), ("module", "!=", "__export__")]],
            {"fields": ["res_id"]},
        )
        return sorted({row["res_id"] for row in rows})

    def export_model(self, model, domain, extra, previous):
        if domain == "discovered":
            try:
                domain = [("id", "not in", self.shipped_ids(model))]
                self.fields_of(model)
            except xmlrpc.client.Fault as exc:
                self.manifest["skipped"][model] = {"unreadable": str(exc)[:120]}
                self.log(f"skip {model}: not readable ({str(exc)[:60]})")
                return {"done": True, "count": 0, "last_id": 0, "sha256": "", "fields": []}
        fields = self.fields_of(model)
        wanted = [
            name for name, meta in fields.items()
            if name not in SKIP_FIELDS and meta.get("store", True) and meta["type"] not in ("binary", "one2many", "properties")
        ]
        skipped_secret = secret_fields(wanted)
        wanted = [n for n in wanted if n not in skipped_secret]
        wanted += [name for name in extra if name in fields and name not in wanted]
        copy_fields = [f for f in BINARY_COPY.get(model, []) if f in fields]
        binaries = sorted(
            n for n, m in fields.items()
            if m["type"] == "binary" and model != "ir.attachment" and n not in copy_fields
            and (model, n) not in NOISY_BINARIES
        )
        if skipped_secret or binaries:
            self.manifest["skipped"][model] = {"secret_fields": skipped_secret, "binary_fields": binaries}
        if model == "ir.attachment":
            domain = self.attachment_domain() + [("id", "not in", self.shipped_ids(model))]
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
            fetched_last = rows[-1]["id"]
            rows = self.filter_rows(model, rows)
            with open(path, "a", encoding="utf-8") as handle:  # audit-ignore-path
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
            if model == "ir.attachment":
                for row in rows:
                    self.export_attachment_file(row)
            if rows:
                for field in copy_fields:
                    self.export_binary_field(model, [r["id"] for r in rows], field)
            last_id, count = fetched_last, count + len(rows)
            self.log(f"{model}: {count} records")
        digest = sha256_file(path)
        self.manifest["files"][f"data/{model}.jsonl"] = digest
        info = {"done": True, "count": count, "last_id": last_id, "sha256": digest, "fields": wanted}
        if model == "ir.ui.view":
            info["view_classes"] = {k: sorted(v, key=str) for k, v in self._view_classes.items()}
            if previous and previous.get("view_classes"):
                for cls, keys in previous["view_classes"].items():
                    info["view_classes"][cls] = sorted(set(info["view_classes"].get(cls, [])) | set(keys), key=str)
        return info

    # -- row filters
    def page_view_ids(self):
        if self._page_views is None:
            rows = self.source.call("website.page", "search_read", [[("website_id", "!=", False)]],
                                    {"fields": ["view_id"], "context": {"active_test": False}})
            self._page_views = {m2o_id(r.get("view_id")) for r in rows}
        return self._page_views

    def filter_rows(self, model, rows):
        """Drops rows that are not the site owner's content (see classify_view) and tags the rest."""
        if model != "ir.ui.view":
            return rows
        keys = sorted({r["key"] for r in rows if r.get("key")})
        generic = {}
        for start in range(0, len(keys), 100):
            found = self.source.call(
                "ir.ui.view", "search_read",
                [[("key", "in", keys[start:start + 100]), ("website_id", "=", False)]],
                {"fields": ["key", "arch_db", "active"], "context": {"active_test": False}},
            )
            for item in found:
                generic.setdefault(item["key"], item)
        pages = self.page_view_ids()
        kept = []
        for row in rows:
            cls = classify_view(row, generic, pages)
            self._view_classes.setdefault(cls, []).append(row.get("key") or row["id"])
            if cls in ("theme", "stock_copy"):
                continue
            row["_class"] = cls
            kept.append(row)
        parents = sorted({m2o_id(r.get("inherit_id")) for r in kept if m2o_id(r.get("inherit_id"))})
        parent_key = {}
        if parents:
            found = self.source.call("ir.ui.view", "read", [parents], {"fields": ["key"], "context": {"active_test": False}})
            parent_key = {item["id"]: item.get("key") for item in found}
        for row in kept:
            row["_inherit_key"] = parent_key.get(m2o_id(row.get("inherit_id"))) or False
        return kept

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

    def export_binary_field(self, model, ids, field):
        for record in self.source.call(model, "read", [ids], {"fields": [field]}):
            encoded = record.get(field)
            if not encoded:
                continue
            raw = base64.b64decode(encoded)
            name = f"files/{model}__{record['id']}__{field}"
            with open(os.path.join(self.out, name), "wb") as handle:  # audit-ignore-path
                handle.write(raw)
            self.manifest["files"][name] = sha256_bytes(raw)

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
# Verified by [@ANCHOR: test_odoo_site_migrate:verify_export]
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

    def __init__(self, export_dir, target, apply=False, source_domains=(), website_map=None, log=print,
                 forms_notice=DEFAULT_FORMS_NOTICE, skip_menu_urls=(), remove_extra_menus=False,
                 archive_extra_blogs=False):
        self.dir, self.target, self.apply = export_dir, target, apply
        self.domains = [d.lower() for d in source_domains]
        # Target ids are integers: the command line hands them over as text, and a text id is a missing record.
        self.website_map = {str(source): int(target) for source, target in dict(website_map or {}).items()}
        self.log = log
        self.forms_notice = forms_notice  # None keeps website forms as they are
        self.skip_menu_urls = tuple(skip_menu_urls)
        self.remove_extra_menus = remove_extra_menus
        self.archive_extra_blogs = archive_extra_blogs
        self.idmap_path = os.path.join(export_dir, "idmap.json")
        self.idmap = self._load_idmap()
        self.report = {"created": {}, "updated": {}, "matched": {}, "dropped_fields": {}, "unsupported": [],
                       "redirects": [], "html": {}, "warnings": [], "lost": [], "forms_replaced": 0, "skipped": []}
        self._target_fields = {}
        self._modules = None
        self.data = {}
        self.hold = self._load_hold()

    def target_modules(self):
        """Installed module names in the target, or an empty set when it cannot be told."""
        if self._modules is None:
            rows = self.target.call("ir.module.module", "search_read", [[("state", "=", "installed")]], {"fields": ["name"]})
            self._modules = {row["name"] for row in rows}
        return self._modules

    def module_missing(self, module):
        known = self.target_modules()
        return bool(known) and module not in known

    # -- bookkeeping
    def _load_hold(self):
        """hold.json of a converted export: {"blog.post": [ids], "website.page": [ids]} (source ids)."""
        try:
            with open(os.path.join(self.dir, "hold.json"), "r", encoding="utf-8") as handle:  # audit-ignore-path
                return {model: {int(i) for i in ids} for model, ids in json.load(handle).items()}
        except FileNotFoundError:
            return {}

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
        are listed in the report as dropped. A reference to another record is kept only when the
        record was imported (its id is in the id map): the source's own ids mean nothing in the target
        and writing them blindly would point, for example, a website at the wrong user or theme."""
        target = self.fields(model)
        values, dropped = {}, []
        for name, value in row.items():
            if name in SKIP_FIELDS or name in extra_skip or name.startswith("_"):
                continue
            meta = target.get(name)
            if meta is None or (meta.get("readonly") and not meta.get("required")):
                if value not in (False, None, [], ""):
                    dropped.append(name)
                continue
            if meta["type"] == "many2one":
                if not value:
                    values[name] = False
                    continue
                new = self.mapped(meta.get("relation"), value)
                if new:
                    values[name] = new
                else:
                    dropped.append(f"{name}->{meta.get('relation')}")
            elif meta["type"] == "many2many":
                new = [self.mapped(meta.get("relation"), item) for item in value or []]
                new = [item for item in new if item and (item > 0 or not self.apply)]
                if new:
                    values[name] = [(6, 0, new)]
                elif value:
                    dropped.append(f"{name}->{meta.get('relation')}")
            elif meta["type"] == "one2many":
                if value:
                    dropped.append(name)
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

    def create_or_match(self, model, source_id, domain, values, update=False, context=None):
        """Returns the target id. Matches by `domain` first (natural key). Honest in dry-run: the id
        is a negative placeholder for a record that would be created. `context` goes to the create call."""
        existing = self.target.call(model, "search", [domain], {"limit": 1, "context": {"active_test": False}})
        if source_id in self.hold.get(model, set()):
            # Held for review (hold.json of a converted export): always created unpublished, and a later import
            # never changes the publication state, so a record Bruce has published in Odoo stays published.
            if existing:
                values = {k: v for k, v in values.items() if k not in PUBLICATION_FIELDS}
            else:
                values = dict(values, **{k: False for k in PUBLICATION_FIELDS if k in values})
            if [model, source_id] not in self.report.setdefault("held", []):
                self.report["held"].append([model, source_id])
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
        new_id = self.target.call(model, "create", [values], {"context": context} if context else {})
        new_id = new_id[0] if isinstance(new_id, list) else new_id
        self._remember(model, source_id, new_id)
        return new_id

    # -- link rewriting
    def rewrite(self, html):
        # A dry run has only placeholder (negative) ids for what it would create; counting the
        # rewrites with them still shows how many links will change.
        def usable(model):
            return {int(k): v for k, v in self.idmap.get(model, {}).items() if v > 0 or not self.apply}

        return rewrite_html(
            html, self.domains, usable("ir.attachment"), usable("blog.blog"), usable("blog.post"),
            self.report["html"],
        )

    # -- the run
    def run(self):
        for model in ("website", "ir.attachment", "ir.ui.view", "ir.asset", "website.page", "website.menu", "website.rewrite",
                      "blog.blog", "blog.tag.category", "blog.tag", "blog.post"):
            self.data[model] = read_jsonl(os.path.join(self.dir, "data", f"{model}.jsonl"))
        self.check_languages()
        self.import_websites()
        self.import_attachments()
        self.import_views()
        self.import_assets()
        self.import_pages()
        self.import_menus()
        self.import_blog()
        self.import_rewrites()  # after the blog: redirects may point at imported posts
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

    def sole_target_website(self):
        """The one target website of an import that has a single source website (--website-map or the id
        remembered for it), else False."""
        candidates = {v for v in self.website_map.values()} | {v for v in self.idmap.get("website", {}).values() if v}
        return next(iter(candidates)) if len(candidates) == 1 else False

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
            # Signup settings are never copied: the tenant's lockdown decides them.
            values = self.clean("website", site, extra_skip=(
                "domain", "company_id", "default_lang_id", "language_ids", "auth_signup_uninvited", "specific_user_account"))
            values = {k: v for k, v in values.items() if v not in (False, None)}
            for field in BINARY_COPY["website"]:
                path = os.path.join(self.dir, "files", f"website__{site['id']}__{field}")
                if os.path.exists(path) and field in self.fields("website"):
                    with open(path, "rb") as handle:  # audit-ignore-path
                        values[field] = base64.b64encode(handle.read()).decode()
            self._count("updated" if self.apply else "matched", "website")
            if self.apply and values:
                self.target.call("website", "write", [[target_id], values])
            if self.apply:
                self.set_company_name(target_id, site.get("name"))
        self._save_idmap()

    def set_company_name(self, website_id, site_name):
        """A new Odoo's company is called "My Company"; it appears in /website/info and in the default
        contact details. Give it the site's name, but never replace a name somebody already chose."""
        if not site_name or not self.has_model("res.company"):
            return
        rows = self.target.call("website", "read", [[website_id]], {"fields": ["company_id"]})
        company = m2o_id(rows[0].get("company_id")) if rows else False
        if not company:
            return
        current = self.target.call("res.company", "read", [[company]], {"fields": ["name"]})[0].get("name")
        if current in ("My Company", "YourCompany"):
            self.target.call("res.company", "write", [[company], {"name": site_name}])
            self._count("updated", "res.company (name)")

    def import_attachments(self):
        done = 0
        for row in self.data["ir.attachment"]:
            path = os.path.join(self.dir, "files", f"att_{row['id']}")
            if (row.get("url") or "").startswith("/_custom/"):
                continue  # a design file: applied by import_assets through Odoo's own asset customization
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
            # image_no_postprocess: Odoo would otherwise shrink every photo over 1920 px and re-encode it at
            # quality 80. That changes the bytes (so the checksum above no longer matches and a second run
            # created every such photo again, found 2026-10-05) and loses the original. Keep the original.
            self.create_or_match("ir.attachment", row["id"], domain, values, context={"image_no_postprocess": True})
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
            self.import_one_view(view)
        self._save_idmap()

    def import_one_view(self, view):
        website = self.target_website_for(view.get("website_id"))
        key = view.get("key") or ""
        cls = view.get("_class") or "custom"
        label = key or str(view.get("name") or view["id"])
        if key and cls in ("toggle", "customized") and self.module_missing(key.split(".", 1)[0]):
            self.report["lost"].append({"view": label, "kind": cls, "reason": f"module {key.split('.', 1)[0]} is not installed in the target"})
            return
        if cls == "toggle":
            self.apply_toggle(view, website)
            return
        values = self.clean("ir.ui.view", view, extra_skip=("inherit_id", "model_data_id", "arch_fs", "arch", "arch_prev"))
        values["website_id"] = website or False
        parent = self.mapped("ir.ui.view", view.get("inherit_id")) or self.view_key_target(view.get("inherit_id"))
        if not parent and view.get("_inherit_key"):
            found = self.target.call("ir.ui.view", "search", [[("key", "=", view["_inherit_key"])]],
                                     {"limit": 1, "order": "website_id desc", "context": {"active_test": False}})
            parent = found[0] if found else False
        if m2o_id(view.get("inherit_id")) and not parent:
            self.report["lost"].append({"view": label, "kind": cls, "reason": "its parent view does not exist in the target"})
            return
        if parent and parent > 0:
            values["inherit_id"] = parent
        if view.get("arch_db"):
            arch = view["arch_db"]
            if self.forms_notice is not None and cls == "page":
                arch, replaced = neutralize_forms(arch, self.forms_notice)
                if replaced:
                    self.report["forms_replaced"] += replaced
                    self.report["warnings"].append(f"page view {label}: {replaced} form(s) replaced by a notice (no outgoing mail)")
            values["arch_db"] = arch
        domain = [("key", "=", view.get("key")), ("website_id", "=", website or False)] if view.get("key") else [
            ("name", "=", view.get("name")), ("website_id", "=", website or False)]
        try:
            self.create_or_match("ir.ui.view", view["id"], domain, values, update=True)
        except xmlrpc.client.Fault as exc:
            self.report["lost"].append({"view": label, "kind": cls, "reason": f"the target rejected it: {str(exc.faultString)[:160]}"})

    def apply_toggle(self, view, website):
        """A header/footer option the owner switched: set the same switch on the target's own view for the
        website. Writing a generic view with the website in the context makes Odoo copy it for that website."""
        key, wanted = view["key"], bool(view.get("active"))
        generic = self.target.call("ir.ui.view", "search", [[("key", "=", key), ("website_id", "=", False)]],
                                   {"limit": 1, "context": {"active_test": False}})
        if not generic:
            self.report["lost"].append({"view": key, "kind": "toggle", "reason": "the target has no such view"})
            return
        current = self.target.call("ir.ui.view", "search_read", [[("key", "=", key), ("website_id", "in", [website or False])]],
                                   {"fields": ["active", "website_id"], "context": {"active_test": False}})
        specific = [r for r in current if m2o_id(r.get("website_id"))]
        effective = bool((specific or current or [{"active": True}])[0].get("active"))
        if effective == wanted:
            self._count("matched", "ir.ui.view (switch)")
            return
        self._count("updated", "ir.ui.view (switch)")
        if self.apply:
            try:
                self.target.call("ir.ui.view", "write", [[(specific[0]["id"] if specific else generic[0])], {"active": wanted}],
                                 {"context": {"website_id": website}} if website else {})
            except xmlrpc.client.Fault as exc:
                self.report["lost"].append({"view": key, "kind": "toggle", "reason": f"the target rejected it: {str(exc.faultString)[:160]}"})

    def import_assets(self):
        """The site's own SCSS (colour palette, fonts) goes in through Odoo's customization call, which
        creates the /_custom/ attachment and the replacing ir.asset exactly as the website editor does."""
        attachments = {a.get("url"): a for a in self.data.get("ir.attachment", []) if (a.get("url") or "").startswith("/_custom/")}
        for row in self.data.get("ir.asset", []):
            path = row.get("path") or ""
            match = re.match(r"^/_custom/([^/]+)/(.+\.(?:scss|js))$", path)
            attachment = attachments.get(path)
            source = os.path.join(self.dir, "files", f"att_{attachment['id']}") if attachment else ""
            if not match or not os.path.exists(source):
                self.report["lost"].append({"view": path, "kind": "asset", "reason": "no custom file in the export"})
                continue
            with open(source, "r", encoding="utf-8") as handle:  # audit-ignore-path
                content = handle.read()
            website = self.target_website_for(row.get("website_id"))
            target_url = row.get("target") or "/" + match.group(2)
            kind = "js" if path.endswith(".js") else "scss"
            self._count("updated" if self.apply else "matched", "design asset")
            if not self.apply:
                continue
            try:
                self.target.call("website.assets", "save_asset", [target_url, match.group(1), content, kind],
                                 {"context": {"website_id": website}} if website else {})
            except xmlrpc.client.Fault as exc:
                # save_asset returns None, which Odoo's XML-RPC layer cannot marshal; the work was done.
                if "cannot marshal None" not in str(exc.faultString):
                    self.report["lost"].append({"view": path, "kind": "asset", "reason": f"the target rejected it: {str(exc.faultString)[:160]}"})

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
        skipped_ids = set()
        for menu in order:
            if m2o_id(menu.get("parent_id")) in skipped_ids:
                skipped_ids.add(menu["id"])
                continue
            website = self.target_website_for(menu.get("website_id"))
            url = menu.get("url") or ""
            module = module_for_url(url)
            if any(url == bad or url.startswith(bad.rstrip("/") + "/") for bad in self.skip_menu_urls) or (
                    module and self.module_missing(module)):
                skipped_ids.add(menu["id"])
                why = f"module {module} is not installed in the target" if module else "excluded on the command line"
                self.report["skipped"].append({"menu": menu.get("name"), "url": url, "reason": why})
                continue
            if not m2o_id(menu.get("parent_id")) and url.startswith("/default-main-menu"):
                # The site's root menu: the target website already has one, which the site's menu hangs from.
                root = self.target.call("website.menu", "search", [[("website_id", "=", website or False), ("parent_id", "=", False)]],
                                        {"limit": 1, "order": "id"}) if website else []
                if root:
                    self._remember("website.menu", menu["id"], root[0])
                    self._count("matched", "website.menu")
                    continue
            values = self.clean("website.menu", menu, extra_skip=("parent_id", "page_id", "child_id", "parent_path", "mega_menu_content"))
            values["website_id"] = website or False
            parent = self.mapped("website.menu", menu.get("parent_id"))
            if parent and parent > 0:
                values["parent_id"] = parent
            page = self.mapped("website.page", menu.get("page_id"))
            if page and page > 0:
                values["page_id"] = page
            parent_term = [("parent_id", "=", parent)] if parent and parent > 0 else (
                [("parent_id", "=", False)] if not m2o_id(menu.get("parent_id")) else [])
            # A menu the target already has for the same address is the same entry under another name
            # (the default "Home" is the source's "PostOpen.org"): match by URL first, then by name.
            by_url = []
            if url and parent_term and url not in ("#", ""):
                by_url = self.target.call("website.menu", "search", [[("url", "=", url), ("website_id", "=", website or False)] + parent_term],
                                          {"limit": 1})
            domain = [("url", "=", url), ("website_id", "=", website or False)] + parent_term if by_url else [
                ("name", "=", menu["name"]), ("website_id", "=", website or False)] + parent_term
            self.create_or_match("website.menu", menu["id"], domain, values, update=True)
        self.report_extra_menus(website_ids=sorted({m for m in self.idmap.get("website", {}).values()}))
        self._save_idmap()

    def report_extra_menus(self, website_ids):
        """Menu entries the target had before the import and the source does not have. Listed; removed only
        with --remove-extra-menus (the one deliberate delete of target data, of its own default entries)."""
        if not self.data.get("website.menu"):
            return
        kept = {v for v in self.idmap.get("website.menu", {}).values() if v > 0}
        for website in website_ids:
            rows = self.target.call("website.menu", "search_read", [[("website_id", "=", website), ("parent_id", "!=", False)]],
                                    {"fields": ["name", "url"], "context": {"active_test": False}})
            for row in rows:
                if row["id"] in kept:
                    continue
                self.report["warnings"].append(f"target menu {row['name']!r} ({row.get('url')}) is not in the source")
                if self.remove_extra_menus and self.apply:
                    self.target.call("website.menu", "unlink", [[row["id"]]])
                    self._count("updated", "website.menu (removed extra)")

    def import_rewrites(self):
        for row in self.data["website.rewrite"]:
            website = self.target_website_for(row.get("website_id"))
            values = self.clean("website.rewrite", row)
            # A converted site points old URLs at attachments and posts by their SOURCE ids (/web/image/12,
            # /blog/blog-1/title-7); they are mapped to the target ids exactly like links inside content.
            if values.get("url_to"):
                values["url_to"] = self.rewrite(values["url_to"])
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
        scoped = "website_id" in self.fields("blog.blog")
        for blog in self.data["blog.blog"]:
            values = self.clean("blog.blog", blog)
            match = [("name", "=", blog["name"])]
            if scoped:
                # A blog with no website shows on EVERY website of the target (ADR 0106: hams.com's own /blog
                # would list another site's posts), and a blog of the same name on another website is not
                # this one: a blog belongs to the target website it is imported for.
                website = self.target_website_for(blog.get("website_id")) or self.sole_target_website()
                values["website_id"] = website
                match.append(("website_id", "=", website))
            self.create_or_match("blog.blog", blog["id"], match, values, update=True)
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
        self.report_extra_blogs()
        self._save_idmap()

    def report_extra_blogs(self):
        """Blogs the target had before (Odoo creates "Our blog") and the source does not have. They show
        on /blog next to the imported ones; --archive-extra-blogs archives them (reversible, nothing deleted)."""
        kept = {v for v in self.idmap.get("blog.blog", {}).values() if v > 0}
        # Only blogs that belong to the target website itself. On a database that serves other websites
        # (ADR 0106: hams_prod) a blog of another website, or one shared by all websites, is not this
        # import's to report, and above all not its to archive.
        websites = sorted({m for m in self.idmap.get("website", {}).values() if m})
        rows = self.target.call("blog.blog", "search_read", [[("website_id", "in", websites)]], {"fields": ["name"]})
        for row in rows:
            if row["id"] in kept:
                continue
            self.report["warnings"].append(f"target blog {row['name']!r} is not in the source and still shows on /blog")
            if self.archive_extra_blogs and self.apply and "active" in self.fields("blog.blog"):
                self.target.call("blog.blog", "write", [[row["id"]], {"active": False}])
                self._count("updated", "blog.blog (archived extra)")

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
            warning = f"blog author {name!r} has no partner in the target; post author left to the default"
            if warning not in self.report["warnings"]:
                self.report["warnings"].append(warning)
        return False

    # -- second pass: rewrite internal references now that every id is known
    def rewrite_content(self):
        passes = (("ir.ui.view", "arch_db"), ("blog.post", "content"), ("blog.post", "teaser_manual"),
                  ("blog.post", "cover_properties"), ("website.menu", "mega_menu_content"), ("website.menu", "url"))
        for model, field in passes:
            for row in self.data.get(model) or read_jsonl(os.path.join(self.dir, "data", f"{model}.jsonl")):
                value = row.get(field)
                new_id = self.mapped(model, row["id"])
                if not value or not new_id or (new_id < 0 and self.apply) or field not in self.fields(model):
                    continue
                rewritten = self.rewrite(value)
                if self.forms_notice is not None and field in ("arch_db", "content"):
                    rewritten, replaced = neutralize_forms(rewritten, self.forms_notice)
                    if replaced and model != "ir.ui.view":
                        self.report["forms_replaced"] += replaced
                        self.report["warnings"].append(f"{model} {row['id']}: {replaced} form(s) replaced by a notice")
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
                if not new_id or new_id < 0:
                    continue
                # blog.blog has no website_url field: its address is /blog/<slug>-<id>
                old_url = row.get("website_url") or (f"/blog/{slugify(row.get('name'))}-{row['id']}" if model == "blog.blog" else None)
                if not old_url:
                    continue
                new_url = None
                if model == "blog.post" or "website_url" in self.fields(model):
                    new_url = self.target.call(model, "read", [[new_id]], {"fields": ["website_url"]})[0].get("website_url")
                if not new_url and model == "blog.blog":
                    new_url = f"/blog/{slugify(row.get('name'))}-{new_id}"
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
                   "blog.blog", "blog.tag.category", "blog.tag", "blog.post", "res.lang", "ir.asset"}
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
    for item in report.get("lost", []):
        lines.append(f"  LOST {item['kind']} {item['view']}: {item['reason']}")
    for item in report.get("skipped", []):
        lines.append(f"  SKIPPED menu {item['menu']!r} ({item['url']}): {item['reason']}")
    if report.get("forms_replaced"):
        lines.append(f"  forms replaced by a static notice: {report['forms_replaced']}")
    for item in report["unsupported"]:
        lines.append(f"  UNSUPPORTED {item}")
    for model, ident in report.get("held", []):
        lines.append(f"  HELD {model} {ident}: unpublished, held for review (hold.json)")
    for item in report["warnings"]:
        lines.append(f"  WARNING {item}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Lockdown: a public-site tenant has no signup, no invitations and no mail (NIGHT_PLAN decision 38)
# --------------------------------------------------------------------------------------------

# Settings that open a way to create a login or reset a password.
LOCKDOWN_PARAMS = {"auth_signup.invitation_scope": "b2b", "auth_signup.reset_password": "False"}
# Scheduled actions that send or fetch mail (external id module, name).
MAIL_CRONS = (
    ("mail", "ir_cron_mail_scheduler_action"), ("mail", "ir_cron_mail_gateway_action"),
    ("mail", "ir_cron_send_scheduled_message"), ("mail", "ir_cron_post_scheduled_message"),
    ("mail", "ir_cron_web_push_notification"), ("auth_signup", "ir_cron_auth_signup_send_pending_user_reminder"),
)


# [@ANCHOR: odoo_site_migrate:lockdown]
# Verified by [@ANCHOR: test_odoo_site_migrate:lockdown]
def lockdown_target(target, apply=False, log=print):
    """Makes a tenant a public site only. Idempotent; a dry run lists what would change.

    - auth_signup.invitation_scope = b2b (a signup needs an invitation, and none can be sent) and password
      reset off, so /web/signup and /web/reset_password answer 404;
    - the website's own signup setting, where the field exists, is set the same way;
    - every outgoing mail server is deactivated, every mail template is deactivated, and the scheduled
      actions that send or fetch mail are switched off;
    - nothing is deleted. Returns a list of {"what", "from", "to", "changed"}.
    """
    actions = []

    def record(what, before, after):
        changed = before != after
        actions.append({"what": what, "from": before, "to": after, "changed": changed})
        log(f"  {'SET ' if changed else 'ok  '} {what}: {before!r} -> {after!r}")
        return changed

    for key, wanted in LOCKDOWN_PARAMS.items():
        rows = target.call("ir.config_parameter", "search_read", [[("key", "=", key)]], {"fields": ["key", "value"]})
        current = rows[0]["value"] if rows else None
        if record(f"config parameter {key}", current, wanted) and apply:
            if rows:
                target.call("ir.config_parameter", "write", [[rows[0]["id"]], {"value": wanted}])
            else:
                target.call("ir.config_parameter", "create", [{"key": key, "value": wanted}])
    fields = target.call("website", "fields_get", [], {"attributes": ["type"]})
    if "auth_signup_uninvited" in fields:
        for site in target.call("website", "search_read", [[]], {"fields": ["auth_signup_uninvited", "name"]}):
            if record(f"website {site['name']!r} auth_signup_uninvited", site.get("auth_signup_uninvited"), "b2b") and apply:
                target.call("website", "write", [[site["id"]], {"auth_signup_uninvited": "b2b"}])
    for model, label in (("ir.mail_server", "outgoing mail server"), ("mail.template", "mail template")):
        if target.call("ir.model", "search_count", [[("model", "=", model)]]) == 0:
            continue
        rows = target.call(model, "search_read", [[("active", "=", True)]], {"fields": ["id"]})
        record(f"active {label}s", len(rows), 0)
        if rows and apply:
            target.call(model, "write", [[r["id"] for r in rows], {"active": False}])
    for module, name in MAIL_CRONS:
        data = target.call("ir.model.data", "search_read", [[("module", "=", module), ("name", "=", name), ("model", "=", "ir.cron")]],
                           {"fields": ["res_id"]})
        for item in data:
            rows = target.call("ir.cron", "search_read", [[("id", "=", item["res_id"])]], {"fields": ["active"], "context": {"active_test": False}})
            if rows and record(f"scheduled action {module}.{name} active", bool(rows[0].get("active")), False) and apply:
                target.call("ir.cron", "write", [[rows[0]["id"]], {"active": False}])
    return actions


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


def open_source(args):
    """(source, counter): a live Odoo over XML-RPC, or an offline snapshot database (--snapshot-db)."""
    if getattr(args, "snapshot_db", None):
        import odoo_snapshot_source as snap

        if args.creds:
            raise MigrateError("use either --creds (a live server) or --snapshot-db (an offline snapshot), not both")
        dsn = args.pg_dsn or f"dbname={args.snapshot_db}"
        source = snap.SnapshotSource(snap.PgDb(dsn), args.filestore)
        return ReadOnlySource(source), source
    if not args.creds:
        raise MigrateError("--creds FILE (a live server) or --snapshot-db NAME (an offline snapshot) is required")
    creds = load_credentials(args.creds)
    transport = XmlRpcTransport(creds, args.min_interval, args.max_requests)
    return ReadOnlySource(transport), transport


def cmd_probe(args, out):
    source, _counter = open_source(args)
    exporter = Exporter(source, ".", log=out)
    out(json.dumps(exporter.probe(), indent=2, sort_keys=True))
    return 0


def cmd_inventory(args, out):
    source, _counter = open_source(args)
    exporter = Exporter(source, ".", log=out)
    out(json.dumps(exporter.inventory(), indent=2, sort_keys=True))
    return 0


def cmd_restore(args, out):
    import odoo_snapshot_source as snap

    try:
        snap.restore_snapshot(args.dump, args.db)
    except snap.SnapshotError as exc:
        raise MigrateError(str(exc)) from exc
    out(f"restored {args.dump} into scratch database {args.db} (pg_restore warnings about roles are normal)")
    return 0


def cmd_export(args, out):
    source, counter = open_source(args)
    include = [item for item in (args.include or "").split(",") if item]
    exporter = Exporter(source, args.out, include, args.page_size, args.max_file_mb, out)
    os.makedirs(args.out, mode=0o700, exist_ok=True)
    manifest = exporter.run(args.resume)
    unit = "queries" if getattr(args, "snapshot_db", None) else "requests"
    out(f"exported {sum(m.get('count', 0) for m in manifest['models'].values())} records in "
        f"{len(manifest['models'])} models; {getattr(counter, 'requests', 0)} {unit}")
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
    target = make_transport(args.target_transport, creds, min_interval=0.0)
    website_map = dict(item.split(":", 1) for item in args.website_map or [])
    notice = None if args.keep_forms else args.forms_notice
    importer = Importer(args.export, target, args.apply, args.source_domain or [], website_map, out,
                        forms_notice=notice, skip_menu_urls=args.skip_menu_url or [],
                        remove_extra_menus=args.remove_extra_menus, archive_extra_blogs=args.archive_extra_blogs)
    problems = verify_export(args.export)
    if problems:
        raise MigrateError(f"the export is damaged: {problems[:3]}")
    report = importer.run()
    out(render_report(report, args.apply))
    if args.apply:
        with open(os.path.join(args.export, "import_report.json"), "w", encoding="utf-8") as handle:  # audit-ignore-path
            json.dump(report, handle, indent=2, sort_keys=True)
    return 0


def cmd_lockdown(args, out):
    try:
        with open(args.target_password_file, "r", encoding="utf-8") as handle:  # audit-ignore-path
            secret = handle.read().strip()
    except OSError as exc:
        raise MigrateError(f"cannot read {args.target_password_file}: {exc}") from exc
    creds = Credentials(args.target_url, args.target_db, args.target_login, secret)
    target = make_transport(args.target_transport, creds, min_interval=0.0)
    out("APPLYING lockdown" if args.apply else "DRY RUN lockdown (nothing is written)")
    actions = lockdown_target(target, args.apply, out)
    changed = sum(1 for a in actions if a["changed"])
    out(f"{changed} setting(s) {'changed' if args.apply else 'would change'}; {len(actions) - changed} already as required")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def source_opts(p):
        p.add_argument("--creds", help="credentials file of a live server")
        p.add_argument("--snapshot-db", help="scratch database holding a restored dump (offline source; see `restore`)")
        p.add_argument("--filestore", help="directory of the snapshot's filestore (with --snapshot-db)")
        p.add_argument("--pg-dsn", help="libpq connection string for --snapshot-db (default: dbname=<snapshot-db>)")
        p.add_argument("--min-interval", type=float, default=0.5)
        p.add_argument("--max-requests", type=int, default=0)

    p = sub.add_parser("probe")
    source_opts(p)
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("inventory", help="what makes up the public site and what does not (read only)")
    source_opts(p)
    p.set_defaults(func=cmd_inventory)
    p = sub.add_parser("restore", help="restore a pg_dump (custom format) into a NEW scratch database mig_<name>")
    p.add_argument("--dump", required=True)
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_restore)
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
    p.add_argument("--target-password-file", required=True, help="the admin password (xmlrpc) or an API key (json2)")
    p.add_argument("--target-transport", choices=("xmlrpc", "json2"), default="xmlrpc",
                   help="json2 (Odoo 19+, API key) replaces the deprecated XML-RPC on the target; default xmlrpc")
    p.add_argument("--source-domain", action="append", help="old domain(s); links to them become relative")
    p.add_argument("--website-map", action="append", help="SRC_ID:DST_ID (repeatable)")
    p.add_argument("--forms-notice", default=DEFAULT_FORMS_NOTICE,
                   help="text that replaces every website form (a tenant has no outgoing mail)")
    p.add_argument("--keep-forms", action="store_true", help="leave website forms as they are (only with mail configured)")
    p.add_argument("--skip-menu-url", action="append", help="do not import menu entries at or below this URL (repeatable)")
    p.add_argument("--remove-extra-menus", action="store_true",
                   help="delete the target's own default menu entries the source does not have (default: only list them)")
    p.add_argument("--archive-extra-blogs", action="store_true",
                   help="archive the target's own default blog(s) the source does not have (default: only list them)")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("lockdown", help="make the target a public site only: no signup, no invitations, no mail")
    p.add_argument("--target-url", required=True)
    p.add_argument("--target-db", required=True)
    p.add_argument("--target-login", default="admin")
    p.add_argument("--target-password-file", required=True, help="the admin password (xmlrpc) or an API key (json2)")
    p.add_argument("--target-transport", choices=("xmlrpc", "json2"), default="xmlrpc",
                   help="json2 (Odoo 19+, API key) replaces the deprecated XML-RPC on the target; default xmlrpc")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_lockdown)
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
