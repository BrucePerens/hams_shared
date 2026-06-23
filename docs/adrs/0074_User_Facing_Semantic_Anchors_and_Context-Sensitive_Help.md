# ADR 0074: User-Facing Semantic Anchors and Context-Sensitive Help

## Context

We need a standardized methodology to bind frontend interactive elements to our centralized system help documentation manuals across different modules. This architecture ensures that contextual help links remain completely unbreakable and highly traceable even when view layouts or route endpoints shift.

## Specification Rules

1. **Inline HTML Tracking Attributes:** User-facing text nodes or layout headings in documentation manuals must utilize an explicit tracking token identifier matching the designated features framework.
2. **Explicit Module Prefixes:** To maintain modular boundaries, cross-module structural references inside global design docs must explicitly target their destination context namespace.

* *Example Definition:* `[@ANCHOR: user_websites:UX_REPORT_VIOLATION]`
* *Example Implementation:* `<h3 id="UX_REPORT_VIOLATION" data-trace="[@ANCHOR: user_websites:UX_REPORT_VIOLATION]">Reporting Content</h3>`

3. **Centralized Fragment Routing:** Frontend template view buttons or helper icons targeting deep documentation elements must map their source parameters to standardized target URL fragment hashes.
* *Example Route Link:* These links must point directly to the manual's URL fragment: `href="/user-websites/documentation#UX_REPORT_VIOLATION"`.

## Consequences

By adopting this structure, the semantic anchor verification linter will mathematically validate that user documentation manual endpoints match layout specifications bi-directionally across separate functional clusters.
