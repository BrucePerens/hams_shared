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
* **Ownership Validation:** Safely asserted by mixin create `[@ANCHOR: mixin_proxy_ownership_create]` and write `[@ANCHOR: mixin_proxy_ownership_write]` methods. Explicitly verified by `[@ANCHOR: test_mixin_ownership_validation]`.

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
* **Home Header:** `id="user_websites_dropzone_home_header"` -> `[@ANCHOR: dropzone_home_header]`

* **Home Footer:** `id="user_websites_dropzone_home_footer"` -> `[@ANCHOR: dropzone_home_footer]`

* **Global Navbar:** `id="user_websites_dropzone_navbar"` -> `[@ANCHOR: dropzone_navbar]`

* **Portal Templates:** `id="user_websites_dropzone_templates"` -> `[@ANCHOR: dropzone_templates]`

* **Snippets Sidebar:** `id="user_websites_dropzone_snippets"` -> `[@ANCHOR: dropzone_snippets]`

* **Website Layout:** `id="user_websites_dropzone_layout"` -> `[@ANCHOR: dropzone_layout]`

* **User Settings:** `id="user_websites_dropzone_users"` -> `[@ANCHOR: dropzone_users]`

* **Blog Post Form:** `id="user_websites_dropzone_blog_post"` -> `[@ANCHOR: dropzone_blog_post]`

* **Navbar Actions:** `id="user_websites_dropzone_navbar_actions"` -> `[@ANCHOR: dropzone_navbar_actions]`

* **Directory Card:** `id="user_websites_dropzone_directory_card"` -> `[@ANCHOR: dropzone_directory_card]`

* **Global Settings Form:** -> `[@ANCHOR: dropzone_settings]`

### Prohibited Dropzones
DO NOT USE user_websites_settings_dropzone. All settings views must now inherit directly from base.res_config_settings_view_form and target the //form element using the modernized `<app>`, `<block>`, and `<setting>` XML tags.

### Endpoints & Webhooks
* **Community Directory:** Renders public pages `/community` `[@ANCHOR: UX_COMMUNITY_DIRECTORY]`.

* **Violation Reporting:** Form endpoint `/website/report_violation` `[@ANCHOR: user_websites:UX_REPORT_VIOLATION]`, `[@ANCHOR: violation_report_logic]`, verified by `[@ANCHOR: test_tour_violation_report]`. Protected by the `form_shield` module (proof of work or question, honeypots, minimum time-to-submit, per-network, per-page and per-site limits `[@ANCHOR: user_websites:report_limits]`, `[@ANCHOR: user_websites:report_violation_shield]`, the form's fields `[@ANCHOR: user_websites:report_violation_shield_fields]`); the reported URL must be a page of this site (this host, the base URL or a website domain, http and https alike, www ignored `[@ANCHOR: user_websites:report_own_site_hosts]`) `[@ANCHOR: user_websites:normalize_report_target]` and a guest's report is refused when it is short, one random-looking string, link-heavy or lacks a valid address `[@ANCHOR: user_websites:report_problem]`. `GET /website/report_violation` is the question variant for browsers without scripts `[@ANCHOR: user_websites:report_violation_page]`. Helpers: the redirect path is forced to a single leading slash `[@ANCHOR: user_websites:reported_path]`, hosts compare without `www.` `[@ANCHOR: user_websites:bare_host]`, a guest's address may not start or end its local part with a dot `[@ANCHOR: user_websites:local_part_ok]`, the question page is rendered by `[@ANCHOR: user_websites:render_report_page]` and a refusal is answered by `[@ANCHOR: user_websites:report_refusal]`; the old per-address redirect-path anchor `[@ANCHOR: user_websites:report_violation_redirect_target]` stays.

* **Home Routing:** Target view `/<slug>` (the old `/<slug>/home` answers a permanent 301 to it, `[@ANCHOR: user_websites:COMM_user_home_legacy_redirect]`) `[@ANCHOR: controller_user_websites_home]`.

* **Site Creation:** `/<slug>/create_site` `[@ANCHOR: UX_CREATE_SITE]`, concurrency scaling proven by `[@ANCHOR: test_site_creation_performance_scaling]`.

* **Blog Routing:** `/<slug>/blog` `[@ANCHOR: controller_user_blog_index]`.

* **Blog Creation:** `/<slug>/create_blog` `[@ANCHOR: UX_CREATE_BLOG_POST]`.

* **Post Creation & Editing:** `POST /<slug>/create_blog_post` creates a blank "New Post" in the owner's blog (or redirects to `/<slug>/blog` if no blog exists yet) and redirects to `/blog_post/edit/<post_id>`, this module's own ownership-scoped title/body/publish form (the website-builder Edit toolbar needs the sitewide `website.group_website_designer` permission, which site owners do not hold).

* **Documentation:** Proxies knowledge records `/user-websites/documentation` `[@ANCHOR: controller_user_websites_documentation]`.

* **Appeals:** User submission endpoint `/website/submit_appeal` `[@ANCHOR: UX_SUBMIT_APPEAL]`.

* **Subscriptions:** `/<slug>/subscribe` `[@ANCHOR: UX_SUBSCRIBE]`. Unsubscribe verification with HMAC-SHA256 token validation over the model, record, partner and timestamp `[@ANCHOR: controller_unsubscribe_digest]`. The timestamp is signed but its age is not checked, so a valid link does not expire.

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
* **Cache Invalidation Hooks:** Distributed slug caches are safely invalidated upon user mutation via `[@ANCHOR: slug_cache_invalidation]`, `[@ANCHOR: slug_cache_invalidation_unlink]`, `[@ANCHOR: group_slug_cache_invalidation]`, and `[@ANCHOR: group_slug_cache_invalidation_unlink]`.

* **String Utilities:** Safe slugification generation logic (`slugify()` in `edge_routing.utils`) `[@ANCHOR: utils_slugify]`.

* **Limits:** Individual page quota enforcement `[@ANCHOR: website_page_quota_check]`.

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
* Features an automated `ir.cron` job (`send_weekly_digest` `[@ANCHOR: ir_cron_send_weekly_digest]`, `[@ANCHOR: send_weekly_digest]`) that iterates through `blog.post` objects and dispatches emails to followers. Re-entrant batching algorithm resumption is tested by `[@ANCHOR: test_cron_batching_resumption]`. QWeb Mail templates are verified by `[@ANCHOR: test_weekly_digest_mail_template]`.

* Utilizes HMAC-SHA256 tokens to generate secure, one-click `List-Unsubscribe` header links for GDPR/CAN-SPAM compliance `[@ANCHOR: test_weekly_digest_secret]`.

* **Background View Counter Sync:** High-throughput Redis view counters are safely flushed to Postgres via cron `[@ANCHOR: ir_cron_flush_view_counters]`, tested extensively by `[@ANCHOR: test_cron_redis_flush]`. Round-trips are optimized via a PostgreSQL procedure `[@ANCHOR: procedure_flush_view_counters]`.

* **High-Speed Simulation Tests:** The full operational load of the module is end-to-end verified via the High-Speed Simulation Environment `[@ANCHOR: simulation_environment]`.
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

* **Owner toolbar** `[@ANCHOR: user_websites:UX_OWNER_TOOLBAR]` (`views/owner_site_templates.xml`, called from `user_navbar`): shown above the site's own navbar to the site's owner, or a member of the owning group, on every page of that site, internal users included. `?visitor=1` hides it.
* **Dashboard** `/my/site/<slug>` `[@ANCHOR: user_websites:COMM_owner_site_dashboard]`, `/my/site` redirects to the caller's own. Page editor `/my/site/<slug>/page/new|<id>` `[@ANCHOR: user_websites:UX_OWNER_PAGE_EDITOR]`, save `[@ANCHOR: user_websites:COMM_owner_page_save]`, publish toggles `[@ANCHOR: user_websites:COMM_owner_page_publish]` and `[@ANCHOR: user_websites:COMM_owner_post_publish]`, image upload `[@ANCHOR: user_websites:COMM_owner_image_upload]` (`controllers/owner_site.py`). Every route resolves the site from the database and answers 404 unless the caller owns it (`[@ANCHOR: user_websites:owner_resolve_site]`).
* **Editor** `[@ANCHOR: user_websites:UX_RICH_EDITOR]`: a contenteditable WYSIWYG (`static/src/js/rich_editor.js`), no raw HTML and no external library. Odoo's own `html_editor` is an OWL backend component tied to the website builder's `website.group_website_designer` permission and its asset bundles; mounting it for portal members would hand them the sitewide editor, which this module deliberately never grants.
* **Sanitizer** `[@ANCHOR: user_websites:member_html_sanitize]` (`models/member_html.py`): strict allow-list in the request path for every caller, administrators included; hostile input is also filed through `website.page._trigger_malicious_arch_violation()`.
* **Limits**: title 120 characters, body 200,000 characters, images JPEG/PNG/GIF/WebP at most 2 MB and 100 per site, 30 writes and 10 uploads per user per minute (Redis counters, `[@ANCHOR: user_websites:owner_rate_limit]`; Redis down answers 503).
* **Retention**: no route deletes. Unpublish keeps the record. Uploaded images are public `ir.attachment` rows owned by the account (`res_model='res.users'`/`user.websites.group`, description `user_websites site image`); the erasure flow removes them with the member's other content.
* **Layout** `[@ANCHOR: user_websites:member_site_standalone_layout]`: on a member site `layout_inherit_user_navbar` sets `no_header`/`no_footer`, so the main site's header and footer are not rendered, and the site's own header (`user_navbar`) and footer (`member_site_footer`) are. Assets: `static/src/css/member_site.css` and `rich_editor.js` in `web.assets_frontend`.
* **Home address** `/<slug>` `[@ANCHOR: user_websites:COMM_user_home_fallback]`; `/<slug>/home` answers 301 `[@ANCHOR: user_websites:COMM_user_home_legacy_redirect]`.
* **Owner-site internals**: `/my/site` `[@ANCHOR: user_websites:COMM_owner_site_entry]`; the new-page and edit-page forms `[@ANCHOR: user_websites:COMM_owner_page_new]`, `[@ANCHOR: user_websites:COMM_owner_page_edit]`; a page's arch is the fixed member-page frame around sanitized body markup `[@ANCHOR: user_websites:owner_page_arch]` and the editor reads the body back out of it `[@ANCHOR: user_websites:owner_page_arch_body]`; uploads are accepted by magic number, not by file name or declared type `[@ANCHOR: user_websites:owner_image_mimetype]`.
* **Editor functions** (`static/src/js/rich_editor.js`, exercised by `[@ANCHOR: test_tour_owner_site]`): `[@ANCHOR: user_websites:rich_editor_apply_link]`, `[@ANCHOR: user_websites:rich_editor_clean_node]`, `[@ANCHOR: user_websites:rich_editor_close_link_bar]`, `[@ANCHOR: user_websites:rich_editor_image_picked]`, `[@ANCHOR: user_websites:rich_editor_link_bar_click]`, `[@ANCHOR: user_websites:rich_editor_link_keydown]`, `[@ANCHOR: user_websites:rich_editor_open_link_bar]`, `[@ANCHOR: user_websites:rich_editor_paste]`, `[@ANCHOR: user_websites:rich_editor_remember_range]`, `[@ANCHOR: user_websites:rich_editor_restore_range]`, `[@ANCHOR: user_websites:rich_editor_say]`, `[@ANCHOR: user_websites:rich_editor_start]`, `[@ANCHOR: user_websites:rich_editor_sync_field]`, `[@ANCHOR: user_websites:rich_editor_toolbar_click]`, `[@ANCHOR: user_websites:rich_editor_toolbar_state]`, `[@ANCHOR: user_websites:rich_editor_wrap_code]`.
* **Helper functions** (owner interface and sanitizer, each exercised by `tests/test_owner_site.py`): `[@ANCHOR: user_websites:fn_main_serve_core_fallback]`, `[@ANCHOR: user_websites:fn_owner_escape]`, `[@ANCHOR: user_websites:fn_owner_escape_attr]`, `[@ANCHOR: user_websites:fn_owner_svc]`, `[@ANCHOR: user_websites:fn_owner_owner_domain]`, `[@ANCHOR: user_websites:fn_owner_qcontext]`, `[@ANCHOR: user_websites:fn_owner_report_if_hostile]`, `[@ANCHOR: user_websites:fn_owner_clean_body]`, `[@ANCHOR: user_websites:fn_owner_own_page]`, `[@ANCHOR: user_websites:fn_owner_back_to_editor]`, `[@ANCHOR: user_websites:fn_owner_attachment_owner]`, `[@ANCHOR: user_websites:fn_hmtl_clean_url]`, `[@ANCHOR: user_websites:fn_hmtl_is_hostile]`, `[@ANCHOR: user_websites:fn_hmtl_sanitize_member_html]`, `[@ANCHOR: user_websites:fn_hmtl_clean_children]`, `[@ANCHOR: user_websites:fn_hmtl_clean_attributes]`, `[@ANCHOR: user_websites:fn_hmtl_drop]`, `[@ANCHOR: user_websites:fn_hmtl_unwrap]`, `[@ANCHOR: user_websites:fn_hmtl_menu_pages]`, `[@ANCHOR: user_websites:fn_users_user_websites_menu_pages]`, `[@ANCHOR: user_websites:fn_group_user_websites_menu_pages]`.
* **SVG schematics** survive the sanitizer through zero_sudo's SVG allow-list `[@ANCHOR: user_websites:member_html_svg_allowlist]`; every other active or unknown element is removed.

<stage1_sweep>
## 7. Function-Level Anchors

### Moderation
* **Strike action:** `[@ANCHOR: action_take_action_and_strike]` -- defined in the `content_moderation` module (see Moderation Models above); this module's enforcement hook turns it into a strike and, past 3 strikes, a suspension.

### Controllers
* **User Blog Index Route:** `[@ANCHOR: user_websites:COMM_user_blog_index]` -- `/<slug>/blog`.

* **Create Site Route:** `[@ANCHOR: user_websites:COMM_create_site]`.

* **Create Blog Route:** `[@ANCHOR: user_websites:COMM_create_blog]`.

* **Privacy Dashboard Route:** `[@ANCHOR: user_websites:COMM_privacy_dashboard]` -- `/my/privacy`.

* **Privacy Delete Content Route:** `[@ANCHOR: user_websites:COMM_privacy_delete_content]` -- `/my/privacy/delete_content`.

* **Pending Reports API Route:** `[@ANCHOR: user_websites:COMM_pending_reports]`.

### Blog Ownership, Quota, and Access
* **Blog Create/Check-Access/Write/Unlink:** `[@ANCHOR: user_websites:COMM_blog_blog_create]`, `[@ANCHOR: user_websites:COMM_blog_blog_check_access]`, `[@ANCHOR: user_websites:COMM_blog_blog_write]`, `[@ANCHOR: user_websites:COMM_blog_blog_unlink]`.

* **Blog Post URL Helper:** `[@ANCHOR: user_websites:COMM_get_blog_urls]` -- constructs the blog-index URLs invalidated on any post mutation.

* **Blog Post Check-Access/Write/Unlink:** `[@ANCHOR: user_websites:COMM_blog_post_check_access]`, `[@ANCHOR: user_websites:COMM_blog_post_write]`, `[@ANCHOR: user_websites:COMM_blog_post_unlink]`.

### Content Moderation and Appeals
* **Appeal Target Constraint:** `[@ANCHOR: user_websites:COMM_check_appeal_target]` -- an appeal must be tied to exactly one of a user or a group.

* **Approve Appeal:** `[@ANCHOR: user_websites:COMM_appeal_action_approve]` -- pardons the appeal's user or group and moves the appeal to `approved`; it raises a `UserError` if the appeal is no longer in the `new` state, so a repeated call cannot pardon twice.

* **Reject Appeal:** `[@ANCHOR: user_websites:COMM_appeal_action_reject]`.

* **Strike Count Increment:** `[@ANCHOR: user_websites:COMM_increment_strike_count]` -- the atomic stored-procedure call backing the 3-strike rule.

* **Mark Report Under Review:** `[@ANCHOR: user_websites:COMM_action_mark_under_review]`.

* **Dismiss Report:** `[@ANCHOR: user_websites:COMM_report_action_dismiss]`.

* **Suspend/Pardon a Group:** `[@ANCHOR: user_websites:COMM_action_suspend_group_websites]`, `[@ANCHOR: user_websites:COMM_action_pardon_group_websites]`.

* **Suspend/Pardon a User:** `[@ANCHOR: user_websites:COMM_action_suspend_user_websites]`, `[@ANCHOR: user_websites:COMM_action_pardon_user_websites]`.

* **Suspended-Group Membership Compute:** `[@ANCHOR: user_websites:COMM_compute_suspended_group_ids]` -- which of a user's groups are currently suspended.

### GDPR Export
* **Export Token Name Compute:** `[@ANCHOR: user_websites:COMM_gdpr_token_compute_name]`.

* **Mint Export Token For Current User:** `[@ANCHOR: user_websites:COMM_create_for_current_user]`.

* **Streamed GDPR Export Keys:** `[@ANCHOR: user_websites:COMM_get_gdpr_streamed_keys]` -- generator-based streaming to avoid OOM on large exports.

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
  verified by `[@ANCHOR: test_dropzone_settings]`.

### SQL Views and Functions (`_auto=False`, proven live by any test that successfully queries the resulting view -- a broken `init()` would fail module installation before any such test could run)
* **Public Directory View:** `[@ANCHOR: user_websites:COMM_public_directory_view_init]`.

* **Content Routing View:** `[@ANCHOR: user_websites:COMM_content_routing_view_init]`.

* **Weekly Digest View:** `[@ANCHOR: user_websites:COMM_weekly_digest_view_init]`.

* **DB Functions (`increment_strike_count`):** `[@ANCHOR: user_websites:COMM_db_functions_init]`.

### res.users Extensions
* **Background Content Unpublish:** `[@ANCHOR: user_websites:COMM_async_unpublish_content]` -- module-level helper run on a background thread when a user is suspended or archived.

* **Registry Hook:** `[@ANCHOR: user_websites:COMM_register_hook]` -- provisions the `sys_provisioner` service account at registry build time.

* **Reserved Slug Checks:** `[@ANCHOR: user_websites:COMM_res_users_check_reserved_slugs]` (users), `[@ANCHOR: user_websites:COMM_group_check_reserved_slugs]` (groups).

* **Admin Check:** `[@ANCHOR: user_websites:COMM_is_admin]`.

* **Slug-to-User-ID Resolver:** `[@ANCHOR: user_websites:COMM_get_user_id_by_slug]` -- direct raw-SQL resolver against the content routing view. Nothing else in this repository calls this method (the cache-invalidation code only mentions it in comments); the generic `edge.routing.mixin.get_record_by_slug()` dispatch this module also relies on does its own separate ORM search instead. It is tested directly.

* **User Create/Write:** `[@ANCHOR: user_websites:COMM_res_users_create]`, `[@ANCHOR: user_websites:COMM_res_users_write]` -- the latter includes the 301-redirect-on-slug-change automation.

* **Group Write:** `[@ANCHOR: user_websites:COMM_group_write]` -- the same slug-redirect automation for `user.websites.group`.

* **Quota Helpers:** `[@ANCHOR: user_websites:COMM_get_page_limit]`, `[@ANCHOR: user_websites:COMM_get_blog_limit]`, `[@ANCHOR: user_websites:COMM_get_blog_post_limit]`.

### website.page Extensions
* **Cloudflare Cache Invalidation:** `[@ANCHOR: user_websites:COMM_page_invalidate_cloudflare_cache]`.

* **Automated Malicious-Arch Violation Report:** `[@ANCHOR: user_websites:COMM_trigger_malicious_arch_violation]` -- files a `content.violation.report` against the page's owner whenever the arch sanitizer actually strips something.

* **URL-to-Page-ID Resolver:** `[@ANCHOR: user_websites:COMM_get_page_id_by_url]` -- direct URL lookup respecting suspension state. Likewise, nothing else in this repository calls it. It is tested directly.

* **Page Unlink:** `[@ANCHOR: user_websites:COMM_website_page_unlink]`.
</stage1_sweep>
