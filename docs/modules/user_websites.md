# User Websites (`user_websites`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

This Odoo 19 module lets users build their own personal or group websites and blogs right inside your Odoo instance.

**Open Source Rule:** We built this for the open-source community. It runs perfectly on its own and does not rely on any proprietary code.

## 🌟 Key Features

* **Personal Sites & Blogs:** Give every user their own URL (like `/<username>/home`) where they can drag and drop pages or write blog posts.
* **Group Sites:** Let teams or clubs share a website. Anyone in the group can edit pages or post to the group's blog.
* **Community Directory:** A public list where users can show off their sites (if they choose to opt-in).
* **Built-in Moderation:** Every page has a "Report Violation" button. If users post bad content, admins can review it, hand out strikes, and automatically suspend accounts that break the rules.
* **Page Limits:** Stop spam by setting limits on how many pages a single user can create.
* **GDPR Compliance:** Built-in Data Portability (Export) and Right to Erasure (Delete) features for user-generated content.
* **Security-First Sanitization:** Automated neutralization of malicious code (SSTI/XSS) attempting to execute on user-managed pages.

## 🛠️ Installation

1. Drop the `user_websites` folder into your Odoo `addons` directory.
2. Restart your Odoo server.
3. Turn on Developer Mode, go to **Apps**, and click **Update Apps List**.
4. Search for `User Websites` and click **Install**.

## ⚙️ Configuration

Go to **Settings > General Settings > User Websites** to configure the app.
* **Global Page Limit:** Set the default maximum number of pages a user is allowed to build.
* **Administrators:** Pick the users who are allowed to review abuse reports and manage group websites.

## 🏗️ How It Works Under the Hood

We used a few neat tricks to make this secure and fast:

* **Just-In-Time Creation:** We don't waste database space creating empty blogs for users who never use them. The system only creates the website records the exact moment the user visits their URL for the first time.
* **One Big Blog:** Instead of creating a thousand separate blog containers, everyone shares a single Odoo `blog.blog` record named "Community Blog". We just filter the posts so users only see their own stuff.
* **Proxy Ownership:** Odoo normally only lets admins build web pages. We get around this securely. When a user creates a page, the system briefly logs in as a background Service Account to save the HTML to the database, but tags the user as the real "owner" so only they can edit it later.
* **Security Shield:** The module includes an automated sanitization engine that intercepts and neutralizes XSS and SSTI attempts. If a user tries to inject malicious code, the system strips it, notifies administrators, and issues a strike.

---

# Technical Documentation

<system_role>
*Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

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
* **`website_slug`**: URL-safe identifier.
* **`privacy_show_in_directory`**: Opt-in for the public `/community` directory.
* **`violation_strike_count`**: Number of upheld content violations.
* **`is_suspended_from_websites`**: If True, all personal content is forcefully unpublished.
* **`appeal_ids`** (`One2many`): Links to Moderation Appeals.

### Content Models (`website.page`, `blog.post`)
* **`owner_user_id`**: The proxy owner.
* **`user_websites_group_id`**: For shared group websites.
* **`view_count`**: Privacy-friendly server-side view tracker.

### Moderation Models
* **`content.violation.report`**: Stores abuse reports. Originator is masked from the target owner. The system automatically generates a report and issues a strike if a user attempts to inject malicious SSTI/XSS payloads into their site architecture `[@ANCHOR: action_take_action_and_strike]`, tested by `[@ANCHOR: test_moderation_suspension]`. Admin spam is prevented via a daily digest cron (`ir_cron_notify_pending_reports` `[@ANCHOR: ir_cron_notify_pending_reports]`, `[@ANCHOR: cron_notify_pending_reports]`, verified by `[@ANCHOR: test_cron_pending_reports]`) and a session-guarded UI toast (`[@ANCHOR: toast_notifications_logic]`, `[@ANCHOR: admin_toast_logic]`, tested by `[@ANCHOR: test_tour_toast_notifications]`).
* **Security Auto-Moderation**: The `website.page` model includes `_sanitize_user_arch` `[@ANCHOR: website_page_sanitize_arch]`, verified by `[@ANCHOR: test_website_page_sanitize_arch]`, which forcefully strips dangerous QWeb directives (`t-*`) and JS event handlers.
* **`content.violation.appeal`**: Used by suspended users to petition for account restoration.
</data_model>

---

<public_api>
## 3. 🐍 Public API & Extensibility Methods

### Explicit Dropzones
To prevent monolithic entanglement, `user_websites` provides the following explicitly designated dropzones. You MUST use `<xpath>` targeting these specific IDs and cite the corresponding Semantic Anchor:
* **Home Header:** `id="user_websites_master_header"` -> `[@ANCHOR: dropzone_home_header]`
* **Home Footer:** `id="user_websites_master_footer"` -> `[@ANCHOR: dropzone_home_footer]`
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
* **Violation Reporting:** Form endpoint `/website/report_violation` `[@ANCHOR: user_websites:UX_REPORT_VIOLATION]`, `[@ANCHOR: violation_report_logic]`, verified by `[@ANCHOR: test_tour_violation_report]`.
* **Home Routing:** Target view `/<slug>/home` `[@ANCHOR: controller_user_websites_home]`.
* **Site Creation:** `/<slug>/create_site` `[@ANCHOR: UX_CREATE_SITE]`, concurrency scaling proven by `[@ANCHOR: test_site_creation_performance_scaling]`.
* **Blog Routing:** `/<slug>/blog` `[@ANCHOR: controller_user_blog_index]`.
* **Blog Creation:** `/<slug>/create_blog` `[@ANCHOR: UX_CREATE_BLOG_POST]`.
* **Documentation:** Proxies knowledge records `/user-websites/documentation` `[@ANCHOR: controller_user_websites_documentation]`.
* **Appeals:** User submission endpoint `/website/submit_appeal` `[@ANCHOR: UX_SUBMIT_APPEAL]`.
* **Subscriptions:** `/<slug>/subscribe` `[@ANCHOR: UX_SUBSCRIBE]`. Unsubscribe verification with HMAC TTL token validation `[@ANCHOR: controller_unsubscribe_digest]`.
* **`GET /api/v1/user_websites/pending_reports`**: Returns a JSON object `{'count': int}` of unhandled violation reports. Restricted to administrators `[@ANCHOR: api_pending_reports]`. Verified by `[@ANCHOR: test_admin_violation_toast_rpc]`.
* **GDPR Actions:** Privacy dashboard `/my/privacy` `[@ANCHOR: controller_my_privacy_dashboard]`. Data exports `/my/privacy/export` `[@ANCHOR: UX_GDPR_EXPORT]`. Data erasure via background cascading unlinks `/my/privacy/delete_content` `[@ANCHOR: UX_GDPR_ERASURE]`.

### 🚨 Privilege Deprecation & Cross-Module Execution (CRITICAL)
In adherence to the Micro-Service Account Pattern (ADR-0062), the `user_websites` internal service account (`user_websites.user_websites_service_account`) has been stripped of omnipotent ERP privileges. It **can no longer create or delete** core identity records (`res.users`, `res.partner`). Furthermore, it retains only microscopic read-only access (`1,0,0,0`) to framework tables like `discuss.channel`, `res.company`, and `res.partner.bank` strictly to satisfy Odoo's internal ORM cascade requirements (The Framework ACL Tax - ADR-0064).

If your dependent module (e.g., `cloudflare`, `custom_dns`) needs to programmatically resolve slugs or provision websites, **you MUST NOT rely on the `user_websites` service account to bypass ACLs for you.** Instead, you must fetch your own domain-specific service account and pass it using the `override_svc_uid` parameter. Your module must explicitly declare the necessary Access Control Lists (`ir.model.access.csv`) for its own service account to perform the required operations.

### Programmatic Setup & Hooks
**The Secure Cached Resolver Pattern (ADR-0066)**: The `user_websites` module offers high-performance `@tools.ormcache` resolvers for cross-module use. ALWAYS use these instead of `.search()` in frontend controllers to prevent database exhaustion. Callers **MUST** pass their own `override_svc_uid` to execute the database search under their own service account's context instead of relying on the default System Provisioner, preventing cross-module access rule failures due to the privilege deprecation mentioned above.
* **`res.users._get_user_id_by_slug(slug, override_svc_uid=None)`**: Resolves a user's slug to their User ID.
* **`res.users.action_suspend_user_websites()`**: Forcefully unpublishes user content (moderation).
* **`res.users.action_pardon_user_websites()`**: Restores user content after appeal.
* **`user.websites.group._get_group_id_by_slug(slug, override_svc_uid=None)`**: Resolves a group's slug to its Group ID.
* **`website.page._get_page_id_by_url(url, website_id, override_svc_uid=None)`**: Resolves a page URL to its Page ID.
* **`RESERVED_SLUGS`**: A set of slugs that are reserved by the system and cannot be used by users or groups (e.g., `community`, `blog`, `my`). Available in `user_websites.utils`.
* **`user_websites.owned.mixin`**: Inherit this in your custom models (e.g., `custom.portfolio`) to instantly inherit the Proxy Ownership security rules via `self._check_proxy_ownership_write(vals)`.
  * **Mandatory Assignment:** Standard users MUST supply either `owner_user_id` OR `user_websites_group_id` upon record creation.
  * **Mutual Exclusivity:** A record CANNOT be owned by both a user and a group simultaneously. Attempting to assign both will raise a strict `ValidationError`.
* **Cache Invalidation Hooks:** Distributed slug caches are safely invalidated upon user mutation via `[@ANCHOR: slug_cache_invalidation]`, `[@ANCHOR: slug_cache_invalidation_unlink]`, `[@ANCHOR: group_slug_cache_invalidation]`, and `[@ANCHOR: group_slug_cache_invalidation_unlink]`.
* **String Utilities:** Safe slugification generation logic `[@ANCHOR: utils_slugify]`.
* **Limits:** Individual page quota enforcement `[@ANCHOR: website_page_quota_check]`.
* **GDPR Hooks**: The module extends `_get_gdpr_export_data()` `[@ANCHOR: res_users_gdpr_export]`, tested by `[@ANCHOR: test_gdpr_export_hook]`, and `_execute_gdpr_erasure()` `[@ANCHOR: gdpr_sudo_erasure]`, tested by `[@ANCHOR: test_gdpr_erasure_pages]` and `[@ANCHOR: test_gdpr_erasure_posts]`. Dependent modules storing PII MUST override these to append their data to the export payload and hard-delete it during erasure.
* **Documentation Injection**: The module follows the soft-dependency pattern for documentation. It attempts to install its `data/documentation.html` into `knowledge.article` or `knowledge.article` via `res.users._register_hook()`. This ensures compatibility with both Odoo Enterprise and the Community `knowledge` module without hard dependencies `[@ANCHOR: documentation_bootstrap]`.
</public_api>

---

<stories_and_journeys>
## 4. Architectural Stories & Journeys

For detailed narratives and end-to-end workflows, refer to the following:

### Stories
* [Group Websites](user_websites/docs/stories/group_sites.md)
* [Content Moderation](user_websites/docs/stories/moderation.md)
* [Personal Site Management](user_websites/docs/stories/personal_site.md)
* [Privacy and GDPR Compliance](user_websites/docs/stories/privacy.md)
* [Technical Foundation and Utilities](user_websites/docs/stories/technical_foundation.md)

### Journeys
* [Extension and Customization](user_websites/docs/journeys/customization.md)
* [User Data Management (GDPR)](user_websites/docs/journeys/gdpr_compliance.md)
* [Content Reporting and Resolution](user_websites/docs/journeys/moderation_workflow.md)
* [First Time Site Setup](user_websites/docs/journeys/onboarding.md)
</stories_and_journeys>

---

<crons_and_subscriptions>
## 5. 📧 Weekly Digests & Subscriptions
* Features an automated `ir.cron` job (`send_weekly_digest` `[@ANCHOR: ir_cron_send_weekly_digest]`, `[@ANCHOR: send_weekly_digest]`) that iterates through `blog.post` objects and dispatches emails to followers. Re-entrant batching algorithm resumption is tested by `[@ANCHOR: test_cron_batching_resumption]`. QWeb Mail templates are verified by `[@ANCHOR: test_weekly_digest_mail_template]`.
* Utilizes HMAC-SHA256 tokens to generate secure, one-click `List-Unsubscribe` header links for GDPR/CAN-SPAM compliance `[@ANCHOR: test_weekly_digest_secret]`.
* **Background View Counter Sync:** High-throughput Redis view counters are safely flushed to Postgres via cron `[@ANCHOR: ir_cron_flush_view_counters]`, tested extensively by `[@ANCHOR: test_cron_redis_flush]`.
* **High-Speed Simulation Tests:** The full operational load of the module is end-to-end verified via the High-Speed Simulation Environment `[@ANCHOR: simulation_environment]`.
</crons_and_subscriptions>
