# User Websites (`user_websites`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

Welcome to User Websites! This Odoo 19 module allows you to build and manage your own personal or group websites and blogs directly within Odoo. Whether you're a beginner or an expert, you'll find everything you need to create a professional online presence.

**Open Source Rule:** We built this for the open-source community. It runs perfectly on its own and does not rely on any proprietary code.

## 🌟 What Can I Do?

*   **Create Your Personal Site:** Get a unique URL (like `/yourname`) and use our easy drag-and-drop editor to build your pages.
*   **Start a Blog:** Share your thoughts and stories with the community. Everyone gets their own blog section.
*   **Collaborate with Groups:** Create shared websites for your team, club, or project.
*   **Join the Community:** Choose to show off your site in our public directory and discover what others are building.
*   **Control Your Privacy:** You decide what's public. We also provide full GDPR-compliant data export and deletion tools.

## 🚀 Getting Started

### 1. Initialize Your Site
The first time you visit your personal URL (e.g., click on your name in the navbar), you'll see a "Create Your Site" button. Click it to set up your initial layout.

### 2. Customize Your Pages
Once initialized, use the **Edit** button in the top right corner of any of your pages. You can drag and drop different "snippets" (blocks of content like text, images, or contact forms) onto your page.

### 3. Start Blogging
Visit your blog page (e.g., `/yourname/blog`) and click "Create Your Blog" to start posting. You can manage all your posts from this central location.

## 🛡️ Community & Safety

We want to keep our community safe and professional.
*   **Report Violations:** If you see content that breaks our rules, every personal or group website page (home page, blog, or blog post) shows a "Report Violation" button to visitors other than its owner (on a group website, other than a group member). Our admins will review reports promptly.
*   **Moderation:** We use a 3-strike system. If a user repeatedly violates our community guidelines, their account may be suspended from using website features.
*   **Automated Security:** Our system automatically scans for and removes malicious code to protect all users.

## ⚙️ Configuration (For Administrators)

Go to **Settings > General Settings > User Websites** to configure the app.
* **Global Page Limit:** Set the default maximum number of pages a user is allowed to build (100 unless changed).
* **Administrators:** The "Manage Administrators" button opens Odoo's own Groups form for the User Websites "Administrator" group; add the users who are allowed to review abuse reports and manage all user and group websites there.
* **Abuse Reporting Email:** The address that receives the daily summary of pending violation reports (if empty, the company's own email is used).

## 🏗️ How It Works Under the Hood

We used a few neat tricks to make this secure and fast:

* **Just-In-Time Creation:** We don't waste database space creating empty blogs for users who never use them. The system only creates the website records when the owner first clicks "Create Your Site" (or "Create Your Blog") on their own not-yet-initialized page; until then, visitors see a placeholder.
* **Your Own Blog:** Each user or group gets its own Odoo `blog.blog` record (named `<Name>'s Blog`), owned by that user or group, so only its owner (or the group's members) and administrators can change it. A user may have up to 5 blogs by default.
* **Proxy Ownership:** Odoo normally only lets admins build web pages. We get around this securely. When a user creates a page, the system briefly logs in as a background Service Account to save the HTML to the database, but tags the user as the real "owner" so only they can edit it later.
* **Security Shield:** The module includes an automated sanitization engine that intercepts and neutralizes XSS and SSTI attempts. If a user tries to inject malicious code, the system strips it, files a violation report against the page's owner, and issues a strike.

---

# Technical Documentation

<system_role>
*Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

**Context:** Technical documentation strictly for LLMs and Integrators. Use this to build dependent modules without needing the source code.
</system_role>

---

<core_patterns>
## 1. 🏗️ Overview & Core Patterns
**Open Source Isolation Mandate:** This module is Open Source and available to the Odoo Community. It MUST NEVER be given dependencies on proprietary modules or anything else from the proprietary codebase.

The `user_websites` module enables decentralized content creation. It employs the **Proxy Ownership Pattern**: standard Odoo users cannot create `ir.ui.view` or `website.page` records due to core security. The module securely circumvents this by assigning an `owner_user_id`, evaluating custom Record Rules against it, and escalating privileges via a dedicated Service Account (`.with_user(svc_uid)`) strictly for the database write.
* **Ownership Validation:** Safely asserted by mixin create `mixin_proxy_ownership_create` and write `mixin_proxy_ownership_write` methods. Explicitly verified by `test_mixin_ownership_validation`.

* **Tenant Isolation:** Enforced via strict record rules verified by `[@ANCHOR: test_tenant_view_isolation]` and ACL overhead elimination to prevent log spam `[@ANCHOR: test_acl_overhead_loop_elimination]`.
* **Lazy JIT Provisioning:** Websites and Blogs do not exist upon user creation. They are provisioned Just-In-Time when the owner visits their slug root and triggers a POST request to `create_site`. This ensures explicit user consent to publish.
</core_patterns>

---

<data_model>
## 2. 🗄️ Data Model Reference

### Extended `res.users`
* **`website_slug`**: URL-safe identifier. A slug is the first path segment of a user's (or group's) site URLs, e.g. `yourname` in `/yourname` and `/yourname/blog`; it must not be one of `RESERVED_SLUGS` (section 3).
* **`privacy_show_in_directory`**: Opt-in for the public `/community` directory.
* **`violation_strike_count`**: Number of upheld content violations.
* **`is_suspended_from_websites`**: If True, all personal content is forcefully unpublished (the unpublishing itself is done in the background by `action_suspend_user_websites()`; a direct write of this field only invalidates the slug/page resolver caches) and the user's slug routes return 404.
* **`appeal_ids`** (`One2many`): Links to Moderation Appeals.

### Content Models (`website.page`, `blog.post`)
* **`owner_user_id`**: The proxy owner.
* **`user_websites_group_id`**: For shared group websites.
* **`view_count`**: Privacy-friendly server-side view tracker (the server counts non-admin page views in Redis and a cron flushes them to this column every 15 minutes). Only `website.page` counts are incremented today; this module declares `blog.post.view_count` but never increments it.

### Moderation Models
* **`content.violation.report`** (model itself now in `content_moderation`, this module's own extension in `models/content_violation_report_moderation.py`): Stores abuse reports. Originator is masked from the target owner. The system automatically generates a report and issues a strike if a user attempts to inject malicious SSTI/XSS payloads into their site architecture `[@ANCHOR: content_moderation:action_take_action_and_strike]` (via this module's own enforcement-hook override `[@ANCHOR: user_websites:COMM_apply_enforcement_action]`), tested by `[@ANCHOR: test_moderation_suspension]`. Admin spam is prevented via a daily digest cron (`ir_cron_notify_pending_reports` `[@ANCHOR: ir_cron_notify_pending_reports]`, `[@ANCHOR: cron_notify_pending_reports]`, verified by `[@ANCHOR: test_cron_pending_reports]`) and a UI toast that is sticky but remembered in localStorage for 12 h per (count, newest id), with Review/Dismiss buttons, (`[@ANCHOR: toast_notifications_logic]`, `[@ANCHOR: admin_toast_logic]`, tested by `[@ANCHOR: test_tour_toast_notifications]`).

* **Security Auto-Moderation**: The `website.page` model includes `_sanitize_user_arch` `[@ANCHOR: website_page_sanitize_arch]`, verified by `[@ANCHOR: test_website_page_sanitize_arch]`, which forcefully strips dangerous QWeb directives (`t-*`) and JS event handlers.
* **Member CSS**: `<link>` is removed; `<style>` is rebuilt rule by rule and every `style="..."` attribute declaration by declaration with zero_sudo's `css_sanitizer` `[@ANCHOR: user_websites:page_arch_style_attribute_filter]`, verified by `[@ANCHOR: test_user_arch_style_attribute_filter]`: no `url()` except `#fragment`, no `image-set()`, `data:`, `expression`, escapes, and `position` only static, relative or absolute. A refused declaration is dropped (no strike); malformed CSS drops the whole attribute. Blog post content goes through `html_sanitize` instead and is not covered by this filter.
* **`content.violation.appeal`**: Used by suspended users to petition for account restoration.
</data_model>

---

<public_api>
## 3. 🐍 Public API & Extensibility Methods

### Explicit Dropzones
To prevent monolithic entanglement, `user_websites` provides the following explicitly designated dropzones. You MUST use `<xpath>` targeting these specific IDs and cite the corresponding Semantic Anchor:
* **Home Header:** `id="user_websites_dropzone_home_header"` -> `dropzone_home_header`

* **Home Footer:** `id="user_websites_dropzone_home_footer"` -> `dropzone_home_footer`

* **Global Navbar:** `id="user_websites_dropzone_navbar"` -> `dropzone_navbar`

* **Portal Templates:** `id="user_websites_dropzone_templates"` -> `dropzone_templates`

* **Snippets Sidebar:** `id="user_websites_dropzone_snippets"` -> `dropzone_snippets`

* **Website Layout:** `id="user_websites_dropzone_layout"` -> `dropzone_layout`

* **User Settings:** `id="user_websites_dropzone_users"` -> `dropzone_users`

* **Blog Post Form:** `id="user_websites_dropzone_blog_post"` -> `dropzone_blog_post`

* **Navbar Actions:** `id="user_websites_dropzone_navbar_actions"` -> `dropzone_navbar_actions`

* **Directory Card:** `id="user_websites_dropzone_directory_card"` -> `dropzone_directory_card`

* **Global Settings Form:** -> `dropzone_settings`

### Prohibited Dropzones
DO NOT USE user_websites_settings_dropzone. All settings views must now inherit directly from base.res_config_settings_view_form and target the //form element using the modernized `<app>`, `<block>`, and `<setting>` XML tags.

### Endpoints & Webhooks
* **Community Directory:** Renders public pages `/community` `UX_COMMUNITY_DIRECTORY`.

* **Violation Reporting:** Form endpoint `/website/report_violation` `[@ANCHOR: user_websites:UX_REPORT_VIOLATION]`, `[@ANCHOR: violation_report_logic]`, verified by `[@ANCHOR: test_tour_violation_report]`. Protected by the `form_shield` module (proof of work or question, honeypots, minimum time-to-submit, per-network, per-page and per-site limits `[@ANCHOR: user_websites:report_limits]`, `[@ANCHOR: user_websites:report_violation_shield]`, the form's fields `[@ANCHOR: user_websites:report_violation_shield_fields]`); the reported URL must be a page of this site (this host, the base URL or a website domain, http and https alike, www ignored `[@ANCHOR: user_websites:report_own_site_hosts]`) `[@ANCHOR: user_websites:normalize_report_target]` and a guest's report is refused when it is short, one random-looking string, link-heavy or lacks a valid address `[@ANCHOR: user_websites:report_problem]`. `GET /website/report_violation` is the question variant for browsers without scripts `[@ANCHOR: user_websites:report_violation_page]`. Helpers: the redirect path is forced to a single leading slash `[@ANCHOR: user_websites:reported_path]`, hosts compare without `www.` `[@ANCHOR: user_websites:bare_host]`, a guest's address may not start or end its local part with a dot `[@ANCHOR: user_websites:local_part_ok]`, the question page is rendered by `[@ANCHOR: user_websites:render_report_page]` and a refusal is answered by `[@ANCHOR: user_websites:report_refusal]`; the old per-address redirect-path anchor `[@ANCHOR: user_websites:report_violation_redirect_target]` stays.

* **Home Routing:** Target view `/<slug>` (the old `/<slug>/home` answers a permanent 301 to it, `user_websites:COMM_user_home_legacy_redirect`) `controller_user_websites_home`.

* **Site Creation:** `/<slug>/create_site` `UX_CREATE_SITE`, concurrency scaling proven by `test_site_creation_performance_scaling`.

* **Blog Routing:** `/<slug>/blog` `controller_user_blog_index`.

* **Blog Creation:** `/<slug>/create_blog` `UX_CREATE_BLOG_POST`.

* **Post Creation & Editing:** `POST /<slug>/create_blog_post` creates a blank "New Post" in the owner's blog (or redirects to `/<slug>/blog` if no blog exists yet) and redirects to `/blog_post/edit/<post_id>`, this module's own ownership-scoped title/body/publish form (the website-builder Edit toolbar needs the sitewide `website.group_website_designer` permission, which site owners do not hold).

* **Documentation:** Proxies knowledge records `/user-websites/documentation` `controller_user_websites_documentation`.

* **Appeals:** User submission endpoint `/website/submit_appeal` `UX_SUBMIT_APPEAL`.

* **Subscriptions:** `/<slug>/subscribe` `UX_SUBSCRIBE`. Unsubscribe verification with HMAC-SHA256 token validation over the model, record, partner and timestamp `controller_unsubscribe_digest`. The timestamp is signed but its age is not checked, so a valid link does not expire.

* **`GET /api/v1/user_websites/pending_reports`**: Returns a JSON object `{'count': int, 'latest_id': int, 'url': str}` of unhandled violation reports (`url` is the back-office pending queue, `/odoo/action-user_websites.action_content_violation_report_pending`). Restricted to administrators `[@ANCHOR: api_pending_reports]`; any other caller gets `{'count': 0, 'error': 'Forbidden'}` with HTTP 200, not an error status. Verified by `[@ANCHOR: test_admin_violation_toast_rpc]`.

* **GDPR Actions:** Privacy dashboard `/my/privacy` `[@ANCHOR: controller_my_privacy_dashboard]`. Data exports `/my/privacy/export` `[@ANCHOR: UX_GDPR_EXPORT]`. Data erasure via batched unlinks (5000 records per batch, run within the request under the `zero_sudo.gdpr_service_internal` service account) `/my/privacy/delete_content` `[@ANCHOR: UX_GDPR_ERASURE]`, then redirects to the public confirmation `/privacy/erased` `[@ANCHOR: user_websites:COMM_privacy_erased]`.

### 🚨 Privilege Deprecation & Cross-Module Execution (CRITICAL)
In adherence to the Micro-Service Account Pattern (ADR-0062), the `user_websites` internal service account (`user_websites.user_websites_service_account`) has been stripped of omnipotent ERP privileges. It **can no longer create or delete** core identity records (`res.users`, `res.partner`). Furthermore, it retains only microscopic access to framework tables (for example, read-only `1,0,0,0` on `res.company` and `res.partner.bank`, and read/write without create/delete `1,1,0,0` on `discuss.channel`; see `security/ir.model.access.csv` for the full list) strictly to satisfy Odoo's internal ORM cascade requirements (The Framework ACL Tax - ADR-0064). ADR-0062 and ADR-0064 are consolidated in `hams_shared/docs/adrs/MASTER_01_SECURITY_ZERO_SUDO.md`.

If your dependent module (e.g., `cloudflare`, `custom_dns`) needs to programmatically resolve slugs or provision websites, **you MUST NOT rely on the `user_websites` service account to bypass ACLs for you.** Instead, you must fetch your own domain-specific service account and pass it using the `override_svc_uid` parameter. Your module must explicitly declare the necessary Access Control Lists (`ir.model.access.csv`) for its own service account to perform the required operations.

### Programmatic Setup & Hooks
**The Secure Cached Resolver Pattern (ADR-0066, consolidated in `hams_shared/docs/adrs/MASTER_08_CORE_ARCHITECTURE_PERFORMANCE.md`)**: The `user_websites` module offers high-performance `@distributed_cache()` resolvers (a per-process cache backed by Redis, from `distributed_redis_cache`) for cross-module use. ALWAYS use these instead of `.search()` in frontend controllers to prevent database exhaustion. Callers **MUST** pass their own `override_svc_uid` to execute the database search under their own service account's context instead of relying on the default System Provisioner, preventing cross-module access rule failures due to the privilege deprecation mentioned above.
* **`res.users._get_user_id_by_slug(slug, override_svc_uid=None)`**: Resolves a user's slug to their User ID. Note: it resolves through raw SQL on the `user_websites_content_routing_view` SQL view, so `override_svc_uid` is accepted but currently has no effect.
* **`res.users.action_suspend_user_websites()`**: Forcefully unpublishes user content (moderation).
* **`res.users.action_pardon_user_websites()`**: Lifts the suspension and resets the strike count to 0 after an approved appeal. It does NOT republish content; previously unpublished pages and posts stay unpublished until manually restored.
* **`get_record_by_slug(slug)`** (from `edge.routing.mixin`, inherited by both `res.users` and `user.websites.group`): Resolves a user's or group's slug to its record ID (`@distributed_cache()`-cached). It takes no `override_svc_uid`; it runs under the `edge_routing` service account when that account exists. The controllers use it for both users and groups.
* **`website.page._get_page_id_by_url(url, website_id, override_svc_uid=None)`**: Resolves a page URL to its Page ID.
* **`RESERVED_SLUGS`**: A set of slugs that are reserved by the system and cannot be used by users or groups (e.g., `community`, `blog`, `my`). Available in `edge_routing.utils` (imported from there by this module). A slug is also refused when it is the first path segment of any route registered in the running database (`edge_routing.utils.routed_first_segments(env)`, read from the live routing map) or an installed language prefix; `edge_routing.utils.reserved_slug_words(env)` is the union the constraints use. The fixed set stays as the list a guard test (`test_09_reserved_slugs_cover_static_single_segment_routes`) keeps in step with the routes, and covers the words that are not routes (sub-paths of a member site, service names).
* **`user_websites.owned.mixin`**: Inherit this in your custom models (e.g., `custom.portfolio`) to get the `owner_user_id`/`user_websites_group_id` ownership fields and the Proxy Ownership checks. The mixin does not override `create()`/`write()` itself: your model must call `self._check_proxy_ownership_create(vals_list)` from its `create()` and `self._check_proxy_ownership_write(vals)` from its `write()`, and must declare its own record rules (this module's `ir.rule` records are declared per model, not on the mixin).
  * **Mandatory Assignment:** Every record created by a non-administrator must be owned by a user or a group. If a non-public caller supplies neither `owner_user_id` nor `user_websites_group_id`, `owner_user_id` defaults to the caller; a public (not logged-in) caller who supplies neither gets an `AccessError`.
  * **Mutual Exclusivity:** A record CANNOT be owned by both a user and a group simultaneously. Attempting to assign both will raise a strict `ValidationError`.
* **Cache Invalidation Hooks:** Distributed slug caches are safely invalidated upon user mutation via `slug_cache_invalidation`, `slug_cache_invalidation_unlink`, `group_slug_cache_invalidation`, and `group_slug_cache_invalidation_unlink`.

* **String Utilities:** Safe slugification generation logic (`slugify()` in `edge_routing.utils`) `utils_slugify`.

* **Limits:** Individual page quota enforcement `website_page_quota_check`.

* **GDPR Hooks**: The module extends `_get_gdpr_export_data()` `[@ANCHOR: res_users_gdpr_export]`, tested by `[@ANCHOR: test_gdpr_export_hook]`, and `_execute_gdpr_erasure()` `[@ANCHOR: gdpr_sudo_erasure]`, tested by `[@ANCHOR: test_gdpr_erasure_pages]` and `[@ANCHOR: test_gdpr_erasure_posts]`. Dependent modules storing PII MUST override these to append their data to the export payload and hard-delete it during erasure.

* **Documentation Injection**: `knowledge` is a hard dependency of this module. Its `data/documentation.html` is declared in the manifest's `knowledge_docs` entry (with `"public": True`, so portal and public users can open it) and installed as a published `knowledge.article` by `zero_sudo`'s `ir.module.module._bootstrap_knowledge_docs()` when the registry loads; `/user-websites/documentation` redirects to that article `[@ANCHOR: documentation_bootstrap]`.
</public_api>

---

<stories_and_journeys>
## 4. Architectural Stories & Journeys

For detailed narratives and end-to-end workflows, refer to the following:

### Stories
* [Group Websites](user_websites/hams_shared/docs/stories/group_sites.md)
* [Content Moderation](user_websites/hams_shared/docs/stories/moderation.md)
* [Personal Site Management](user_websites/hams_shared/docs/stories/personal_site.md)
* [Privacy and GDPR Compliance](user_websites/hams_shared/docs/stories/privacy.md)
* [Technical Foundation and Utilities](user_websites/hams_shared/docs/stories/technical_foundation.md)

### Journeys
* [Extension and Customization](user_websites/hams_shared/docs/journeys/customization.md)
* [User Data Management (GDPR)](user_websites/hams_shared/docs/journeys/gdpr_compliance.md)
* [Content Reporting and Resolution](user_websites/hams_shared/docs/journeys/moderation_workflow.md)
* [First Time Site Setup](user_websites/hams_shared/docs/journeys/onboarding.md)
</stories_and_journeys>

---

<crons_and_subscriptions>
## 5. 📧 Weekly Digests & Subscriptions
* Features an automated `ir.cron` job (`send_weekly_digest` `ir_cron_send_weekly_digest`, `send_weekly_digest`) that iterates through `blog.post` objects and dispatches emails to followers. Re-entrant batching algorithm resumption is tested by `test_cron_batching_resumption`. QWeb Mail templates are verified by `test_weekly_digest_mail_template`.

* Utilizes HMAC-SHA256 tokens to generate secure, one-click `List-Unsubscribe` header links for GDPR/CAN-SPAM compliance `test_weekly_digest_secret`.

* **Background View Counter Sync:** High-throughput Redis view counters are safely flushed to Postgres via cron `ir_cron_flush_view_counters`, tested extensively by `test_cron_redis_flush`. Round-trips are optimized via a PostgreSQL procedure `procedure_flush_view_counters`.

* **High-Speed Simulation Tests:** The full operational load of the module is end-to-end verified via the High-Speed Simulation Environment `simulation_environment`.
</crons_and_subscriptions>

---

<function_reference>
## 6. Comprehensive Function Reference

This section ensures all module functions and their developer usage are thoroughly documented:

### `res.users` (`models/res_users.py`, `models/res_users_moderation.py`)
*   **`_async_unpublish_content(db_name, user_ids)`**: Background task to unpublish pages/posts for suspended users.
*   **`_register_hook()`**: System hook that creates the `sys_provisioner` service user (if missing) and maps the `user_websites.user_websites_service_account` XML ID to it, so other modules can resolve that account early. (Documentation is bootstrapped by `zero_sudo`, not here; see Documentation Injection above.)
*   **`_check_reserved_slugs()`**: ORM constraint ensuring `website_slug` doesn't conflict with system routes.
*   **`_is_admin()`**: Utility returning True if the user belongs to the `base.group_system` or `user_websites.group_user_websites_administrator`.
*   **`_get_page_limit()`**: Returns the user's own `website_page_limit` if set above 0, otherwise the configured global page limit (default 100).
*   **`_get_gdpr_streamed_keys()`**, **`_get_gdpr_export_data()`**, **`_execute_gdpr_erasure()`**: Privacy compliance methods for exporting and purging PII.
*   **`_compute_suspended_group_ids()`**: Computes related suspended groups for UI filtering.
*   **`action_suspend_user_websites()`** / **`action_pardon_user_websites()`**: State transitions for moderation.

### `user.websites.group` (`models/user_websites_groups.py`)
*   **`_async_unpublish_group_content(db_name, group_ids)`**: Background task to unpublish suspended group content.
*   **`_check_reserved_slugs()`**: Ensures group slugs don't overlap with system routes.
*   **`action_suspend_group_websites()`** / **`action_pardon_group_websites()`**: Moderation state transitions for groups.

### `website.page` (`models/website_page.py`)
*   **`_serve_page()`**: Override that increments the page's Redis view counter for non-admin visitors (a Redis failure is logged, never raised).
*   **`_invalidate_cloudflare_cache()`**: Purges Cloudflare edge caches upon page modification.
*   **`_sanitize_user_arch(arch_content)`**: XSS/SSTI sanitizer for user-submitted QWeb/HTML.
*   **`_trigger_malicious_arch_violation(vals, records=None)`**: Automatically reports users and issues strikes upon payload detection: one strike per distinct owner/group among `records` (keyed on that owner's first affected page URL). `write()` refuses a non-admin batch with a sanitizer-modified arch that spans more than one owner/group.
*   **`_flush_redis_view_counters()`**: Cron method flushing Redis counts to Postgres.
*   Standard ORM Overrides: **`create()`**, **`check_access()`**, **`write()`**, **`unlink()`** are heavily overridden to enforce `owner_user_id` proxies and quota limits.

### `blog.post` (`models/blog_post.py`)
*   **`_invalidate_cloudflare_cache()`**: Purges Cloudflare edge caches upon post modification.
*   **`_get_blog_urls()`**: Resolves slug paths for posts.
*   **`send_weekly_digest()`**: Compiles and dispatches the weekly digest email to subscribers.
*   Standard ORM Overrides: **`create()`**, **`check_access()`**, **`write()`**, **`unlink()`** enforce proxy ownership similar to `website.page`.

### `blog.blog` (`models/blog_blog.py`)
*   Standard ORM Overrides: **`create()`**, **`check_access()`**, **`write()`**, **`unlink()`** enforce proxy ownership: a non-admin may write or delete only a blog they own or that belongs to a group they are a member of; creation is limited to 5 blogs per user or group by default (`user_websites.global_blog_limit`).

### `user_websites.owned.mixin` (`models/user_websites_owned_mixin.py`)
*   **`_check_proxy_ownership_create(vals_list)`** / **`_check_proxy_ownership_write(vals)`**: Core assertions ensuring the operating user legally owns the modified proxy record.

### Moderation Models
`content.violation.report` itself (state machine, `action_mark_under_review()`, `action_dismiss()`, `action_take_action_and_strike()`, and the extensible `_apply_enforcement_action()` hook) moved to the `content_moderation` module on 2026-09-23 -- see that module's own README/model file. This module keeps only what's genuinely its own:
*   **`models/content_violation_report_moderation.py`**: this module's `content_group_id` extension field, its `_apply_enforcement_action()` override (the real strike-and-suspend consequence), `_increment_strike_count()`, and `_cron_notify_pending_reports()` (admin digest emails for pending reports -- kept here rather than genericized, since it's tied to this module's own `company_abuse_email` config parameter and service account).
*   **`migrations/0.4/post-migration.py`**: removes the four `content.violation.report` ir.rule records and the `seq_content_violation_report` ir.sequence this module declared (in `noupdate="1"` blocks) before the extraction, which Odoo left behind on already-installed databases because it never deletes noupdate rows a module stops declaring. Only those exact `user_websites.*` xmlids, constrained to the expected model/code; content_moderation's own replacements are untouched `[@ANCHOR: user_websites_orphan_content_violation_cleanup]`.
*   **`models/content_violation_appeal.py`**: `_check_appeal_target()` validates appeals point to a valid suspension; `action_approve()`/`action_reject()` are the admin workflow actions for appeals. Stayed in this module (not moved to `content_moderation`) since both actions call this module's own `action_pardon_user_websites()`/`action_pardon_group_websites()` directly, with no second consumer yet to justify a generic hook the way `content.violation.report` got one.

### Configuration (`models/res_config_settings.py`)
*   No `get_values()`/`set_values()` overrides: two plain `config_parameter` fields, `global_website_page_limit` (`user_websites.global_website_page_limit`, default 100) and `company_abuse_email` (`user_websites.company_abuse_email`). Administrator membership is managed on the group itself (the Settings page only links to it).

### SQL Views (`models/sql_views.py`)
*   **`init()`**: Executes the `CREATE OR REPLACE VIEW` statements for three plain (non-materialized) SQL views: the community directory (opted-in, active, non-suspended users with a slug), content routing (slug resolution), and the weekly digest. `UserWebsitesDbFunctions.init()` also creates the `increment_strike_count()` database function.

### Controllers (`controllers/main.py`, `controllers/user_websites_api.py`)
*   **`UserWebsitesController`**:
    *   `report_violation()`: POST handler for violation flags.
    *   `user_blog_index()`, `user_home_fallback()`: Resolvers for `/<slug>/blog` and `/<slug>` (plus the `/<slug>/home` 301).
    *   `create_site()`, `create_blog()`: JIT provisioners for missing infrastructure.
    *   `create_blog_post()`, `blog_post_edit()`, `blog_post_edit_submit()`: New-post creation and the owner-only post edit form (`/blog_post/edit/<post_id>`).
    *   `documentation()`: Redirects to the module's published knowledge article.
    *   `community_directory()`: Renders `/community`.
    *   `privacy_dashboard()`, `privacy_export()`, `privacy_export_zip()`, `privacy_delete_content()`, `privacy_erased()`: GDPR endpoints (`privacy_export_zip()` mints a short-lived, single-use token and redirects to the separate GDPR export download service at `/api/v1/gdpr_export/download`).
    *   `submit_appeal()`: Handler for moderation appeals.
    *   `pending_reports()`: Plain HTTP GET endpoint returning JSON, used by the admin toast notifications.
    *   `subscribe()`, `unsubscribe()`: Handlers for weekly digest opt-in/opt-out.
*   **`UserWebsitesApi`** (`controllers/user_websites_api.py`):
    *   `api_domains()`: Public `GET /api/v1/user_websites/domains` returning `{"domains": [...]}`, every custom domain (`edge.routing.domain`) plus, if `ham_dns` is installed, every `ham.dns.zone` name, for Let's Encrypt certificate maintenance.

### System Hooks (`hooks.py`)
*   **`post_init_hook(env)`**: Triggered post-installation to execute initial setup routines: adds every existing real user (except the public user and service accounts) to `group_user_websites_user`, backfills empty `website_page.name` values from the linked view, creates partial indexes on published pages and posts, and flags the Cloudflare purge and User Websites service accounts as `is_service_account`.
</function_reference>

## Owner Interface (2026-10-12)

* **Owner toolbar** `[@ANCHOR: user_websites:UX_OWNER_TOOLBAR]` (`views/owner_site_templates.xml`, called from `user_navbar`): shown above the site's own navbar to the site's owner, or a member of the owning group, on every page of that site (an internal user sees it on their own site only). `?visitor=1` hides it.
* **Dashboard** `/my/site/<slug>` `[@ANCHOR: user_websites:COMM_owner_site_dashboard]`, `/my/site` redirects to the caller's own. Page editor `/my/site/<slug>/page/new|<id>` `[@ANCHOR: user_websites:UX_OWNER_PAGE_EDITOR]`, save `[@ANCHOR: user_websites:COMM_owner_page_save]`, publish toggles `[@ANCHOR: user_websites:COMM_owner_page_publish]` and `[@ANCHOR: user_websites:COMM_owner_post_publish]`, image upload `[@ANCHOR: user_websites:COMM_owner_image_upload]` (`controllers/owner_site.py`). Every route resolves the site from the database and answers 404 unless the caller owns it, or belongs to the owning group (`[@ANCHOR: user_websites:owner_resolve_site]`); an administrator who is not the owner gets the 404 too. The owner of a suspended or not-yet-eligible site gets a 403 page that says why instead (hidden from everyone else); a retired slug redirects to the live one.
* **Editor** `[@ANCHOR: user_websites:UX_RICH_EDITOR]`: a contenteditable WYSIWYG (`static/src/js/rich_editor.js`), no raw HTML and no external library. Odoo's own `html_editor` is an OWL backend component tied to the website builder's `website.group_website_designer` permission and its asset bundles; mounting it for portal members would hand them the sitewide editor, which this module deliberately never grants.
* **Sanitizer** `[@ANCHOR: user_websites:member_html_sanitize]` (`models/member_html.py`): strict allow-list in the request path for every caller, administrators included; hostile input is also filed through `website.page._trigger_malicious_arch_violation()`.
* **Limits**: title 120 characters, body 200,000 characters, images JPEG/PNG/GIF/WebP at most 2 MB and 100 per site, 30 writes and 10 uploads per user per minute (Redis counters, `[@ANCHOR: user_websites:owner_rate_limit]`; Redis down answers 503).
* **Retention**: no route erases. Unpublish keeps the record as a draft. Delete (`POST .../<page|post>/<id>/delete`, with a confirmation step) takes a page or post off the site and every public surface but archives it (`active` false, `deleted_by_owner` stamped; a page is moved to a private address so its own can be reused), and the dashboard's Deleted list can restore it, unpublished. The home page cannot be deleted. Uploaded images are public `ir.attachment` rows owned by the account (`res_model='res.users'`/`user.websites.group`, description `user_websites site image`); a member's account-erasure flow removes the images owned by that member's own account (not a group's images).
* **Layout** `[@ANCHOR: user_websites:member_site_standalone_layout]`: on a member site `layout_inherit_user_navbar` sets `no_header`/`no_footer`, so the main site's header and footer are not rendered, and the site's own header (`user_navbar`) and footer (`member_site_footer`) are. Assets: `static/src/css/member_site.css` and `rich_editor.js` in `web.assets_frontend`.
* **Home address** `/<slug>` `[@ANCHOR: user_websites:COMM_user_home_fallback]`; `/<slug>/home` answers 301 `[@ANCHOR: user_websites:COMM_user_home_legacy_redirect]`.
* **Owner-site internals**: `/my/site` `[@ANCHOR: user_websites:COMM_owner_site_entry]`; the new-page and edit-page forms `[@ANCHOR: user_websites:COMM_owner_page_new]`, `[@ANCHOR: user_websites:COMM_owner_page_edit]`; a page's arch is the fixed member-page frame around sanitized body markup `[@ANCHOR: user_websites:owner_page_arch]` and the editor reads the body back out of it `[@ANCHOR: user_websites:owner_page_arch_body]`; uploads are accepted by magic number, not by file name or declared type `[@ANCHOR: user_websites:owner_image_mimetype]`.
* **Editor functions** (`static/src/js/rich_editor.js`, exercised by `[@ANCHOR: test_tour_owner_site]`): `[@ANCHOR: user_websites:rich_editor_apply_link]`, `[@ANCHOR: user_websites:rich_editor_clean_node]`, `[@ANCHOR: user_websites:rich_editor_close_link_bar]`, `[@ANCHOR: user_websites:rich_editor_image_picked]`, `[@ANCHOR: user_websites:rich_editor_link_bar_click]`, `[@ANCHOR: user_websites:rich_editor_link_keydown]`, `[@ANCHOR: user_websites:rich_editor_open_link_bar]`, `[@ANCHOR: user_websites:rich_editor_paste]`, `[@ANCHOR: user_websites:rich_editor_remember_range]`, `[@ANCHOR: user_websites:rich_editor_restore_range]`, `[@ANCHOR: user_websites:rich_editor_say]`, `[@ANCHOR: user_websites:rich_editor_start]`, `[@ANCHOR: user_websites:rich_editor_sync_field]`, `[@ANCHOR: user_websites:rich_editor_toolbar_click]`, `[@ANCHOR: user_websites:rich_editor_toolbar_state]`, `[@ANCHOR: user_websites:rich_editor_wrap_code]`.
* **Helper functions** (owner interface and sanitizer, each exercised by `tests/test_owner_site.py`): `[@ANCHOR: user_websites:fn_main_serve_core_fallback]`, `[@ANCHOR: user_websites:fn_owner_escape]`, `[@ANCHOR: user_websites:fn_owner_escape_attr]`, `[@ANCHOR: user_websites:fn_owner_svc]`, `[@ANCHOR: user_websites:fn_owner_owner_domain]`, `[@ANCHOR: user_websites:fn_owner_qcontext]`, `[@ANCHOR: user_websites:fn_owner_report_if_hostile]`, `[@ANCHOR: user_websites:fn_owner_clean_body]`, `[@ANCHOR: user_websites:fn_owner_own_page]`, `[@ANCHOR: user_websites:fn_owner_back_to_editor]`, `[@ANCHOR: user_websites:fn_owner_attachment_owner]`, `[@ANCHOR: user_websites:fn_hmtl_clean_url]`, `[@ANCHOR: user_websites:fn_hmtl_is_hostile]`, `[@ANCHOR: user_websites:fn_hmtl_sanitize_member_html]`, `[@ANCHOR: user_websites:fn_hmtl_clean_children]`, `[@ANCHOR: user_websites:fn_hmtl_clean_attributes]`, `[@ANCHOR: user_websites:fn_hmtl_drop]`, `[@ANCHOR: user_websites:fn_hmtl_unwrap]`, `[@ANCHOR: user_websites:fn_hmtl_menu_pages]`, `[@ANCHOR: user_websites:fn_users_user_websites_menu_pages]`, `[@ANCHOR: user_websites:fn_group_user_websites_menu_pages]`.
* **SVG schematics** survive the sanitizer through zero_sudo's SVG allow-list `[@ANCHOR: user_websites:member_html_svg_allowlist]`; every other active or unknown element is removed.

<stage1_sweep>
## 7. Function-Level Anchors

### Moderation
* **Strike action:** `action_take_action_and_strike` -- defined in the `content_moderation` module (see Moderation Models above); this module's enforcement hook turns it into a strike and, past 3 strikes, a suspension.

### Controllers
* **User Blog Index Route:** `user_websites:COMM_user_blog_index` -- `/<slug>/blog`.

* **Create Site Route:** `user_websites:COMM_create_site`.

* **Create Blog Route:** `user_websites:COMM_create_blog`.

* **Privacy Dashboard Route:** `user_websites:COMM_privacy_dashboard` -- `/my/privacy`.

* **Privacy Delete Content Route:** `user_websites:COMM_privacy_delete_content` -- `/my/privacy/delete_content`.

* **Pending Reports API Route:** `user_websites:COMM_pending_reports`.

### Blog Ownership, Quota, and Access
* **Blog Create/Check-Access/Write/Unlink:** `user_websites:COMM_blog_blog_create`, `user_websites:COMM_blog_blog_check_access`, `user_websites:COMM_blog_blog_write`, `user_websites:COMM_blog_blog_unlink`.

* **Blog Post URL Helper:** `user_websites:COMM_get_blog_urls` -- constructs the blog-index URLs invalidated on any post mutation.

* **Blog Post Check-Access/Write/Unlink:** `user_websites:COMM_blog_post_check_access`, `user_websites:COMM_blog_post_write`, `user_websites:COMM_blog_post_unlink`.

### Content Moderation and Appeals
* **Appeal Target Constraint:** `user_websites:COMM_check_appeal_target` -- an appeal must be tied to exactly one of a user or a group.

* **Approve Appeal:** `user_websites:COMM_appeal_action_approve` -- pardons the appeal's user or group and moves the appeal to `approved`; it raises a `UserError` if the appeal is no longer in the `new` state, so a repeated call cannot pardon twice.

* **Reject Appeal:** `user_websites:COMM_appeal_action_reject`.

* **Strike Count Increment:** `user_websites:COMM_increment_strike_count` -- the atomic stored-procedure call backing the 3-strike rule.

* **Mark Report Under Review:** `user_websites:COMM_action_mark_under_review`.

* **Dismiss Report:** `user_websites:COMM_report_action_dismiss`.

* **Suspend/Pardon a Group:** `user_websites:COMM_action_suspend_group_websites`, `user_websites:COMM_action_pardon_group_websites`.

* **Suspend/Pardon a User:** `user_websites:COMM_action_suspend_user_websites`, `user_websites:COMM_action_pardon_user_websites`.

* **Suspended-Group Membership Compute:** `user_websites:COMM_compute_suspended_group_ids` -- which of a user's groups are currently suspended.

### GDPR Export
* **Export Token Name Compute:** `user_websites:COMM_gdpr_token_compute_name`.

* **Mint Export Token For Current User:** `user_websites:COMM_create_for_current_user`.

* **Streamed GDPR Export Keys:** `user_websites:COMM_get_gdpr_streamed_keys` -- generator-based streaming to avoid OOM on large exports.

### Settings
* **Settings fields:** `global_website_page_limit`, `company_abuse_email` -- plain scalar
  `config_parameter`-backed fields persisted by `res.config.settings`'s own base
  `get_values()`/`set_values()`, no override needed. The module's own prior
  `get_values()`/`set_values()` overrides (anchors `user_websites:COMM_settings_get_values` /
  `user_websites:COMM_settings_set_values`) were removed -- see ADR-0096
  (`hams_shared/docs/adrs/0096_res_config_settings_authorization_model.md`) -- because they wrote
  `group_user_websites_administrator.user_ids` as a side effect of every Settings save, which is
  what let an unrelated field's write cascade into a crash, and because that access-csv row also
  granted this role model-level access to every OTHER module's Settings fields. Managing this
  group's membership now goes through Odoo's own "Groups" screen (button on the Settings page),
  verified by `test_dropzone_settings`.

### SQL Views and Functions (`_auto=False`, proven live by any test that successfully queries the resulting view -- a broken `init()` would fail module installation before any such test could run)
* **Public Directory View:** `user_websites:COMM_public_directory_view_init`.

* **Content Routing View:** `user_websites:COMM_content_routing_view_init`.

* **Weekly Digest View:** `user_websites:COMM_weekly_digest_view_init`.

* **DB Functions (`increment_strike_count`):** `user_websites:COMM_db_functions_init`.

### res.users Extensions
* **Background Content Unpublish:** `user_websites:COMM_async_unpublish_content` -- module-level helper run on a background thread when a user is suspended or archived.

* **Registry Hook:** `user_websites:COMM_register_hook` -- provisions the `sys_provisioner` service account at registry build time.

* **Reserved Slug Checks:** `user_websites:COMM_res_users_check_reserved_slugs` (users), `user_websites:COMM_group_check_reserved_slugs` (groups).

* **Admin Check:** `user_websites:COMM_is_admin`.

* **Slug-to-User-ID Resolver:** `[@ANCHOR: user_websites:COMM_get_user_id_by_slug]` -- direct raw-SQL resolver against the content routing view. Nothing else in this repository calls this method (the cache-invalidation code only mentions it in comments); the generic `edge.routing.mixin.get_record_by_slug()` dispatch this module also relies on does its own separate ORM search instead. It is tested directly.

* **User Create/Write:** `user_websites:COMM_res_users_create`, `user_websites:COMM_res_users_write` -- the latter includes the 301-redirect-on-slug-change automation.

* **Group Write:** `user_websites:COMM_group_write` -- the same slug-redirect automation for `user.websites.group`.

* **Quota Helpers:** `user_websites:COMM_get_page_limit`, `user_websites:COMM_get_blog_limit`, `user_websites:COMM_get_blog_post_limit`.

### website.page Extensions
* **Cloudflare Cache Invalidation:** `user_websites:COMM_page_invalidate_cloudflare_cache`.

* **Automated Malicious-Arch Violation Report:** `user_websites:COMM_trigger_malicious_arch_violation` -- files a `content.violation.report` against the page's owner whenever the arch sanitizer actually strips something.

* **URL-to-Page-ID Resolver:** `user_websites:COMM_get_page_id_by_url` -- direct URL lookup respecting suspension state. Likewise, nothing else in this repository calls it. It is tested directly.

* **Page Unlink:** `user_websites:COMM_website_page_unlink`.
</stage1_sweep>

## 8. Anchor reference

One entry per anchor, in the order the sections above cite them. The sections above name each id in backticks; the entry here carries the tag.

### `mixin_proxy_ownership_create` [@ANCHOR: mixin_proxy_ownership_create]

`user_websites.owned.mixin._check_proxy_ownership_create(self, vals_list)` (`models/user_websites_owned_mixin.py`) is an `@api.model` check that a model inheriting the mixin must call from its own `create()`.

| Argument | Type | Meaning |
| --- | --- | --- |
| `vals_list` | list of dict | The create values. It is mutated: a missing owner is filled in with `owner_user_id` set to the caller. |

**Returns:** nothing. **Errors:** `ValidationError` when one record names both `owner_user_id` and `user_websites_group_id`; `AccessError` when a non-privileged caller names no owner (a public caller), names another user, or names a group they do not belong to or that does not exist.

**Privilege and cost:** superuser, `base.group_system`, the module administrator group and `group_user_websites_ownership_bypass` skip the membership checks but still get the both-owners check. Group membership is prefetched once, as the module service account, so the check is a single group read however many records are created. If the service account is not yet provisioned the check is skipped (early install only). Test: `test_mixin_ownership_validation`.

### `mixin_proxy_ownership_write` [@ANCHOR: mixin_proxy_ownership_write]

`user_websites.owned.mixin._check_proxy_ownership_write(self, vals)` is called from the `write()` of every owned model before the real write.

| Argument | Type | Meaning |
| --- | --- | --- |
| `vals` | dict | The values about to be written. Only `owner_user_id` and `user_websites_group_id` matter here. |

**Behaviour:** a caller who is not privileged (superuser, `base.group_system`, module administrator, ownership bypass) and writes either ownership field gets an `AccessError`, so ownership cannot be transferred or spoofed after creation. A privileged caller may change ownership, but the check computes the resulting owner and group for each record and raises `ValidationError` if the result would be dual-owned. **Returns:** nothing. **Locking:** none; it only reads relations, prefetched in one pass. Tests: `test_mixin_ownership_validation`, `test_api_armor_mutual_exclusion`.

### `test_mixin_ownership_validation` [@ANCHOR: test_mixin_ownership_validation]

Test `test_03_mixin_ownership_validation` in `tests/test_sdk_extensibility.py`. It creates a second portal user in the website-user group and has that user, through `with_user`, try to create a `website.page` whose `owner_user_id` is another user. The test passes only when the create raises `AccessError`, which shows the mixin blocks cross-user creation. It covers `mixin_proxy_ownership_create` and, with `test_api_armor_mutual_exclusion`, `mixin_proxy_ownership_write`.

### `dropzone_home_header` [@ANCHOR: dropzone_home_header]

An empty `<div id="user_websites_dropzone_home_header">` at the top of the `user_websites.template_default_home` template (`views/user_websites_templates.xml`), placed before the page body slot. Other modules inject content into a member's home page header with an `<xpath>` on that id; the module never fills it. It is public content, rendered for every visitor. Rendering is checked by `test_dropzone_home_header` through `test_07_navbar_rendering`, which loads a member home page and looks for the id in the HTML.

### `dropzone_home_footer` [@ANCHOR: dropzone_home_footer]

An empty `<div id="user_websites_dropzone_home_footer">` at the end of `user_websites.template_default_home`, after the page body slot (`<t t-out="0"/>`). Dependent modules extend it with an `<xpath>` on the id to add content below every member or group home page. The module puts nothing in it. `test_07_navbar_rendering` loads a published member home and asserts the id is present in the HTML.

### `dropzone_navbar` [@ANCHOR: dropzone_navbar]

An empty `<div id="user_websites_dropzone_navbar">` inserted right after the site `<header>` on every website page (an xpath on `website.layout`, `views/user_websites_templates.xml`). It sits before the member navbar, which is called only when a slug is resolved and the page is a member site. Extend it with an xpath on the id for content that should appear under the global header. Verified by `test_dropzone_navbar` in `test_07_navbar_rendering`.

### `dropzone_templates` [@ANCHOR: dropzone_templates]

An empty `<div id="user_websites_dropzone_templates">` placed above `o_portal_my_home` on the portal home page (`/my/home`), by the `portal_my_home_appeal` template that also carries the appeal and privacy cards. Other modules extend it with an xpath on the id to add portal content. Verified by `test_05_portal_templates`: a logged-in portal user requests `/my/home` and the response must be 200, contain the privacy text, and contain the id.

### `dropzone_snippets` [@ANCHOR: dropzone_snippets]

An empty `<div id="user_websites_dropzone_snippets">` added inside the website builder snippets panel (`website.snippets`, `views/snippets.xml`), just before the module's "Profile Widgets" accordion group. Dependent modules use an xpath on the id to add their own snippets next to this group. The module adds nothing to it. `test_04` in `tests/test_xpath_rendering.py` checks that the snippet injection roots into the parent view.

### `dropzone_layout` [@ANCHOR: dropzone_layout]

An empty `<div id="user_websites_dropzone_layout">` added after the `<footer>` of the global `website.layout` (`views/website_layout.xml`), immediately before the report-violation modal include. It exists on every public page. Dependent modules extend it with an xpath on the id for site-wide additions. `test_06_layout_templates` fetches `/` as a guest and checks the injected modal and the guest email field render, which proves the surrounding xpath works.

### `dropzone_users` [@ANCHOR: dropzone_users]

An empty `<div id="user_websites_dropzone_users">` at the top of the "User Websites" notebook page of the `res.users` form (`views/res_users_views.xml`), above the module's configuration groups (slug, page limit, directory opt-in, suspension). Back-office modules extend the page with an xpath on this id. Verified by `test_02_res_users`, which compiles the form view and requires the `user_websites_settings` page in the arch.

### `dropzone_blog_post` [@ANCHOR: dropzone_blog_post]

An empty `<div id="user_websites_dropzone_blog_post">` in the back-office blog post form (`views/blog_post_views.xml`), placed after the `blog_id` field together with the `owner_user_id` and `user_websites_group_id` fields. Dependent modules extend the form with an xpath on the id to add owner-related fields. Tested by `test_dropzone_blog_post` in `test_03_blog_post`, which compiles the view.

### `dropzone_navbar_actions` [@ANCHOR: dropzone_navbar_actions]

An empty `<div id="user_websites_dropzone_navbar_actions">` inside the member navbar (`user_navbar`), after the navigation list that holds Blog and the owner's menu pages. Add buttons or links there with an xpath on the id; they render only on member and group sites, not on every page. `test_07_navbar_rendering` creates a member with a home page, loads it, and asserts the id is in the response.

### `dropzone_directory_card` [@ANCHOR: dropzone_directory_card]

An extension point inside each member card of the `/community` directory (`views/user_websites_templates.xml`). Other modules inject card content, for example a badge, with an xpath on `user_websites_dropzone_directory_card`. The module leaves it empty. The view cites a `test_dropzone_directory_card` test id, but no test file defines or runs it: the card dropzone is not render-tested (to-do). The directory page itself is tested by `test_03_community_directory_rendering`.

### `dropzone_settings` [@ANCHOR: dropzone_settings]

An empty `<div id="user_websites_dropzone_settings">` injected inside the `<form>` of `base.res_config_settings_view_form`, just before the module's `<app>` block (`views/res_config_settings_views.xml`). Extend the module's Settings block with an xpath on the id. The older `user_websites_settings_dropzone` is prohibited. Verified by `test_dropzone_settings` in `test_01_res_config_settings`: the compiled arch must contain `data-key="user_websites"`.

### `UX_COMMUNITY_DIRECTORY` [@ANCHOR: UX_COMMUNITY_DIRECTORY]

The public `/community` page (`community_directory` in `controllers/main.py`). It lists members who opted in with `privacy_show_in_directory`, who are active, not suspended and have a slug, read from the community directory SQL view as the module service account (up to 1000 rows). Tests: `test_03_community_directory_rendering` (opted-in user visible), `test_04_community_directory_privacy_respect` (opted-out user absent), and the `community_directory_tour` tour (the page shows a "Community Directory" heading).

### `user_websites:COMM_user_home_legacy_redirect` [@ANCHOR: user_websites:COMM_user_home_legacy_redirect]

`UserWebsitesController.user_home_legacy_redirect(website_slug)`, route `GET /<uwslug:website_slug>/home`, public, not in the sitemap. The old home address `/<slug>/home` answers a permanent 301 to `/<slug>`. The target is built from the slug as stored in the database (lower case, `[a-z0-9-]`) and quoted, never from the request path, so it is always a same-origin local path. For a slug that is not a member or group site it tries the retired-slug redirect and then Odoo's stock page fallback before returning 404. The lookup runs as the module service account.

### `controller_user_websites_home` [@ANCHOR: controller_user_websites_home]

The member or group home route `GET /<uwslug:website_slug>`, handled by `user_home_fallback` (anchor `user_websites:COMM_user_home_fallback`). It serves the home page or the placeholder that lets the owner create it, 301s a differently cased slug to the lower-case one, and hands a non-member single segment to Odoo's stock page lookup. Hidden or suspended sites give 404. This anchor is used as a test citation: `create_site_tour` and `test_02_group_member_access` in `tests/test_lifecycle_and_groups.py` exercise it.

### `UX_CREATE_SITE` [@ANCHOR: UX_CREATE_SITE]

The "Create Your Site" action: a POST to `/<slug>/create_site`, handled by `create_site` (see `user_websites:COMM_create_site`). It builds the member's home page from the standard template and redirects to it. In this document the id is cited as the user experience, and the tests cite it: `create_site_tour` clicks the button on `/sitetour` and checks the home header dropzone exists afterwards; `test_02_group_member_access` has a group member create the group home and expects HTTP 200.

### `test_site_creation_performance_scaling` [@ANCHOR: test_site_creation_performance_scaling]

Test `test_01_site_creation_query_scaling` in `tests/test_performance_regressions.py` (also cited from `website.page.create`). It creates 15 users, then creates one home `website.page` per user as the module service account, counting SQL queries with `cr.sql_log_count` around each create after a flush. After discarding the first (warm-up) count, the spread between the highest and lowest count must be at most 5. A larger spread means creation cost grows with the number of existing sites, the view-invalidation storm this guards against.

### `controller_user_blog_index` [@ANCHOR: controller_user_blog_index]

The blog index route `/<slug>/blog`, handled by `user_blog_index` (anchor `user_websites:COMM_user_blog_index`). The id is a test citation: `create_blog_tour` opens `/blogtour/blog`, clicks the create button on the placeholder page, and after the reload checks the blog dropzone element exists. See also `UX_CREATE_BLOG_POST`.

### `UX_CREATE_BLOG_POST` [@ANCHOR: UX_CREATE_BLOG_POST]

The "Create Your Blog" action, a POST to `/<slug>/create_blog` handled by `create_blog` (`user_websites:COMM_create_blog`), after which the browser lands on `/<slug>/blog`. The id is the user-experience name used by the stories and journeys. Tests: `create_blog_tour` and `test_08_group_inverse_relationships` in `tests/test_lifecycle_and_groups.py`, which creates a group page.

### `controller_user_websites_documentation` [@ANCHOR: controller_user_websites_documentation]

The documentation route `GET /user-websites/documentation`, login required, handled by `documentation` in `controllers/main.py` (code anchor `user_websites:COMM_documentation`). It searches `knowledge.article` for a name like "User Websites Documentation" as the current user, so the article's own access rules apply, and redirects to its website URL. If none exists it renders the `documentation_page` fallback template. No code or test carries this id itself, so the test is `test_documentation_route`.

### `UX_SUBMIT_APPEAL` [@ANCHOR: UX_SUBMIT_APPEAL]

`submit_appeal(reason="", group_id=None)`, route `POST /website/submit_appeal`, logged-in users, CSRF protected. It creates a `content.violation.appeal` in state `new`, as the module service account. With a `group_id` the caller must be a member of that group, otherwise `/my/home?error=appeal_failed`. An empty reason redirects to `?error=missing_reason`; success redirects to `/my/home?appeal_submitted=1`. A `KeyError` or `ValueError` is logged and answered as `appeal_failed`. Tests: `moderation_appeal_tour` and `test_02_submit_and_approve_appeal`.

### `UX_SUBSCRIBE` [@ANCHOR: UX_SUBSCRIBE]

`subscribe(website_slug)`, route `POST /<slug>/subscribe`, logged-in users, CSRF protected. It resolves the slug to a user or group (404 if neither), then as the service account calls `message_subscribe` on the user's partner or on the group, adding the caller's partner as follower. It returns JSON `{"success": true}`. The weekly digest goes to these followers. Test: `test_03_subscribe_to_site` posts as a follower and expects HTTP 200.

### `controller_unsubscribe_digest` [@ANCHOR: controller_unsubscribe_digest]

`unsubscribe(model, record_id, partner_id, timestamp, token)`, route `/website/unsubscribe/<model>/<record_id>/<partner_id>/<timestamp>/<token>`, public. The model must be `res.partner` or `user.websites.group`, the record must exist, and the token must equal the HMAC-SHA256 hexdigest of `model-record_id-partner_id-timestamp` under the crypto secret, compared with `hmac.compare_digest`. Any failure returns JSON `{"error": "Forbidden"}` with status 403. On success the partner is unsubscribed and `unsubscribe_success` renders. The timestamp is signed but not age-checked. Tested in `test_subscriptions.py`.

### `user_websites:COMM_user_blog_index` [@ANCHOR: user_websites:COMM_user_blog_index]

`user_blog_index(website_slug, tag=None, search=None, date_begin=None, date_end=None, page=1)`, route `/<slug>/blog`, public. The filter arguments are accepted but unused; the page lists at most 100 blogs and 100 posts. It resolves the slug through the service account, returns 404 for an unknown, hidden or suspended site (or a retired-slug redirect), and treats the profile owner or a group member as owner. Non-owners see only published posts; owners also see drafts. With no blogs and no posts it renders `placeholder_page`; otherwise `blog_index`. Tests: `test_qweb_context.py`, `test_user_blog_urls.py`.

### `user_websites:COMM_create_site` [@ANCHOR: user_websites:COMM_create_site]

`create_site(website_slug)`, route `POST /<slug>/create_site`, `auth="user"`, CSRF protected. The caller must be the site's user, or a member of the group; anything else, or a suspended site, is a 404. An ineligible user is redirected (303) to `/my/site/<slug>`. If a home page already exists it is restored or republished as needed and the caller is redirected; a blocked home goes to `?error=home_blocked`. Otherwise it creates a published qweb home page, with the entity name XML-escaped before it enters the arch, owned by the user or group. Tests: `test_create_site_existing_home.py`, `test_ui_tours.py`.

### `user_websites:COMM_create_blog` [@ANCHOR: user_websites:COMM_create_blog]

`create_blog(website_slug)`, route `POST /<slug>/create_blog`, `auth="user"`, CSRF protected. It applies the same ownership gates as `create_site` (own site or group member, not suspended, eligible, else 404 or a 303 to the owner dashboard), then creates a `blog.blog` with a generated name for the entity as the service account and redirects to `/<slug>/blog`. The per-owner blog limit is enforced inside `blog.blog.create`, not here. Test: `tests/test_user_websites_blog_post.py`.

### `user_websites:COMM_privacy_dashboard` [@ANCHOR: user_websites:COMM_privacy_dashboard]

`privacy_dashboard()`, route `GET /my/privacy`, login required. It renders `user_websites.portal_my_privacy` with an empty context, the page with the export and erasure controls. It does no data access itself. Tested by `test_03_gdpr_privacy_tour` in `tests/test_ui_tours.py`, which opens the page and runs `gdpr_privacy_tour`.

### `user_websites:COMM_privacy_delete_content` [@ANCHOR: user_websites:COMM_privacy_delete_content]

`privacy_delete_content()`, route `POST /my/privacy/delete_content`, login required, CSRF protected. It calls `_execute_gdpr_erasure()` on the current user (batched unlinks under the GDPR service account, then the account is deactivated) and redirects to the public `/privacy/erased` page, because a logged-in landing page would bounce to the login form. The action is irreversible. Tested by `test_privacy_gdpr.py`, which posts to the route.

### `user_websites:COMM_pending_reports` [@ANCHOR: user_websites:COMM_pending_reports]

`pending_reports()`, route `GET /api/v1/user_websites/pending_reports`, `auth="public"`, excluded from the sitemap. For administrators (module administrator or `base.group_system`) it returns JSON `{"count", "latest_id", "url"}` for new violation reports (count capped at 1000). Anyone else, including the public user, gets `{"count": 0, "error": "Forbidden"}` with HTTP 200, so the toast script does not raise. Tested in `tests/test_controllers.py`.

### `slug_cache_invalidation` [@ANCHOR: slug_cache_invalidation]

Behaviour: after a user's slug changes, the cached slug lookup `get_record_by_slug()` (a `@distributed_cache()` method from `edge.routing.mixin`) must stop returning the old answer. `edge.routing.mixin.write()` collects the old and new slugs and calls `zero_sudo.security.utils._notify_cache_invalidation(model, slugs)`; a failure to notify is logged as a warning and never fails the write. `res.users.write()` also calls `notify_model_invalidation("res.users")` when `website_slug`, `active`, `is_suspended_from_websites`, `is_service_account` or `website_ineligible` is written, and only after the real write succeeded. Tested by `test_04_bdd_ormcache_query_counting_slugs`.

### `slug_cache_invalidation_unlink` [@ANCHOR: slug_cache_invalidation_unlink]

Behaviour: deleting a user must evict that user's slug from the slug cache. `edge.routing.mixin.unlink()` reads the slugs of the records first, performs the real unlink, then calls `_notify_cache_invalidation(model, slugs)`; a notification failure is only logged. Without it a deleted user's slug would keep resolving to a missing id until the cache entry expired. The test `test_04_bdd_ormcache_query_counting_slugs` ends by unlinking its user after proving that a cache hit runs no `res_users` query and a rename runs one.

### `group_slug_cache_invalidation` [@ANCHOR: group_slug_cache_invalidation]

The group counterpart of `slug_cache_invalidation`: writing `website_slug` (or `name`, which can derive one) on a `user.websites.group` record goes through the same `edge.routing.mixin.write()`, which evicts the old and new slugs. `user.websites.group.write()` additionally evicts the cached page lookup when `is_suspended_from_websites` is written. Tested by `test_05_bdd_ormcache_query_counting_group_slugs`: a repeated lookup runs no `user_websites_group` query, and after a rename the new slug resolves to the group.

### `group_slug_cache_invalidation_unlink` [@ANCHOR: group_slug_cache_invalidation_unlink]

Deleting a `user.websites.group` evicts its slug from the slug cache through `edge.routing.mixin.unlink()`, as for users (see `slug_cache_invalidation_unlink`). The slugs are read before the delete and the invalidation is sent after it, and a failed notification is logged, not raised. Tested by `test_05_bdd_ormcache_query_counting_group_slugs` in `tests/test_audit_edge_cases.py`.

### `utils_slugify` [@ANCHOR: utils_slugify]

`slugify(s, max_length=None)` in `edge_routing/utils.py` (code anchor `edge_routing:utils_slugify`, test `test_utils_slugify`). It lower-cases and strips the text, folds accents to ASCII (NFKD, dropping what cannot be folded), replaces every run of characters outside `a-z0-9` with a single hyphen and trims hyphens at both ends. A falsy input gives `""`. With `max_length` the result is cut to that length and trailing hyphens are trimmed, and `max_length=0` really gives the empty string. Pure and side-effect free; it does not check reserved words or uniqueness.

### `website_page_quota_check` [@ANCHOR: website_page_quota_check]

The quota block in `website.page.create()` (`models/website_page.py`). For each distinct `owner_user_id` in `vals_list` it asks the owner for `_get_page_limit()` as the service account, counts existing pages with one `_read_group`, adds the pages in this batch, and raises `ValidationError` ("You have reached your limit of N website pages.") when a user's total would exceed the limit. Groups are checked the same way against the global limit, `user_websites.global_website_page_limit` (default 100). If the service account cannot be resolved, it uses the `sys_provisioner` login read by SQL. The blog and post create paths cite it. Tests: `test_page_quota_limit`, `test_page_limits`.

### `ir_cron_send_weekly_digest` [@ANCHOR: ir_cron_send_weekly_digest]

The `ir.cron` record in `data/user_websites_data.xml`. It runs `model.send_weekly_digest()` on `blog.post` once a week, as the module service account, and is active by default. The method re-triggers this same cron while a backlog remains, so one weekly tick can span several runs. See `send_weekly_digest` for the batching and mail details. Test: `test_02_cron_batching_resumption` triggers the cron and checks that an interrupted batch resumes and finishes.

### `send_weekly_digest` [@ANCHOR: send_weekly_digest]

`blog.post.send_weekly_digest()`, an `@api.private @api.model` cron method (`models/blog_post.py`) with no arguments and no return value. It reads up to 50 rows after `user_websites.last_digest_id` from the weekly digest view, one row per follower and author with the last seven days of published posts. For each row with an email it builds an unsubscribe URL signed with HMAC-SHA256 over `model-record-partner-timestamp`, sets `List-Unsubscribe` and `List-Unsubscribe-Post`, and sends the `email_template_weekly_digest` mail under the mail service account (queued, not forced). A full batch stores the last id and re-triggers the cron; a short batch resets the id to 0. With no crypto secret it logs an error and sends nothing. Tests: `test_weekly_digest_secret`, `test_cron_batching_resumption`.

### `test_cron_batching_resumption` [@ANCHOR: test_cron_batching_resumption]

Test `test_02_cron_batching_resumption` in `tests/test_audit_edge_cases.py`. It triggers `ir_cron_send_weekly_digest`, resets `user_websites.last_digest_id` to 0, creates a published post, then sets the last digest id to 999999 to simulate an interrupted batch and runs `send_weekly_digest()`. Nothing is left past that id, so the method must finish and reset the stored id to "0". It proves that resumption reads the stored id and that a finished queue clears it.

### `test_weekly_digest_mail_template` [@ANCHOR: test_weekly_digest_mail_template]

Verification of the weekly digest mail template, carried in code by the test id `COMM_test_weekly_digest_mail_template` in `tests/test_subscriptions.py`. `test_01_weekly_digest_and_unsubscribe_headers` runs `send_weekly_digest()` and finds the `mail.mail` for the follower with the subject "Weekly Update from Content Creator", created by the mail service account. The same file also renders the template directly for a post with `send_mail`. The plain id used in this document has no separate code anchor.

### `test_weekly_digest_secret` [@ANCHOR: test_weekly_digest_secret]

Test `test_01_weekly_digest_and_unsubscribe_headers` in `tests/test_subscriptions.py`. It runs `send_weekly_digest()`, takes the generated email, and checks that `List-Unsubscribe` and `List-Unsubscribe-Post` headers exist and that the URL points to `/website/unsubscribe/res.partner/...`. It then opens that URL as a public visitor, expects HTTP 200 with "Unsubscribed Successfully", and checks that the follower is no longer a follower, so the HMAC token round trip works. It also covers `controller_unsubscribe_digest`.

### `ir_cron_flush_view_counters` [@ANCHOR: ir_cron_flush_view_counters]

The `ir.cron` record in `data/user_websites_data.xml` that runs `model._flush_redis_view_counters()` on `website.page` every 15 minutes as the module service account. It moves Redis page view counters into the `view_count` column. The method can re-trigger the cron when a run did not drain everything; see `website_page_flush_redis_view_counters` for the exact rule. Test: `test_06_cron_redis_flush_batching`.

### `test_cron_redis_flush` [@ANCHOR: test_cron_redis_flush]

Test `test_06_cron_redis_flush_batching` in `tests/test_audit_edge_cases.py`, with a real Redis. One counter holds 42 views and a second sits at 0. After `_flush_redis_view_counters()` the page's `view_count` rose by 42, both keys are gone from Redis, and the patched `_trigger` was never called. A second run finds nothing, triggers nothing and changes nothing. It guards against the 2026-10 storm in which counters left at zero made the cron retrigger itself about 500,000 times a day.

### `procedure_flush_view_counters` [@ANCHOR: procedure_flush_view_counters]

The server action in `data/procedures.xml`, run at module update, that creates the PostgreSQL function `user_websites_flush_view_counters(updates JSONB)`. The argument is a JSON list of objects `{"inc": int, "pid": int}`. The function adds `inc` to `website_page.view_count` (null counts as 0) for page `pid`, in one call for all pages. It returns nothing. `_flush_redis_view_counters()` calls it with `SELECT user_websites_flush_view_counters(%s)` so a flush costs one round trip rather than one per page.

### `simulation_environment` [@ANCHOR: simulation_environment]

Test `test_01_high_speed_full_platform_exercise` in `tests/test_simulation.py`. It repeats a full platform step (guest page loads, login, member actions, administrator report processing and appeal approval, a final database flush) for `SIMULATION_DURATION_MINUTES` iterations (default 60; the name is historical, it counts iterations). Each operation is timed; averages above a threshold (default 0.5 s, `SIMULATION_MAX_AVG_TIME`, with higher limits for GDPR export, report processing and flush) are logged as `[REGRESSION]`. Regressions only warn; the test does not fail on speed.

### `action_take_action_and_strike` [@ANCHOR: action_take_action_and_strike]

`content.violation.report.action_take_action_and_strike()` is defined in the `content_moderation` module (`content_moderation/models/content_violation_report.py`), a `@public_rpc` method. For each report not already `action_taken` or `dismissed` it sets state `action_taken` and calls `_apply_enforcement_action()`, so a resolved report is never struck twice. This module overrides the hook: it adds a strike to the content owner or group (stored procedure `increment_strike_count`) and suspends at three strikes. Tests: `test_01_three_strikes_suspension` and the `content_moderation` test.

### `test_dropzone_settings` [@ANCHOR: test_dropzone_settings]

Test `test_01_res_config_settings` in `tests/test_xpath_rendering.py`. It requests the compiled form view of `base.res_config_settings_view_form` and asserts the arch contains `data-key="user_websites"`, so the module's settings block and the `dropzone_settings` injection point are present. It checks that the block exists, not what the settings fields do. Settings fields are plain `config_parameter` fields without overrides (ADR 0096).

### `user_websites:COMM_blog_blog_create` [@ANCHOR: user_websites:COMM_blog_blog_create]

`blog.blog.create(vals_list)` (`models/blog_blog.py`), `@api.model_create_multi`. It runs `check_access("create")`, `_check_proxy_ownership_create` (owner required, no dual ownership, group membership) and `_check_blog_quota` (by default 5 blogs per user or group, `user_websites.global_blog_limit`, ValidationError past it). Non-administrators have unknown keys dropped, keeping only name, subtitle, owners and SEO fields. The write is then done as the module service account with tracking off. Unique name per owner is a database constraint. Test: `test_01_blog_creation_is_blocked_past_the_configured_limit`.

### `user_websites:COMM_blog_blog_check_access` [@ANCHOR: user_websites:COMM_blog_blog_check_access]

`blog.blog.check_access(operation)`. For `write` and `unlink`, when the caller is a website user who is not a system or module administrator, every blog in the recordset must be owned by that user or by a group they belong to; otherwise it raises `AccessError` ("Access Denied: You do not have permission to modify this blog."). It exists to fail early rather than let the record rule log a denial. Group membership is read once as the service account. Other operations pass to the base method. Test: `test_01_community_blog_container_protection`.

### `user_websites:COMM_blog_blog_write` [@ANCHOR: user_websites:COMM_blog_blog_write]

`blog.blog.write(vals)`. It calls `check_access("write")` and `_check_proxy_ownership_write` (no ownership transfer by ordinary users). For non-privileged callers, keys outside name, subtitle, owners and SEO fields are silently dropped. The real write runs as the module service account with mail tracking disabled. Returns the base result. Tested in `test_01_community_blog_container_protection`, where a second user's `write` on a community blog raises.

### `user_websites:COMM_blog_blog_unlink` [@ANCHOR: user_websites:COMM_blog_blog_unlink]

`blog.blog.unlink()`. It runs `check_access("unlink")`, which raises `AccessError` unless the caller owns the blog, belongs to the owning group, or is an administrator, and then deletes as the module service account with tracking off. Tested in `test_01_community_blog_container_protection` in `tests/test_exhaustive_isolation.py`.

### `user_websites:COMM_get_blog_urls` [@ANCHOR: user_websites:COMM_get_blog_urls]

`blog.post._get_blog_urls()` returns a list (a set made unique) of blog index paths, `/<slug>/blog`, for the owner user or, failing that, the owning group of each post in the recordset. Posts whose owner has no slug add nothing. It prefetches the slug fields, so one call is two reads. `write` and `unlink` call it before and after a change to tell other workers which blog index pages are stale. Tested in `test_02_blog_post_creation_is_blocked_past_the_configured_limit`.

### `user_websites:COMM_blog_post_check_access` [@ANCHOR: user_websites:COMM_blog_post_check_access]

`blog.post.check_access(operation)`: for `write` or `unlink`, a website user who is not an administrator may only touch posts they own or that belong to a group they are a member of; otherwise `AccessError` ("...permission to modify this post."). Group membership is read once as the service account. Everything else defers to the base class. Tested by `test_05_blog_post_cross_tenant_mutation`.

### `user_websites:COMM_blog_post_write` [@ANCHOR: user_websites:COMM_blog_post_write]

`blog.post.write(vals)`. It runs `check_access("write")` and `_check_proxy_ownership_write`. Non-administrators keep only allowed keys (name, content, is_published, owners, blog_id, website_id and SEO fields), their content is sanitized, and a new `blog_id` must be writable by them. It writes as the service account, falling back to the caller on `AccessError`. When publication, name, content or owner changes it computes the blog index URLs before and after and sends cache invalidation for them, then queues the Cloudflare purge. Tested by `test_05_blog_post_cross_tenant_mutation`.

### `user_websites:COMM_blog_post_unlink` [@ANCHOR: user_websites:COMM_blog_post_unlink]

`blog.post.unlink()`. It checks `check_access("unlink")`, captures the affected blog index URLs, enqueues the Cloudflare purge before the delete, deletes as the service account (falling back to the caller on `AccessError`) and then sends cache invalidation for those URLs. Tested by `test_05_blog_post_cross_tenant_mutation` in `tests/test_exhaustive_isolation.py`, where a second user's `unlink` of a victim's post raises.

### `user_websites:COMM_check_appeal_target` [@ANCHOR: user_websites:COMM_check_appeal_target]

`content.violation.appeal._check_appeal_target()`, an `@api.constrains("user_id", "group_id")` constraint. Exactly one of `user_id` (suspended user) and `group_id` (suspended group) must be set; both or neither raises `ValidationError` ("An appeal must be tied to either a User or a Group, but not both."). It runs on create and on writes to those two fields. Tested by `test_03_reject_appeal_leaves_suspension_in_place`, which creates an appeal with no target and expects the error.

### `user_websites:COMM_appeal_action_approve` [@ANCHOR: user_websites:COMM_appeal_action_approve]

`content.violation.appeal.action_approve()`, a `@public_rpc` method used by the admin form button. Every record must be in state `new`, else `UserError` and nothing is changed, so a stale tab cannot pardon twice. Each appeal moves to `approved`, calls `action_pardon_group_websites()` or `action_pardon_user_websites()` (strikes reset, suspension lifted, content not republished) and posts a note as the mail service account. Tests: `test_02_submit_and_approve_appeal`, `test_04_state_guards_block_reprocessing_a_resolved_appeal`.

### `user_websites:COMM_appeal_action_reject` [@ANCHOR: user_websites:COMM_appeal_action_reject]

`content.violation.appeal.action_reject()`, a `@public_rpc` method. Any record not in state `new` raises `UserError` ("already been resolved"), so an approved appeal cannot be flipped to rejected with a false message. Otherwise the state becomes `rejected`, the suspension stays, and a note ("remains suspended") is posted as the mail service account. Tests: `test_03_reject_appeal_leaves_suspension_in_place` and `test_04_state_guards_block_reprocessing_a_resolved_appeal`.

### `user_websites:COMM_increment_strike_count` [@ANCHOR: user_websites:COMM_increment_strike_count]

`content.violation.report._increment_strike_count(table_name, rec_id)` (`models/content_violation_report_moderation.py`). It runs `SELECT increment_strike_count(%s, %s)`. The stored procedure (see `user_websites:COMM_db_functions_init`) locks the row `FOR NO KEY UPDATE` and adds 1 to `violation_strike_count`, so concurrent reports cannot lose a count. `table_name` must be `res_users` or `user_websites_group`; any other value does nothing. Returns nothing. It is a separate method so tests can verify it without mocking the cursor. Test: `test_01_three_strikes_suspension`.

### `user_websites:COMM_action_mark_under_review` [@ANCHOR: user_websites:COMM_action_mark_under_review]

`content.violation.report.action_mark_under_review()`, in `content_moderation/models/content_violation_report.py`. A report already `action_taken` or `dismissed` raises `UserError` ("cannot be reopened for review"); otherwise the state is written as `under_review`. It does not touch strikes or content. Tested from this module by `tests/test_moderation.py` and `tests/test_audit_edge_cases.py`.

### `user_websites:COMM_report_action_dismiss` [@ANCHOR: user_websites:COMM_report_action_dismiss]

`content.violation.report.action_dismiss()`, a `@public_rpc` method in `content_moderation`. Resolved reports (`action_taken` or `dismissed`) raise `UserError`; otherwise the state becomes `dismissed`, with no strike and no change to the content. Exercised by `tests/test_moderation.py` and by the simulation, where the administrator dismisses reports.

### `user_websites:COMM_action_suspend_group_websites` [@ANCHOR: user_websites:COMM_action_suspend_group_websites]

`user.websites.group.action_suspend_group_websites()`, a `@public_rpc` method. It sets `is_suspended_from_websites` on each group and evicts the page caches. Unpublishing pages and posts is done after commit on a background thread; in test mode it runs inline in batches of 5000 as the service account. It posts a note as the service account. It does not check strikes: the caller decides, normally at three strikes. Test: `test_01_group_suspension`, which polls until the content is unpublished.

### `user_websites:COMM_action_pardon_group_websites` [@ANCHOR: user_websites:COMM_action_pardon_group_websites]

`user.websites.group.action_pardon_group_websites()`, a `@public_rpc` method. For each group it resets `violation_strike_count` to 0, clears `is_suspended_from_websites`, evicts page caches and posts a note. It does not republish content, which stays unpublished until restored by hand. Called by `content.violation.appeal.action_approve()`. Test: `test_02_group_pardoning`.

### `user_websites:COMM_action_suspend_user_websites` [@ANCHOR: user_websites:COMM_action_suspend_user_websites]

`res.users.action_suspend_user_websites()`, an `@api.private` method. After commit, `_async_unpublish_content` runs on a background executor for the user ids. Each user is flagged `is_suspended_from_websites`, page caches are evicted and a note is posted on the partner as the mail service account. Called by the strike rule at three strikes. The unpublish is asynchronous, so content disappears slightly after the flag. Test: `test_01_three_strikes_suspension`.

### `user_websites:COMM_action_pardon_user_websites` [@ANCHOR: user_websites:COMM_action_pardon_user_websites]

`res.users.action_pardon_user_websites()`, a `@public_rpc` method. For each user it sets `violation_strike_count` to 0, clears `is_suspended_from_websites`, evicts page caches and posts a note. It does not republish pages or posts. Called by `content.violation.appeal.action_approve()`. It posts its note on the user's partner as the mail service account. Test: `test_02_pardon_functionality`, which sets three strikes, suspends, pardons and expects zero strikes and no suspension.

### `user_websites:COMM_compute_suspended_group_ids` [@ANCHOR: user_websites:COMM_compute_suspended_group_ids]

`res.users._compute_suspended_group_ids()` fills the non-stored `suspended_group_ids`: groups the user belongs to that are suspended. It searches suspended groups whose members include any user in the recordset, in pages of 1000 by id, then maps members to group ids, so cost is a few queries however many users are computed. A user with none gets an empty set. Test: `test_03_compute_suspended_group_ids`, in both directions.

### `user_websites:COMM_gdpr_token_compute_name` [@ANCHOR: user_websites:COMM_gdpr_token_compute_name]

`ham.gdpr.export.token._compute_name()`, a stored compute depending on `user_id` and its login and name. It sets `name` to "GDPR Export Token for <user name, else login, else id>" so tokens are recognizable in the backend. The token string itself is not part of the name. Tested in `tests/test_privacy_gdpr.py`.

### `user_websites:COMM_create_for_current_user` [@ANCHOR: user_websites:COMM_create_for_current_user]

`ham.gdpr.export.token.create_for_current_user()`, `@api.private @api.model`, no arguments. It returns a new `secrets.token_urlsafe(32)` string and stores it for the calling user (`env.uid`), creating the row as the `user_gdpr_export_service` service account because ordinary users have no create access. It is the only path that makes a token, so `user_id` is always an authenticated identity. The token expires after 5 minutes and is single use (see `_consume`). Test: `test_privacy_gdpr.py`.

### `user_websites:COMM_get_gdpr_streamed_keys` [@ANCHOR: user_websites:COMM_get_gdpr_streamed_keys]

`res.users._get_gdpr_streamed_keys()` (single record) returns a dict of key name to a zero-argument generator function: `pages` (name, url, arch), `blog_posts`, `submitted_reports` and `appeals`, added to what the base class supplies. In production each generator pages through records 1000 at a time under a fresh cursor and the service account, so a large export streams instead of being held in memory. In test mode they iterate lists loaded up to 10000 rows. Test: `test_01e_consume_and_export_materializes_data_and_streamed_keys`.

### `user_websites:COMM_public_directory_view_init` [@ANCHOR: user_websites:COMM_public_directory_view_init]

`UserWebsitesPublicDirectoryView.init()` (`models/sql_views.py`), model `user_websites.public_directory_view`, `_auto = False`. It drops and recreates the plain SQL view `user_websites_public_directory_view` at every module load. Columns: `id` and `user_id` (the user), `name` (partner name), `website_slug`, `write_date`, and `view_count`, the sum of the user's page and post view counts. A row exists only for active users with a non-empty slug who opted in (`privacy_show_in_directory`), and who are not service accounts, not suspended and not `website_ineligible`. `/community` reads it as the service account. Test: `tests/test_lifecycle_and_groups.py`.

### `user_websites:COMM_content_routing_view_init` [@ANCHOR: user_websites:COMM_content_routing_view_init]

`UserWebsitesContentRoutingView.init()` recreates the plain view `user_websites_content_routing_view`, which answers "which record owns this slug". Columns: `id`, `res_model` (`res.users` or `user.websites.group`), `res_id` and `website_slug`. Users appear when active, with a slug, not a service account, not suspended and not ineligible; groups when they have a slug and are not suspended. Group ids are offset by 1,000,000 so the union has unique ids. `_get_user_id_by_slug` queries it by raw SQL. Tests: `test_slug_resolution_cache_and_filters.py`, `test_audit_edge_cases.py`.

### `user_websites:COMM_weekly_digest_view_init` [@ANCHOR: user_websites:COMM_weekly_digest_view_init]

`UserWebsitesWeeklyDigestView.init()` recreates the view `user_websites_weekly_digest_view` that feeds `send_weekly_digest`. One row per follower and author (a user's partner, or a group) that has posts published in the last 7 days (measured in UTC). Columns: `id` (stable `row_number()`, groups offset by 5,000,000), `partner_id` (the subscriber), `author_name`, `owner_model` (`res.partner` or `user.websites.group`), `owner_record_id`, `post_ids_string` (comma-separated post ids) and `first_post_id`. Tests: `test_subscriptions.py` covers the seven-day cutoff in real UTC and stable ids across repeated queries.

### `user_websites:COMM_db_functions_init` [@ANCHOR: user_websites:COMM_db_functions_init]

`UserWebsitesDbFunctions.init()`, on the abstract model `user_websites.db_functions`, creates or replaces the PostgreSQL function `increment_strike_count(tbl_name text, rec_id integer)` inside a savepoint. For `res_users` or `user_websites_group` it locks the row `FOR NO KEY UPDATE` and adds 1 to `violation_strike_count`; any other table name does nothing. The call is atomic even under concurrent reports. It returns void and is called through `_increment_strike_count`. A broken `init()` would fail module installation. Test: `test_01_three_strikes_suspension`.

### `user_websites:COMM_async_unpublish_content` [@ANCHOR: user_websites:COMM_async_unpublish_content]

`_async_unpublish_content(db_name, user_ids)`, a module-level function in `models/res_users.py` run on `BACKGROUND_EXECUTOR` after the suspending transaction commits (or when a user is archived). It opens its own registry cursor as `sys_provisioner` (raising `ValueError` if missing) and, as the module service account, unpublishes in batches of 5000: pages (`website_published` false), posts (`is_published` false) and archives blogs, committing after each batch with a 0.1 s pause. Access and validation errors are rolled back and logged as a warning, other failures as an error. The cursor is always closed. Test: `test_01_three_strikes_suspension` polls for the result.

### `user_websites:COMM_register_hook` [@ANCHOR: user_websites:COMM_register_hook]

`res.users._register_hook()` runs when the registry is built. Inside a savepoint it finds the `sys_provisioner` user (including inactive) or creates it as an active service account in the main company, then creates the `user_websites.user_websites_service_account` XML id (`noupdate`) pointing at it if missing. This lets other modules resolve the account early. It has no arguments and no return value; the base `_register_hook` runs first. Test: `test_01_three_strikes_suspension`, which depends on the account.

### `user_websites:COMM_res_users_check_reserved_slugs` [@ANCHOR: user_websites:COMM_res_users_check_reserved_slugs]

`res.users._check_reserved_slugs()`, an `@api.constrains("website_slug")` constraint. If a user's slug (lower-cased) is in `reserved_slug_words(env)`, the union of the fixed reserved set, first segments of registered routes and installed language prefixes, it raises `ValidationError` ("The slug '%s' is reserved and cannot be used."). Empty slugs pass. It stops a member from taking an address such as `community` that would shadow a real route. Tests: `test_09_reserved_slug_validation`, `test_member_site_home_url.py`.

### `user_websites:COMM_group_check_reserved_slugs` [@ANCHOR: user_websites:COMM_group_check_reserved_slugs]

`user.websites.group._check_reserved_slugs()`, the group copy of the reserved-slug constraint on `website_slug`: a slug found in `reserved_slug_words(env)` raises `ValidationError`, empty slugs pass. Tested by `test_09_reserved_slug_validation`, which creates a group with a reserved word and expects the error.

### `user_websites:COMM_is_admin` [@ANCHOR: user_websites:COMM_is_admin]

`res.users._is_admin()` extends the base method: true if the base check passes, or the user is in `user_websites.group_user_websites_administrator` or `base.group_system`. No arguments; returns a bool; it only reads group membership. Other code uses it to decide who may bypass ownership checks. Test: `test_res_users_is_admin` in `tests/test_tdd_fixes.py`, which creates an ERP manager and expects true.

### `user_websites:COMM_res_users_create` [@ANCHOR: user_websites:COMM_res_users_create]

`res.users.create(vals_list)`, `@api.model_create_multi`. An empty `website_slug` is stored as false. Each `vals` goes through `_apply_slug_policy(None, vals)`, which, when the deployment's slug policy is "callsign", derives the slug from the member's callsign and returns True when no slug may be derived yet from the name. In that case the create runs with `edge_routing_no_name_slug` so no slug is made from the name. The slug is validated and made unique by the routing mixin. Test: `test_01_user_specific_limit_enforcement` and the slug tests.

### `user_websites:COMM_res_users_write` [@ANCHOR: user_websites:COMM_res_users_write]

`res.users.write(vals)`. With the callsign policy and a relevant key it applies the slug policy (one record at a time for several). The base write runs in a savepoint; an `IntegrityError` naming a `website_slug` constraint becomes `ValidationError` ("The Website Slug must be unique and valid."), other errors are re-raised. After success it evicts slug and page caches for the keys named in the code, queues the unpublish of content when `active` becomes false, and, when the slug changed, records 301 redirects from the old address (and the old blog address if the user has posts) and rehomes site pages. Tests: `test_page_limits.py`, `test_cache_coherence.py`, `test_slug_resolution_cache_and_filters.py`.

### `user_websites:COMM_group_write` [@ANCHOR: user_websites:COMM_group_write]

`user.websites.group.write(vals)`: the group version of `res.users.write` slug handling. The base write runs in a savepoint and a violated `website_slug` constraint becomes `ValidationError` ("The Group Website Slug must be unique and valid."). Afterwards it evicts the page cache when `is_suspended_from_websites` was written; when the slug changed it rehomes the group's pages and creates 301 redirects from `/old` to `/new` and, if the group has posts, `/old/blog` to `/new/blog`, as the service account. Tested by `test_audit_edge_cases.py`.

### `user_websites:COMM_get_page_limit` [@ANCHOR: user_websites:COMM_get_page_limit]

`res.users._get_page_limit()` (single record) returns an int: the user's own `website_page_limit` when above 0, otherwise the system parameter `user_websites.global_website_page_limit` (default 100). It is read by the page quota check in `website.page.create()` through the service account. Test: `test_01_user_specific_limit_enforcement` in `tests/test_page_limits.py`.

### `user_websites:COMM_get_blog_limit` [@ANCHOR: user_websites:COMM_get_blog_limit]

`res.users._get_blog_limit()` (single record) returns the maximum number of blogs as an int, from the system parameter `user_websites.global_blog_limit`, default 5. There is no per-user override. Used by `blog.blog` quota checks. Test: `tests/test_blog_limits.py`, which sets a lower limit and expects the next blog to be refused.

### `user_websites:COMM_get_blog_post_limit` [@ANCHOR: user_websites:COMM_get_blog_post_limit]

`res.users._get_blog_post_limit()` (single record) returns the maximum number of posts as an int from `user_websites.global_blog_post_limit`, default 500, with no per-user override. Used by `blog.post` quota checks. Test: `test_02_blog_post_creation_is_blocked_past_the_configured_limit` creates posts up to the limit and expects `ValidationError` on the next.

### `user_websites:COMM_page_invalidate_cloudflare_cache` [@ANCHOR: user_websites:COMM_page_invalidate_cloudflare_cache]

`website.page._invalidate_cloudflare_cache()` queues a Cloudflare edge purge for the pages in the recordset. It groups cache tags `site-<slug>` by website for pages that have an owner or group slug, and enqueues them through `cloudflare.purge.queue.enqueue_tags` as the `cloudflare.user_cloudflare_purge` service account, in a savepoint. A failure is logged and never fails the caller's create, write or unlink. Pages without a slug are skipped. Test: `test_01_user_specific_limit_enforcement`; the failure path is `test_cloudflare_purge_failsafe`.

### `user_websites:COMM_trigger_malicious_arch_violation` [@ANCHOR: user_websites:COMM_trigger_malicious_arch_violation]

`website.page._trigger_malicious_arch_violation(vals, records=None)` runs when the arch sanitizer stripped something. As the service account it files, per target, a `content.violation.report` (or reuses the one for that URL and reporter) and calls `action_take_action_and_strike()`, so the owner or group gets a strike. Targets: ownership in `vals` names one target; otherwise one per distinct owner or group among `records`, keyed on the first affected URL, so a batch costs each owner one strike. The reporter is the current user. Tests: `test_orm_security.py`, `test_violation_batch_write.py`.

### `user_websites:COMM_get_page_id_by_url` [@ANCHOR: user_websites:COMM_get_page_id_by_url]

`website.page._get_page_id_by_url(url, website_id, override_svc_uid=None)`, `@api.model @distributed_cache()`. Returns the id of the published page at `url` for that website (or for all websites), or false. It searches as `override_svc_uid` or the module service account, and returns false for an empty url, no page, or a page whose owner user or group is suspended. Because the suspension is checked when the cache entry is filled, suspension changes evict the cache. Nothing else in this repository calls it. Tests: `tests/test_page_limits.py`.

### `user_websites:COMM_website_page_unlink` [@ANCHOR: user_websites:COMM_website_page_unlink]

`website.page.unlink()`. It checks `check_access("unlink")`, enqueues the Cloudflare purge, deletes as the module service account with tracking off (falling back to the caller when the account is missing), re-raises serialization and deadlock errors (`40001`, `40P01`) so the transaction can retry, and finally sends one cache invalidation for the removed page URLs. Test: `test_07_page_write_and_unlink_signal_cache_invalidation_once` proves one signal per call, not three.
