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

* **Tenant Isolation:** Enforced via strict record rules verified by ``test_tenant_view_isolation`` and ACL overhead elimination to prevent log spam ``test_acl_overhead_loop_elimination``.
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
* **`content.violation.report`** (model itself now in `content_moderation`, this module's own extension in `models/content_violation_report_moderation.py`): Stores abuse reports. Originator is masked from the target owner. The system automatically generates a report and issues a strike if a user attempts to inject malicious SSTI/XSS payloads into their site architecture ``content_moderation:action_take_action_and_strike`` (via this module's own enforcement-hook override ``user_websites:COMM_apply_enforcement_action``), tested by ``test_moderation_suspension``. Admin spam is prevented via a daily digest cron (`ir_cron_notify_pending_reports` ``ir_cron_notify_pending_reports``, ``cron_notify_pending_reports``, verified by ``test_cron_pending_reports``) and a UI toast that is sticky but remembered in localStorage for 12 h per (count, newest id), with Review/Dismiss buttons, (``toast_notifications_logic``, ``admin_toast_logic``, tested by ``test_tour_toast_notifications``).

* **Security Auto-Moderation**: The `website.page` model includes `_sanitize_user_arch` ``website_page_sanitize_arch``, verified by ``test_website_page_sanitize_arch``, which forcefully strips dangerous QWeb directives (`t-*`) and JS event handlers.
* **Member CSS**: `<link>` is removed; `<style>` is rebuilt rule by rule and every `style="..."` attribute declaration by declaration with zero_sudo's `css_sanitizer` ``user_websites:page_arch_style_attribute_filter``, verified by ``test_user_arch_style_attribute_filter``: no `url()` except `#fragment`, no `image-set()`, `data:`, `expression`, escapes, and `position` only static, relative or absolute. A refused declaration is dropped (no strike); malformed CSS drops the whole attribute. Blog post content goes through `html_sanitize` instead and is not covered by this filter.
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

* **Violation Reporting:** Form endpoint `/website/report_violation` ``user_websites:UX_REPORT_VIOLATION``, ``violation_report_logic``, verified by ``test_tour_violation_report``. Protected by the `form_shield` module (proof of work or question, honeypots, minimum time-to-submit, per-network, per-page and per-site limits ``user_websites:report_limits``, ``user_websites:report_violation_shield``, the form's fields ``user_websites:report_violation_shield_fields``); the reported URL must be a page of this site (this host, the base URL or a website domain, http and https alike, www ignored ``user_websites:report_own_site_hosts``) ``user_websites:normalize_report_target`` and a guest's report is refused when it is short, one random-looking string, link-heavy or lacks a valid address ``user_websites:report_problem``. `GET /website/report_violation` is the question variant for browsers without scripts ``user_websites:report_violation_page``. Helpers: the redirect path is forced to a single leading slash ``user_websites:reported_path``, hosts compare without `www.` ``user_websites:bare_host``, a guest's address may not start or end its local part with a dot ``user_websites:local_part_ok``, the question page is rendered by ``user_websites:render_report_page`` and a refusal is answered by ``user_websites:report_refusal``; the old per-address redirect-path anchor ``user_websites:report_violation_redirect_target`` stays.

* **Home Routing:** Target view `/<slug>` (the old `/<slug>/home` answers a permanent 301 to it, `user_websites:COMM_user_home_legacy_redirect`) `controller_user_websites_home`.

* **Site Creation:** `/<slug>/create_site` `UX_CREATE_SITE`, concurrency scaling proven by `test_site_creation_performance_scaling`.

* **Blog Routing:** `/<slug>/blog` `controller_user_blog_index`.

* **Blog Creation:** `/<slug>/create_blog` `UX_CREATE_BLOG_POST`.

* **Post Creation & Editing:** `POST /<slug>/create_blog_post` creates a blank "New Post" in the owner's blog (or redirects to `/<slug>/blog` if no blog exists yet) and redirects to `/blog_post/edit/<post_id>`, this module's own ownership-scoped title/body/publish form (the website-builder Edit toolbar needs the sitewide `website.group_website_designer` permission, which site owners do not hold).

* **Documentation:** Proxies knowledge records `/user-websites/documentation` `controller_user_websites_documentation`.

* **Appeals:** User submission endpoint `/website/submit_appeal` `UX_SUBMIT_APPEAL`.

* **Subscriptions:** `/<slug>/subscribe` `UX_SUBSCRIBE`. Unsubscribe verification with HMAC-SHA256 token validation over the model, record, partner and timestamp `controller_unsubscribe_digest`. The timestamp is signed but its age is not checked, so a valid link does not expire.

* **`GET /api/v1/user_websites/pending_reports`**: Returns a JSON object `{'count': int, 'latest_id': int, 'url': str}` of unhandled violation reports (`url` is the back-office pending queue, `/odoo/action-user_websites.action_content_violation_report_pending`). Restricted to administrators ``api_pending_reports``; any other caller gets `{'count': 0, 'error': 'Forbidden'}` with HTTP 200, not an error status. Verified by ``test_admin_violation_toast_rpc``.

* **GDPR Actions:** Privacy dashboard `/my/privacy` ``controller_my_privacy_dashboard``. Data exports `/my/privacy/export` ``UX_GDPR_EXPORT``. Data erasure via batched unlinks (5000 records per batch, run within the request under the `zero_sudo.gdpr_service_internal` service account) `/my/privacy/delete_content` ``UX_GDPR_ERASURE``, then redirects to the public confirmation `/privacy/erased` ``user_websites:COMM_privacy_erased``.

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

* **GDPR Hooks**: The module extends `_get_gdpr_export_data()` ``res_users_gdpr_export``, tested by ``test_gdpr_export_hook``, and `_execute_gdpr_erasure()` ``gdpr_sudo_erasure``, tested by ``test_gdpr_erasure_pages`` and ``test_gdpr_erasure_posts``. Dependent modules storing PII MUST override these to append their data to the export payload and hard-delete it during erasure.

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

* **Owner toolbar** ``user_websites:UX_OWNER_TOOLBAR`` (`views/owner_site_templates.xml`, called from `user_navbar`): shown above the site's own navbar to the site's owner, or a member of the owning group, on every page of that site (an internal user sees it on their own site only). `?visitor=1` hides it.
* **Dashboard** `/my/site/<slug>` ``user_websites:COMM_owner_site_dashboard``, `/my/site` redirects to the caller's own. Page editor `/my/site/<slug>/page/new|<id>` ``user_websites:UX_OWNER_PAGE_EDITOR``, save ``user_websites:COMM_owner_page_save``, publish toggles ``user_websites:COMM_owner_page_publish`` and ``user_websites:COMM_owner_post_publish``, image upload ``user_websites:COMM_owner_image_upload`` (`controllers/owner_site.py`). Every route resolves the site from the database and answers 404 unless the caller owns it, or belongs to the owning group (``user_websites:owner_resolve_site``); an administrator who is not the owner gets the 404 too. The owner of a suspended or not-yet-eligible site gets a 403 page that says why instead (hidden from everyone else); a retired slug redirects to the live one.
* **Editor** ``user_websites:UX_RICH_EDITOR``: a contenteditable WYSIWYG (`static/src/js/rich_editor.js`), no raw HTML and no external library. Odoo's own `html_editor` is an OWL backend component tied to the website builder's `website.group_website_designer` permission and its asset bundles; mounting it for portal members would hand them the sitewide editor, which this module deliberately never grants.
* **Sanitizer** ``user_websites:member_html_sanitize`` (`models/member_html.py`): strict allow-list in the request path for every caller, administrators included; hostile input is also filed through `website.page._trigger_malicious_arch_violation()`.
* **Limits**: title 120 characters, body 200,000 characters, images JPEG/PNG/GIF/WebP at most 2 MB and 100 per site, 30 writes and 10 uploads per user per minute (Redis counters, ``user_websites:owner_rate_limit``; Redis down answers 503).
* **Retention**: no route erases. Unpublish keeps the record as a draft. Delete (`POST .../<page|post>/<id>/delete`, with a confirmation step) takes a page or post off the site and every public surface but archives it (`active` false, `deleted_by_owner` stamped; a page is moved to a private address so its own can be reused), and the dashboard's Deleted list can restore it, unpublished. The home page cannot be deleted. Uploaded images are public `ir.attachment` rows owned by the account (`res_model='res.users'`/`user.websites.group`, description `user_websites site image`); a member's account-erasure flow removes the images owned by that member's own account (not a group's images).
* **Layout** ``user_websites:member_site_standalone_layout``: on a member site `layout_inherit_user_navbar` sets `no_header`/`no_footer`, so the main site's header and footer are not rendered, and the site's own header (`user_navbar`) and footer (`member_site_footer`) are. Assets: `static/src/css/member_site.css` and `rich_editor.js` in `web.assets_frontend`.
* **Home address** `/<slug>` ``user_websites:COMM_user_home_fallback``; `/<slug>/home` answers 301 `[@ANCHOR: user_websites:COMM_user_home_legacy_redirect]`.
* **Owner-site internals**: `/my/site` ``user_websites:COMM_owner_site_entry``; the new-page and edit-page forms ``user_websites:COMM_owner_page_new``, ``user_websites:COMM_owner_page_edit``; a page's arch is the fixed member-page frame around sanitized body markup ``user_websites:owner_page_arch`` and the editor reads the body back out of it ``user_websites:owner_page_arch_body``; uploads are accepted by magic number, not by file name or declared type ``user_websites:owner_image_mimetype``.
* **Editor functions** (`static/src/js/rich_editor.js`, exercised by ``test_tour_owner_site``): ``user_websites:rich_editor_apply_link``, ``user_websites:rich_editor_clean_node``, ``user_websites:rich_editor_close_link_bar``, ``user_websites:rich_editor_image_picked``, ``user_websites:rich_editor_link_bar_click``, ``user_websites:rich_editor_link_keydown``, ``user_websites:rich_editor_open_link_bar``, ``user_websites:rich_editor_paste``, ``user_websites:rich_editor_remember_range``, ``user_websites:rich_editor_restore_range``, ``user_websites:rich_editor_say``, ``user_websites:rich_editor_start``, ``user_websites:rich_editor_sync_field``, ``user_websites:rich_editor_toolbar_click``, ``user_websites:rich_editor_toolbar_state``, ``user_websites:rich_editor_wrap_code``.
* **Helper functions** (owner interface and sanitizer, each exercised by `tests/test_owner_site.py`): ``user_websites:fn_main_serve_core_fallback``, ``user_websites:fn_owner_escape``, ``user_websites:fn_owner_escape_attr``, ``user_websites:fn_owner_svc``, ``user_websites:fn_owner_owner_domain``, ``user_websites:fn_owner_qcontext``, ``user_websites:fn_owner_report_if_hostile``, ``user_websites:fn_owner_clean_body``, ``user_websites:fn_owner_own_page``, ``user_websites:fn_owner_back_to_editor``, ``user_websites:fn_owner_attachment_owner``, ``user_websites:fn_hmtl_clean_url``, ``user_websites:fn_hmtl_is_hostile``, ``user_websites:fn_hmtl_sanitize_member_html``, ``user_websites:fn_hmtl_clean_children``, ``user_websites:fn_hmtl_clean_attributes``, ``user_websites:fn_hmtl_drop``, ``user_websites:fn_hmtl_unwrap``, ``user_websites:fn_hmtl_menu_pages``, ``user_websites:fn_users_user_websites_menu_pages``, ``user_websites:fn_group_user_websites_menu_pages``.
* **SVG schematics** survive the sanitizer through zero_sudo's SVG allow-list ``user_websites:member_html_svg_allowlist``; every other active or unknown element is removed.

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

### `utils_slugify` [@ANCHOR: edge_routing:utils_slugify]

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

### `action_take_action_and_strike` [@ANCHOR: content_moderation:action_take_action_and_strike]

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

## 9. Anchor reference: isolation, moderation, sanitizer, reports and privacy

One entry per anchor. The sections above name each of these ids in plain backticks; the tag lives on the heading here. Code is read from `user_websites` in `hams_open` unless another module is named.

### `test_tenant_view_isolation` [@ANCHOR: test_tenant_view_isolation]

Test `test_03_tenant_view_isolation` in `tests/test_performance_regressions.py`. It gives two members each a home `website.page` created through the module service account, then has member A write an edited `arch` (a marker `<div>` added to the page's `oe_structure`) with `with_user(user_a)`. A guest then opens both `/<slug>` addresses: the marker must be in A's response and must not appear in B's. It proves that record rules and the proxy-ownership write path keep one member's page edit out of another member's site. It also covers `user_websites:website_page_write`. It needs a database and HTTP, so it runs only inside the Odoo test run.

### `test_acl_overhead_loop_elimination` [@ANCHOR: test_acl_overhead_loop_elimination]

Test `test_02_acl_overhead_loop_elimination` in `tests/test_performance_regressions.py`. It creates 15 members with published home pages, authenticates as the public guest, captures the `odoo.addons.base.models.ir_model` logger at WARNING while the guest opens `/` and `/community`, and fails if any "Access Denied by ACLs" message mentions `ir.ui.view` or `website`. The guarantee is that rendering the routing map and QWeb does not make Odoo probe each member page in a linear loop of denied reads (log spam and wasted queries). It also covers `user_websites:website_page_check_access`.

### `content_moderation:action_take_action_and_strike` [@ANCHOR: content_moderation:action_take_action_and_strike]

`content.violation.report.action_take_action_and_strike(self)` in `content_moderation/models/content_violation_report.py` (published with `@public_rpc`, so moderators can call it from the back office). It loops over the records: one already in `action_taken` or `dismissed` is skipped, so saving the same page twice cannot strike the owner twice; any other record gets `state = "action_taken"` and then `_apply_enforcement_action()`, the hook this module overrides. **Returns:** nothing. **Errors:** whatever the hook raises, including `AccessError`. **Cost:** one write per report and the hook's own work. Test: `test_moderation_suspension`. See also `user_websites:COMM_apply_enforcement_action`.

### `user_websites:COMM_apply_enforcement_action` [@ANCHOR: user_websites:COMM_apply_enforcement_action]

`content.violation.report._apply_enforcement_action(self)` in `models/content_violation_report_moderation.py`, this module's override of the `content_moderation` hook. It runs once per upheld report. With a `content_owner_id` it calls the stored procedure through `_increment_strike_count("res_users", id)` (an atomic lock and increment), notifies cache invalidation, reloads `violation_strike_count`, and at 3 or more strikes calls `action_suspend_user_websites()` unless the owner is already suspended. With a `content_group_id` it does the same for `user.websites.group` and `action_suspend_group_websites()`. In both cases it posts a note as the mail service account. With neither it falls back to the base hook (a chatter note, no consequence). **Returns:** nothing. Test: `test_moderation_suspension`.

### `test_moderation_suspension` [@ANCHOR: test_moderation_suspension]

Test `test_01_three_strikes_suspension` in `tests/test_moderation.py`. It starts with a user holding zero strikes, a published page and a published blog post, creates three `content.violation.report` rows naming that user as `content_owner_id`, and calls `action_take_action_and_strike()` on each. It asserts that the strike count is 3, that `is_suspended_from_websites` is set, and (after polling up to ten seconds, because the unpublish runs in a background thread) that both the page and the post are unpublished. It also carries the test links for the strike counter, the asynchronous unpublish, the registry hook and the SQL function setup.

### `ir_cron_notify_pending_reports` [@ANCHOR: ir_cron_notify_pending_reports]

The `ir.cron` record in `data/user_websites_data.xml` named "User Websites: Notify Admins of Pending Reports". It runs `model._cron_notify_pending_reports()` on `content.violation.report` once a day (`interval_number` 1, `days`), active by default, as the user `user_websites.user_websites_service_account`. Its purpose is to replace a flood of one email per report with a daily digest to the abuse mailbox. It writes no data of its own; the work is in `cron_notify_pending_reports`. Tests: `test_cron_pending_reports`.

### `cron_notify_pending_reports` [@ANCHOR: cron_notify_pending_reports]

`content.violation.report._cron_notify_pending_reports(self)` (`@api.model`, `models/content_violation_report_moderation.py`; the code carries the id as `user_websites:cron_notify_pending_reports`). No arguments, no return value. For each company the service account is granted (not every company in the database, so an out-of-scope company can never raise `AccessError` and abort the run) it counts reports in state `new` with `with_company(company)`. If the count is above zero it sends the mail template `user_websites.email_template_pending_violations_summary` as the mail service account to the company's `user_websites.company_abuse_email` parameter, then the company email, then `admin@example.com`, with `force_send=False` (queued, not sent inline). **Locking:** none. Test: `test_09_cron_pending_reports_ignores_out_of_scope_company`.

### `test_cron_pending_reports` [@ANCHOR: test_cron_pending_reports]

Test anchor for the daily digest: `test_08_cron_pending_reports` in `tests/test_audit_edge_cases.py` (its code id is `COMM_test_cron_pending_reports`), plus `test_09_...` for the company scope; the cron record and the mail send cite it as the audited test. The tests create a `new` report and call `_cron_notify_pending_reports()` directly. One case adds a second, unrelated company the service account is not scoped to and asserts that the cron does not raise and that the main company's notification is still queued. The cron is never waited on; the tests call the method rather than the scheduler.

### `toast_notifications_logic` [@ANCHOR: toast_notifications_logic]

`UrlToastNotification._checkUrlForNotifications()` in `static/src/js/toast_notifications.js`, started from `start()` on `#wrapwrap` (a public interaction). It reads the page URL's query string and maps one parameter to a toast: `report_error=<reason>` (danger, text from `REPORT_ERRORS`, `default` when the reason is unknown), `report_submitted`, `appeal_submitted`, `subscribed` or `erased` (success). When a message was chosen it calls the Odoo notification service (not sticky) and then strips the query with `history.replaceState`, so a refresh does not show it again. No server call and no storage. Test: `test_tour_toast_notifications`.

### `admin_toast_logic` [@ANCHOR: admin_toast_logic]

`AdminViolationToast._checkPendingReports()` in `static/src/js/toast_notifications.js`, called once from `start()`. It `fetch`es `/api/v1/user_websites/pending_reports`; a network error or a non-OK answer is swallowed silently. It shows a sticky "Pending Moderation" warning toast only when `shouldShowToast(readSeen(), data, now)` is true, with a "Review reports" button (only if the returned `url` starts with `/`) and a "Dismiss" button, and then records what was shown through `writeSeen`. The "seen" record lives in `localStorage` under `admin_violation_toast_seen` and suppresses the toast for 12 hours unless the count or the newest report id grew. Non-administrators get `count: 0` from the API, so nothing shows. Tests: `test_tour_toast_notifications`, `test_admin_violation_toast_rpc`.

### `test_tour_toast_notifications` [@ANCHOR: test_tour_toast_notifications]

The tour `toast_notifications_tour` in `static/tests/tours/toast_notifications_tour.js`. It opens `/?report_submitted=1`, dismisses the cookie bar and waits for `.o_notification_manager .o_notification` to appear in the DOM, which shows that the URL-parameter toast fires on a public page. It covers `toast_notifications_logic`; the administrator toast is exercised by the JSON test `test_admin_violation_toast_rpc` instead. It runs in a headless browser, started from a Python tour test.

### `website_page_sanitize_arch` [@ANCHOR: website_page_sanitize_arch]

`website.page._sanitize_user_arch(self, arch_content)` (`@api.model`, `models/website_page.py`). It cleans the QWeb `arch` of a member-owned page.

| Argument | Type | Meaning |
| --- | --- | --- |
| `arch_content` | str | Markup of the page body. An empty string comes back unchanged with `False`. |

**Returns:** the tuple `(cleaned_markup, was_modified)`. `was_modified` is true when something hostile was removed, and the caller (`_trigger_malicious_arch_violation`) then files a strike report; removal of `<link>`, `<style>` rules and `style` declarations is logged but does not set it. **Steps, in order:** SVG blocks rebuilt from the zero_sudo allow-list; names with an undeclared namespace prefix removed; `script`, `iframe`, `object`, `embed` and `base` removed (case-insensitive); `<link>` removed and `<style>` text re-filtered; `style="..."` attributes filtered; `<meta http-equiv="refresh">` neutralised; attributes with `javascript:`, `data:` or `vbscript:` URLs (control characters stripped first) renamed to `data-blocked-*`; `on*` and `t-*` attributes renamed the same way except `t-name` and a `t-call` naming one of four allowed templates; every `<` in text turned into `&lt;`. **Errors:** a `KeyError` or `ValueError` returns `<div>Sanitization Error</div>` with true. Pure function, no database access. Tests: `test_website_page_sanitize_arch`, `test_user_arch_svg_allowlist`.

### `test_website_page_sanitize_arch` [@ANCHOR: test_website_page_sanitize_arch]

Test `test_07_qweb_arch_sanitization` in `tests/test_orm_security.py`. A non-administrator member creates a `website.page` whose arch has a `<script>`, an `<iframe>`, and a `<div>` carrying `t-eval` and `onmouseover`. The test asserts that neither tag survives in the stored arch, that the attributes were renamed to `data-blocked-t-eval` and `data-blocked-onmouseover`, and that an automatic `content.violation.report` exists against the member (the effect of `_trigger_malicious_arch_violation`); a second write with a script tag is stripped too. It covers `website_page_sanitize_arch`; `test_website_page_sanitize_arch_bare_env_bypass` covers the same function with a bare environment.

### `user_websites:page_arch_style_attribute_filter` [@ANCHOR: user_websites:page_arch_style_attribute_filter]

The `style="..."` pass inside `_sanitize_user_arch` (`models/website_page.py`). For every element, any attribute whose name is `style` in any letter case is passed to zero_sudo's `sanitize_style_attribute`, which returns `(cleaned, dropped)`: declarations are rewritten one by one, a `url()` is allowed only as a `#fragment`, `image-set()`, `data:`, `expression` and escapes are refused, and `position` may only be `static`, `relative` or `absolute`. Malformed CSS drops the whole attribute, and an attribute left empty is removed. A removal is logged at info level only and is not a strike. Elements inside `<svg>` were already rebuilt by the SVG allow-list. Tests: `test_user_arch_style_attribute_filter`.

### `test_user_arch_style_attribute_filter` [@ANCHOR: test_user_arch_style_attribute_filter]

Class `TestUserArchStyleAttribute` in `tests/test_user_arch_style_link.py` (an `HttpCase`, tagged `post_install`). Five tests feed arch strings to `_sanitize_user_arch`: every entry of the hostile list is removed without a strike (`modified` false); a harmless `color:red;margin:0 auto` survives while the hostile part of a second declaration list goes, including an upper-case `STYLE`; a malformed or empty attribute is removed whole; nesting and every element type are covered; and an SVG's own style still goes through the SVG filter.

### `user_websites:UX_REPORT_VIOLATION` [@ANCHOR: user_websites:UX_REPORT_VIOLATION]

`UserWebsitesController.report_violation(self, url="", reason="", description="", email="", website_honeypot="", **post)`, route `POST /website/report_violation`, `auth="public"`, `csrf=True`, `website=True`.

| Argument | Type | Meaning |
| --- | --- | --- |
| `url` | str | The reported page; the `Referer` header is used if it is empty. |
| `description` | str | The complaint, cut to 5000 characters. |
| `email` | str | A guest's address (a member's account is recorded instead). |
| `website_honeypot` | str | Hidden field; any value means a bot. |
| `post` | dict | The form_shield fields (proof of work or answer, second honeypot, issue time). |

**Flow:** a filled honeypot is counted and answered with the success redirect. Missing `url` or `description` redirects with `report_error=missing_fields`. Then the shield check (`report_violation_shield`), then the structural check (`report_problem`). The report row is created as the module service account with `target_url`, `description`, state `new`, the email, `reported_by_user_id` for a signed-in member (taken from the session, never the form), and `content_owner_id` or `content_group_id` found from the first path segment. **Returns:** always a redirect to the reported path plus `report_submitted=1` or `report_error=<reason>`; a refused shield may return a rendered page (see `report_refusal`). Tests: `test_tour_violation_report`, `test_report_violation_honeypot_rejected`, `test_report_violation_records_logged_in_reporter`.

### `violation_report_logic` [@ANCHOR: violation_report_logic]

`ViolationReportModal._onModalShow(ev)` in `static/src/js/violation_report.js`, bound to the Bootstrap `show.bs.modal` event of `#reportViolationModal` by `start()`. The event's `relatedTarget` is the button that opened the modal: its `data-url` is copied into the form's `url` input, and the description and email fields are cleared so a previous report's text does not carry over. With no related button it does nothing. It never submits and holds no state. Test: `test_tour_violation_report`.

### `test_tour_violation_report` [@ANCHOR: test_tour_violation_report]

The Python test in `tests/test_ui_tours.py` that starts the tour `test_tour_violation_report` (`static/tests/tours/violation_report_tour.js`) on a member's page. The tour opens the report modal, fills the description and a guest email, waits until the form_shield status line says it is verifying (the proof of work runs in a Web Worker), clicks Submit and waits for the redirect. The Python side then checks that a `content.violation.report` row exists with the guest email as `reported_by_email`. An edge-cached variant checks the CSRF refresh.

### `user_websites:report_limits` [@ANCHOR: user_websites:report_limits]

The constants `REPORT_LIMITS` and `REPORT_MEMBER_LIMITS` (tuples of `(scope, count, seconds)`) in `controllers/main.py`, passed to `form.shield.check_form(limits=...)`. A guest is allowed 5 reports per hour per network, a member 30 per hour per user; both get 10 per day per reported page (`target`) and 100 per hour for the whole site (`global`). Counters live in Redis; when Redis is down the shield answers "unavailable" and the page says so with HTTP 503. Over the limit gives HTTP 429. Tests: `test_report_violation_limits_and_unavailable_store`, `test_report_violation_is_rate_limited`.

### `user_websites:report_violation_shield` [@ANCHOR: user_websites:report_violation_shield]

The block inside `report_violation` that runs after the honeypot and before any structural check. It normalises the target (`normalize_report_target(url, _own_site_hosts())`), decides `member = not env.user._is_public()` and calls `request.env["form.shield"].check_form("report_violation", post, target=target, limits=...)`. A verdict that is not `ok` is answered by `_report_refusal`. Programs that cannot solve the proof of work never reach the later checks, and each refusal is counted by reason without keeping any text. Tests: `test_report_violation_shield_outcomes` (no answer, forged answer, either honeypot, another form's answer, expired, too fast, moved networks, solved once, replay).

### `user_websites:report_violation_shield_fields` [@ANCHOR: user_websites:report_violation_shield_fields]

The `<t t-call="form_shield.fields">` call in the `report_violation_modal` template (`views/user_websites_templates.xml`). It adds the shield's hidden fields and status line to the form, with `issued` set to false (so the question is not shown at once; the page around the modal may use that name for something else) and `fallback_url` set to `/website/report_violation`, the page that shows the plain-text question for visitors without scripts. The proof of work runs in the browser's Web Worker. Tested by the report tour and by `form_shield:test_report_form_happy_path_tour`.

### `user_websites:report_own_site_hosts` [@ANCHOR: user_websites:report_own_site_hosts]

`_own_site_hosts()` in `controllers/main.py`; needs the current request. **Returns:** a set of host names (lower case, `www.` and port dropped, empty values removed) that a report may be about: this request's host, the host of the `web.base.url` parameter and the domain of up to 500 `website` records. It reads the database once. The result feeds `normalize_report_target`. Test: `test_normalize_report_target`.

### `user_websites:normalize_report_target` [@ANCHOR: user_websites:normalize_report_target]

`normalize_report_target(raw, hosts)` in `controllers/main.py`.

| Argument | Type | Meaning |
| --- | --- | --- |
| `raw` | str or None | The posted URL or path. |
| `hosts` | set of str | Result of `_own_site_hosts()`. |

**Returns:** the URL in one canonical form (`scheme://host/path?query`, fragment dropped), or `None` when it is not a page of this site. Refused: empty, over 2048 characters, control characters, credentials in the URL, a scheme other than http or https, a host not in `hosts` (comparison ignores `www.`), and a path that is protocol-relative (`//x`) or not rooted. A bare path takes this request's origin. http and https are treated as the same page. No exceptions escape (a `ValueError` from parsing gives `None`). Tests: `test_normalize_report_target`, `test_report_violation_refuses_another_sites_url`.

### `user_websites:report_problem` [@ANCHOR: user_websites:report_problem]

`report_problem(target, description, email, member)` in `controllers/main.py`. **Returns:** a reason string or `None`. Reasons, in order: `foreign_target` (target is `None`), `short_description` (under 10 characters after stripping), `gibberish` (one unbroken random-looking token of 16 or more characters with a lower-case letter, an upper-case letter and a digit). A member stops there. A guest may also get `too_many_urls` (more than two `http(s)://` or `www.` occurrences) or `bad_email` (no strictly valid address, or a local part failing `_local_part_ok`). Only structure is judged; rude or wrong reports are left to moderators. Pure function. Test: `test_report_problem_reasons`.

### `user_websites:report_violation_page` [@ANCHOR: user_websites:report_violation_page]

`UserWebsitesController.report_violation_form(self, url="", **_ignored)`, route `GET /website/report_violation`, public, `sitemap=False`. It shows the report form as a page of its own, with the plain-text question in place of the proof of work, for browsers without scripts and for visitors the modal links to. The reported address is `url`, or the `Referer` header when empty. It delegates to `_render_report_page`, which marks the response `no-store`, because a question is issued per request and must not be cached. Test: `form_shield:test_question_form_page_renders_accessible_question`.

### `user_websites:reported_path` [@ANCHOR: user_websites:reported_path]

`_reported_path(reported_url)` in `controllers/main.py`. **Returns:** the path (with its query) of the URL, or `/` for an empty or unparsable one, never the scheme or host. Leading slashes and backslashes are collapsed to exactly one slash, so `http://site//evil.example` cannot turn into a protocol-relative redirect to another site. Used by `_report_violation_redirect_target` and `_report_refusal`. Pure. Test: `test_normalize_report_target`.

### `user_websites:bare_host` [@ANCHOR: user_websites:bare_host]

`_bare_host(host)` in `controllers/main.py`. **Returns:** the host lower-cased, with trailing dots removed and one leading `www.` dropped; `None` or an empty value gives an empty string. It is the single comparison rule used by `_own_site_hosts` and `normalize_report_target`, so `WWW.Example.com.` and `example.com` are the same host. Pure. Test: `test_normalize_report_target`.

### `user_websites:local_part_ok` [@ANCHOR: user_websites:local_part_ok]

`_local_part_ok(address)` in `controllers/main.py`. **Returns:** true unless the part of `address` before the first `@` starts or ends with a dot or contains `..`. It runs after `email_normalize(strict=True)` in `report_problem` and refuses addresses that mail providers fold into someone else's mailbox (such as `me.g.astop.l.ay.@gmail.com`). Pure. Test: `test_report_problem_reasons`.

### `user_websites:render_report_page` [@ANCHOR: user_websites:render_report_page]

`UserWebsitesController._render_report_page(self, url, description="", email="", notice="", status=200)`. It issues a new question with `form.shield.issue("q")` and renders `user_websites.report_violation_page` with `url`, `description`, `email`, `notice` and `issued`, so what the visitor typed is kept. It sets the HTTP status (200, 429 or 503) and the header `Cache-Control: no-store`. **Returns:** the response. Used by the GET page and by `_report_refusal`. Test: `test_report_violation_shield_outcomes`.

### `user_websites:report_refusal` [@ANCHOR: user_websites:report_refusal]

`UserWebsitesController._report_refusal(self, verdict, url, description, email)`. It answers a submission the shield refused. For the outcome `QUESTION` it re-renders the page with "Please answer this short question"; `LIMITED` renders "Too many reports from your network" with HTTP 429; `UNAVAILABLE` renders "Reporting is temporarily unavailable" with HTTP 503. Any other outcome (forged answers, honeypots, replays, bad signatures) goes to form_shield's `refusal_response`, which redirects to the success address (`report_submitted=1`) for a program and gives a person the reason, so a program learns nothing. **Returns:** a response. Test: `test_report_violation_shield_outcomes`.

### `user_websites:report_violation_redirect_target` [@ANCHOR: user_websites:report_violation_redirect_target]

`_report_violation_redirect_target(reported_url, query_param)` in `controllers/main.py`. **Returns:** the path from `_reported_path(reported_url)` followed by `?` or `&` and `query_param` (for example `report_submitted=1`). Using only the path stops the posted URL or `Referer` from being echoed as an open redirect; a bare path is same-origin. Pure. Test: `test_report_violation_redirects_back_to_reported_page`.

### `api_pending_reports` [@ANCHOR: api_pending_reports]

`UserWebsitesController.pending_reports(self, **kwargs)` (the code carries the id as `user_websites:COMM_pending_reports`), route `GET /api/v1/user_websites/pending_reports`, `auth="public"`, `sitemap=False`. **Returns:** JSON `{"count": int, "latest_id": int, "url": str}` for a user in `group_user_websites_administrator` or `base.group_system`; `count` is capped at 1000 and counts reports in state `new`, `latest_id` is the newest such id or 0. Every other caller (guests included) gets `{"count": 0, "error": "Forbidden"}` with HTTP 200, on purpose, so the page's fetch does not raise errors in headless tours. The counting runs as the module service account. Tests: `test_01_api_pending_reports_admin_access`, `test_02_api_pending_reports_unauthorized_access`.

### `test_admin_violation_toast_rpc` [@ANCHOR: test_admin_violation_toast_rpc]

Test `test_01_api_pending_reports_admin_access` in `tests/test_controllers.py`. It logs in as an administrator, calls `/api/v1/user_websites/pending_reports` and asserts HTTP 200 and a JSON body carrying the count. Its sibling `test_02` asserts that an unauthorised caller gets the `Forbidden` body with HTTP 200. Together they cover `api_pending_reports` and the data source of `admin_toast_logic`.

### `controller_my_privacy_dashboard` [@ANCHOR: controller_my_privacy_dashboard]

`UserWebsitesController.privacy_dashboard(self, **kwargs)` (code id `user_websites:COMM_privacy_dashboard`), route `GET /my/privacy`, `auth="user"`, `website=True`. Renders the template `user_websites.portal_my_privacy`: a "Download My Data" card (a POST form to `/my/privacy/export`), a "Right to Erasure" card that warns about the permanent ban and posts to `/my/privacy/delete_content`. **Returns:** the rendered page; a guest is sent to login. No side effects. Test: tour `test_tour_gdpr_privacy` (`gdpr_privacy_tour`).

### `UX_GDPR_EXPORT` [@ANCHOR: UX_GDPR_EXPORT]

The data export as a user sees it: the form on the privacy dashboard posting to `/my/privacy/export` (`privacy_export`, code id `user_websites:COMM_privacy_export`, `auth="user"`). The response is streamed JSON with `Content-Disposition: attachment; filename="gdpr_export.json"` and `Cache-Control: private, no-store`. The body holds the plain keys from `_get_gdpr_export_data()` (such as `user`) followed by the streamed keys from `_get_gdpr_streamed_keys()` (`pages`, `blog_posts`, `submitted_reports`, `appeals`), each written item by item, so a large export never sits in memory. `/my/privacy/export.zip` redirects to the CSV daemon with a single-use token cookie. Tests: `test_gdpr_export_api`, `test_tour_gdpr_privacy`.

### `UX_GDPR_ERASURE` [@ANCHOR: UX_GDPR_ERASURE]

The erasure action as a user sees it: a POST form on the privacy dashboard, with a confirmation prompt and a warning that erasure permanently bans the account. It posts to `/my/privacy/delete_content` (`privacy_delete_content`, `csrf=True`, `auth="user"`), which calls `_execute_gdpr_erasure()` (`gdpr_sudo_erasure`) and redirects to the public page `/privacy/erased`, because the deactivated account can no longer see logged-in pages. **Irreversible.** Tests: `test_02_right_to_erasure` (pages and posts gone, account inactive, removed from the directory), the tour `test_tour_gdpr_privacy`.

### `user_websites:COMM_privacy_erased` [@ANCHOR: user_websites:COMM_privacy_erased]

`UserWebsitesController.privacy_erased(self, **kwargs)`, route `GET /privacy/erased`, `auth="public"`. Renders the static template `user_websites.privacy_erased` (element `user_websites_erasure_confirmed`). It is public because the erased account's session is gone; it ignores the query string, reflects nothing and takes no redirect target, so it cannot be used to find out whether an account ever existed. **Returns:** the page. Tests: `test_02b_privacy_erased_public_page`, the tour `test_tour_gdpr_privacy`.

### `res_users_gdpr_export` [@ANCHOR: res_users_gdpr_export]

`res.users._get_gdpr_export_data(self)` in `models/res_users.py`, an override of the compliance hook. It calls `ensure_one()`, takes the parent's dictionary, makes sure it has a `user` key, and adds `name`, `email` and `website_slug` (or false). **Returns:** the dict that `privacy_export` writes. The large collections are not here; they are provided as generators by `_get_gdpr_streamed_keys()`. A module storing personal data must extend this method the same way. Reads only the user's own record; no side effects. Test: `test_gdpr_export_hook`.

### `test_gdpr_export_hook` [@ANCHOR: test_gdpr_export_hook]

Test `test_01_gdpr_export_hook` in `tests/test_sdk_extensibility.py`. It creates a page for a user named "SDK Tester", calls `_get_gdpr_export_data()` and `_get_gdpr_streamed_keys()`, and asserts that the data has a `user` key with the right name, that `pages` and `blog_posts` are streamed keys (so a large account is not loaded into memory at once), and that running the `pages` generator yields the created page. It covers `res_users_gdpr_export`.

### `gdpr_sudo_erasure` [@ANCHOR: gdpr_sudo_erasure]

`res.users._execute_gdpr_erasure(self)` in `models/res_users.py`. It calls `ensure_one()` and takes the service environment `zero_sudo.gdpr_service_internal`. Using that environment for the search as well as the delete, it removes, in batches of 5000 inside savepoints: the user's `website.page` rows, then `blog.post`, then `blog.blog`; on a concurrent-update, serialization or deadlock error it waits 0.5 s and retries, any other error is raised. Outside test mode it commits after each batch. Then it deletes the user's site images (`ir.attachment` with description `user_websites site image`, up to 500), sets `privacy_show_in_directory` false and calls the parent erasure. **Returns:** nothing. **Irreversible.** The search must not use the caller's rights: a caller outside the owner's group would otherwise find nothing. Tests: `test_gdpr_erasure_pages`, `test_gdpr_erasure_posts`.

### `test_gdpr_erasure_pages` [@ANCHOR: test_gdpr_erasure_pages]

Part of `test_02_right_to_erasure` in `tests/test_privacy_gdpr.py`. A signed-in user with one page and one post posts the CSRF-protected form to `/my/privacy/delete_content`; the test asserts the redirect lands on `/privacy/erased` and shows "Your data has been erased", the account is inactive, the page no longer `exists()`, and the user was removed from the directory. It covers the page deletion of `gdpr_sudo_erasure`.

### `test_gdpr_erasure_posts` [@ANCHOR: test_gdpr_erasure_posts]

Same test as `test_gdpr_erasure_pages` (`test_02_right_to_erasure`): after the erasure request it asserts that the user's blog post no longer exists. It covers the `blog.post` and `blog.blog` loops of `gdpr_sudo_erasure`, which run as the GDPR service account, not as the user.

## 10. Anchor reference: owner site, part 1 (controller, views, home routes)

The owner interface lives in `controllers/owner_site.py` (class `UserWebsitesOwnerController`) and `views/owner_site_templates.xml`; the home routes live in `controllers/main.py`. Every owner route is `auth="user"`, `website=True`, `sitemap=False` and is wrapped by `answers_site_response`, which turns a `SiteResponse` exception (a redirect from a retired slug, a 403 explanation page) into the route's own response. All POST routes carry `csrf=True`. Tests are in `tests/test_owner_site.py` unless named otherwise.

### `user_websites:UX_OWNER_TOOLBAR` [@ANCHOR: user_websites:UX_OWNER_TOOLBAR]

The QWeb template `user_websites.owner_toolbar` (`views/owner_site_templates.xml`), rendered by `user_navbar` above the site's own navbar. It is a dark bar labelled "Your site" that appears only when `is_owner` is true for the page being viewed, so a visitor, a member of another site and (on someone else's slug) an internal administrator never see it; `?visitor=1` hides it for the owner. It holds links to Manage site, New page, Images and Privacy, a POST form for a new blog post, and, depending on the page, Edit this page, Edit this post, Publish or Unpublish (POST to `.../publish` with a `next` address), Delete this page (never for the home page) and a View-as-visitor link. Inputs come from the render context (`resolved_slug`, `main_object`, `page_type`). Tests: `test_owner_toolbar_visible_to_owner_only`, the tour `test_tour_owner_site`.

### `user_websites:COMM_owner_site_dashboard` [@ANCHOR: user_websites:COMM_owner_site_dashboard]

`owner_site_dashboard(self, site_slug, **kwargs)`, route `GET /my/site/<site_slug>`. After `_resolve_site` it lists, as the module service account, up to 200 pages (ordered by url) and 200 blog posts (newest first) owned by the user or group, up to `MAX_IMAGES_PER_SITE` images (attachments with description `user_websites site image`), and up to 100 pages and posts each that the owner deleted (`active_test=False`, `deleted_by_owner` set). It renders `user_websites.owner_site_dashboard` with the image limits and a flag `has_home` saying whether the home page exists. **Returns:** the rendered page; a caller who does not own the slug gets 404. Read-only, no rate limit. Test: `test_owner_site_dashboard_lists_content`.

### `user_websites:UX_OWNER_PAGE_EDITOR` [@ANCHOR: user_websites:UX_OWNER_PAGE_EDITOR]

The QWeb template `user_websites.owner_page_edit` (`views/owner_site_templates.xml`), the form for creating or editing one page. Context values: `page` (record or none), `title`, `page_slug`, `body` (already sanitized markup), `published`, `can_edit_body`, `is_home`, `error`, `saved`. It shows an error alert for the codes `title`, `address`, `exists`, `too_long` and `not_editable`; a page built by the website builder (`can_edit_body` false) is shown with a warning and no form. The form posts `page_id`, `title`, `page_slug`, `content` (filled by the rich editor from the page markup) and `is_published` to `/my/site/<slug>/page/save` with the CSRF token. Test: `test_owner_page_editor_renders_wysiwyg`.

### `user_websites:COMM_owner_page_save` [@ANCHOR: user_websites:COMM_owner_page_save]

`owner_page_save(self, site_slug, page_id="", title="", page_slug="", content="", is_published="", **kwargs)`, route `POST /my/site/<site_slug>/page/save`.

| Argument | Type | Meaning |
| --- | --- | --- |
| `page_id` | str | Existing page id (ASCII digits only, else 404), empty for a new page. |
| `title` | str | Stripped; 1 to 120 characters. |
| `page_slug` | str | New pages only; default from the title; must match `PAGE_SLUG_RE` and not be reserved. |
| `content` | str | Editor HTML, at most `MAX_BODY_CHARS` (200,000); sanitized by `sanitize_member_html`. |
| `is_published` | str | Any non-empty value publishes. |

**Flow:** resolve the site, count one write against the 30-per-minute limit, sanitize the body. Refusals (`title`, `too_long`, `address`, `exists`) re-render the editor with HTTP 400 and what the member typed (`_editor_with_error`); `not_editable` redirects back. A hostile body is filed as a violation by `_report_if_hostile` and saved clean. An existing page is written in the caller's own environment, so the model's ownership guard, field allow-list and quotas still apply; a new page is created as the service account at `/<slug>/<page_slug>` with a `qweb` arch. **Returns:** a redirect (303) to `/my/site/<slug>/page/<id>?saved=1`. Errors: 404 for foreign content, 429 over the limit, 503 when Redis is down. Test: `test_owner_page_create_edit_publish`.

### `user_websites:COMM_owner_page_publish` [@ANCHOR: user_websites:COMM_owner_page_publish]

`owner_page_publish(self, site_slug, page_id, publish="", **kwargs)`, route `POST /my/site/<site_slug>/page/<page_id>/publish`. It resolves the site, counts a write, finds the owner's page with `_own_page` (404 otherwise) and writes `website_published = (publish == "1")` in the caller's own environment, so the model's guards apply. Nothing is deleted: unpublishing keeps the page as a draft. **Returns:** a 303 redirect to the `next` parameter if it starts with `/my/site/`, otherwise to the dashboard with `?saved=1`. Test: `test_owner_page_create_edit_publish`.

### `user_websites:COMM_owner_post_publish` [@ANCHOR: user_websites:COMM_owner_post_publish]

`owner_post_publish(self, site_slug, post_id, publish="", **kwargs)`, route `POST /my/site/<site_slug>/post/<post_id>/publish`. Same pattern as the page toggle for a blog post: resolve the site, count a write, load the post through `UserWebsitesController._get_own_blog_post_for_edit`, require that its owner user or group is this site's (else 404), then write `is_published = (publish == "1")`. **Returns:** a 303 redirect to the dashboard with `?saved=1`. The post stays; nothing is erased. Test: `test_owner_post_publish_toggle`.

### `user_websites:COMM_owner_image_upload` [@ANCHOR: user_websites:COMM_owner_image_upload]

`owner_image_upload(self, site_slug, ufile=None, json_reply="", **kwargs)`, route `POST /my/site/<site_slug>/image`. Counts against the separate 10-uploads-per-minute limit. Checks in order: a file was sent (400), it is not empty (400), at most 2 MB (413, only `MAX_IMAGE_BYTES + 1` bytes are read), a JPEG, PNG, GIF or WebP by magic number (415, see `owner_image_mimetype`), and fewer than 100 images for this site (409). It then creates a public `ir.attachment` as the service account (name from the slugified file name, at most 60 characters, `res_model` and `res_id` from `_attachment_owner`, description `user_websites site image`). **Returns:** with `json_reply` set, JSON `{"ok", "error", "url"}` where `url` is `/web/image/<id>` (errors keep their HTTP status); otherwise a 303 redirect to the dashboard with `?saved=1` or `?error=image`. Test: `test_owner_image_upload_rules`.

### `user_websites:owner_resolve_site` [@ANCHOR: user_websites:owner_resolve_site]

`UserWebsitesOwnerController._resolve_site(self, site_slug)`, the ownership gate for every owner route. As the service account it looks the slug up as a user and as a group. **Returns:** `(profile_user, profile_group)`, one of them an empty recordset. **Errors:** 404 when the user slug is not the caller's own (an administrator who is not the owner included) or when the group slug names a group the caller is not a member of or one that is suspended; a user slug whose owner is suspended raises `SiteResponse` with the 403 page `owner_site_suspended`; one whose owner has eligibility gaps reconciles the account and raises the 403 `owner_site_ineligible` page; an unknown slug first tries a legacy-slug redirect (301 for GET, 308 otherwise), then 404. The decision comes from the database, never from the URL. Tests: `test_owner_site_denied_for_other_member`, `test_owner_site_suspended_explains_and_points_to_appeal`, `test_site_eligibility_hook`, `test_site_slug_one_site`.

### `user_websites:UX_RICH_EDITOR` [@ANCHOR: user_websites:UX_RICH_EDITOR]

The QWeb template `user_websites.rich_editor` (`views/user_websites_templates.xml`), included by the page and post editors. Its context is `editor_name`, `editor_label`, `editor_html` and `upload_url`. It draws a formatting toolbar (`role="toolbar"`, buttons with `data-uw-cmd` and an `aria-label` each: Bold, Italic, Underline, Inline code, headings, lists, links, images, undo, redo, quote, code block, alignment) and a `contenteditable` area, plus a hidden field that `static/src/js/rich_editor.js` fills with the area's HTML when the form is submitted. The page never shows markup to the member; the server sanitizes the field. Tests: `test_owner_page_editor_renders_wysiwyg` (no raw-markup textarea, labels present) and the tour `test_tour_owner_site`.

### `user_websites:owner_rate_limit` [@ANCHOR: user_websites:owner_rate_limit]

`UserWebsitesOwnerController._rate_limit(self, bucket, limit)`. `bucket` is `"write"` (30 a minute) or `"upload"` (10 a minute); the counter key is `uw:rl:<database>:<bucket>:<user id>` and is incremented with `expiring_counter(redis_client, key, 60)`, which always sets a 60-second expiry. **Returns:** nothing. **Errors:** `TooManyRequests` (HTTP 429) when the count passes the limit; `ServiceUnavailable` (503) when Redis raises, so the limiter fails closed rather than allowing unbounded writes. Called after the ownership check, before any write. Tests: `test_owner_write_rate_limited`, `test_15b_the_write_rate_limit_counter_always_has_an_expiry`.

### `user_websites:member_site_standalone_layout` [@ANCHOR: user_websites:member_site_standalone_layout]

The block in the `layout_inherit_user_navbar` template (`views/user_websites_templates.xml`) that sets `member_site` true for a member or group site (the page's `main_object` is a `website.page`, `blog.blog` or `blog.post` owned by the profile, or the profile record itself) and for owner tool pages that pass `member_site=True`. When it is true it sets `no_header` and `no_footer`, so `website.layout` omits the hams.com menu, banners and footer, and the site's own `user_navbar` and `member_site_footer` render instead. Other pages keep the main chrome. Test: `test_member_site_has_no_hams_chrome`.

### `user_websites:COMM_user_home_fallback` [@ANCHOR: user_websites:COMM_user_home_fallback]

`user_home_fallback(self, website_slug, **kwargs)`, route `GET /<uwslug:website_slug>`, `auth="public"`. Finds the slug as a user or group site (service account). An unknown slug first tries a legacy-slug 301, then Odoo's own page, redirect and attachment lookup (`_serve_core_fallback`), then 404. A user whose site is not publicly visible, or a suspended group, is 404. A slug that differs in case from the canonical one is redirected 301 to the canonical address. If a published page exists at `/<slug>` it is served by Odoo's router (and a Redis view counter is incremented for non-administrators; Redis failure is only logged). Otherwise it renders `user_websites.placeholder_page`, with `is_owner` and `home_state` telling the owner how to create the home. Tests: `test_member_site_home_url`, `test_create_site_existing_home`.

### `user_websites:COMM_owner_site_entry` [@ANCHOR: user_websites:COMM_owner_site_entry]

`owner_site_entry(self, **kwargs)`, route `GET /my/site`. A signed-in user without a site slug is sent to `/my/home`; otherwise to `/my/site/<own slug>` with a 303. No database writes. It exists so a link to "my site" works without knowing the slug.

### `user_websites:COMM_owner_page_new` [@ANCHOR: user_websites:COMM_owner_page_new]

`owner_page_new(self, site_slug, **kwargs)`, route `GET /my/site/<site_slug>/page/new`. After `_resolve_site` it renders `user_websites.owner_page_edit` with no page, an empty body, unpublished, `can_edit_body` true, and any `title` and `page_slug` query parameters prefilled. **Returns:** the form page. No write happens until the form is posted to the save route.

### `user_websites:COMM_owner_page_edit` [@ANCHOR: user_websites:COMM_owner_page_edit]

`owner_page_edit(self, site_slug, page_id, **kwargs)`, route `GET /my/site/<site_slug>/page/<page_id>`. Resolves the site, loads the page with `_own_page` (404 if it is not the owner's or not under the owner's address), reads the body back with `_arch_body`, sanitizes it again for display, and renders the editor with the page name, its address part (empty for the home page), the published flag, `can_edit_body` (false for builder-made layouts) and `is_home`. **Returns:** the rendered page. Read only.

### `user_websites:owner_page_arch` [@ANCHOR: user_websites:owner_page_arch]

`_page_arch(site_slug, page_key, title, body_html)` (module function). **Returns:** the stored arch string for a member page: `<t name=... t-name="user_websites.page_<page_key>"><t t-call="user_websites.template_default_home"><div id="wrap" class="oe_structure uw-member-page">...body...</div></t></t>`. The body (already sanitized HTML) is re-serialised as XML through lxml, the title is escaped for an attribute with `_escape_attr`. Only the two QWeb directives the arch sanitizer allows are produced. Empty body gives an empty wrapper. Pure. Test: `test_owner_page_create_edit_publish`.

### `user_websites:owner_page_arch_body` [@ANCHOR: user_websites:owner_page_arch_body]

`_arch_body(arch_base)` (module function). **Returns:** `(body_html, editable)`. It parses the stored arch and takes the children of `<div id="wrap">` as HTML. If the arch is empty it returns `("", True)`; if it fails to parse, has no `wrap` div, or contains any `<t>` element or `t-*` attribute inside the wrapper (a website-builder layout) it returns `("", False)`, so the editor refuses to overwrite a page it cannot represent. Pure. Test: `test_owner_page_create_edit_publish`.

### `user_websites:owner_image_mimetype` [@ANCHOR: user_websites:owner_image_mimetype]

`_image_mimetype(data)` (module function). **Returns:** `image/jpeg`, `image/png`, `image/gif` or `image/webp` chosen from the leading bytes (`FF D8 FF`, the PNG signature, `GIF87a`, `GIF89a`, or `RIFF....WEBP`), else `None`. The file name and the declared content type are ignored, so a renamed script is refused. Argument `data` is bytes (at most 2 MB + 1 byte, as read by the upload route). Pure. Tests: `test_owner_image_upload_rules` and `test_image_magic_numbers`.

### `test_tour_owner_site` [@ANCHOR: test_tour_owner_site]

The tour `owner_site_tour` (`static/tests/tours/owner_site_tour.js`), started by a Python test in `tests/test_ui_tours.py`. The owner opens their own site (`/touruser`), sees the toolbar, starts a new blog post, finds the contenteditable editor, types a title and a body with `execCommand("insertText")` (no markup), publishes and sees the post on the site. Its header lists the rich-editor function anchors it exercises (link bar, paste, range memory, toolbar state, history). It runs in a headless browser.

### `user_websites:fn_main_serve_core_fallback` [@ANCHOR: user_websites:fn_main_serve_core_fallback]

`_serve_core_fallback()` in `controllers/main.py`, no arguments. It removes `website_slug` from `request.params` and returns `request.env["ir.http"]._serve_fallback()`: Odoo's own page, redirect or attachment lookup for the path (or `False` when there is none). The removal matters because `website.page._allow_to_use_cache()` refuses to cache a response whose request has parameters, so stock pages reached through the `/<slug>` route would otherwise lose edge caching. Used by both home routes for slugs that are not member sites. Test: `test_helper_functions_are_exercised`.

### `user_websites:fn_owner_escape` [@ANCHOR: user_websites:fn_owner_escape]

`_escape(text)` in `controllers/owner_site.py`. **Returns:** `text` with `&`, `<` and `>` replaced by `&amp;`, `&lt;`, `&gt;`, for building the arch XML by hand. It does not touch quotes, so it is not safe for attributes; use `_escape_attr` there. Argument must be a string. Pure. Test: `test_helper_functions_are_exercised`.

### `user_websites:fn_owner_escape_attr` [@ANCHOR: user_websites:fn_owner_escape_attr]

`_escape_attr(text)`. **Returns:** `_escape(text)` with `"` also replaced by `&quot;`, for a double-quoted XML attribute (the page title inside `name="..."` in the arch). Argument must be a string. Pure. Test: `test_helper_functions_are_exercised`.

### `user_websites:fn_owner_svc` [@ANCHOR: user_websites:fn_owner_svc]

`UserWebsitesOwnerController._svc(self)`. **Returns:** the environment of the service account `user_websites.user_websites_service_account`, from `zero_sudo.security.utils._get_service_env`. Used for reads of other members' tables and for creates after the ownership check; it is never `sudo()`. Calling it does not check ownership, so the caller must have passed `_resolve_site` first. Test: `test_helper_functions_are_exercised`.

### `user_websites:fn_owner_owner_domain` [@ANCHOR: user_websites:fn_owner_owner_domain]

`_owner_domain(self, profile_user, profile_group)`. **Returns:** the search domain `[("owner_user_id", "=", user.id)]` for a user site, else `[("user_websites_group_id", "=", group.id)]`. One of the two arguments is a record, the other empty. Used by the dashboard to scope searches. Pure. Test: `test_helper_functions_are_exercised`.

### `user_websites:fn_owner_qcontext` [@ANCHOR: user_websites:fn_owner_qcontext]

`_qcontext(self, profile_user, profile_group, **extra)`. **Returns:** the render dictionary shared by all owner templates: `profile_user`, `profile_group`, `main_object` (the user or group), `resolved_slug` and `site_slug` (its slug), `member_site` true, `is_owner` true, `error` and `saved` from the query string, then the keyword arguments merged on top. `member_site` makes the page wear the site's own layout; `is_owner` shows the toolbar. Pure apart from reading the request. Test: `test_helper_functions_are_exercised`.

### `user_websites:fn_owner_report_if_hostile` [@ANCHOR: user_websites:fn_owner_report_if_hostile]

`_report_if_hostile(self, hostile, profile_user, profile_group, url)`. When `hostile` is false it does nothing. Otherwise it calls `website.page._trigger_malicious_arch_violation({"url": url, "owner_user_id" | "user_websites_group_id": ...})` in the caller's environment, which files an automated `content.violation.report` and strike for the owner. It is called after the body was sanitized, so the hostile markup is already gone from what is stored. **Returns:** nothing. Test: `test_13_hostile_body_is_sanitized_and_reported`.

### `user_websites:fn_owner_clean_body` [@ANCHOR: user_websites:fn_owner_clean_body]

`_clean_body(self, content)`. **Returns:** `(None, False)` when `content` is longer than `MAX_BODY_CHARS` (200,000), else the result of `sanitize_member_html(content)`, a tuple `(clean_html, hostile)`. The length check runs first so an oversized body is not parsed. The caller treats `None` as the `too_long` error. Pure. Test: `test_helper_functions_are_exercised`.

### `user_websites:fn_owner_own_page` [@ANCHOR: user_websites:fn_owner_own_page]

`_own_page(self, profile_user, profile_group, page_id)`. Loads the page as the service account and **returns** it, or raises 404 when it does not exist, belongs to another user or group, or its `url` is not `/<slug>` or under `/<slug>/`. The last check keeps a page that was somehow moved out of the owner's address space from being edited here. Archived pages are not found (see `_own_page_any`). Test: `test_21_page_urls_stay_in_the_owners_namespace` and `test_11_cannot_touch_other_members_content`.

### `user_websites:fn_owner_back_to_editor` [@ANCHOR: user_websites:fn_owner_back_to_editor]

`_back_to_editor(self, slug, page_id, error)`. **Returns:** a redirect (Odoo's default 303) to `/my/site/<slug>/page/<page_id>?error=<error>`, or to `.../page/new` when `page_id` is not ASCII digits. Used by the save route when an existing page is not editable (`not_editable`). `slug` is quoted. Test: `test_helper_functions_are_exercised`.

### `user_websites:fn_owner_attachment_owner` [@ANCHOR: user_websites:fn_owner_attachment_owner]

`_attachment_owner(self, profile_user, profile_group)`. **Returns:** `("res.users", user.id)` for a user site, else `("user.websites.group", group.id)`: the `res_model` and `res_id` under which a site's images are stored and counted. Account erasure removes the images of the member's own account (not a group's). Pure. Test: `test_20_image_limit_per_site`.

## 11. Anchor reference: owner site, part 2 (rich editor script and member HTML sanitizer)

The editor script is `static/src/js/rich_editor.js` (a public `Interaction` named `RichEditor`, selector `.uw-rich-editor`, no external library). It is a convenience only: the server (`models/member_html.py`) decides what is kept. All the editor functions are exercised by the tour `test_tour_owner_site`, which types, formats, links, undoes and publishes in a real browser. The sanitizer functions are exercised by `tests/test_owner_site.py` (class with `test_script_handlers_and_qweb_are_removed_and_flagged`, `test_svg_schematic_is_kept_and_hostile_svg_is_stripped`, `test_toolbar_markup_survives`, `test_external_images_and_foreign_classes_are_dropped`).

### `user_websites:rich_editor_start` [@ANCHOR: user_websites:rich_editor_start]

`RichEditor.start()`, run by Odoo's public interaction framework for each element matching `.uw-rich-editor` (the template `user_websites.rich_editor`). It finds the editable area (`[data-uw-area]`), the hidden field (`[data-uw-field]`), the status line, the link bar and its input, and the hidden file input; initialises the private undo history (the area's current HTML, index 0, because the browser's own undo is shared with the title field); copies the area into the field once; and registers listeners: toolbar click and mousedown (the latter keeps the selection in the page), paste, input, keydown, beforeinput, keyup and mouseup (toolbar state), the link bar click and keydown, the file input change, and the enclosing form's submit. Takes no arguments, returns nothing, and does not touch the network.

### `user_websites:rich_editor_sync_field` [@ANCHOR: user_websites:rich_editor_sync_field]

`RichEditor.syncField()`. Copies `area.innerHTML` into the hidden form field, so the form always posts the page the member sees. It is called at start, after every input, toolbar action, paste, link and image insertion, and on form submit. No arguments or return value. The server treats the posted value as untrusted and sanitizes it (`user_websites:fn_hmtl_sanitize_member_html`).

### `user_websites:rich_editor_say` [@ANCHOR: user_websites:rich_editor_say]

`RichEditor.say(message)`. Sets the text of the status element (`[data-uw-status]`, an `aria-live` region in the template) to `message`, or clears it when `message` is empty or missing. It is how the editor reports link-format errors, upload progress, upload failures and the reminder to describe an image. Returns nothing; no network.

### `user_websites:rich_editor_toolbar_click` [@ANCHOR: user_websites:rich_editor_toolbar_click]

`RichEditor.onToolbarClick(ev)`. Handles a click inside the toolbar. It finds the nearest enabled `button[data-uw-cmd]`, focuses the area and acts on the command: `link` opens the link bar, `image` remembers the selection and opens the file chooser, `code` wraps the selection in `<code>`, `undo` and `redo` step the private history, `formatBlock` runs `document.execCommand` with the button's `data-uw-arg` (a heading, quote or code block), and every other command is passed straight to `execCommand` (bold, italic, lists, alignment and so on). Commands other than link, image, undo and redo record one history step. It then syncs the field and refreshes the pressed state of the buttons.

### `user_websites:rich_editor_toolbar_state` [@ANCHOR: user_websites:rich_editor_toolbar_state]

`RichEditor.refreshToolbarState()`. For each toolbar button whose command is in `TOGGLE_STATE` (bold, italic, underline, the two list types, the three alignments) it asks `document.queryCommandState` and sets `aria-pressed` and the `active` class accordingly. A browser exception counts as "off". Run after toolbar clicks, key-up and mouse-up inside the area. It reads the current selection and changes only button attributes.

### `user_websites:rich_editor_remember_range` [@ANCHOR: user_websites:rich_editor_remember_range]

`RichEditor.rememberRange()`. If the page selection has a range and its anchor lies inside the editable area, it stores a clone in `this.savedRange`; otherwise it leaves any earlier value. It is called before the link bar or the file chooser takes focus away, because those would lose the selection. Returns nothing.

### `user_websites:rich_editor_restore_range` [@ANCHOR: user_websites:rich_editor_restore_range]

`RichEditor.restoreRange()`. Focuses the area and, if a range was saved by `rememberRange`, makes it the current selection again so an inserted link or image lands where the member was working. With no saved range it just focuses. Called when the link bar closes and before an uploaded image is inserted. Returns nothing.

### `user_websites:rich_editor_wrap_code` [@ANCHOR: user_websites:rich_editor_wrap_code]

`RichEditor.wrapSelectionInCode()`. Replaces a non-empty selection with a `<code>` element holding the selected plain text (nested formatting inside the selection is flattened). With no selection or a collapsed one it does nothing. The caller records the history step and syncs the field. It exists because `execCommand` has no inline-code command.

### `user_websites:rich_editor_open_link_bar` [@ANCHOR: user_websites:rich_editor_open_link_bar]

`RichEditor.openLinkBar()`. Remembers the selection, shows the link bar (removes `d-none`), prefills its input with `https://` and focuses it. A small in-page bar replaces `window.prompt`, which is not accessible and is blocked in some browsers. No arguments or return value.

### `user_websites:rich_editor_close_link_bar` [@ANCHOR: user_websites:rich_editor_close_link_bar]

`RichEditor.closeLinkBar()`. Hides the link bar and restores the saved selection. Used by the Cancel button (through `onLinkBarClick`), the Escape key (through `onLinkKeydown`) and at the start of `applyLink`, so the bar is always closed before a link is inserted or refused. It takes no arguments and returns nothing; the status message, if any, is left as it was.

### `user_websites:rich_editor_link_bar_click` [@ANCHOR: user_websites:rich_editor_link_bar_click]

`RichEditor.onLinkBarClick(ev)`. A click on an element inside `[data-uw-link-apply]` calls `applyLink()`; one inside `[data-uw-link-cancel]` calls `closeLinkBar()`; any other click in the bar, such as one in the input itself, is ignored. The listener is on the bar element, so it also catches clicks on the icon inside a button. It takes the click event, returns nothing and changes no state of its own.

### `user_websites:rich_editor_link_keydown` [@ANCHOR: user_websites:rich_editor_link_keydown]

`RichEditor.onLinkKeydown(ev)`. Keyboard handling for the link input: Enter applies the link and Escape closes the bar, both with `preventDefault` so Enter does not submit the page form. Other keys are left alone, so the member can type the address normally. The handler is attached to the link input only, not to the page area. It takes the keyboard event and returns nothing.

### `user_websites:rich_editor_apply_link` [@ANCHOR: user_websites:rich_editor_apply_link]

`RichEditor.applyLink()`. Reads and trims the link input and closes the bar. A URL that does not start with `http://`, `https://`, `mailto:` or `/` (case-insensitive) is refused with a status message "Links must start with http://, https://, mailto: or /." and nothing is inserted. Otherwise the message is cleared, `document.execCommand("createLink", false, url)` wraps the restored selection, one history step is recorded and the field is synced. The server filters links again.

### `user_websites:rich_editor_image_picked` [@ANCHOR: user_websites:rich_editor_image_picked]

`RichEditor.onImagePicked()` (async), the change handler of the hidden file input. It clears the input, ignores an empty choice, refuses a file over 2 MB with a status message, then POSTs a `FormData` (`ufile`, `json_reply=1`, the CSRF token from `data-csrf`) to the URL in `data-upload-url` (`/my/site/<slug>/image`, same-origin credentials). A network failure or `ok: false` reply is reported through `say`. On success it restores the selection, inserts `<img src="<reply.url>" alt="<file name without extension, quotes and angle brackets removed>"/>` with `insertHTML`, records history, syncs, and reminds the member to describe the image. The server enforces type, size and quota; this check is only a courtesy.

### `user_websites:rich_editor_paste` [@ANCHOR: user_websites:rich_editor_paste]

`RichEditor.onPaste(ev)`. Cancels the browser's paste. If the clipboard holds HTML it is parsed with `DOMParser` and reduced by `cleanNode` before `insertHTML`; otherwise the plain text is inserted with `insertText`. Then one history step is recorded and the field synced. Nothing from the pasted markup reaches the page unfiltered. Without `clipboardData` it does nothing and lets the browser paste.

### `user_websites:rich_editor_clean_node` [@ANCHOR: user_websites:rich_editor_clean_node]

`RichEditor.cleanNode(node)`. **Returns:** an HTML string built from the children of a DOM node. Text is escaped (`&`, `<`, `>`); elements in `DROP_TAGS` (script, style, frames, forms, SVG and similar) vanish with their content; `A` keeps only an `href` starting with `http(s)://`, `mailto:` or `/`, else just its inner text; `IMG` is dropped (pasted images are never hot-linked); `BR` and `HR` are written as self-closing tags; tags in `BLOCK_TAGS` or `INLINE_TAGS` are kept without attributes, except `SPAN` and `DIV`, which are unwrapped; any other element is replaced by its cleaned children. Styles, classes and ids are never copied. Recursive; no side effects.

## 12. Anchor reference: member HTML sanitizer and site menu

`models/member_html.py` holds the allow-list sanitizer used by the owner routes. It runs in the request path for every caller, administrators included, because the model-level sanitizers skip administrators and the service account.

### `user_websites:member_html_sanitize` [@ANCHOR: user_websites:member_html_sanitize]

The allow-list constants at the top of `models/member_html.py` that define what member HTML may contain, with the public entry point `sanitize_member_html`. `ALLOWED_TAGS`: p, br, hr, h1 to h4, strong, b, em, i, u, s, strike, sub, sup, ul, ol, li, blockquote, pre, code, a, img, div, span, table, thead, tbody, tr, th, td, figure, figcaption. `DROP_WITH_CONTENT`: script, style, iframe, frames, object, embed, applet, form controls, link, meta, base, head, title, noscript, template, math, audio, video, source, track, canvas, dialog. `ALLOWED_CLASSES`: only the alignment classes `text-start`, `text-center`, `text-end`, `text-justify` (a `text-align` style is converted to one). `LINK_SCHEMES`: http, https, mailto. `IMAGE_PATH_RE`: only `/web/image/<id>...` and `/web/content/<id>...`. `MAX_BODY_CHARS` is 200,000. Tests: `test_member_html_sanitize_strips_hostile_markup` and its cases.

### `user_websites:fn_hmtl_clean_url` [@ANCHOR: user_websites:fn_hmtl_clean_url]

`_clean_url(value, image=False)`. **Returns:** the stripped URL when allowed, else `None`. Empty values and any value with a control character are refused. With `image=True` only a path matching `IMAGE_PATH_RE` passes, so a member cannot hot-link an external tracking image. For links: a `#fragment`, a same-site path starting with a single `/` (not `//`), or a URL whose scheme is http, https or mailto passes; `javascript:`, `data:` and everything else returns `None`. Pure.

### `user_websites:fn_hmtl_is_hostile` [@ANCHOR: user_websites:fn_hmtl_is_hostile]

`_is_hostile(el)`. **Returns:** true when the lxml element is something only a hand-made request would carry: a tag in `DROP_WITH_CONTENT`, an attribute starting with `on`, `t-` or `xlink`, or an `href`, `src`, `action` or `formaction` using `javascript:`, `vbscript:` or a `data:` URL other than `data:image/`. It only classifies; removal is done by `_clean_children`. The flag is what lets the controller file the input as a violation. Pure.

### `user_websites:fn_hmtl_sanitize_member_html` [@ANCHOR: user_websites:fn_hmtl_sanitize_member_html]

`sanitize_member_html(source)` (public). `source` is the posted HTML string, cut to twice `MAX_BODY_CHARS`. **Returns:** `(clean_html, hostile)`: a well-formed fragment of allow-listed markup (stripped of the wrapping `<div>`), and true when any element was hostile per `_is_hostile`. A blank input gives `("", False)`; markup the parser cannot read also gives `("", False)`. Parsing uses lxml's fragment parser, then `_clean_children` rewrites the tree. No database or network access. Callers: the owner save route (through `_clean_body`) and the editor display. Tests: the sanitizer cases in `tests/test_owner_site.py`.

### `user_websites:fn_hmtl_clean_children` [@ANCHOR: user_websites:fn_hmtl_clean_children]

`_clean_children(parent)`, recursive. For each child: a comment or processing instruction is dropped; a tag in `DROP_WITH_CONTENT` is dropped with its content; `svg` goes through `_replace_with_sanitized_svg`; anything else is cleaned first (depth first), then a tag outside `ALLOWED_TAGS` is unwrapped (its text and children stay), an allowed tag gets `_clean_attributes`, an `a` with no surviving `href` is unwrapped, an `img` with no `src` is dropped, and every kept link gets `rel="nofollow noopener ugc"`. Mutates the tree in place; returns nothing.

### `user_websites:fn_hmtl_clean_attributes` [@ANCHOR: user_websites:fn_hmtl_clean_attributes]

`_clean_attributes(el, tag)`. Removes every attribute, then sets back only what is allowed: a `class` made of the alignment classes (a `text-align:` value in the old `style` replaces it with the matching class), for `a` a cleaned `href` and a `title` of up to 300 characters, for `img` a cleaned same-site `src`, an `alt` of up to 300 characters and numeric `width` and `height`, for `td` and `th` numeric `colspan` and `rowspan`. No `style`, `id`, `data-*`, event handler or QWeb attribute survives. Mutates in place.

### `user_websites:fn_hmtl_drop` [@ANCHOR: user_websites:fn_hmtl_drop]

`_drop(el)`. Removes the element and everything in it from its parent, but keeps the element's tail text by appending it to the previous sibling's tail or the parent's text. An element without a parent is left alone. Returns nothing; mutates the tree.

### `user_websites:fn_hmtl_unwrap` [@ANCHOR: user_websites:fn_hmtl_unwrap]

`_unwrap(el)`. Removes the element's tag but keeps its content: its text and its children move into the parent at the same position, and its tail text follows. An element with no parent is left alone. It is how disallowed but harmless tags (for example `font`) lose their markup and keep their words. Returns nothing; mutates the tree.

### `user_websites:fn_hmtl_menu_pages` [@ANCHOR: user_websites:fn_hmtl_menu_pages]

`menu_pages(env, domain_field, record_id, slug)`. **Returns:** a list of `{"name", "url"}` for the site's own navbar: up to 8 published `website.page` records owned by the record (`domain_field` is `owner_user_id` or `user_websites_group_id`, `record_id` its id), whose url is `/<slug>/<one segment>`, ordered by name, excluding any whose last segment is in `RESERVED_PAGE_SLUGS`. The search runs as the module service account. Read only. Callers: the two `_user_websites_menu_pages` methods.

### `user_websites:fn_users_user_websites_menu_pages` [@ANCHOR: user_websites:fn_users_user_websites_menu_pages]

`res.users._user_websites_menu_pages(self)` (`models/res_users.py`). Calls `ensure_one()` and returns `menu_pages(env, "owner_user_id", self.id, self.website_slug)`, or `[]` when the user has no site slug. Used by the `user_navbar` template to list the member's pages. Read only.

### `user_websites:fn_group_user_websites_menu_pages` [@ANCHOR: user_websites:fn_group_user_websites_menu_pages]

`user.websites.group._user_websites_menu_pages(self)` (`models/user_websites_groups.py`). Calls `ensure_one()` and returns `menu_pages(env, "user_websites_group_id", self.id, self.website_slug)`, or `[]` for a group without a slug. The group-site twin of `fn_users_user_websites_menu_pages`, used by the navbar when the site belongs to a group. It needs one record (`ensure_one`) and is read only; the search itself runs as the module service account inside `menu_pages`.

### `user_websites:member_html_svg_allowlist` [@ANCHOR: user_websites:member_html_svg_allowlist]

`_replace_with_sanitized_svg(el)` in `models/member_html.py`. An `<svg>` element is serialised and passed through Odoo's `html_sanitize` with zero_sudo's SVG allow-list, which keeps shapes and text and removes scripts, handlers, links, `<use>` and `data:` URLs. The cleaned nodes replace the original at the same position, tail text kept. If the cleaned result cannot be parsed the element is removed. The editor cannot draw SVG; this keeps schematics members pasted or imported earlier. Test: `test_svg_allowlist_sanitizer_fields`, and `test_svg_schematic_is_kept_and_hostile_svg_is_stripped`.
