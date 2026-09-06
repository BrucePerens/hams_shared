# Knowledge Module

A deliberate from-scratch open-source work-alike of Odoo's proprietary Knowledge module -- its own
primary definition, not a wrapper around or accidental duplicate of the real one.

## Technical Specification

### 1. Odoo-Knowledge-URL-Shape Compatibility
Roughly a dozen "Help" links across other modules were written assuming Odoo's real (proprietary)
Knowledge module's own URL shape, `/knowledge/home?search=...`, rather than this module's own
`/manual/*` routes.
* **`/knowledge/home` Alias:** `[@ANCHOR: knowledge:COMM_knowledge_home_alias]` -- redirects straight to an exact-name match, or falls through to the substring search-results page.

* **Exact-Match Article Lookup:** `[@ANCHOR: knowledge:COMM_manual_article_by_name]` -- `/manual/by_name/<name>`, the lookup `knowledge_home_alias` delegates to rather than duplicating.

### 2. Article Computed Fields
* **Author Resolution:** `[@ANCHOR: knowledge:COMM_compute_author_id]` -- the last writer, falling back to the creator.

* **Plain-Text Body Preview:** `[@ANCHOR: knowledge:COMM_compute_body_snippet]` -- a 300-character, tag-stripped snippet of the article body.

### 3. Article Duplication
* **Copy Override:** `[@ANCHOR: knowledge:COMM_copy]` -- preserves the parent/child hierarchy while giving the duplicate a distinct "(copy)"-suffixed name.

### 4. Website Search Integration
Found live 2026-08-29 across three separate usability-audit personas: the site's own search never
covered knowledge articles at all, only products/blog/pages -- a genuinely public, published manual
article was completely unfindable through search.
* **Article Search Detail Provider:** `[@ANCHOR: knowledge:COMM_search_get_detail]` -- registers `knowledge.article` with website's search dispatch, matching the same pattern `website_blog` uses for `blog.post`; enforces the same internal-staff-sees-everything / everyone-else-published-or-member-only visibility rule the controllers already use.

* **Website Search Dispatch Hook:** `[@ANCHOR: knowledge:COMM_website_search_get_details]` -- `website._search_get_details()`'s own override that appends the knowledge-article search detail when the search type matches.

## External Dependencies

* `website` for the search dispatch integration and published-content mixin.

## Cross-Module Interfaces

None beyond the shared `website`/`res.users` core models this module extends.
