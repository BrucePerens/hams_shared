# Site-Wide Advertising (`advertising`)

*Copyright © HAMS project. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

Built 2026-08-29 against `docs/proposals/ADVERTISING.md` phase 1 (Google AdSense), following that
proposal's own recommended defaults: a single footer banner, consent-gated, off by default. **Not
yet approved for launch** -- several real product/legal questions the proposal itself flags
(revenue share with personal-site owners, category blocklist, notice/opt-out) are deliberately left
open by this module, not decided unilaterally. This is scaffolding landed for review, not a
launched feature.

**Updated 2026-09-01** per Bruce's own direct instruction ("I think a sidebar and footer per page
would be a good start. Can we also fit an ad in ham_shack?"): a second, fixed-position sidebar slot
was added, and the footer slot's earlier `/shack` exclusion was removed -- both placements now
render identically on `/shack` and everywhere else.

## 1. Overview

Adds three `website`-level fields (`google_adsense_client_id`, `google_adsense_footer_slot_id`,
`google_adsense_sidebar_slot_id`) and a `website.layout` inheritance that renders the AdSense loader
script plus a footer ad slot and a fixed-position sidebar ad slot -- each ad slot only once **both**
the publisher ID and that slot's own ID are configured in Website > Configuration > Settings. Any
field left empty renders zero ad-related markup or script for that slot; there is no default "on"
state.

## 2. Consent gating

Google Consent Mode (`ad_storage`/`ad_user_data`/`ad_personalization`, defaulting `denied`, updated
to `granted` on the existing cookies bar's `optionalCookiesAccepted` event) is wired independently
of `website.google_analytics_key` -- confirmed by reading
`odoo/addons/website/views/website_templates.xml` directly, not assumed: core's own GA consent
wiring only exists when GA itself is configured, so AdSense needed its own, active whenever
`google_adsense_client_id` is set regardless of whether GA is also configured on the same site
[@ANCHOR: xpath_rendering_advertising_head].

## 3. Placement

Footer banner, `//footer` position `after`, matching `user_websites`'s own
`layout_inherit_report_violation` placement precedent -- the proposal's own "least intrusive
starting point" recommendation. Renders on every page including `/shack`, per Bruce's 2026-09-01
instruction overriding the earlier `/shack` exclusion
[@ANCHOR: xpath_rendering_advertising_footer].

A fixed-position ("skyscraper") sidebar unit, CSS-`position:fixed` to the page's right edge rather
than injected into a per-page sidebar region (no such region exists generically across every page
type this site has), hidden below the Bootstrap `xl` breakpoint so it never competes with content
on narrower viewports. Renders on every page including `/shack`
[@ANCHOR: xpath_rendering_advertising_sidebar].

## 4. Settings

Exposed under Website Settings' existing "Tracking & SEO" block via a new "Advertising" block
[@ANCHOR: xpath_rendering_advertising_settings].

## 5. Deliberately not built

- **Category/content blocklist** (borderline-legal RF gear ads on a licensing-compliance platform)
  -- a real policy decision, not decided here.
- **Revenue share with personal-site owners** whose pages carry the footer ad -- currently
  platform-only by omission, not by decision.
- **Phase 2 (direct ad sales)** -- explicitly scoped by the proposal as a separate, later effort.

## 6. Testing

`tests/test_advertising_layout.py` covers: no markup when unconfigured, partial-configuration
still renders nothing in either slot, full configuration renders the footer and sidebar slots with
the right IDs, `/shack` shows both slots identically to any other page, and the
consent-default-denied wiring is present independent of GA
[@ANCHOR: test_xpath_rendering_advertising].

The settings-view injection covered above (section 4) is verified separately by its own test
[@ANCHOR: test_xpath_rendering_advertising_settings].
