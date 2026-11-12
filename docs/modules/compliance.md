# Global Compliance & Privacy (`compliance`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

This module automatically handles the annoying parts of running a legal website. It makes sure your Odoo instance complies with GDPR (the EU General Data Protection Regulation), CCPA (the California Consumer Privacy Act), and ePrivacy (the EU ePrivacy Directive, the "Cookie Law") rules without you having to configure anything manually.

## 🌟 What It Does

* **Turns on the Cookie Banner:** As soon as you install this, it flips the switch to turn on Odoo's native Cookie Consent Bar across all your websites. It also ensures that any **new** websites created later have this enabled by default. This stops optional tracking scripts until the user clicks "Accept."
* **Writes Your Legal Pages:** It automatically creates standard, editable pages for your Privacy Policy (`/privacy`), Cookie Policy (`/cookie-policy`), Terms of Service (`/terms`) and Accessibility Statement (`/accessibility`). It also adds footer links to them and to the `/my/privacy` dashboard, and a public index of active compliance documents at `/compliance`.
* **Doesn't Break Your Edits:** If you've already written a privacy policy at `/privacy` when the module is installed, the module detects it and leaves yours alone (it unpublishes its own copy). If you edit the pages it creates, it won't overwrite your work when you update the module.

## ⚖️ Included Policy Coverage
The boilerplate policies we generate are written specifically to cover the features in our other open-source modules. They explain:
* How our privacy-friendly view counters work.
* How users can download or permanently delete their data at the `/my/privacy` dashboard.
* How our abuse reporting system hides the reporter's email to protect them.
* How our 3-strike moderation and suspension system works.

## 🛠️ Installation

1. Drop the `compliance` folder into your Odoo `addons` directory.
2. Restart your Odoo server.
3. Turn on Developer Mode, go to **Apps**, and click **Update Apps List**.
4. Search for `Global Compliance` and click **Install**.

---

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for LLMs and Integrators.
</system_role>

<enforcement_details>
## 1. Overview
A non-interactive configuration module that enforces baseline regulatory compliance across the Odoo instance upon installation.

### 📚 User Stories & Journeys

#### Stories
* [Automatic Legal Pages Generation](./hams_shared/docs/stories/automatic_legal_pages.md) `story_automatic_legal_pages`

* [Enforced Cookie Consent](./hams_shared/docs/stories/cookie_consent.md) `story_cookie_consent`

* [Site Owner Documentation](./hams_shared/docs/stories/compliance_documentation.md) `story_compliance_documentation`

#### Journeys
* [Compliance Setup Journey](./hams_shared/docs/journeys/compliance_setup_journey.md) `journey_compliance_setup`

## 2. Enforcement Details
* **Automated Cookie Consent:** Programmatically enables the Odoo `website` native `cookies_bar` boolean on install and sets it as the default for new websites. `compliance_post_init_cookie_bar`

* **Safe Legal Page Provisioning:** Provisions AGPL-3 compatible legal pages safely via `noupdate="1"` XML records. `compliance_legal_pages_rendering`

    * Privacy Policy Template `compliance_privacy_policy_template`

    * Cookie Policy Template `compliance_cookie_policy_template`

    * Terms of Service Template `compliance_terms_of_service_template`

* **Non-Destructive Mandate:** If, at install time, a page already exists at one of the target URLs (`/privacy`, `/cookie-policy`, `/terms`, `/accessibility`), the module's boilerplate is unpublished to avoid duplication. A page counts as custom when its view key does not start with `compliance.compliance_` and its `website_id` equals the boilerplate's. [@ANCHOR: test_compliance_non_destructive_mandate]
* **Editability Mandate:** Legal pages are standard `website.page` records, allowing administrators to use the Odoo website builder for customization.

## 3. API & Integration
### Standardized Routes
Dependent modules requiring legal links MUST use:
* `/privacy` : Privacy Policy
* `/cookie-policy` : Cookie Policy
* `/terms` : Terms of Service
* `/accessibility` : Accessibility Statement

### Integration Rules
1. **Do Not Build Custom Banners:** Rely entirely on Odoo's native `website.cookies_bar`.
2. **Tracking Scripts:** Any third-party JavaScript tracking MUST hook into the Odoo consent state.
</enforcement_details>

<security_architecture>
## 4. Security & Zero-Sudo
This module adheres to **ADR-0002 (Zero-Sudo)** and **ADR-0005 (Service Account Web Isolation)**, both consolidated in [MASTER_01_SECURITY_ZERO_SUDO.md](../adrs/MASTER_01_SECURITY_ZERO_SUDO.md) (Zero-Sudo: no `.sudo()`; privileged work runs as a dedicated service account, see [zero_sudo.md](zero_sudo.md); Web Isolation: service accounts cannot log in through the web interface).

* **Micro-Privilege Account:** Automated post-install configuration is executed via the `compliance.user_compliance_service` service account.
* **ACLs:** The service account (group `group_compliance_service`) is granted read and write on `website`, read, write and create on `website.page` and `ir.ui.view`, and read-only on `res.groups` and `res.company` (`security/ir.model.access.csv`); it has no delete rights on these models. `compliance_security_acls`

* **Impersonation:** Escalation is handled via `env.with_user(svc_uid)` instead of `.sudo()` for core operations. `compliance_zero_sudo_impersonation`

## 4b. Documents Portal and GDPR Base Contract
* **Compliance Documents:** Dependent modules provision `compliance.document` records (via `noupdate` XML, as `data/compliance_data.xml` does). The public `/compliance` page lists up to 100 active ones.
* **GDPR (General Data Protection Regulation) hooks on `res.users`:** `_execute_gdpr_erasure()` deactivates the account (writing as the `zero_sudo.gdpr_service_internal` service account) [@ANCHOR: compliance_execute_gdpr_erasure]; `_get_gdpr_export_data()` returns an empty dict that overriding modules extend [@ANCHOR: compliance_get_gdpr_export_data]; `_get_gdpr_streamed_keys()` returns an empty dict to which modules add `{key: generator_function}` entries for datasets too large to hold in memory [@ANCHOR: compliance_get_gdpr_streamed_keys]. Every override must call `super()` and merge into, not replace, the returned dict.

## 5. Website-Aware Scope
The module is multi-website aware. When detecting custom pages at target URLs, it only unpublishes a boilerplate page whose `website_id` matches the custom page's (both global, or both the same website); a website-specific custom page does not unpublish the global boilerplate. The check that unpublishes and restores the boilerplate lives in a PostgreSQL function the module's `post_init_hook` creates and runs, so it runs only when the module is installed (Odoo runs the hook only then), not when a page is deleted or the module is updated: after a custom page is removed, the boilerplate returns on the next install, and between installs a site owner re-publishes it manually in Site > Pages. [@ANCHOR: compliance_website_aware_scope]

## 6. Documentation Installation
This module implements a **soft dependency** on documentation providers (`knowledge` or Odoo Enterprise `knowledge`).

* **Mechanism:** Documentation is automatically provisioned during the final registry reload by the central engine (`_bootstrap_knowledge_docs` in `zero_sudo`). `zero_sudo:zero_sudo_doc_installer`
* **Article Title:** "Site Owner's Guide to Regulatory Compliance"

## 7. Verification and Testing
Comprehensive test coverage ensures ongoing compliance:
* **Hook Testing:** `test_hooks.py` verifies `cookies_bar` enforcement and non-destructive page provisioning.
* **Page Integrity:** `test_pages.py` ensures all legal routes are active and contain valid boilerplate content.
* **Security Audit:** `test_security.py` confirms service account configuration and hook idempotency.
* **UI Tours:** `compliance_tour.js` simulates end-to-end user navigation across all legal pages. `test_compliance_ui_tour`
</security_architecture>

## Anchor reference

### `story_automatic_legal_pages` [@ANCHOR: story_automatic_legal_pages]

**What it is.** The user story "Automatic Legal Pages Generation", kept in `compliance/docs/stories/automatic_legal_pages.md` (its anchor there is the story's title). A small-business owner installs the module and gets privacy, cookie, terms and accessibility pages and footer links without writing them.

**What it commits the code to.** The pages at `/privacy`, `/cookie-policy`, `/terms` and `/accessibility` exist and are published after install; an existing custom page at the same URL suppresses the boilerplate; the `/compliance` index lists active documents.

**Verified by.** `test_compliance_pages_presence` (`tests/test_pages.py`), `test_compliance_pages_content`, `test_compliance_non_destructive_mandate` and `test_compliance_ui_tour`.

**See also.** `compliance_legal_pages_rendering`, `compliance_privacy_policy_template`, `journey_compliance_setup`.

### `story_cookie_consent` [@ANCHOR: story_cookie_consent]

**What it is.** The user story "Enforced Cookie Consent" in `compliance/docs/stories/cookie_consent.md`: a visitor sees Odoo's native cookie bar on every website, and optional tracking stays blocked until the visitor accepts.

**What it commits the code to.** `post_init_hook` sets `website.cookies_bar = TRUE` on every existing website (and the module's `website` default makes the bar on for new ones), and the Cookie Policy page is reachable from the banner. Third-party scripts must hook Odoo's consent state; the module does not add its own banner.

**Verified by.** `test_compliance_post_init_cookie_bar` (`tests/test_hooks.py`, `test_02_post_init_hook_cookie_bar`) and the browser tour `test_compliance_ui_tour`.

**See also.** `compliance_post_init_cookie_bar`, `story_automatic_legal_pages`.

### `story_compliance_documentation` [@ANCHOR: story_compliance_documentation]

**What it is.** The user story "Site Owner Documentation" in `compliance/docs/stories/compliance_documentation.md`: an administrator answering a data-protection query finds the article "Site Owner's Guide to Regulatory Compliance" in the Knowledge app and reads how data minimisation, the privacy-friendly view counters and the `/my/privacy` export and erasure work.

**What it commits the code to.** The article comes from `compliance/data/documentation.html`, installed through the manifest's `knowledge_docs` entry by `zero_sudo`'s documentation installer (see `zero_sudo:zero_sudo_doc_installer`). The module has only a soft dependency on `knowledge`: with no Knowledge app the install quietly does nothing.

**Verified by.** The installer tests in `knowledge/tests/test_zero_sudo_doc_installer.py`. TODO: a compliance-side test that the article exists.

### `journey_compliance_setup` [@ANCHOR: journey_compliance_setup]

**What it is.** The end-to-end journey "New Site Compliance Setup" in `compliance/docs/journeys/compliance_setup_journey.md`, from installing the module to a compliant site.

**Steps it covers.** (1) Install runs `post_init_hook` in `compliance/hooks.py`. (2) The native cookie bar is enabled on all websites. (3) Default legal pages are provisioned unless a custom page already exists. (4) The compliance guide is added to Knowledge.

**Verified by.** `test_compliance_post_init_cookie_bar`, `test_compliance_views` (`test_03_views_rendering`), `test_compliance_pages_presence` and the tour `test_compliance_ui_tour`.

**See also.** `compliance_post_init_cookie_bar`, `compliance_legal_pages_rendering`, `story_automatic_legal_pages`.

### `compliance_post_init_cookie_bar` [@ANCHOR: compliance_post_init_cookie_bar]

**Purpose.** The module's install hook forces Odoo's native cookie consent bar on, so optional tracking is blocked until a visitor accepts. It is the function `post_init_hook(env)` in `compliance/hooks.py`.

**Signature.** `post_init_hook(env)`; `env` is the install-time environment; returns nothing.

**Behaviour.** It switches to the `compliance.user_compliance_service` account, creates (or replaces) a PostgreSQL function `compliance_enforce_protection()` and calls it. That function sets `website.cookies_bar = TRUE` on every website, unpublishes boilerplate pages that are shadowed by a custom page and re-publishes boilerplate that is no longer shadowed. It then runs `_bootstrap_knowledge_docs()` and invalidates the ORM cache for `website.cookies_bar` and `website.page.is_published`.

**Errors.** Any SQL failure is logged and re-raised after a savepoint rollback, so the install fails loudly rather than leaving the banner off.

**See also.** Tests `test_compliance_post_init_cookie_bar`, `test_compliance_ui_tour`; `compliance_zero_sudo_impersonation`.

### `compliance_legal_pages_rendering` [@ANCHOR: compliance_legal_pages_rendering]

**Purpose.** The legal pages are ordinary website pages shipped as data, so they render at fixed URLs and an administrator can edit them in the website builder.

**Where.** `compliance/data/legal_pages_data.xml` holds a QWeb `template` for each page and a `website.page` record binding it to a URL (`/privacy`, `/cookie-policy`, `/terms`, `/accessibility`), `is_published` true and a name (required, because a nameless page can crash Odoo's site search). The records sit in `noupdate="1"`, so a module upgrade does not overwrite an owner's edits; `migrations/1.1/pre-migration.py` clears stale `noupdate` flags on older installs.

**Special concerns.** The shadowing check in `compliance_enforce_protection()` decides which boilerplate stays published (`compliance_post_init_cookie_bar`).

**See also.** `compliance_privacy_policy_template`, `compliance_cookie_policy_template`, `compliance_terms_of_service_template`; tests `test_compliance_views`, `test_compliance_pages_presence`.

### `compliance_privacy_policy_template` [@ANCHOR: compliance_privacy_policy_template]

**Purpose.** The boilerplate Privacy Policy page, served at `/privacy`, written to describe the data practices of the platform's other open-source modules (view counters, the `/my/privacy` export and erasure, abuse reports that hide the reporter).

**Where.** QWeb template `compliance.compliance_privacy_policy_template` (view key starts `compliance.compliance_`, which is how the hook recognises boilerplate) and the `website.page` record named "Privacy Policy" in `compliance/data/legal_pages_data.xml`.

**Special concerns.** A site may replace the wording by creating its own page at `/privacy`; the hook then unpublishes this one. The text is generic and not legal advice.

**Verified by.** `test_compliance_pages_presence`, `test_compliance_pages_content` (`tests/test_pages.py`) and the tour `test_compliance_ui_tour`, which checks the page has at least three section headings.

### `compliance_cookie_policy_template` [@ANCHOR: compliance_cookie_policy_template]

**Purpose.** The boilerplate Cookie Policy page at `/cookie-policy`, linked from the cookie banner and the footer. It explains which cookies the platform sets and that optional ones wait for consent.

**Where.** QWeb template `compliance.compliance_cookie_policy_template` and the "Cookie Policy" `website.page` record in `compliance/data/legal_pages_data.xml`; published by default, in `noupdate="1"`.

**Special concerns.** Same shadowing rule as the privacy policy: an owner's own `/cookie-policy` page wins and the boilerplate is unpublished by `compliance_enforce_protection()`.

**Verified by.** `test_compliance_pages_presence`, `test_compliance_pages_content` and `test_compliance_ui_tour` (footer link `.o_tour_footer_cookie_policy`).

**See also.** `compliance_post_init_cookie_bar`, `compliance_legal_pages_rendering`.

### `compliance_terms_of_service_template` [@ANCHOR: compliance_terms_of_service_template]

**Purpose.** The boilerplate Terms of Service page at `/terms`, under the AGPL-compatible wording the module supplies.

**Where.** QWeb template `compliance.compliance_terms_of_service_template` and the "Terms of Service" `website.page` record in `compliance/data/legal_pages_data.xml`, published by default and in `noupdate="1"` so edits survive upgrades.

**Special concerns.** An owner's own page at `/terms` for the same website suppresses it (the website id must match, or both be global). Other modules should link to `/terms`, not copy the text.

**Verified by.** `test_compliance_pages_presence`, `test_compliance_pages_content`, `test_compliance_ui_tour` (footer link `.o_tour_footer_terms`).

**See also.** `compliance_legal_pages_rendering`.

### `compliance_security_acls` [@ANCHOR: compliance_security_acls]

**Purpose.** The access rules for the `compliance.user_compliance_service` account that runs the install hook, kept to the minimum the hook needs.

**Where.** `compliance/security/ir.model.access.csv`, group `group_compliance_service`.

| model | read | write | create | delete |
|---|---|---|---|---|
| `website` | yes | yes | no | no |
| `website.page` | yes | yes | yes | no |
| `ir.ui.view` | yes | yes | yes | no |
| `res.groups` | yes | no | no | no |
| `res.company` | yes | no | no | no |

**Other rows.** The same CSV gives the GDPR service group (`zero_sudo.group_gdpr_service`) read and write on `res.users` for erasure, lets public, portal and internal users read `compliance.document`, and gives the compliance service group full rights on that model.

**Verified by.** `test_service_account` in `tests/test_security.py` (account active, a service account, in the group).

### `compliance_zero_sudo_impersonation` [@ANCHOR: compliance_zero_sudo_impersonation]

**Purpose.** Shows how the install hook gets the privileges it needs without `.sudo()`, as the zero-sudo rule requires.

**Behaviour.** In `post_init_hook` the code calls `env["zero_sudo.security.utils"]._get_service_uid("compliance.user_compliance_service")` and builds `env_svc = env(user=svc_uid)`. All DDL, the call to `compliance_enforce_protection()`, `_bootstrap_knowledge_docs()` and the cache invalidation run in that environment, with the ACLs of `compliance_security_acls`. `_get_service_uid` refuses an account that is not a real service account.

**Special concerns.** Never run the hook as the superuser; if the service account is missing, the install fails rather than falling back to privileged access.

**Verified by.** `test_service_account` (`tests/test_security.py`) and `test_compliance_post_init_cookie_bar`.

### `zero_sudo:zero_sudo_doc_installer` [@ANCHOR: zero_sudo:zero_sudo_doc_installer]

**Purpose.** The central installer that turns each installed module's `knowledge_docs` manifest list into Knowledge articles. `compliance` uses it to publish "Site Owner's Guide to Regulatory Compliance"; the code is `_bootstrap_knowledge_docs` in `zero_sudo/models/ir_module_module.py` (the code anchor is in `zero_sudo/__init__.py`).

**Signature.** `ir.module.module._bootstrap_knowledge_docs(self)`; `@api.model`; no arguments, returns nothing. It also runs from `_register_hook`, on every registry load.

**Behaviour.** It resolves the knowledge service account (falling back to `zero_sudo.odoo_facility_service_internal`), reads manifests of installed modules in batches of 1000, loads stored content hashes and article ids from `zero_sudo.kv` in one query, and calls `_install_single_doc` per document. With no `knowledge.article` model or no service account it returns quietly (soft dependency).

**See also.** Tests in `knowledge/tests/test_zero_sudo_doc_installer.py`; `story_compliance_documentation`.

### `test_compliance_ui_tour` [@ANCHOR: test_compliance_ui_tour]

**Kind.** Browser tour test `test_compliance_tour` in `compliance/tests/test_ui_tours.py`, driving the JS tour `compliance_tour` in `compliance/static/tests/tours`.

**What it checks.** It opens `/en_US/privacy?debug=1`, dismisses the cookie bar with the shared `TourUtils.dismissCookiesBar()`, checks the page is a privacy policy with at least three `h3` sections, then checks that the footer links for privacy, cookie policy, terms, accessibility and `/my/privacy` exist. The footer links are `.o_tour_footer_*` classes.

**Failure meaning.** A legal page or footer link is missing, or the cookie bar blocks the page.

**Cost.** Slow (headless browser). Run with `test.py --wait -u compliance`.

**See also.** `compliance_post_init_cookie_bar`, `compliance_privacy_policy_template`, `story_cookie_consent`.
