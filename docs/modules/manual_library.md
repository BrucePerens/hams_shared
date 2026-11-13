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
*   **Article Feedback:** Atomic helpfulness increments: a single SQL `UPDATE ... SET helpful_count = COALESCE(helpful_count, 0) + 1` (or `unhelpful_count`) executed on the request's database cursor after a read-access check on the article, so concurrent votes cannot overwrite each other. The controller also resolves the Knowledge service account's environment, but the SQL statement itself is not subject to Odoo access rules. (`data/procedures.xml` also installs a `knowledge_increment_helpful()` Postgres function, but the controller does not call it.) `controller_manual_feedback`.

*   **Search Engine:** Case-insensitive substring (`ilike`) search over title and body, with multi-tenant filtering `controller_manual_search`.

*   **URL Resolution:** Dynamic slug generation including ID prefix `manual_compute_website_url`.

*   **Hierarchy Integrity:** Recursive cycle detection using `_has_cycle()` `manual_check_hierarchy`.

*   **Recursive Breadcrumbs:** Path computation from root to current node; archived ancestors and ancestors the viewer cannot read are left out `manual_compute_breadcrumbs`.

*   **Reading Time Calculation:** Automatic estimation of reading time based on word count `manual_compute_reading_time`.
*   **Author Attribution:** Automatically identifies and displays the article author based on the last editor.
*   **Dynamic Table of Contents:** The page script `manual_toc.js` builds the table of contents in the browser from the article's headings `manual_toc_logic`.
*   **Automated Documentation Installation:** `_bootstrap_knowledge_docs` (from `zero_sudo`) installs each installed module's `knowledge_docs` manifest entries as articles `manual_doc_auto_install` `manual_doc_injection`. It does nothing when no provider of `knowledge.article` is installed, so the dependency is soft: either this module or Odoo Enterprise's Knowledge app can provide the model.
*   **Optimized Sidebar Search:** Combined ORM domain to fetch all root articles in a single database round-trip `manual_sidebar_search_optimization`.

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
*   `_compile_markdown(html_body, article_id, write_date)` `manual_compile_markdown`: Heuristically detects markdown signatures in the text. If found, compiles it to HTML. Results are cached in an LRU cache. Compilation is skipped when the body already contains structural HTML (headings, lists, tables, images, `div`s and similar), because that body is already real HTML; the compiled output is passed through Odoo's HTML sanitizer; if the markdown parser fails, the original HTML body is served.
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
* [Article Feedback](hams_shared/docs/stories/feedback.md) `story_manual_feedback`

* [Article Hierarchy Integrity](hams_shared/docs/stories/hierarchy.md) `story_manual_hierarchy`

* [Automated Documentation Installation](hams_shared/docs/stories/doc_installation.md) `story_manual_doc_installation`

* [Backend Management Views](hams_shared/docs/stories/backend_views.md) `story_manual_backend_views`

* [Dynamic Table of Contents](hams_shared/docs/stories/toc.md) `story_manual_toc`

* [Dynamic URL Generation](hams_shared/docs/stories/url_generation.md) `story_manual_url_generation`

* [Searching the Manual](hams_shared/docs/stories/search.md) `story_manual_search`

* [Viewing Manual Articles](hams_shared/docs/stories/article_view.md) `story_article_view`

### Journeys
* [Administrator Managing Articles](hams_shared/docs/journeys/admin_managing_articles.md) `journey_admin_managing`

* [Developer Integrating Documentation](hams_shared/docs/journeys/developer_doc_integration.md) `journey_developer_integration`

* [User Browsing the Manual](hams_shared/docs/journeys/user_browsing_journey.md) `journey_user_browsing`
</stories_and_journeys>

---

## Anchor reference

One entry per anchor, written from the code in the `knowledge` module (`models/knowledge_article.py`, `controllers/main.py`, `static/src/js/manual_toc.js`) and the installer in `zero_sudo/models/ir_module_module.py`. Test anchors carry the same ids in `knowledge/tests/`; the tours are `test_tour_manual_search`, `test_tour_manual_feedback` and `test_tour_manual_toc`.

### `controller_manual_feedback` [@ANCHOR: knowledge:controller_manual_feedback]
Purpose: record a reader's "Helpful" or "Not helpful" vote. `manual_feedback(article_id, is_helpful, website_feedback_honeypot=None, **kwargs)` on route `/manual/feedback` (POST, public, website).

| Argument | Type | Meaning |
|---|---|---|
| `article_id` | str or int | `knowledge.article` id, converted with `int()`. |
| `is_helpful` | str | `"1"` increments `helpful_count`; any other value increments `unhelpful_count`. |
| `website_feedback_honeypot` | str or None | Hidden form field; a non-empty value means a bot, and the vote is dropped. |

Behaviour: the redirect target is the referrer's path only (a path not starting with `/` becomes `/manual`), which prevents open redirects. A second vote on the same article in one browser session is ignored (session key `feedback_submitted_<id>`). The article is read first with the caller's own rights (`check_access("read")`); the increment is then a single SQL `UPDATE ... SET count = COALESCE(count, 0) + 1` on the cursor of the `knowledge.user_knowledge_service_account` environment, so concurrent votes cannot overwrite each other. A bad id or an access error is logged and ignored, which gives an attacker no way to probe for articles. Returns a redirect to the referrer path with `feedback_submitted=1` appended. Tests: `test_tour_manual_feedback`, `tests/test_features.py`.

### `controller_manual_search` [@ANCHOR: knowledge:controller_manual_search]
Purpose: the search page of the manual. `manual_search(search="", **kwargs)` on route `/manual/search` (GET, public, website). It builds the Help domain (`_help_domain()`, which limits results to member user manuals the reader may see, for every reader including administrators) and, when `search` is non-empty, adds `name ilike search OR body ilike search`: a case-insensitive substring match, not a ranked index. At most 1000 articles are returned, rendered with `knowledge.search_results_template` together with the sidebar context. Returns an HTTP response; never raises for an empty search (it lists all visible articles). Record rules still apply to the search. Read-only. Tests: `test_tour_manual_search`; stories `story_manual_search`, `journey_user_browsing`.

### `manual_compute_website_url` [@ANCHOR: knowledge:manual_compute_website_url]
Purpose: the `website_url` of an article, `/manual/<id>-<slug>`. `knowledge.article._compute_website_url()` is `@api.depends("name")` and extends the `website.published.mixin` method. For a saved record it lowercases the name, folds accents to ASCII (`unicodedata.normalize("NFKD")`), replaces every run of characters outside `a-z0-9` with `-` and strips leading and trailing dashes; an unsaved record (no id) gets `""`. No arguments, no return value (it sets the field). Only the id matters for lookup, so a rename keeps old links working through a canonical redirect in the view controller. Pure computation, no access to other records. Test: `test_manual_url_slug_generation`. See `story_manual_url_generation`.

### `manual_check_hierarchy` [@ANCHOR: knowledge:manual_check_hierarchy]
Purpose: keep the article tree acyclic. `_check_hierarchy()` is an `@api.constrains("parent_id")` method that calls Odoo's `_has_cycle()` and raises `ValidationError("You cannot create recursive articles.")` when an article would become its own ancestor, which also covers setting the parent to a descendant. No arguments and no return value; it runs on create and write of `parent_id` inside the transaction, so the change is rolled back. Cost grows with tree depth. Test: `test_manual_check_hierarchy`. See `story_manual_hierarchy`.

### `manual_compute_breadcrumbs` [@ANCHOR: knowledge:manual_compute_breadcrumbs]
Purpose: the ancestor list shown as breadcrumbs. `_compute_breadcrumb_article_ids()` (`@api.depends("parent_id")`) runs one recursive SQL CTE for all records in `self`, following `parent_id` up to 50 levels, and orders each article's ancestors from root to parent. Because raw SQL ignores the `active` flag and record rules, the ids are then re-read through an ORM `search()` so archived ancestors and ancestors the viewer may not read are dropped instead of appearing in the public page or raising an access error. Unsaved records get an empty list. Sets the field `breadcrumb_article_ids`; no return value. Tests: `test_manual_breadcrumb_archived_ancestor`, `test_manual_breadcrumb_inaccessible_ancestor`.

### `manual_compute_reading_time` [@ANCHOR: knowledge:manual_compute_reading_time]
Purpose: the "N min read" label. `_compute_reading_time()` (`@api.depends("body")`) converts the HTML body to text with `html2plaintext`, counts words and sets `reading_time` (minutes, integer) to `max(1, round(words / 200))`, assuming 200 words per minute. An empty body gives `0`. No arguments, no return value, no side effects. Tests: `test_manual_reading_time`, `test_manual_ui_rendering`.

### `manual_toc_logic` [@ANCHOR: knowledge:manual_toc_logic]
Purpose: build the sticky "On this page" table of contents in the browser. `ManualTOC.start()` in `static/src/js/manual_toc.js` is an Odoo `Interaction` bound to `.o_manual_body` and registered as `knowledge.ManualTOC` in `public.interactions`. It first turns a leading `h1` that repeats the page title into an `h2`; it then returns without change if `#manual_toc_container` is absent or the body has no `h2` or `h3`. Otherwise it gives each heading an id (`toc-heading-<n>` when it has none) and fills the container with a list of `#id` links, `h2` bold and `h3` indented. Vanilla JavaScript, no jQuery, no network access, runs once per page view. The text comes from `textContent`, so heading markup cannot inject HTML. Test: `test_tour_manual_toc`. See `story_manual_toc`.

### `manual_doc_auto_install` [@ANCHOR: knowledge:manual_doc_auto_install]
Purpose: install each installed module's documentation as articles. The work is `ir.module.module._bootstrap_knowledge_docs()` (`zero_sudo/models/ir_module_module.py`, anchor `zero_sudo:bootstrap_knowledge_docs`), run from `_register_hook` at every server start and by module install hooks. It takes no arguments and returns nothing. It does nothing when `knowledge.article` does not exist (soft dependency: this module or Odoo Enterprise's Knowledge can provide it). Otherwise it reads the `knowledge_docs` list from the manifest of every installed module (in batches of 1000), compares a stored hash of each file with the one saved in `zero_sudo.kv`, and creates or updates one article per entry only when the content changed. It runs as the `knowledge.user_knowledge_service_account`, or the zero_sudo facility account when that does not exist, never as superuser. An installed document is unpublished and internal unless its entry opts in (see `manual_doc_injection`). Test: `tests/test_features.py` (`test_04_doc_installation`).

### `manual_doc_injection` [@ANCHOR: knowledge:manual_doc_injection]
Purpose: the contract of one `knowledge_docs` manifest entry, the way a module injects a document. Each entry is a dictionary with `name` (article title, default "<module> Documentation"), the file path, optional `public: True` or `audience: "member"`, and `help_topic` (the Help index heading). Without those the article is created with `internal_permission = 'none'`, not published, `audience = internal`. Paths in stories, journeys or runbooks directories, README files and titles starting "Story:" or "Journey:" are always internal. Markdown files are converted once to sanitized HTML (`_markdown_doc_to_html`). The article is identified by module and name, so two modules with the same title do not overwrite each other. Test: `tests/test_features.py` (`test_04_doc_installation`). See `story_manual_doc_installation`.

### `manual_sidebar_search_optimization` [@ANCHOR: knowledge:manual_sidebar_search_optimization]
Purpose: fetch the sidebar's root articles in one query. `_get_sidebar_articles()` in `controllers/main.py` takes no arguments and builds a single combined domain: root articles (`parent_id` empty) inside the Help domain, plus, for a reader who is not a Manual Administrator, those with `internal_permission` `read` or `write`, or listing the reader in `member_ids`, or private (`none`) and created by the reader. It searches once with a limit of 5000 and splits the result in memory into shared and private articles. Returns the grouped articles for the template. It replaced three separate queries; read-only. Tests: `knowledge/tests/test_controllers.py`, `test_tour_manual_search`.

### `manual_compile_markdown` [@ANCHOR: knowledge:manual_compile_markdown]
Purpose: show markdown typed into the editor as HTML. `_compile_markdown(html_body, article_id=None, write_date=None)` returns a `Markup` string.

| Argument | Type | Meaning |
|---|---|---|
| `html_body` | str | The article's HTML body. |
| `article_id` | int or None | With `write_date`, forms the cache key `<id>_<write_date>`. |
| `write_date` | datetime or None | Last write time; a changed article gets a new key. |

Behaviour: an empty body gives empty `Markup`. The body is converted to plain text with `html2plaintext` and scanned for markdown signatures (headings, fenced code, list items, bold, links). Compilation is skipped when the HTML already holds structure (`div`, tables, images, headings, lists, `pre`, `blockquote`, `svg` and similar). The markdown library output (extensions fenced_code, tables, nl2br, toc) always passes through Odoo's `html_sanitize` before it is marked safe, so typed text such as a script tag cannot become live HTML. The result is kept in an LRU cache (`_md_cache`). If the markdown parser fails, the original body is returned. No database access. The LRU cache holds 1024 entries. Test: `knowledge/tests/test_robustness.py`.

### `story_manual_feedback` [@ANCHOR: story_manual_feedback]
Story document `hams_shared/docs/stories/feedback.md`: a reader finishes an article and votes helpful or not helpful. The story walks through the POST to `/manual/feedback`, the honeypot, the read-access check and the atomic SQL increment, and cites `controller_manual_feedback`. It is a narrative anchor with no code of its own; the code and tests are those of `controller_manual_feedback`, and a change to the voting behaviour must change the story in the same pull request.

### `story_manual_hierarchy` [@ANCHOR: story_manual_hierarchy]
Story document `hams_shared/docs/stories/hierarchy.md`: an administrator tries to make an article a child of one of its own descendants, and the `_check_hierarchy` constraint refuses with a `ValidationError`. It cites `manual_check_hierarchy`. A narrative anchor with no code of its own; verification is `test_manual_check_hierarchy`. Change the story together with the constraint.

### `story_manual_doc_installation` [@ANCHOR: story_manual_doc_installation]
Story document `hams_shared/docs/stories/doc_installation.md`: when a module with a `knowledge_docs` manifest entry is installed, the loader in `zero_sudo` finds the Knowledge provider, installs the article and keeps it up to date by content hash. It cites `manual_doc_auto_install`, `manual_doc_injection` and `zero_sudo:zero_sudo_doc_installer`. No code of its own; tests are `test_04_doc_installation` and the zero_sudo tests.

### `story_manual_backend_views` [@ANCHOR: story_manual_backend_views]
Story document `hams_shared/docs/stories/backend_views.md`: a documentation manager works in the backend list and form views (`view_knowledge_article_list`, `view_knowledge_article_form`), editing title, body, hierarchy, `internal_permission` and `member_ids`. It cites the view rendering test `test_manual_backend_views_rendering`. Narrative anchor, no function; creating, editing and deleting require the Manual Administrator group.

### `story_manual_toc` [@ANCHOR: story_manual_toc]
Story document `hams_shared/docs/stories/toc.md`: on a long article the browser builds a sticky table of contents from `h2` and `h3` headings. It cites `manual_toc_logic` and the tour `test_tour_manual_toc`. A narrative anchor; the behaviour is documented in the `manual_toc_logic` entry above.

### `story_manual_url_generation` [@ANCHOR: story_manual_url_generation]
Story document `hams_shared/docs/stories/url_generation.md`: renaming or creating an article recomputes its `/manual/<id>-<safe-name>` address. It cites `manual_compute_website_url` and the test `test_manual_url_slug_generation`. Narrative anchor only, with no code of its own; the slug rules (accent folding, dashes, id prefix) are in the `manual_compute_website_url` entry, and a change to them must change the story.

### `story_manual_search` [@ANCHOR: story_manual_search]
Story document `hams_shared/docs/stories/search.md`: a reader who does not know where a topic lives uses the search bar. The story states that the search is a case-insensitive substring match over title and body of articles the reader may see, and cites `controller_manual_search`. Narrative anchor; tour `test_tour_manual_search` verifies it.

### `story_article_view` [@ANCHOR: story_article_view]
Story document `hams_shared/docs/stories/article_view.md`: a visitor opens `/manual` or `/manual/<id>-<slug>`. The `manual_article_view` controller extracts the id, falls back to the first root article, checks read access, redirects to the canonical slug, compiles markdown and renders article, sidebar and breadcrumbs. It cites `controller_manual_article_view`. Narrative anchor with no function of its own.

### `journey_admin_managing` [@ANCHOR: journey_admin_managing]
Journey document `hams_shared/docs/journeys/admin_managing_articles.md`: a Manual Administrator creates an article, sets a parent (guarded by `manual_check_hierarchy`), checks the generated address (`manual_compute_website_url`) and publishes it. The persona is a member of the Manual Administrator group. Narrative anchor; it links the hierarchy and URL stories and has no code.

### `journey_developer_integration` [@ANCHOR: journey_developer_integration]
Journey document `hams_shared/docs/journeys/developer_doc_integration.md`: a module developer writes `data/documentation.html`, lists it in `knowledge_docs` in the manifest, installs the module and checks `/manual`. It cites the installer anchor `zero_sudo:zero_sudo_doc_installer` and the story `story_manual_doc_installation`. Narrative anchor with no code of its own.

### `journey_user_browsing` [@ANCHOR: journey_user_browsing]
Journey document `hams_shared/docs/journeys/user_browsing_journey.md`: a guest or portal user arrives at `/manual`, searches (`controller_manual_search`), navigates by sidebar and breadcrumbs, reads an article with its table of contents and gives feedback (`controller_manual_feedback`). Narrative anchor with no code; the personas are Guest and Portal User, and what they may see follows the record rules in section 5 above.
