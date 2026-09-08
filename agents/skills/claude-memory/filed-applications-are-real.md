---
name: filed-applications-are-real
description: The patent PDFs under filed_applications/ in hams_com are the actual USPTO submissions; defects there are consequential and must be fixed and verified at the artifact level.
metadata: 
  node_type: memory
  pinned: false
  originSessionId: c0284e30-6ccb-4ad4-9b91-456b9fb13774
  modified: 2026-09-08T19:35:40.314Z
---

# The generated patent PDFs are the real filings, not a preview

In Bruce Perens's `hams_com` repository, the directory
`docs/proposals/patent_disclosures/filed_applications/<NN>_<name>/` holds the
actual documents Bruce submits to the USPTO for each provisional application:
`specification.pdf`, `drawings.pdf`, and the `_src/` HTML they were rendered
from. They are generated from `provisional_specs/<NN>_<name>.md` by
`filed_applications/_pipeline/build_spec.py` (with a per-application
`_src/drawings_desc.txt` supplying the Brief Description of the Drawings) and
`build_drawings.py`.

On 2026-09-08 a review found that 25 of these PDFs stated "No drawings are
submitted with this specification" while sitting beside their own
`drawings.pdf`, because they had been generated without the drawings-description
override. Bruce's reaction was emphatic: "It's really important to fix that on
the applications, they are real applications." Treat this as a standing
priority whenever touching this material:

- Any edit to a provisional-spec markdown file is incomplete until the paired
  PDF is regenerated through the pipeline and installed.
- Verify the generated artifact itself, never just the source: page size, an
  `N/total` footer on every page, no `file://` path, no drafting-artifact terms,
  the "No drawings" placeholder present if and only if no `drawings.pdf` exists,
  and the figure numbers cited in the text equal to those printed on the
  drawing sheets, running 1..N.
- An inconsistency inside a filing document (text referring to a figure that
  does not exist, a denial of drawings that are attached) is a filing defect to
  fix before anything else, not a cosmetic issue to note.
