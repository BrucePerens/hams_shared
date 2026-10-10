# User Websites SEO (`user_websites_seo`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

This module is a lightweight domain extension for `user_websites`. It connects our shared blog architecture with Odoo's native frontend SEO engine, restoring the interactive "Optimize SEO" widget for personal and group blog indexes.

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for LLMs and Integrators.
</system_role>

## 1. 🏗️ Overview & Architecture
This module is a lightweight domain extension for `user_websites`. It connects our shared blog architecture with Odoo's native frontend SEO engine. By giving user and group records the SEO metadata fields (`website_meta_title`, `website_meta_description`, `website_meta_keywords`, `website_meta_og_img` and `seo_name`), it allows these records to act as the "main_object" (the record Odoo's frontend SEO dialog edits) on their respective blog index pages. The same secure-write mixin is also applied to `website.page`, `blog.blog` and `blog.post`.

## 2. ⚙️ Technical Implementation Details
* **Model Injection:** `user.websites.group` inherits Odoo's `website.seo.metadata` mixin (which supplies the SEO fields) directly. `user.websites.seo.metadata.mixin`, this module's own secure-write mixin, is applied to `res.users`, `user.websites.group`, `website.page`, `blog.blog` and `blog.post`.
* **Authorization:** It appends the SEO metadata fields to the `SELF_WRITEABLE_FIELDS` property. This is a critical Odoo idiom that allows users to modify specific fields on their own record without broad write permissions. Verified by `[@ANCHOR: COMM_res_users_self_writeable_fields]`.

* **Controller Interception:** Overrides the `/<slug>/blog` route. It calls `super()` to get the standard response, then injects the `profile_user` or `profile_group` record as `main_object` into the `qcontext`. This is what triggers Odoo's frontend to show the "Optimize SEO" menu item. The base controller loads the profile as the `user_websites` service account, so the override first reads the SEO fields (priming the ORM cache), then re-binds the record to the visiting user's own environment (`request.env.user`, which is the public user for an anonymous visitor) so that the template does not receive a record carrying service-account privileges (SSTI, server-side template injection). Verified by `[@ANCHOR: COMM_controller_user_blog_index_seo_override]`.
* **Secure Elevation (Zero-Sudo, see [zero_sudo.md](zero_sudo.md)):** In `write()`, the mixin separates SEO fields from other fields. The superuser and members of `user_websites.group_user_websites_administrator` (and a call with context `skip_seo_metadata_mixin`) skip all of this. For anyone else, if SEO fields are present it first calls `_check_seo_write_permission()`, which raises `AccessError` and so refuses the whole write, before it writes the non-SEO fields as the caller under normal access rules; it then writes the SEO fields as the `user_websites.user_websites_service_account` service account. This avoids the use of `.sudo()`. The built-in checks: a user may edit only their own `res.users` record, only members may edit a group's, and for pages, posts and blogs the caller needs normal write access to the record.
    * User SEO Write Elevation: `[@ANCHOR: COMM_res_users_seo_write_elevation]` (Verified by `test_check_access_rule_res_users`)

    * Group SEO Write Elevation: `[@ANCHOR: COMM_user_websites_group_seo_write_elevation]` (Verified by `test_check_access_rule_user_websites_group`)
* **SSTI Protection:** To prevent Server-Side Template Injection, the controller injects the `main_object` into the QWeb context *without* elevating the recordset itself. If the recordset was already elevated by a parent controller, it is explicitly de-elevated. All privilege elevation is deferred to the model's `write()` method where it is strictly bounded.
* **Soft Dependency Documentation:** The module's guide, declared in the manifest's `knowledge_docs`, is installed by the `zero_sudo` documentation installer (`_bootstrap_knowledge_docs`) whenever the `knowledge.article` model exists (provided by either this repository's open-source `knowledge` module or Odoo Enterprise's Knowledge app); otherwise it is skipped. The module's own `post_init_hook` only sets the `user_websites_seo.docs_installed` system parameter. Verified by `[@ANCHOR: COMM_test_soft_dependency_docs_installation]`.

---

<stories_and_journeys>
## 3. Architectural Stories & Journeys

For detailed narratives and end-to-end workflows, refer to the following:

### Stories
* [Individual SEO Control](user_websites_seo/hams_shared/docs/stories/individual_seo_control.md)
* [Group SEO Collaboration](user_websites_seo/hams_shared/docs/stories/group_seo_collaboration.md)
* [Seamless Documentation](user_websites_seo/hams_shared/docs/stories/seamless_documentation.md)

### Journeys
* [User Optimizes Blog SEO](user_websites_seo/hams_shared/docs/journeys/user_optimizes_blog_seo.md)
</stories_and_journeys>

## 4. 🔗 Semantic Anchors & Traceability

| Anchor | Description | Verified By |
|--------|-------------|-------------|
| `[@ANCHOR: COMM_res_users_self_writeable_fields]` | Whitelisting SEO fields for users. | `test_self_writeable_fields` |

| `[@ANCHOR: COMM_res_users_seo_write_elevation]` | Elevated write for user SEO metadata. | `test_check_access_rule_res_users` |

| `[@ANCHOR: COMM_user_websites_group_seo_write_elevation]` | Elevated write for group SEO metadata. | `test_check_access_rule_user_websites_group` |

| `[@ANCHOR: COMM_controller_user_blog_index_seo_override]` | Controller override for SEO widget activation. | `test_controller_no_ssti_elevation` |

| `[@ANCHOR: COMM_soft_dependency_docs_installation]` | Automatic documentation installation. | `test_soft_dependency_docs_installation` |

| `[@ANCHOR: COMM_test_seo_widget_tour]` | UI tour for SEO optimization. | `test_seo_widget_tour` |

| `[@ANCHOR: COMM_test_xpath_rendering_res_users]` | Backend view rendering for users. | `test_xpath_rendering_res_users` |

| `[@ANCHOR: COMM_test_xpath_rendering_user_websites_group]` | Backend view rendering for groups. | `test_xpath_rendering_user_websites_group` |

### Stage 1 Anchor-Coverage Sweep Additions (page/post/blog SEO metadata mixin)

* **SEO Field Set:** `[@ANCHOR: user_websites_seo:COMM_get_seo_fields]` -- the fields the mixin treats as SEO metadata (title/description/keywords/og-image/seo-name). Verified by `test_page_seo_write`.

* **Mixin Write Split:** `[@ANCHOR: user_websites_seo:COMM_mixin_write]` -- splits a write into SEO vs. non-SEO fields, escalating only the SEO half through the service account after a permission check. Verified by `test_page_seo_write`.

* **Abstract Permission Check:** `[@ANCHOR: user_websites_seo:COMM_mixin_check_seo_write_permission]` -- the base every concrete model overrides; must refuse (raise `NotImplementedError`), never silently allow. Verified by `test_mixin_base_check_seo_write_permission_is_abstract`.

* **Page Permission Override:** `[@ANCHOR: user_websites_seo:COMM_page_check_seo_write_permission]` -- `website.page`'s own override, delegating to its real ACL/record-rule check. Verified by `test_page_seo_write`.

* **Post Permission Override:** `[@ANCHOR: user_websites_seo:COMM_post_check_seo_write_permission]` -- `blog.post`'s own override. Verified by `test_post_seo_write`.

* **Blog Permission Override:** `[@ANCHOR: user_websites_seo:COMM_blog_check_seo_write_permission]` -- `blog.blog`'s own override. Verified by `test_blog_seo_write`.

## 5. Multi-Website Support
This module is fully multi-website aware. It stores SEO metadata on the record itself (so a `website.page` keeps its own metadata, tied to that page's `website_id`), and its one controller override passes on the response of the `user_websites` blog route unchanged apart from `main_object`; it adds no website filtering of its own.
