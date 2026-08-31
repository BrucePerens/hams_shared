# Site-Wide Advertising (`advertising`)

*Copyright © HAMS project. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

Built 2026-08-29 against `docs/proposals/ADVERTISING.md` phase 1 (Google AdSense), following that
proposal's own recommended defaults: a single footer banner, consent-gated, off by default. **Not
yet approved for launch** -- several real product/legal questions the proposal itself flags
(revenue share with personal-site owners, category blocklist, notice/opt-out) are deliberately left
open by this module, not decided unilaterally. This is scaffolding landed for review, not a
launched feature.

## 1. Overview

Adds two `website`-level fields (`google_adsense_client_id`, `google_adsense_footer_slot_id`) and a
`website.layout` inheritance that renders the AdSense loader script and a single footer ad slot --
but only once **both** fields are configured in Website > Configuration > Settings. Either field
left empty renders zero ad-related markup or script at all; there is no default "on" state.

## 2. Consent gating

Google Consent Mode (`ad_storage`/`ad_user_data`/`ad_personalization`, defaulting `denied`, updated
to `granted` on the existing cookies bar's `optionalCookiesAccepted` event) is wired independently
of `website.google_analytics_key` -- confirmed by reading
`odoo/addons/website/views/website_templates.xml` directly, not assumed: core's own GA consent
wiring only exists when GA itself is configured, so AdSense needed its own, active whenever
`google_adsense_client_id` is set regardless of whether GA is also configured on the same site
[@ANCHOR: xpath_rendering_advertising_head].

## 3. Placement

Single footer banner, `//footer` position `after`, matching `user_websites`'s own
`layout_inherit_report_violation` placement precedent -- the proposal's own "least intrusive
starting point" recommendation. Explicitly excluded on any `/shack`-prefixed path (a real-time
operating console, not a content/reference page) [@ANCHOR: xpath_rendering_advertising_footer].

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
still renders nothing in the footer, full configuration renders the footer slot with the right
IDs, `/shack` is excluded even when fully configured, and the consent-default-denied wiring is
present independent of GA [@ANCHOR: test_xpath_rendering_advertising].

The settings-view injection covered above (section 4) is verified separately by its own test
[@ANCHOR: test_xpath_rendering_advertising_settings].
