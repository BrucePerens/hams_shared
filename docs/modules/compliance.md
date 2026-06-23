# Global Compliance & Privacy (`compliance`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

This module automatically handles the annoying parts of running a legal website. It makes sure your Odoo instance complies with GDPR, CCPA, and ePrivacy rules without you having to configure anything manually.

## 🌟 What It Does

* **Turns on the Cookie Banner:** As soon as you install this, it flips the switch to turn on Odoo's native Cookie Consent Bar across all your websites. It also ensures that any **new** websites created later have this enabled by default. This stops optional tracking scripts until the user clicks "Accept."
* **Writes Your Legal Pages:** It automatically creates standard, editable pages for your Privacy Policy (`/privacy`), Cookie Policy (`/cookie-policy`), and Terms of Service (`/terms`).
* **Doesn't Break Your Edits:** If you've already written a privacy policy at `/privacy`, the module detects it and leaves yours alone. If you edit the pages it creates, it won't overwrite your work when you update the module.

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
* [Automatic Legal Pages Generation](./docs/stories/automatic_legal_pages.md) `[@ANCHOR: story_automatic_legal_pages]`
* [Enforced Cookie Consent](./docs/stories/cookie_consent.md) `[@ANCHOR: story_cookie_consent]`
* [Site Owner Documentation](./docs/stories/compliance_documentation.md) `[@ANCHOR: story_compliance_documentation]`

#### Journeys
* [Compliance Setup Journey](./docs/journeys/compliance_setup_journey.md) `[@ANCHOR: journey_compliance_setup]`

## 2. Enforcement Details
* **Automated Cookie Consent:** Programmatically enables the Odoo `website` native `cookies_bar` boolean on install and sets it as the default for new websites. `[@ANCHOR: compliance_post_init_cookie_bar]`
* **Safe Legal Page Provisioning:** Provisions AGPL-3 compatible legal pages safely via `noupdate="1"` XML records. `[@ANCHOR: compliance_legal_pages_rendering]`
    * Privacy Policy Template `[@ANCHOR: compliance_privacy_policy_template]`
    * Cookie Policy Template `[@ANCHOR: compliance_cookie_policy_template]`
    * Terms of Service Template `[@ANCHOR: compliance_terms_of_service_template]`
* **Non-Destructive Mandate:** If a page already exists at one of the target URLs, the module's boilerplate is unpublished to avoid duplication. `[@ANCHOR: test_compliance_non_destructive_mandate]`
* **Editability Mandate:** Legal pages are standard `website.page` records, allowing administrators to use the Odoo website builder for customization.

## 3. API & Integration
### Standardized Routes
Dependent modules requiring legal links MUST use:
* `/privacy` : Privacy Policy
* `/cookie-policy` : Cookie Policy
* `/terms` : Terms of Service

### Integration Rules
1. **Do Not Build Custom Banners:** Rely entirely on Odoo's native `website.cookies_bar`.
2. **Tracking Scripts:** Any third-party JavaScript tracking MUST hook into the Odoo consent state.
</enforcement_details>

<security_architecture>
## 4. Security & Zero-Sudo
This module adheres to **ADR-0002 (Zero-Sudo)** and **ADR-0005 (Service Account Web Isolation)**.

* **Micro-Privilege Account:** Automated post-install configuration is executed via the `compliance.user_compliance_service` service account.
* **ACLs:** The service account is granted minimal read/write access to `website`, `website.page`, and `ir.ui.view` models. `[@ANCHOR: compliance_security_acls]`
* **Impersonation:** Escalation is handled via `env.with_user(svc_uid)` instead of `.sudo()` for core operations. `[@ANCHOR: compliance_zero_sudo_impersonation]`

## 5. Website-Aware Scope
The module is multi-website aware. When detecting custom pages at target URLs, it only unpublishes the boilerplate for the specific website scope (or global scope) where the custom page is found. If a custom page is removed, the boilerplate is automatically restored. `[@ANCHOR: compliance_website_aware_scope]`

## 6. Documentation Installation
This module implements a **soft dependency** on documentation providers (`knowledge` or Odoo Enterprise `knowledge`).

* **Mechanism:** Documentation is automatically provisioned during the final registry reload by the central engine (`_bootstrap_knowledge_docs` in `zero_sudo`). `[@ANCHOR: zero_sudo:zero_sudo_doc_installer]`
* **Article Title:** "Site Owner's Guide to Regulatory Compliance"

## 7. Verification and Testing
Comprehensive test coverage ensures ongoing compliance:
* **Hook Testing:** `test_hooks.py` verifies `cookies_bar` enforcement and non-destructive page provisioning.
* **Page Integrity:** `test_pages.py` ensures all legal routes are active and contain valid boilerplate content.
* **Security Audit:** `test_security.py` confirms service account configuration and hook idempotency.
* **UI Tours:** `compliance_tour.js` simulates end-to-end user navigation across all legal pages. `[@ANCHOR: test_compliance_ui_tour]`
</security_architecture>
