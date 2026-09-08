---
name: hams-knowledge-module-is-oss-workalike
description: "hams_open/knowledge is a deliberate open-source work-alike of Odoo's proprietary Knowledge module, so its knowledge.article model is an intentional primary definition, not an accidental duplicate to consolidate."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-18T23:11:46.741Z
---

`hams_open/knowledge` exists specifically to reimplement Odoo's proprietary Enterprise "Knowledge"
feature as an open-source equivalent, since hams.com/hams_open targets Odoo Community. Its
`knowledge.article` model (defined in `hams_open/knowledge/models/knowledge_article.py`) is
therefore an intentional, standalone primary model definition -- it exists to give hams_open the
same capability the proprietary module would have provided, not because some other module's
`knowledge.article`-like concept was accidentally duplicated. When auditing this codebase for
cross-module model duplication or extension patterns, `knowledge.article` should not be treated as
a candidate for consolidation on that basis alone.
