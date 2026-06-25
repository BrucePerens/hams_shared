import odoo
odoo.tools.config.parse_config(["-c", "tools/odoo.conf", "-d", "hams_test"])
registry = odoo.registry("hams_test")
with registry.cursor() as cr:
    env = odoo.api.Environment(cr, 1, {})
    user = env["res.users"].create({
        "name": "Widget Tester",
        "login": "k6bp_test_user2",
        "website_slug": "k6bp_test_user2",
    })
    env.cr.commit()
    print("User ID:", user.id)
    print("User Slug:", user.website_slug)
    cr.execute("SELECT website_slug FROM res_users WHERE id = %s", (user.id,))
    print("DB Slug:", cr.fetchone())
    
    # Check _get_user_id_by_slug
    user_id = env["res.users"]._get_user_id_by_slug("k6bp_test_user2")
    print("_get_user_id_by_slug returned:", user_id)
