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
* **Authorization:** It appends the SEO metadata fields to the `SELF_WRITEABLE_FIELDS` property. This is a critical Odoo idiom that allows users to modify specific fields on their own record without broad write permissions. Verified by `COMM_res_users_self_writeable_fields`.

* **Controller Interception:** Overrides the `/<slug>/blog` route. It calls `super()` to get the standard response, then injects the `profile_user` or `profile_group` record as `main_object` into the `qcontext`. This is what triggers Odoo's frontend to show the "Optimize SEO" menu item. The base controller loads the profile as the `user_websites` service account, so the override first reads the SEO fields (priming the ORM cache), then re-binds the record to the visiting user's own environment (`request.env.user`, which is the public user for an anonymous visitor) so that the template does not receive a record carrying service-account privileges (SSTI, server-side template injection). Verified by `COMM_controller_user_blog_index_seo_override`.
* **Secure Elevation (Zero-Sudo, see [zero_sudo.md](zero_sudo.md)):** In `write()`, the mixin separates SEO fields from other fields. The superuser and members of `user_websites.group_user_websites_administrator` (and a call with context `skip_seo_metadata_mixin`) skip all of this. For anyone else, if SEO fields are present it first calls `_check_seo_write_permission()`, which raises `AccessError` and so refuses the whole write, before it writes the non-SEO fields as the caller under normal access rules; it then writes the SEO fields as the `user_websites.user_websites_service_account` service account. This avoids the use of `.sudo()`. The built-in checks: a user may edit only their own `res.users` record, only members may edit a group's, and for pages, posts and blogs the caller needs normal write access to the record.
    * User SEO Write Elevation: `COMM_res_users_seo_write_elevation` (Verified by `test_check_access_rule_res_users`)

    * Group SEO Write Elevation: `COMM_user_websites_group_seo_write_elevation` (Verified by `test_check_access_rule_user_websites_group`)
* **SSTI Protection:** To prevent Server-Side Template Injection, the controller injects the `main_object` into the QWeb context *without* elevating the recordset itself. If the recordset was already elevated by a parent controller, it is explicitly de-elevated. All privilege elevation is deferred to the model's `write()` method where it is strictly bounded.
* **Soft Dependency Documentation:** The module's guide, declared in the manifest's `knowledge_docs`, is installed by the `zero_sudo` documentation installer (`_bootstrap_knowledge_docs`) whenever the `knowledge.article` model exists (provided by either this repository's open-source `knowledge` module or Odoo Enterprise's Knowledge app); otherwise it is skipped. The module's own `post_init_hook` only sets the `user_websites_seo.docs_installed` system parameter. Verified by `COMM_test_soft_dependency_docs_installation`.

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
| `COMM_res_users_self_writeable_fields` | Whitelisting SEO fields for users. | `test_self_writeable_fields` |

| `COMM_res_users_seo_write_elevation` | Elevated write for user SEO metadata. | `test_check_access_rule_res_users` |

| `COMM_user_websites_group_seo_write_elevation` | Elevated write for group SEO metadata. | `test_check_access_rule_user_websites_group` |

| `COMM_controller_user_blog_index_seo_override` | Controller override for SEO widget activation. | `test_controller_no_ssti_elevation` |

| `COMM_soft_dependency_docs_installation` | Automatic documentation installation. | `test_soft_dependency_docs_installation` |

| `COMM_test_seo_widget_tour` | UI tour for SEO optimization. | `test_seo_widget_tour` |

| `COMM_test_xpath_rendering_res_users` | Backend view rendering for users. | `test_xpath_rendering_res_users` |

| `COMM_test_xpath_rendering_user_websites_group` | Backend view rendering for groups. | `test_xpath_rendering_user_websites_group` |

### Stage 1 Anchor-Coverage Sweep Additions (page/post/blog SEO metadata mixin)

* **SEO Field Set:** `COMM_get_seo_fields` -- the fields the mixin treats as SEO metadata (title/description/keywords/og-image/seo-name). Verified by `test_page_seo_write`.

* **Mixin Write Split:** `COMM_mixin_write` -- splits a write into SEO vs. non-SEO fields, escalating only the SEO half through the service account after a permission check. Verified by `test_page_seo_write`.

* **Abstract Permission Check:** `COMM_mixin_check_seo_write_permission` -- the base every concrete model overrides; must refuse (raise `NotImplementedError`), never silently allow. Verified by `test_mixin_base_check_seo_write_permission_is_abstract`.

* **Page Permission Override:** `COMM_page_check_seo_write_permission` -- `website.page`'s own override, delegating to its real ACL/record-rule check. Verified by `test_page_seo_write`.

* **Post Permission Override:** `COMM_post_check_seo_write_permission` -- `blog.post`'s own override. Verified by `test_post_seo_write`.

* **Blog Permission Override:** `COMM_blog_check_seo_write_permission` -- `blog.blog`'s own override. Verified by `test_blog_seo_write`.

## 5. Multi-Website Support
This module is fully multi-website aware. It stores SEO metadata on the record itself (so a `website.page` keeps its own metadata, tied to that page's `website_id`), and its one controller override passes on the response of the `user_websites` blog route unchanged apart from `main_object`; it adds no website filtering of its own.

## Anchor reference

### `COMM_res_users_self_writeable_fields` [@ANCHOR: COMM_res_users_self_writeable_fields]

**Purpose.** Lets an ordinary user edit the SEO fields on their own profile without backend administrator rights.

**Behaviour.** The `res.users` override of the `SELF_WRITEABLE_FIELDS` property (the fields Odoo lets a user change on their own record) returns the base list plus the module's SEO fields: `website_meta_title`, `website_meta_description`, `website_meta_keywords`, `website_meta_og_img` and `seo_name`.

**Returns.** A list of field names.

**Security.** Only the user's own record is covered; writing another user's SEO fields is still refused (see `COMM_res_users_seo_write_elevation`).

**Special concerns.** Pure, no I/O. The property is evaluated each time Odoo checks a self-write.

**See also.** Test `COMM_test_self_writeable_fields`.

### `COMM_controller_user_blog_index_seo_override` [@ANCHOR: COMM_controller_user_blog_index_seo_override]

**Purpose.** `UserWebsitesSEOController` overrides the blog index route of `user_websites` so that Odoo's front-end "Optimize SEO" widget works for the owner of the profile page.

**Behaviour.** The base controller sets `main_object` to the profile user or group but loads it as the service account. The override pre-reads the SEO fields, then re-binds the record to the visiting user's own access rights before rendering, so the template (and any server-side template injection) never sees a service-account-privileged record (ADR-0078).

**Returns.** The same QWeb response as the base route, with the de-elevated `main_object`.

**Errors.** None of its own. A visitor without access to the record gets the access error of the normal rules.

**Special concerns.** Runs in an HTTP request; reads only. Security-critical: the re-binding is the whole point.

**See also.** Test `COMM_test_controller_no_ssti_elevation` (`user_websites_seo/tests/test_seo_controller.py`).

### `COMM_res_users_seo_write_elevation` [@ANCHOR: COMM_res_users_seo_write_elevation]

**Purpose.** The guard that stops a user changing another user's SEO metadata. The module lets SEO fields be written without full backend rights, so this check supplies the limit.

**Behaviour.** In `res_users.py`, `_check_seo_write_permission` runs on write and, when any record in the set is not the current user's own record, raises `AccessError("You can only modify your own SEO metadata.")`.

**Errors.** `AccessError` as stated.

**Special concerns.** Runs on every write that touches SEO fields; no side effects. It is a code check on top of the record rules, so it also holds when a service account elevates the write.

**See also.** Tests `COMM_test_check_access_rule_res_users` and `COMM_test_seo_widget_tour`.

### `COMM_user_websites_group_seo_write_elevation` [@ANCHOR: COMM_user_websites_group_seo_write_elevation]

**Purpose.** The same guard for group websites: only members of a `user.websites.group` may change that group's SEO metadata.

**Behaviour.** In `user_websites_group.py` the SEO write check raises `AccessError("You can only modify SEO metadata for groups you are a member of.")` when the current user is not a member of the group.

**Errors.** `AccessError` as stated.

**Special concerns.** Reads group membership only; no side effects. Like the user version it holds even when an elevated service account performs the write.

**See also.** Tests `COMM_test_check_access_rule_user_websites_group` and `COMM_test_seo_widget_tour`.

### `COMM_test_soft_dependency_docs_installation` [@ANCHOR: COMM_test_soft_dependency_docs_installation]

**Purpose.** The test of the post-install signal in `hooks.py`.

**Scenario.** `test_soft_dependency_docs_installation` (in `user_websites_seo/tests/test_seo_pages_posts.py`) reads the system parameter `user_websites_seo.docs_installed` through `zero_sudo.security.utils` and asserts it equals `"True"` after module installation.

**What it guards.** `COMM_soft_dependency_docs_installation`.

**Special concerns.** `TransactionCase`; relies on the install hook having run, so it fails if the hook swallowed an error (the hook logs the exception).

### `COMM_soft_dependency_docs_installation` [@ANCHOR: COMM_soft_dependency_docs_installation]

**Purpose.** A post-install hook (`user_websites_seo/hooks.py`) that records, once, that the module's documentation set is installed. `user_websites_seo` depends softly on the `knowledge` module, so the hook only signals, and the documentation is installed by whichever module owns it.

**Interface.** Takes the Odoo `env` and returns nothing. It reads the system parameter `user_websites_seo.docs_installed` through `zero_sudo.security.utils`; when unset it resolves the service account `user_websites.user_websites_service_account` and sets the parameter to `"True"`.

**Errors.** Any exception is logged with `_logger.exception` and swallowed, so a missing service account cannot abort module installation.

**Special concerns.** Idempotent: a second run sees the parameter and does nothing. Writes one `ir.config_parameter` row, as the Zero-Sudo helper, not with `.sudo()`.

**See also.** Test `COMM_test_soft_dependency_docs_installation`.

### `COMM_test_seo_widget_tour` [@ANCHOR: COMM_test_seo_widget_tour]

**Purpose.** The browser test that an administrator can fill in a portal user's SEO metadata through the real web client.

**Scenario.** `test_seo_widget_tour` (in `user_websites_seo/tests/test_seo_ui_tour.py`) runs `start_tour("/odoo?debug=1", "user_websites_seo_tour", login="admin")`. The tour (`static/src/js/tours/user_websites_seo_tour.js`) opens the users list, removes the default Internal Users filter, searches for "SEO UI Test User", opens the row, clicks the SEO Metadata tab, types a meta title and saves. It relies on the patched `browser_js` of `zero_sudo`.

**What it guards.** The SEO write paths `COMM_res_users_seo_write_elevation` and `COMM_user_websites_group_seo_write_elevation`, and the controller override, from the UI.

**Special concerns.** Needs a headless browser and the HTTP server.

### `COMM_test_xpath_rendering_res_users` [@ANCHOR: COMM_test_xpath_rendering_res_users]

**Purpose.** The test that the module's inherited `res.users` form actually carries the SEO tab.

**Scenario.** `test_xpath_rendering_res_users` calls `get_view` for `base.view_users_form` and asserts the combined architecture contains the notebook page `user_websites_seo_settings` and the field `website_meta_og_img`. A broken `xpath` in `views/res_users_views.xml` would leave them out.

**What it guards.** The view extension `res_users_views.xml`, which places the page after `user_websites_settings`.

**Special concerns.** `TransactionCase`; the view is compiled, not rendered in a browser.

### `COMM_test_xpath_rendering_user_websites_group` [@ANCHOR: COMM_test_xpath_rendering_user_websites_group]

**Purpose.** The test that the module's inherited group-website form carries the SEO tab, the group counterpart of `COMM_test_xpath_rendering_res_users`.

**Scenario.** `test_xpath_rendering_user_websites_group` calls `get_view` on `user.websites.group` with the base view `user_websites.view_user_websites_group_form` and asserts the combined architecture contains the notebook page `group_seo_settings` and the field `website_meta_og_img`. A stale `xpath` in the inherited view would drop them and fail the test.

**What it guards.** The inherited view `views/user_websites_group_views.xml`.

**Special concerns.** `TransactionCase`; the view is compiled, not rendered in a browser.

### `COMM_get_seo_fields` [@ANCHOR: user_websites_seo:COMM_get_seo_fields]

**Purpose.** `_get_seo_fields()` on the abstract SEO metadata mixin names the fields that count as SEO metadata, so the shared `write` can separate them from other fields.

**Interface.** No arguments. It returns a set of field names: `website_meta_title`, `website_meta_description`, `website_meta_keywords`, `website_meta_og_img` and `seo_name`. `res.users` also uses it to extend `SELF_WRITEABLE_FIELDS`.

**Special concerns.** Pure, no I/O. Adding a field to the set puts it under the write-permission check.

**See also.** `COMM_mixin_write`, `COMM_res_users_self_writeable_fields`; test in `user_websites_seo/tests/test_seo_pages_posts.py`.

### `COMM_mixin_write` [@ANCHOR: user_websites_seo:COMM_mixin_write]

**Purpose.** The `write` override of the SEO mixin: it applies a model's own permission check to SEO fields before anything is written.

**Interface.** `write(self, vals)`; `vals` is the dictionary of fields to write. Returns what the parent write returns (True).

**Behaviour.** With the context key `skip_seo_metadata_mixin`, for the superuser (`env.su`), and for members of `user_websites.group_user_websites_administrator` the write goes straight to the parent. Otherwise the values are split into SEO values and others; if there are SEO values `_check_seo_write_permission()` runs first, before the other fields are written, because the model's check may be stricter than its normal write rule; then the other values are written by the parent.

**Errors.** Whatever `_check_seo_write_permission` raises (usually `AccessError`); the base mixin raises `NotImplementedError`.

**See also.** Test in `user_websites_seo/tests/test_seo_pages_posts.py`; `COMM_mixin_check_seo_write_permission`.

### `COMM_mixin_check_seo_write_permission` [@ANCHOR: user_websites_seo:COMM_mixin_check_seo_write_permission]

**Purpose.** The hook in the SEO mixin that each model overrides to say who may edit its SEO metadata. The base version refuses, so a model that forgets to override cannot be written by an unprivileged user.

**Interface.** `_check_seo_write_permission(self)`; no arguments, returns nothing.

**Errors.** The base method raises `NotImplementedError("Each model must implement its own permission check.")`. Overrides raise `AccessError` to refuse.

**Overrides.** `res.users` (own record only, `COMM_res_users_seo_write_elevation`), `user.websites.group` (members only), and `website.page`, `blog.post`, `blog.blog` (ordinary write access).

**See also.** `COMM_mixin_write`, test in `user_websites_seo/tests/test_seo_models.py`.

### `COMM_page_check_seo_write_permission` [@ANCHOR: user_websites_seo:COMM_page_check_seo_write_permission]

**Purpose.** The permission check for SEO metadata on a website page (`website.page`).

**Interface.** `_check_seo_write_permission(self)` in `user_websites_seo/models/website_page.py`; no arguments, returns nothing. It calls `self.check_access("write")`, so whoever may write the page may write its SEO fields.

**Errors.** `AccessError` from Odoo's own access check (access rights and record rules).

**Special concerns.** Reads access rules only; no side effect. Called from the mixin's `write` before the values are written.

**See also.** `COMM_mixin_write`; test in `user_websites_seo/tests/test_seo_pages_posts.py`.

### `COMM_post_check_seo_write_permission` [@ANCHOR: user_websites_seo:COMM_post_check_seo_write_permission]

**Purpose.** The permission check for SEO metadata on a blog post (`blog.post`).

**Interface.** `_check_seo_write_permission(self)` in `user_websites_seo/models/blog_post.py`; no arguments, returns nothing. It calls `self.check_access("write")`, so the author or anyone else with write access to the post may edit its SEO fields.

**Errors.** `AccessError` from Odoo's access check.

**Special concerns.** Read only; called from the mixin's `write` before the values are written.

**See also.** `COMM_mixin_write`; test in `user_websites_seo/tests/test_seo_pages_posts.py`.

### `COMM_blog_check_seo_write_permission` [@ANCHOR: user_websites_seo:COMM_blog_check_seo_write_permission]

**Purpose.** The permission check for SEO metadata on a blog (`blog.blog`), the container of posts.

**Interface.** `_check_seo_write_permission(self)` in `user_websites_seo/models/blog_blog.py`; no arguments, returns nothing. It calls `self.check_access("write")`.

**Errors.** `AccessError` from Odoo's access check.

**Special concerns.** Read only; called from the mixin's `write` before the values are written.

**See also.** `COMM_mixin_write`; test in `user_websites_seo/tests/test_seo_pages_posts.py`.

### `COMM_get_seo_fields` [@ANCHOR: user_websites_seo:COMM_get_seo_fields]

**Purpose.** `_get_seo_fields()` on the abstract SEO metadata mixin names the fields that count as SEO metadata, so the shared `write` can separate them from other fields.

**Interface.** No arguments. It returns a set of field names: `website_meta_title`, `website_meta_description`, `website_meta_keywords`, `website_meta_og_img` and `seo_name`. `res.users` also uses it to extend `SELF_WRITEABLE_FIELDS`.

**Special concerns.** Pure, no I/O. Adding a field to the set puts it under the write-permission check.

**See also.** `COMM_mixin_write`, `COMM_res_users_self_writeable_fields`; test in `user_websites_seo/tests/test_seo_pages_posts.py`.

### `COMM_mixin_write` [@ANCHOR: user_websites_seo:COMM_mixin_write]

**Purpose.** The `write` override of the SEO mixin: it applies a model's own permission check to SEO fields before anything is written.

**Interface.** `write(self, vals)`; `vals` is the dictionary of fields to write. Returns what the parent write returns (True).

**Behaviour.** With the context key `skip_seo_metadata_mixin`, for the superuser (`env.su`), and for members of `user_websites.group_user_websites_administrator` the write goes straight to the parent. Otherwise the values are split into SEO values and others; if there are SEO values `_check_seo_write_permission()` runs first, before the other fields are written, because the model's check may be stricter than its normal write rule; then the other values are written by the parent.

**Errors.** Whatever `_check_seo_write_permission` raises (usually `AccessError`); the base mixin raises `NotImplementedError`.

**See also.** Test in `user_websites_seo/tests/test_seo_pages_posts.py`; `COMM_mixin_check_seo_write_permission`.

### `COMM_mixin_check_seo_write_permission` [@ANCHOR: user_websites_seo:COMM_mixin_check_seo_write_permission]

**Purpose.** The hook in the SEO mixin that each model overrides to say who may edit its SEO metadata. The base version refuses, so a model that forgets to override cannot be written by an unprivileged user.

**Interface.** `_check_seo_write_permission(self)`; no arguments, returns nothing.

**Errors.** The base method raises `NotImplementedError("Each model must implement its own permission check.")`. Overrides raise `AccessError` to refuse.

**Overrides.** `res.users` (own record only, `COMM_res_users_seo_write_elevation`), `user.websites.group` (members only), and `website.page`, `blog.post`, `blog.blog` (ordinary write access).

**See also.** `COMM_mixin_write`, test in `user_websites_seo/tests/test_seo_models.py`.

### `COMM_page_check_seo_write_permission` [@ANCHOR: user_websites_seo:COMM_page_check_seo_write_permission]

**Purpose.** The permission check for SEO metadata on a website page (`website.page`).

**Interface.** `_check_seo_write_permission(self)` in `user_websites_seo/models/website_page.py`; no arguments, returns nothing. It calls `self.check_access("write")`, so whoever may write the page may write its SEO fields.

**Errors.** `AccessError` from Odoo's own access check (access rights and record rules).

**Special concerns.** Reads access rules only; no side effect. Called from the mixin's `write` before the values are written.

**See also.** `COMM_mixin_write`; test in `user_websites_seo/tests/test_seo_pages_posts.py`.

### `COMM_post_check_seo_write_permission` [@ANCHOR: user_websites_seo:COMM_post_check_seo_write_permission]

**Purpose.** The permission check for SEO metadata on a blog post (`blog.post`).

**Interface.** `_check_seo_write_permission(self)` in `user_websites_seo/models/blog_post.py`; no arguments, returns nothing. It calls `self.check_access("write")`, so the author or anyone else with write access to the post may edit its SEO fields.

**Errors.** `AccessError` from Odoo's access check.

**Special concerns.** Read only; called from the mixin's `write` before the values are written.

**See also.** `COMM_mixin_write`; test in `user_websites_seo/tests/test_seo_pages_posts.py`.

### `COMM_blog_check_seo_write_permission` [@ANCHOR: user_websites_seo:COMM_blog_check_seo_write_permission]

**Purpose.** The permission check for SEO metadata on a blog (`blog.blog`), the container of posts.

**Interface.** `_check_seo_write_permission(self)` in `user_websites_seo/models/blog_blog.py`; no arguments, returns nothing. It calls `self.check_access("write")`.

**Errors.** `AccessError` from Odoo's access check.

**Special concerns.** Read only; called from the mixin's `write` before the values are written.

**See also.** `COMM_mixin_write`; test in `user_websites_seo/tests/test_seo_pages_posts.py`.
