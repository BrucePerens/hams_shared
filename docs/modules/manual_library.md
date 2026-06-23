# Knowledge (`knowledge`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

This is a free, open-source replacement for Odoo's Enterprise Knowledge app. It lets you write, organize, and publish documentation and user manuals directly inside Odoo Community.

Because it uses the exact same database structure (`knowledge.article`) as the Enterprise version, other modules can easily install their own instruction manuals here without breaking.

**Open Source Rule:** We built this for the open-source community. It runs perfectly on its own and does not rely on any proprietary code.

## 🌟 Key Features

* **Nested Folders:** Organize your articles in a tree (parent/child) so they are easy to navigate.
* **Enterprise Compatible:** You can load XML data files meant for the Enterprise Knowledge app, and they will work perfectly here.
* **Rich Text Editor:** Use Odoo's standard editor to write guides, insert images, and format text.
* **Public Web Portal:** Click "Publish" to instantly push your manuals to the public website (`/manual`). The system automatically builds a handy sidebar menu for visitors.
* **Access Control:** Keep private admin notes hidden, share drafts with logged-in coworkers or portal customers, or publish finalized guides to the public.
* **Multi-Website Support:** Restrict articles to specific websites or share them across all websites in a multi-website Odoo instance.
* **Smart Navigation:** Recursive breadcrumbs and a dynamic Table of Contents (TOC) help users navigate deep documentation structures effortlessly.

## 🛠️ Installation

1. Drop the `knowledge` folder into your Odoo `addons` directory.
2. Restart your Odoo server.
3. Turn on Developer Mode, go to **Apps**, and click **Update Apps List**.
4. Search for `Knowledge` and click **Install**.

## 📖 How to Use It

### Writing Articles
1. Click the **Manuals** app in the main Odoo menu.
2. Click **New**.
3. If you want this article to sit inside a folder, pick a **Parent Article**.
4. When you're ready to share it with the world, hit the **Is Published** button at the top.

### Reading Articles on the Web
* Go to `/manual` on your website to see the public knowledge base.
* We included a search bar and an automatically generated Table of Contents that reads your headers so users can jump around long documents easily.

## ⚖️ Legal Note

We built this from scratch. We did not copy any code, logic, or proprietary designs from Odoo Enterprise. We just matched the database field names so the two systems are perfectly compatible (known legally as API Interoperability).

---

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for LLMs and Integrators.
This module provides a hierarchical documentation system. It is designed to be fully compatible with the `knowledge.article` model from Odoo Enterprise.
</system_role>

<architecture>
## 1. Architecture
A clean-room, 100% drop-in API replacement for the proprietary Odoo Enterprise Knowledge module (`knowledge.article`).
Uses a standard parent-child relationship for hierarchy. Inherits from `mail.thread`, `mail.activity.mixin`, and `website.published.mixin`.

## 2. Interoperability
* Dependent modules inject documentation using standard XML records targeting `model="knowledge.article"`.
* **Fields Supported:** `name`, `body` (HTML), `parent_id`, `sequence`, `is_published`, `icon`, `active`, `internal_permission`, `member_ids`.
* If the system is upgraded to Enterprise, the table structure allows perfect data retention.
</architecture>

<features>
## 3. Core Features & Logic
* **User Feedback:** Handles user submissions of helpful/not-helpful article ratings via the feedback controller `[@ANCHOR: controller_manual_feedback]`.
* **Search Integration:** Supports live querying of article contents via the search controller `[@ANCHOR: controller_manual_search]`.
* **URL Resolution:** Computes the public website URL path for articles dynamically based on their hierarchy `[@ANCHOR: manual_compute_website_url]`.
* **Structural Integrity:** Strictly enforces parent-child hierarchy checks to prevent recursive or invalid tree structures using `_has_cycle()` `[@ANCHOR: manual_check_hierarchy]`.
* **Dynamic TOC:** Automatically parses article HTML on the frontend to generate a dynamic Table of Contents `[@ANCHOR: manual_toc_logic]`.
* **Automated Documentation Installation:** Utilizes the central `_bootstrap_knowledge_docs` facility from the `zero_sudo` module to automatically discover and install documentation for all installed modules via the `knowledge_docs` manifest key. This supports soft dependencies on `knowledge.article` or `knowledge.article` `[@ANCHOR: manual_doc_auto_install]`. `[@ANCHOR: manual_doc_injection]`
* **Zero-Sudo Execution:** All automated operations and frontend feedback increments are performed using the `knowledge.user_knowledge_service_account` micro-privilege account.
* **Multi-Website Isolation:** Articles are isolated by `website_id`. Controllers and sidebar logic strictly filter content to the current website context.
* **Hierarchical Breadcrumbs:** Provides a recursive breadcrumb trail in the frontend view to maintain user context within deep folder structures.
</features>

<security>
## 4. Security and Access Rights
* **Public Users:** Can only read articles where `is_published` is True AND (`website_id` is False OR matches current website).
* **Internal Users (`base.group_user`) and Portal Users (`base.group_portal`):**
    - Can read articles if `internal_permission` is not 'none' OR `is_published` is True.
    - Can read their own articles or those shared with them via `member_ids`.
    - Access is further restricted by `website_id` (must be False or match current website).
* **Internal Users (`base.group_user`) Only:**
    - Can edit articles if `internal_permission` is 'write' or if they are the owner/member.
* **Manual Administrators (`group_manual_manager`):** Full CRUD access to all articles.
* **Service Account:** used for atomic feedback increments and automated doc installation.
</security>

---

<stories_and_journeys>
## 5. Architectural Stories & Journeys

For detailed narratives and end-to-end workflows, refer to the following:

### Stories
* [Article Feedback](docs/stories/feedback.md) `[@ANCHOR: story_manual_feedback]`
* [Article Hierarchy Integrity](docs/stories/hierarchy.md) `[@ANCHOR: story_manual_hierarchy]`
* [Automated Documentation Installation](docs/stories/doc_installation.md) `[@ANCHOR: story_manual_doc_installation]`
* [Backend Management Views](docs/stories/backend_views.md) `[@ANCHOR: story_manual_backend_views]`
* [Dynamic Table of Contents](docs/stories/toc.md) `[@ANCHOR: story_manual_toc]`
* [Dynamic URL Generation](docs/stories/url_generation.md) `[@ANCHOR: story_manual_url_generation]`
* [Searching the Manual](docs/stories/search.md) `[@ANCHOR: story_manual_search]`
* [Viewing Manual Articles](docs/stories/article_view.md) `[@ANCHOR: story_article_view]`

### Journeys
* [Administrator Managing Articles](docs/journeys/admin_managing_articles.md) `[@ANCHOR: journey_admin_managing]`
* [Developer Integrating Documentation](docs/journeys/developer_doc_integration.md) `[@ANCHOR: journey_developer_integration]`
* [User Browsing the Manual](docs/journeys/user_browsing_journey.md) `[@ANCHOR: journey_user_browsing]`
</stories_and_journeys>
