---
name: hams-com-private-repo-reference-storage
description: "hams_com is a private (not public) repository, so Claude may store actual copyrighted reference documents (PDFs, papers) there for internal engineering use, per the user's own fair-use judgment -- unlike hams_open, which is public/open-source and should stick to extracting facts and citing the source rather than vendoring the document itself."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 8634d779-d53d-4761-a994-a128e6aa0c52
  modified: 2026-08-23T14:31:44.753Z
---

When Claude finds a useful external reference document (e.g. a technical paper needed to correctly
implement a protocol) while working on the hams.com codebase, where and how to save it depends on
which repository it belongs to:

- **hams_com** is a private, closed-source repository. The user has stated directly: "hams_com is
  not a public repository so we are OK there. We have a good case for unpublished but stored
  references being fair use under copyright law." This means Claude may save the actual source
  document (e.g. a PDF) into hams_com for internal engineering reference, not just an
  extracted-facts summary, when that's more useful than a rewritten summary.

- **hams_open** (and any other genuinely public/open-source repository in this ecosystem) does not
  have this same fair-use footing -- redistributing a third-party copyrighted document there is a
  real, different legal exposure the user has not waived. For those repos, keep to the pattern
  already established in this codebase for protocol references (used for WSPR, PSK31, and FT8's
  GFSK waveform spec): extract the factual/mathematical content by reading the source, cite it
  clearly (title, authors, URL), and do not commit the document itself.

This was raised when Claude fetched a real QEX magazine paper (copyright ARRL, "reprinted with
permission" on WSJT-X's own site) needed to correctly implement FT8's transmit waveform, and chose
to extract-and-cite into hams_open (the correct call there) rather than vendor the PDF -- the user
confirmed that caution was right for hams_open specifically, while clarifying that hams_com would
not need the same caution.
