# ADR 0088: HTML as the Interoperability Format for Complex Documents; PDF Emission Deprecated

## Status
Accepted

## Scope
This ADR governs how this codebase (`hams_com`, `hams_open`, `hams_shared`) **emits** complex
documents -- anything that doesn't represent well as plain text: filled forms, generated reports,
activity/communications logs, and anything similar, today or in the future. It does not govern
plain-text or structured-data output (JSON, CSV, plain email bodies), which were never PDF
candidates in the first place. **It does not govern PDF as an input format.** Accepting a PDF an
external party sends us -- an uploaded document, a form submitted to us, an attachment on an
inbound email -- is unaffected; we don't control what other people send, and rejecting real-world
PDF input would be user-hostile. This ADR is about what we generate, not what we accept.

## Context
The only PDF-generation path in this codebase is `wkhtmltopdf`, invoked via stock Odoo's
`ir.actions.report._run_wkhtmltopdf()` (`ics_forms/models/ics_form_handler.py`'s `render_pdf()`,
called from `ics_form_record.py`'s `action_send_email()` and `ham_events/models/ham_net.py`'s
`_generate_ics_forms()`). Investigating a real blocker with it the same night this ADR was written
surfaced that the tool itself, not just this codebase's use of it, has aged out:

- **It doesn't exist in Debian's repos anymore.** `wkhtmltopdf` was dropped upstream; there is no
  `apt` candidate for it on this codebase's own dev/deploy target (confirmed directly, including
  after a fresh `apt-get update`). The only remaining path to a working binary is downloading an
  unofficial release from GitHub and trusting it -- a real, deliberate trust decision for a binary
  that shells out and processes untrusted HTML content, not something to wave through.
- **Its rendering engine is an abandoned WebKit fork.** It predates the CSS3 features (flexbox,
  grid, modern font handling) that real-world form layouts increasingly assume, and gets no
  security patches.
- **The thing it's duplicating is already built, and better, elsewhere.** Every ICS form template
  in `ics_forms/data/*.html` already carries real `@media print` CSS -- hiding editor chrome,
  cleaning up margins and shadows for the printed page. That's exactly the styling a browser's own
  "Print -> Save as PDF" already uses, for free, through a modern, actively-maintained rendering
  engine (Blink/WebKit-current, not a frozen fork) that every recipient already has. Verified
  directly: headless Chromium -- an *already-installed, officially-packaged* browser this codebase
  already depends on for its entire test suite (`chromium` via `apt`, not an unofficial binary) --
  produces a real PDF via `--headless --print-to-pdf` today, if a byte-identical snapshot is ever
  still needed for a case with no live browser session.
- **HTML email is no longer the reliability risk PDF was invented to route around.** PDF's original
  justification was portability: a self-contained, renderer-independent format for an era when not
  every recipient had a capable HTML renderer, and email clients couldn't be trusted to display
  HTML consistently. Both conditions are gone -- HTML email is close to universal today, and every
  recipient's browser is a fully capable, ubiquitous document viewer. The thing PDF was for no
  longer needs a special-case answer.

**One real implementation obstacle, found investigating this same night, that any migration under
this ADR must account for rather than hit blind:** Odoo's own `ir.attachment._check_contents()`
silently downgrades an HTML-mimetype attachment to `text/plain` unless the creating user has write
access to `ir.ui.view` -- `base.group_system` (full Administrator) only, by this codebase's own
`ir.model.access.csv`. This is a legitimate Odoo security measure (defense against stored XSS via
uploaded/generated HTML attachments), not a bug to route around casually -- but it means a naive
`self.env["ir.attachment"].create({"mimetype": "text/html", ...})` called by an ordinary user (the
realistic case for anything a field responder or portal user generates) will silently serve as raw
HTML source text, not a rendered page. `action_send_email()`'s existing `.html` attachment is
already very likely hitting exactly this today. Migrating a PDF-emission call site to HTML has to
solve this deliberately -- see Decision item 5.

## Decision

1. **The canonical emitted format for a complex document is HTML, not PDF.** New code that needs
   to produce a form, report, log, or similar document-shaped output renders and stores/serves
   HTML.
2. **PDF generation/emission is deprecated.** No new code may add a PDF-generation call site.
   Existing ones (`ics_form_handler.py`'s `render_pdf()` and its two callers) are migrated to
   HTML-only emission as those call sites are next touched -- not a forced rewrite of working code
   in one pass, matching this codebase's own established "migrate opportunistically" posture (see
   ADR 0087).
3. **PDF ingestion is unaffected.** Accepting, storing, and displaying a PDF someone else sent us
   is a completely separate concern from this ADR and needs no change.
4. **If a literal, renderer-independent PDF snapshot is still genuinely wanted, it's produced on
   demand, never stored as the primary artifact:**
   - Interactive case (a user is looking at the HTML in their own browser): their own
     "Print -> Save as PDF", using the print CSS the HTML already carries. Zero server involvement.
   - Unattended/automated case (no live browser session -- e.g. an automated archival flow):
     headless Chromium's `--print-to-pdf`, generated at view/download time, not eagerly rendered
     and cached the way `wkhtmltopdf` is today. This is a genuinely heavier, slower operation than
     serving already-rendered HTML (spawning a browser process per render), so it should follow
     `MASTER_08`'s existing daemon-offload posture rather than running inline in an Odoo worker --
     the same output-pacing/concurrency-limiting shape already designed for
     `docs/proposals/GDPR_CSV_EXPORT.md` is a directly reusable reference for whoever builds this.
5. **Migrating a call site must solve the `ir.attachment` HTML-mimetype-downgrade problem
   deliberately, not by accident.** Serving generated HTML to an actual recipient (not just storing
   it) needs a real design -- e.g. a dedicated, access-controlled controller that explicitly sets
   `Content-Type: text/html` -- that consciously takes on the XSS-review responsibility Odoo's
   generic attachment guard currently absorbs for free. Do not "fix" this by widening the calling
   user's `ir.ui.view` access, which would grant far more than intended.
6. **New document-shaped features write real `@media print` CSS from the start**, following the
   ICS form templates' existing precedent, so client-side print-to-PDF is always available at no
   engineering cost.

## Consequences
This codebase's PDF-generation dependency surface (an unmaintained tool with no official install
path left) shrinks over time instead of growing, and every migrated document gets a more modern,
more widely-compatible rendering path. The cost is real and per-call-site, not free: each migration
has to deliberately solve the `ir.attachment` mimetype-downgrade problem rather than hitting it as
a surprise, and the rare cases that still want a genuine PDF snapshot need the same
daemon-offload/concurrency-discipline as any other heavy, on-demand server-side rendering work.
This is a standing policy direction, not a one-night rewrite -- `ics_form_handler.py`'s `wkhtmltopdf`
path keeps working until each of its callers is migrated.
