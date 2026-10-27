# Knowledge (`knowledge`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

The **Knowledge** is a powerful, free, and open-source replacement for Odoo's Enterprise Knowledge app (its Odoo menu and its website section are both called **Manuals**). It provides a complete documentation system that allows you to write, organize, and publish user manuals, standard operating procedures (SOPs), and help guides directly inside Odoo Community.

This module was designed with **API Interoperability** in mind. It uses the exact same database structure (`knowledge.article`) as the Enterprise version, meaning other modules can install their own instruction manuals here without any modifications, and you can upgrade to Enterprise later without losing your data.

## 🌟 Key Features

*   **Hierarchical Organization:** Organize your articles into a tree structure with parent and child articles, making it easy to navigate complex documentation.
*   **Enterprise Compatibility:** Fully supports XML data files meant for the Enterprise Knowledge app.
*   **Powerful Rich Text Editor:** Leverage Odoo's native editor to create beautiful guides with images, tables, and advanced formatting.
*   **Instant Public Publishing:** One-click publishing pushes your manuals to the public website at `/manual`. The system automatically builds a recursive sidebar and breadcrumbs for visitors.
*   **Granular Access Control:** Keep your internal notes private, share drafts with specific team members, or share guides with customers via the portal.
*   **Dynamic Table of Contents (TOC):** Automatically generates a sticky TOC on the frontend based on your article's headings (H2 and H3).
*   **Multi-Tenant Isolation:** Respects Odoo's multi-website and multi-company architectures. Company assignment is enforced by record rules on every read; website assignment filters the sidebar, search results and by-name links (see Security below).
*   **Zero-Sudo Security:** Built on a secure "Zero-Sudo" foundation, using micro-privilege service accounts for automated tasks. "Zero-Sudo" refers to the `zero_sudo` module's rule that code never calls Odoo's `.sudo()` superuser escalation; work that needs more privilege than the visitor has runs as a dedicated service-account user that holds only the narrow rights that task needs (here, the "Knowledge System" user).

## 🛠️ Installation

1.  Place the `knowledge` folder into your Odoo `addons` directory.
2.  Restart your Odoo service.
3.  In Odoo, activate **Developer Mode**, navigate to **Apps**, and click **Update Apps List**.
4.  Search for **Knowledge** and click **Install**.

## 📖 How to Use It

### Creating and Managing Articles
1.  Open the **Manuals** app from the main Odoo menu.
2.  Click **New** to create a new article. Creating, editing and deleting articles requires the **Manual Administrator** group; other staff and portal users have read-only access.
3.  **Hierarchy:** Use the **Parent Article** field to nest your article inside a folder or under another guide.
4.  **Permissions:** Set the **Internal Permission** to control who can see it:
    *   **Read Only:** Visible to all staff. Portal users see it on the website only once it is published, or if they are a Shared Member.
    *   **Read & Write:** Intended as a collaborative mode where all staff can edit. In this release ordinary staff have read-only access to articles, so it currently behaves like Read Only for everyone except Manual Administrators.
    *   **No Access:** Private—only visible to you, to Manual Administrators, and to people you explicitly add in the **Shared Members** field (shown on the form only when this permission is selected).
5.  **Multi-Tenant:** Select a specific **Website** or **Company** to limit the article's visibility to that context.
6.  **Publishing:** When ready, click the **Is Published** button in the top right to make the guide visible to the general public at `/manual`.

### Reading on the Web
*   Navigate to `/manual` on your website.
*   Use the **Sidebar** to browse the hierarchy.
*   Use the **Search Bar** to find specific topics across all accessible articles.
*   Use the **Table of Contents** on the right side of articles to jump between sections (shown on wide screens, for articles that contain H2 or H3 headings).

## ⚖️ Legal Note

This module is a clean-room implementation. No code, logic, or proprietary designs from Odoo Enterprise were used. We have matched the database schema solely for API Interoperability and data portability.

---

# Technical Documentation

<system_role>
**Context:** Technical documentation for LLMs, Developers, and Integrators.
This module provides a hierarchical documentation system. It is designed to be fully compatible with the `knowledge.article` model from Odoo Enterprise.
</system_role>

<architecture>
## 1. Architecture
A clean-room, 100% drop-in API replacement for the proprietary Odoo Enterprise Knowledge module (`knowledge.article`).
Inherits from `mail.thread`, `mail.activity.mixin`, `website.published.mixin`, `website.multi.mixin`, and `website.searchable.mixin` (the last one lets the website's own site-wide search return articles).

## 2. Interoperability
*   **Fields Supported:** `name`, `body` (HTML), `parent_id`, `sequence`, `is_published`, `icon`, `active`, `internal_permission`, `member_ids`.
*   Supports automated doc installation via the `knowledge_docs` manifest key and the `zero_sudo` bootstrap facility (`ir.module.module._bootstrap_knowledge_docs()`, which creates or updates one article per manifest entry when the file's content changes). An installed doc is unpublished and readable only by Manual Administrators (`internal_permission = 'none'`) unless its manifest entry sets `"public": True` (or `"audience": "member"`) and a `"help_topic"`, the Help index heading; stories, journeys and runbooks directories, README files and "Story:"/"Journey:" titles are never member manuals even then.
</architecture>

<features>
## 3. Core Features & Logic
*   **Article Feedback:** Atomic helpfulness increments: a single SQL `UPDATE ... SET helpful_count = COALESCE(helpful_count, 0) + 1` (or `unhelpful_count`) executed on the request's database cursor after a read-access check on the article, so concurrent votes cannot overwrite each other. The controller also resolves the Knowledge service account's environment, but the SQL statement itself is not subject to Odoo access rules. (`data/procedures.xml` also installs a `knowledge_increment_helpful()` Postgres function, but the controller does not call it.) `[@ANCHOR: controller_manual_feedback]`.

*   **Search Engine:** Case-insensitive substring (`ilike`) search over title and body, with multi-tenant filtering `[@ANCHOR: controller_manual_search]`.

*   **URL Resolution:** Dynamic slug generation including ID prefix `[@ANCHOR: manual_compute_website_url]`.

*   **Hierarchy Integrity:** Recursive cycle detection using `_has_cycle()` `[@ANCHOR: manual_check_hierarchy]`.

*   **Recursive Breadcrumbs:** Path computation from root to current node; archived ancestors and ancestors the viewer cannot read are left out `[@ANCHOR: manual_compute_breadcrumbs]`.

*   **Reading Time Calculation:** Automatic estimation of reading time based on word count `[@ANCHOR: manual_compute_reading_time]`.
*   **Author Attribution:** Automatically identifies and displays the article author based on the last editor.
*   **Dynamic Table of Contents:** The page script `manual_toc.js` builds the table of contents in the browser from the article's headings `[@ANCHOR: manual_toc_logic]`.
*   **Automated Documentation Installation:** `_bootstrap_knowledge_docs` (from `zero_sudo`) installs each installed module's `knowledge_docs` manifest entries as articles `[@ANCHOR: manual_doc_auto_install]` `[@ANCHOR: manual_doc_injection]`. It does nothing when no provider of `knowledge.article` is installed, so the dependency is soft: either this module or Odoo Enterprise's Knowledge app can provide the model.
*   **Optimized Sidebar Search:** Combined ORM domain to fetch all root articles in a single database round-trip `[@ANCHOR: manual_sidebar_search_optimization]`.

## 4. Detailed Method Reference

**`models/knowledge_article.py`**
*   `_compute_author_id()`: Determines the author based on `write_uid` or `create_uid`.
*   `_compute_breadcrumb_article_ids()`: Uses a recursive CTE (SQL common table expression) to compute the path from root to current node, then re-reads the ancestor ids through an ORM `search()` so archived or unreadable ancestors are dropped instead of displayed or raising an access error.
*   `_compute_body_snippet()`: Converts the HTML body to plaintext and truncates it for use in list views and search previews.
*   `_compute_reading_time()`: Estimates reading time based on a 200 words-per-minute average.
*   `_check_hierarchy()`: Constrains the `parent_id` to prevent circular references by calling `_has_cycle()`.
*   `copy(default=None)`: Overrides the built-in copy method to append "(copy)" to the duplicated article's name.
*   `_compute_website_url()`: Generates an SEO-friendly URL slug using the article ID and a sanitized title.

**`controllers/main.py`**
*   `_compile_markdown(html_body, article_id, write_date)` `[@ANCHOR: manual_compile_markdown]`: Heuristically detects markdown signatures in the text. If found, compiles it to HTML. Results are cached in an LRU cache. Compilation is skipped when the body already contains structural HTML (headings, lists, tables, images, `div`s and similar), because that body is already real HTML; the compiled output is passed through Odoo's HTML sanitizer; if the markdown parser fails, the original HTML body is served.
*   `_get_sidebar_articles()`: Fetches root articles for the sidebar, optimized to run in a single combined domain query.
*   `manual_article_view(article_slug, **kwargs)`: Primary frontend controller. Enforces read access, performs canonical redirects, compiles markdown, and renders the view.
*   `manual_search(search, **kwargs)`: Controller for case-insensitive substring search on title and body across accessible articles.
*   `manual_article_by_name(name, **kwargs)`: Convenience route (`/manual/by_name/<string:name>`) to find an article by exact name and redirect to it.
*   `manual_feedback(article_id, is_helpful, website_feedback_honeypot, **kwargs)`: Handles helpful/unhelpful votes. Implements honeypots, one vote per article per browser session, a read-access check on the article, and zero-sudo atomic increments.
</features>

<security>
## 5. Security and Access Rights
Every record rule below is also limited to the user's allowed companies.
*   **Public:** `is_published=True` (record rule). The website match (`website_id` empty or equal to the current website) is applied by the sidebar, search and by-name queries in `controllers/main.py`, not by a record rule, so a direct `/manual/<id>-<slug>` URL is not website-filtered.
*   **Portal/Internal:** read only: `internal_permission != 'none'`, or `is_published=True`, or member of `member_ids`, or `create_uid`. On the website, users outside `base.group_user` additionally need `is_published=True` or membership.
*   **Admin:** Full CRUD. `knowledge.group_manual_manager` (Manual Administrator) is the only interactive group whose access rights allow create, write or delete.
*   **Service Account:** `knowledge.user_knowledge_service_account` for background operations: installing `knowledge_docs` articles (the feedback controller also resolves this account before its SQL increment).
</security>

<dependencies>
## 6. External Dependencies
*   `markdown`
</dependencies>

---

<stories_and_journeys>
## 7. Architectural Stories & Journeys

For detailed narratives and end-to-end workflows, refer to the following:

### Stories
* [Article Feedback](hams_shared/docs/stories/feedback.md) `[@ANCHOR: story_manual_feedback]`

* [Article Hierarchy Integrity](hams_shared/docs/stories/hierarchy.md) `[@ANCHOR: story_manual_hierarchy]`

* [Automated Documentation Installation](hams_shared/docs/stories/doc_installation.md) `[@ANCHOR: story_manual_doc_installation]`

* [Backend Management Views](hams_shared/docs/stories/backend_views.md) `[@ANCHOR: story_manual_backend_views]`

* [Dynamic Table of Contents](hams_shared/docs/stories/toc.md) `[@ANCHOR: story_manual_toc]`

* [Dynamic URL Generation](hams_shared/docs/stories/url_generation.md) `[@ANCHOR: story_manual_url_generation]`

* [Searching the Manual](hams_shared/docs/stories/search.md) `[@ANCHOR: story_manual_search]`

* [Viewing Manual Articles](hams_shared/docs/stories/article_view.md) `[@ANCHOR: story_article_view]`

### Journeys
* [Administrator Managing Articles](hams_shared/docs/journeys/admin_managing_articles.md) `[@ANCHOR: journey_admin_managing]`

* [Developer Integrating Documentation](hams_shared/docs/journeys/developer_doc_integration.md) `[@ANCHOR: journey_developer_integration]`

* [User Browsing the Manual](hams_shared/docs/journeys/user_browsing_journey.md) `[@ANCHOR: journey_user_browsing]`
</stories_and_journeys>
