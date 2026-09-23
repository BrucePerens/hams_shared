---
name: course-copyright-unicode-mandate
description: "Standing mandate for course generation and review tools: use 'Copyright © Bruce Perens K6BP' with literal Unicode © on title pages and chapter footers, never (C)."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 80b1316e-857f-4c8d-93d7-c7ffe5119f20
  modified: 2026-09-22T17:22:00.000Z
---

All generated course material across hams.com and hams_open (including the course title page `index.html`, introduction `intro.html`, narrative chapters `chapter_*.html`, and reference extras `extras.html`) MUST display the copyright notice: `Copyright © Bruce Perens K6BP`.

Key requirements:
1. **Literal Unicode Symbol**: Use the Unicode character `©` (`\u00a9`), NEVER the ASCII fallback `(C)`.
2. **Title Page**: Displayed prominently in the cover header (`.cover-header .copyright`) and centered at the bottom of the page (`.chapter-copyright-footer`).
3. **All Chapters & Extras**: Displayed in a centered footer (`.chapter-copyright-footer`) immediately following the chapter navigation footer (`.chapter-nav-footer`).
4. **Tools & Tests**: Any tool generating course HTML (`ingest/generate_html.py`) and corresponding unit tests (`ingest/test_generate_html.py`) must enforce and verify this invariant on subsequent ingestions.
