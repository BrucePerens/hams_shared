# User Websites SEO (`user_websites_seo`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

This module is a lightweight domain extension for `user_websites`. It connects our shared blog architecture with Odoo's native frontend SEO engine, restoring the interactive "Optimize SEO" widget for personal and group blog indexes.

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for LLMs and Integrators.
</system_role>

## 1. 🏗️ Overview & Architecture
This module is a lightweight domain extension for `user_websites`. It connects our shared blog architecture with Odoo's native frontend SEO engine. By injecting `website.seo.metadata` into user and group models, it allows these records to act as the "main_object" for SEO purposes on their respective blog index pages.

## 2. ⚙️ Technical Implementation Details
* **Model Injection:** It fuses the `website.seo.metadata` mixin into the `res.users` and `user.websites.group` models via `user.websites.seo.metadata.mixin`.
* **Authorization:** It appends the SEO metadata fields to the `SELF_WRITEABLE_FIELDS` property. This is a critical Odoo idiom that allows users to modify specific fields on their own record without broad write permissions. Verified by `[@ANCHOR: res_users_self_writeable_fields]`.
* **Controller Interception:** Overrides the `/<slug>/blog` route. It calls `super()` to get the standard response, then injects the `profile_user` or `profile_group` record as `main_object` into the `qcontext`. This is what triggers Odoo's frontend to show the "Optimize SEO" menu item. It ensures the injected `main_object` is de-elevated to the public user's environment to prevent SSTI. Verified by `[@ANCHOR: controller_user_blog_index_seo_override]`.
* **Secure Elevation (Zero-Sudo):** In `write()`, the module separates SEO fields from other fields. If a non-administrator is writing to SEO fields, it checks permissions (`_check_seo_write_permission`) and then performs the write using the `user_websites.user_websites_service_account` service account. This avoids the use of `.sudo()`.
    * User SEO Write Elevation: `[@ANCHOR: res_users_seo_write_elevation]` (Verified by `test_check_access_rule_res_users`)
    * Group SEO Write Elevation: `[@ANCHOR: user_websites_group_seo_write_elevation]` (Verified by `test_check_access_rule_user_websites_group`)
* **SSTI Protection:** To prevent Server-Side Template Injection, the controller injects the `main_object` into the QWeb context *without* elevating the recordset itself. If the recordset was already elevated by a parent controller, it is explicitly de-elevated. All privilege elevation is deferred to the model's `write()` method where it is strictly bounded.
* **Soft Dependency Documentation:** The module uses the `zero_sudo` automated installer to dynamically install documentation if `knowledge.article` or `knowledge.article` is present. Verified by `[@ANCHOR: test_soft_dependency_docs_installation]`.

---

<stories_and_journeys>
## 3. Architectural Stories & Journeys

For detailed narratives and end-to-end workflows, refer to the following:

### Stories
* [Individual SEO Control](user_websites_seo/docs/stories/individual_seo_control.md)
* [Group SEO Collaboration](user_websites_seo/docs/stories/group_seo_collaboration.md)
* [Seamless Documentation](user_websites_seo/docs/stories/seamless_documentation.md)

### Journeys
* [User Optimizes Blog SEO](user_websites_seo/docs/journeys/user_optimizes_blog_seo.md)
</stories_and_journeys>

## 4. 🔗 Semantic Anchors & Traceability

| Anchor | Description | Verified By |
|--------|-------------|-------------|
| `[@ANCHOR: res_users_self_writeable_fields]` | Whitelisting SEO fields for users. | `test_self_writeable_fields` |
| `[@ANCHOR: res_users_seo_write_elevation]` | Elevated write for user SEO metadata. | `test_check_access_rule_res_users` |
| `[@ANCHOR: user_websites_group_seo_write_elevation]` | Elevated write for group SEO metadata. | `test_check_access_rule_user_websites_group` |
| `[@ANCHOR: controller_user_blog_index_seo_override]` | Controller override for SEO widget activation. | `test_controller_no_ssti_elevation` |
| `[@ANCHOR: soft_dependency_docs_installation]` | Automatic documentation installation. | `test_soft_dependency_docs_installation` |
| `[@ANCHOR: test_seo_widget_tour]` | UI tour for SEO optimization. | `test_seo_widget_tour` |
| `[@ANCHOR: test_xpath_rendering_res_users]` | Backend view rendering for users. | `test_xpath_rendering_res_users` |
| `[@ANCHOR: test_xpath_rendering_user_websites_group]` | Backend view rendering for groups. | `test_xpath_rendering_user_websites_group` |

## 5. Multi-Website Support
This module is fully multi-website aware. It respects the `website_id` field on `website.page` records and uses Odoo's native website-switching logic to ensure that SEO metadata is correctly associated with the active website context. All controller logic uses `request.website` to filter relevant records.
