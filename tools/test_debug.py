import os
os.environ["ODOO_RC"] = ""
import odoo  # noqa: E402
odoo.tools.config.parse_config(["-c", "tools/odoo.conf", "-d", "hams_test"])
odoo.cli.server.report_configuration()
registry = odoo.modules.registry.Registry("hams_test")
with registry.cursor() as cr:
    env = odoo.api.Environment(cr, 1, {})
    user = env["res.users"].search([("login", "=", "k6bp_test_user")])
    print("Found user:", user)
    if user:
        print("Website slug:", user.website_slug)
        cr.execute("SELECT website_slug FROM res_users WHERE id = %s", (user.id,))
        print("Raw SQL website slug:", cr.fetchone())
