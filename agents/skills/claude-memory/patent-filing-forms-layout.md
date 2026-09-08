---
name: patent-filing-forms-layout
description: "How Bruce's USPTO provisional filing packages are laid out, including where the per-application SB/15A and SB/16 forms live and the standing cover-page facts they must carry."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: c0284e30-6ccb-4ad4-9b91-456b9fb13774
  modified: 2026-09-08T19:58:19.411Z
---

# Layout of the USPTO provisional filing packages in hams_com

Bruce Perens files his provisional patent applications pro se as a micro entity.
Each application's filing package lives under
`hams_com/docs/proposals/patent_disclosures/filed_applications/<NN>_<name>/`:

- `specification.pdf` and `drawings.pdf` are generated from
  `provisional_specs/<NN>_<name>.md` and `_src/fig*.svg` by
  `_pipeline/rebuild_specs.py` (which calls `build_spec.py`) and
  `_pipeline/build_drawings.py`; `_pipeline/verify_filed.py` gates them.
- The USPTO forms -- the micro-entity certification (SB/15A) and the provisional
  cover sheet (SB/16) -- are generated per application as HTML rendered to PDF,
  kept as `_src/sb15a.html` / `_src/sb16_cover_sheet.html` with the PDFs beside
  the specification. On 2026-09-08 only application 01 had them; Bruce planned to
  scan his signature and generate signed forms for all applications in a
  separate session, using 01's as the template.

Standing facts every cover-page artifact must carry, all recorded in
`_pipeline/build_spec.py`'s `COVER_HTML` and in
`filed_applications/PRODUCTION_STATUS.md`: Inventor-Applicant Bruce Perens
(37 C.F.R. § 1.33(b), pro se); USPTO Customer Number 227292; micro-entity status
(37 C.F.R. § 1.29); and **no assignee named by default** -- Bruce decided on
2026-09-04 that assignment is a per-patent decision added explicitly, never a
portfolio-wide default. Each application's exact title is the `<h1 class="title">`
in its `_src/specification.html`.
