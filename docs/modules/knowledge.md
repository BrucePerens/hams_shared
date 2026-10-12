# Knowledge Module

A deliberate from-scratch open-source work-alike of Odoo's proprietary Knowledge module -- its own
primary definition, not a wrapper around or accidental duplicate of the real one.

## Technical Specification

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

### 1. Odoo-Knowledge-URL-Shape Compatibility
Roughly a dozen "Help" links across other modules were written assuming Odoo's real (proprietary)
Knowledge module's own URL shape, `/knowledge/home?search=...`, rather than this module's own
`/manual/*` routes.
* **`/knowledge/home` Alias:** `[@ANCHOR: knowledge:COMM_knowledge_home_alias]` -- calls the exact-name lookup directly (not an HTTP redirect to it, so the caller's `#anchor` fragment survives), and renders the substring search-results page (`/manual/search`) when no article has that name, or when `search` is empty.

* **Exact-Match Article Lookup:** `[@ANCHOR: knowledge:COMM_manual_article_by_name]` -- `/manual/by_name/<name>` (a `+` in the name is read as a space), the lookup `knowledge_home_alias` delegates to rather than duplicating. The match is case-insensitive but otherwise exact: `%`, `_` and backslash in the name are escaped so they cannot act as wildcards. A match redirects to the article's `website_url`; no match answers 404. Staff in the internal-user group see any matching article; everyone else sees only published articles or those whose `member_ids` include them.

### 2. Article Computed Fields
* **Author Resolution:** `[@ANCHOR: knowledge:COMM_compute_author_id]` -- `write_uid` (the last writer), falling back to `create_uid`; stored, and recomputed when either changes.

* **Plain-Text Body Preview:** `[@ANCHOR: knowledge:COMM_compute_body_snippet]` -- a snippet of the article body, tag-stripped and whitespace-collapsed, cut to its first 300 characters (empty when the body is empty).

### 3. Article Duplication
* **Copy Override:** `[@ANCHOR: knowledge:COMM_copy]` -- preserves the parent/child hierarchy while giving the duplicate a distinct "(copy)"-suffixed name.

### 4. Website Search Integration
Found live 2026-08-29 across three separate usability-audit personas: the site's own search never
covered knowledge articles at all, only products/blog/pages -- a genuinely public, published manual
article was completely unfindable through search.
* **Article Search Detail Provider:** `[@ANCHOR: knowledge:COMM_search_get_detail]` -- registers `knowledge.article` with website's search dispatch, matching the same pattern `website_blog` uses for `blog.post`; enforces the same internal-staff-sees-everything / everyone-else-published-or-member-only visibility rule the search controller (`/manual/search`) uses. Matching is on the article name, and also on the body when the search asks for descriptions.

* **Website Search Dispatch Hook:** `[@ANCHOR: knowledge:COMM_website_search_get_details]` -- `website._search_get_details()`'s own override that appends the knowledge-article search detail when the search type is `knowledge_articles` or `all`.

## External Dependencies

* `website` for the search dispatch integration and published-content mixin.

## Cross-Module Interfaces

None beyond the shared `website`/`res.users` core models this module extends.
