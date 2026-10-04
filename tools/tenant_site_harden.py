#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""tenant_site_harden: make a freshly created website tenant a public, mail-less, sign-up-less site.

    tenant_site_harden.py apply --url URL --db DB --password-file FILE [--site-name N] [--blog-name N] [--author-name N] [--write]
    tenant_site_harden.py check --url URL --db DB --password-file FILE

Night plan decision 38 (Bruce, 2026-10-03): the perens.com and postopen.org tenants are public sites with
NO e-mail (no outgoing or incoming mail features), and with sign-up, invitations and login creation
disabled; the back end is reachable only by the administrator through a private path.

What a stock Odoo `website` + `website_blog` install does that this switches off (each a tested step):

* `auth_signup.invitation_scope` is `b2c` ("free sign-up"): set to `b2b` (invited users only) and
  `auth_signup.reset_password` to False (the reset form sends mail); outgoing mail servers and mail templates are
  deactivated. These steps are `odoo_site_migrate.lockdown_target`, the importer's own `lockdown` command.
* The `/contactus` form posts to `mail.mail` through `/website/form/`: the Contact Us page and its thank-you
  page are unpublished, the "Contact us" menu entries removed and `mail.mail` is taken off the models the
  public form may create (`ir.model.website_form_access`).
* Scheduled actions that send mail, push notifications, SMS, letters or phone home (the mail queue, scheduled
  messages, web push, SMS and snail-mail queues, the digest, the "unregistered users" notifier and Odoo's
  publisher notification) are switched off.
* The stock theme's placeholders are removed: the "Contact Us" header button, the header phone/sentence
  element, the "Sign in" link, the demo footer ("We are a team of passionate people...") and the
  "Create a free website" advertisement; the copyright line carries the site name; the default logo is replaced
  by a plain text logo made from the site name (replace it later in the editor).
* The placeholder blog name is replaced, the administrator's partner is named after the
  site's author so imported posts show the right author, and the placeholder blog is renamed.

`apply` is a dry run unless `--write` is given and is idempotent (a second run changes nothing). `check` is
read-only and also probes the public site over HTTP: sign-up, password reset and the contact form must not
work, the login page may exist (administrator only), and no `mail.mail` or user record may appear. Nothing here
creates users, passwords or mail servers.
"""

import argparse
import re
import sys
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import odoo_site_migrate as mig  # noqa: E402

MAIL_CRON_NAMES = re.compile(r"(mail|notif|digest|sms|snailmail|publisher|push|unregistered|scheduled)", re.I)
CONTACT_URLS = ("/contactus", "/contactus-thank-you")
PLACEHOLDER_BLOG = "Our blog"
# Views of the stock theme that carry placeholder content or a link to a feature the tenant does not have.
THEME_VIEWS_OFF = (
    "website.header_call_to_action", "website.header_call_to_action_large", "website.header_call_to_action_sidebar",
    "website.header_call_to_action_stretched", "website.header_text_element", "portal.user_sign_in",
    "website.footer_custom", "website.brand_promotion",
)
FOOTER_COPYRIGHT_VIEW = "website.footer_copyright_company_name"
DEFAULT_SITE_NAME = "My Website"


def text_logo(text):
    """A plain SVG wordmark (base64, the format Odoo stores in `website.logo`)."""
    import base64
    import html as htmllib

    safe = htmllib.escape(text, quote=True)
    width = max(120, int(len(text) * 24 + 8))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="48" viewBox="0 0 {width} 48">'
           f'<text x="2" y="36" font-family="Georgia, serif" font-size="36" fill="#222222">{safe}</text></svg>')
    return base64.b64encode(svg.encode()).decode()


# [@ANCHOR: tenant_site_harden:hardener]
# Verified by [@ANCHOR: test_tenant_site_harden:hardener]
class Hardener:
    def __init__(self, target, write=False, log=print):
        self.t, self.write, self.log = target, write, log
        self.changes = []
        self.proxy_version = None

    def call(self, model, method, args, kwargs=None):
        return self.t.call(model, method, args, kwargs)

    def note(self, text, changed=True):
        if changed:
            self.changes.append(text)
        self.log(("CHANGE " if changed else "ok     ") + text + ("" if self.write or not changed else "  (dry run)"))

    # -- steps
    def lockdown(self):
        """Sign-up scope, password reset, mail servers, mail templates and the mail scheduled actions: the
        importer's own `lockdown` (odoo_site_migrate.lockdown_target), so the two cannot drift apart."""
        for action in mig.lockdown_target(self.t, self.write, lambda *_: None):
            self.note(f"{action['what']}: {action['from']!r} -> {action['to']!r}", changed=action["changed"])

    def crons(self):
        rows = self.call("ir.cron", "search_read", [[("active", "=", True)]], {"fields": ["cron_name"]})
        for row in rows:
            if MAIL_CRON_NAMES.search(row["cron_name"] or "") and not (row["cron_name"] or "").startswith("Notification: Delete"):
                self.note(f"scheduled action off: {row['cron_name']}")
                if self.write:
                    self.call("ir.cron", "write", [[row["id"]], {"active": False}])

    def contact_pages(self):
        for url in CONTACT_URLS:
            for page in self.call("website.page", "search_read", [[("url", "=", url)]], {"fields": ["is_published"]}):
                if page["is_published"]:
                    self.note(f"unpublish page {url}")
                    if self.write:
                        self.call("website.page", "write", [[page["id"]], {"is_published": False}])
        menus = self.call("website.menu", "search_read", [[("url", "in", list(CONTACT_URLS))]], {"fields": ["name"]})
        for menu in menus:
            self.note(f"remove menu entry {menu['name']!r} ({menu['id']})")
        if menus and self.write:
            # one call: removing a generic entry also removes its per-website copies
            self.call("website.menu", "unlink", [[m["id"] for m in menus]])

    def form_models(self):
        fields = self.call("ir.model", "fields_get", [], {"attributes": ["type"]})
        if "website_form_access" not in fields:
            return
        for model in self.call("ir.model", "search_read", [[("website_form_access", "=", True)]], {"fields": ["model"]}):
            self.note(f"public form may no longer create {model['model']}")
            if self.write:
                self.call("ir.model", "write", [[model["id"]], {"website_form_access": False}])

    def identity(self, site_name, blog_name, author_name):
        site = self.call("website", "search_read", [[]], {"fields": ["name", "logo"], "order": "id", "limit": 1})[0]
        values = {}
        if site_name and site["name"] != site_name:
            values["name"] = site_name
        if site_name and site.get("logo") != text_logo(site_name):
            values["logo"] = text_logo(site_name)
        if values:
            self.note(f"website {site['id']}: " + ", ".join(sorted(values)) + " (a text logo made from the site name)")
            if self.write:
                self.call("website", "write", [[site["id"]], values])
        else:
            self.note("website name and logo", changed=False)
        if blog_name:
            blogs = self.call("blog.blog", "search_read", [[]], {"fields": ["name"]}) if self.has("blog.blog") else []
            if len(blogs) == 1 and blogs[0]["name"] == PLACEHOLDER_BLOG:
                self.note(f"rename placeholder blog {PLACEHOLDER_BLOG!r} -> {blog_name!r}")
                if self.write:
                    self.call("blog.blog", "write", [[blogs[0]["id"]], {"name": blog_name}])
            else:
                self.note("placeholder blog", changed=False)
        if author_name:
            users = self.call("res.users", "search_read", [[("login", "=", "admin")]], {"fields": ["partner_id"], "limit": 1})
            if users:
                partner = self.call("res.partner", "read", [[users[0]["partner_id"][0]]], {"fields": ["name"]})[0]
                if partner["name"] != author_name:
                    self.note(f"administrator's partner named {author_name!r} (author of imported posts)")
                    if self.write:
                        self.call("res.partner", "write", [[partner["id"]], {"name": author_name}])
                else:
                    self.note("administrator's partner name", changed=False)

    def theme(self, site_name):
        views = self.call("ir.ui.view", "search_read", [[("key", "in", list(THEME_VIEWS_OFF)), ("active", "=", True)]],
                          {"fields": ["key"], "context": {"active_test": False}})
        for view in views:
            self.note(f"theme view off: {view['key']}")
        if views and self.write:
            self.call("ir.ui.view", "write", [[v["id"] for v in views], {"active": False}])
        if site_name:
            rows = self.call("ir.ui.view", "search_read", [[("key", "=", FOOTER_COPYRIGHT_VIEW)]], {"fields": ["arch_db"]})
            for row in rows:
                if "Company name" in (row["arch_db"] or ""):
                    self.note("footer copyright line carries the site name")
                    if self.write:
                        escaped = site_name.replace("&", "&amp;amp;").replace("<", "&amp;lt;")
                        arch = row["arch_db"].replace("Company name", escaped)
                        self.call("ir.ui.view", "write", [[row["id"]], {"arch_db": arch}])
        company = self.call("res.company", "search_read", [[]], {"fields": ["name"], "order": "id", "limit": 1})
        if site_name and company and company[0]["name"] != site_name:
            self.note(f"company name {company[0]['name']!r} -> {site_name!r}")
            if self.write:
                self.call("res.company", "write", [[company[0]["id"]], {"name": site_name}])

    def has(self, model):
        return bool(self.call("ir.model", "search_count", [[("model", "=", model)]]))

    def run(self, site_name=None, blog_name=None, author_name=None):
        self.lockdown()
        self.crons()
        self.contact_pages()
        self.form_models()
        self.theme(site_name)
        self.identity(site_name, blog_name, author_name)
        return self.changes


# --------------------------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------------------------


class _Session:
    """HTTP with a cookie jar and no redirects, so a probe sees exactly what the server answered."""

    def __init__(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        self.opener = urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def __call__(self, url, method="GET", data=None, headers=None):
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, response.read(400000).decode("utf-8", "replace"), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(400000).decode("utf-8", "replace"), dict(exc.headers)


def http_status(url, method="GET", data=None, headers=None):
    return _Session()(url, method, data, headers)


def csrf_token(fetch, base_url):
    """A real CSRF token from the login page (the same session), so the probes below are refused by the
    site's own rules and not merely by the missing token."""
    status, body, _ = fetch(base_url + "/web/login")
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body) or re.search(r'value="([^"]+)"[^>]*name="csrf_token"', body)
    return match.group(1) if match else ""


# [@ANCHOR: tenant_site_harden:run_checks]
# Verified by [@ANCHOR: test_tenant_site_harden:run_checks]
def run_checks(target, base_url, log=print, fetch=None):
    """Returns the list of failed checks (empty = pass)."""
    failures = []
    fetch = fetch or _Session()
    token = csrf_token(fetch, base_url)

    def expect(ok, text):
        log(("PASS " if ok else "FAIL ") + text)
        if not ok:
            failures.append(text)

    def value(key):
        rows = target.call("ir.config_parameter", "search_read", [[("key", "=", key)]], {"fields": ["value"], "limit": 1})
        return rows[0]["value"] if rows else None

    expect(value("auth_signup.invitation_scope") == "b2b", "sign-up is by invitation only (auth_signup.invitation_scope = b2b)")
    expect(value("auth_signup.reset_password") == "False", "password reset form is off (auth_signup.reset_password = False)")
    expect(target.call("ir.mail_server", "search_count", [[("active", "=", True)]]) == 0,
           "no outgoing mail server is active")
    expect(target.call("fetchmail.server", "search_count", [[]]) == 0 if target.call(
        "ir.model", "search_count", [[("model", "=", "fetchmail.server")]]) else True, "no incoming mail server is configured")
    active = [r["cron_name"] for r in target.call("ir.cron", "search_read", [[("active", "=", True)]], {"fields": ["cron_name"]})
              if MAIL_CRON_NAMES.search(r["cron_name"] or "") and not (r["cron_name"] or "").startswith("Notification: Delete")]
    expect(not active, f"no mail, push, SMS, digest or publisher scheduled action is active {active or ''}")
    forms = target.call("ir.model", "search_read", [[("website_form_access", "=", True)]], {"fields": ["model"]})
    expect(not forms, f"the public form creates nothing {[f['model'] for f in forms] or ''}")
    for url in CONTACT_URLS:
        pages = target.call("website.page", "search_read", [[("url", "=", url), ("is_published", "=", True)]],
                            {"fields": ["url"]})
        expect(not pages, f"page {url} is not published")
    users_before = target.call("res.users", "search_count", [[("active", "in", [True, False])]])
    mail_before = target.call("mail.mail", "search_count", [[]])
    partners_before = target.call("res.partner", "search_count", [[("active", "in", [True, False])]])
    for path in ("/web/signup", "/web/reset_password", "/contactus", "/contactus-thank-you"):
        status, body, _ = fetch(base_url + path)
        expect(status in (404, 403, 301, 302, 303, 308) and not re.search(r"<form[^>]+(signup|reset|contactus)", body, re.I)
               and "type=\"password\"" not in body and "name=\"email_from\"" not in body,
               f"GET {path} shows no form (HTTP {status})")
    status, body, _ = fetch(base_url + "/website/form/mail.mail", "POST",
                            urllib.parse.urlencode({"name": "probe", "email_from": "probe@example.invalid", "csrf_token": token,
                                                    "subject": "probe", "description": "probe"}).encode(),
                            {"Content-Type": "application/x-www-form-urlencoded"})
    expect(not (status == 200 and '"id"' in body), f"POST /website/form/mail.mail creates nothing (HTTP {status})")
    status, body, _ = fetch(base_url + "/web/signup", "POST", urllib.parse.urlencode(
        {"login": "probe@example.invalid", "name": "probe", "password": "x", "confirm_password": "x",
         "csrf_token": token}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"})
    expect(status in (400, 403, 404, 405) or "o_login_form" in body and "signup" not in body.lower(),
           f"POST /web/signup is refused (HTTP {status})")
    expect(bool(token), "the probes carried a real CSRF token (so they were refused on their merits)")
    both = [("active", "in", [True, False])]
    expect(target.call("res.users", "search_count", [both]) == users_before, "no user record was created by the probes")
    expect(target.call("res.partner", "search_count", [both]) == partners_before,
           "no partner record was created by the probes")
    expect(target.call("mail.mail", "search_count", [[]]) == mail_before, "no mail.mail record was created by the probes")
    return failures


def connect(args):
    with open(args.password_file, "r", encoding="utf-8") as handle:  # audit-ignore-path
        secret = handle.read().strip()
    creds = mig.Credentials(args.url, args.db, args.login, secret)
    return mig.make_transport(getattr(args, "transport", "xmlrpc"), creds, min_interval=0.0)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("apply", "check"):
        p = sub.add_parser(name)
        p.add_argument("--url", required=True)
        p.add_argument("--db", required=True)
        p.add_argument("--login", default="admin")
        p.add_argument("--password-file", required=True, help="the admin password (xmlrpc) or an API key (json2)")
        p.add_argument("--transport", choices=("xmlrpc", "json2"), default="xmlrpc")
        if name == "apply":
            p.add_argument("--site-name")
            p.add_argument("--blog-name")
            p.add_argument("--author-name")
            p.add_argument("--write", action="store_true")
    return parser


def main(argv=None, out=print):
    args = build_parser().parse_args(argv)
    try:
        target = connect(args)
        if args.command == "apply":
            changes = Hardener(target, args.write, out).run(args.site_name, args.blog_name, args.author_name)
            out(f"{len(changes)} change(s) " + ("made" if args.write else "pending (dry run; add --write)"))
            return 0
        failures = run_checks(target, args.url.rstrip("/"), out)
        out(f"{len(failures)} check(s) failed")
        return 1 if failures else 0
    except mig.MigrateError as exc:
        out(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
