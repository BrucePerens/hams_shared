# Site-Wide Advertising (`advertising`)

*Copyright © HAMS project. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

Built 2026-08-29 against phase 1 (Google AdSense) of the project's advertising proposal (a design document kept outside this repository), following that
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
script plus a footer ad slot and a fixed-position sidebar ad slot. The three fields are set in
Website > Configuration > Settings. An ad slot renders only once **both** the publisher ID and that slot's own
ID are configured. The loader script (with the consent wiring of section 2) renders as soon as the
publisher ID alone is set, even when no slot ID is, but never while the website editor is active
(`editable`). With the publisher ID empty nothing ad-related renders at all: there is no default "on"
state. (The settings help text still says "single footer banner"; the sidebar slot was added later.)

## 2. Consent gating

Google Consent Mode (the `ad_storage`, `ad_user_data` and `ad_personalization` signals, plus `analytics_storage`, all defaulting to
`denied`; the first three are updated to `granted` on the existing cookies bar's `optionalCookiesAccepted` event, or at once
when every optional cookie is already accepted when the page is served) is wired independently
of `website.google_analytics_key` -- confirmed by reading
`odoo/addons/website/views/website_templates.xml` directly, not assumed: core's own GA consent
wiring only exists when GA itself is configured, so AdSense needed its own, active whenever
`google_adsense_client_id` is set regardless of whether GA is also configured on the same site
`xpath_rendering_advertising_head`.

## 3. Placement

Footer banner, `//footer` position `after`, matching `user_websites`'s own
`layout_inherit_report_violation` placement precedent -- the proposal's own "least intrusive
starting point" recommendation. Renders on every page including `/shack`, per Bruce's 2026-09-01
instruction overriding the earlier `/shack` exclusion
`xpath_rendering_advertising_footer`.

A fixed-position ("skyscraper") sidebar unit, CSS-`position:fixed` to the page's right edge rather
than injected into a per-page sidebar region (no such region exists generically across every page
type this site has), 160 by 600 pixels at `right:8px; top:120px`, hidden below the Bootstrap `xl` breakpoint so it never competes with content
on narrower viewports. Renders on every page including `/shack`
`xpath_rendering_advertising_sidebar`.

## 4. Settings

Exposed in Website Settings as a new "Advertising" block placed after the `website_settings` block, with fields Publisher ID, Footer Ad Slot ID and Sidebar Ad Slot ID
`xpath_rendering_advertising_settings`.

## 5. Deliberately not built

- **Category/content blocklist** (borderline-legal RF gear ads on a licensing-compliance platform)
  -- a real policy decision, not decided here.
- **Revenue share with personal-site owners** whose pages carry the footer ad -- currently
  platform-only by omission, not by decision.
- **Phase 2 (direct ad sales)** -- explicitly scoped by the proposal as a separate, later effort.

## 6. Testing

`tests/test_advertising_layout.py` covers: no markup when unconfigured, a publisher ID alone loads the script but renders no slot, a slot ID without the publisher ID renders nothing, full configuration renders the footer and sidebar slots with
the right IDs, `/shack` shows both slots identically to any other page, and the
consent-default-denied wiring is present independent of GA
`test_xpath_rendering_advertising`.

The settings-view injection covered above (section 4) is verified separately by its own test
`test_xpath_rendering_advertising_settings`.

## Anchor reference

One entry per anchor of the `advertising` module, read from its views, scripts and tests.

### `xpath_rendering_advertising_head` [@ANCHOR: xpath_rendering_advertising_head]

**Purpose.** The `<head>` part of the AdSense placement: the consent wiring and the AdSense loader.

**Definition.** Template `layout_inherit_adsense` (`advertising/views/website_layout.xml`) inherits `website.layout`; an xpath `//head` with position `inside` adds markup only when `website.google_adsense_client_id` is set and the page is not in the website editor (`not editable`). It adds a non-executed `<script type="application/json" id="advertising_adsense_consent">` holding `{"all_consents_granted": ...}` from `website._allConsentsGranted()`, the script `adsense_consent_config` (`/advertising/static/src/js/adsense_consent.js`) and the async, `crossorigin="anonymous"` script `adsense_loader` for `https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=<publisher id>`.

**Behaviour.** A publisher id alone loads the script and wiring but no slot. This consent wiring is independent of Google Analytics: core's own wiring exists only when `google_analytics_key` is set. There is no inline script, so a strict Content-Security-Policy works. With the id empty, nothing renders.

**See also.** Tests: `test_xpath_rendering_advertising` (unconfigured renders nothing), `tests/test_advertising_layout.py` tests 02 and 05; `advertising:adsense_consent_js`.

### `xpath_rendering_advertising_footer` [@ANCHOR: xpath_rendering_advertising_footer]

**Purpose.** The footer banner placement of the AdSense integration.

**Definition.** In template `layout_inherit_adsense` (`advertising/views/website_layout.xml`, inheriting `website.layout`) the xpath `//footer` with position `after` contains a block rendered only when `website.google_adsense_client_id` and `website.google_adsense_footer_slot_id` are both set and the page is not in the editor. It is a `<div id="advertising_footer_slot" class="container mt-3 mb-3 text-center">` holding an `<ins class="adsbygoogle">` with `data-ad-client` (publisher id), `data-ad-slot` (footer slot id), `data-ad-format="auto"` and `data-full-width-responsive="true"`, followed by the include of `adsense_slot_push.js`.

**Behaviour.** One responsive banner under the footer on every page, `/shack` included (a decision of 2026-09-01 that removed the earlier exclusion). Setting only one of the two fields renders nothing. Values are escaped by QWeb attributes (`t-att-`), so an administrator cannot inject markup through them.

**See also.** Tests in `tests/test_advertising_layout.py` (tests 03 and 04); `advertising:adsense_slot_push_js`.

### `xpath_rendering_advertising_sidebar` [@ANCHOR: xpath_rendering_advertising_sidebar]

**Purpose.** The fixed-position "skyscraper" advertisement at the right edge of every page.

**Definition.** In the same template `layout_inherit_adsense` and the same `//footer` `after` xpath as the footer slot, a second block rendered only when `google_adsense_client_id` and `google_adsense_sidebar_slot_id` are set and the page is not in the editor: `<div id="advertising_sidebar_slot" class="d-none d-xl-block" style="position:fixed; right:8px; top:120px; width:160px; z-index:1020;">` containing an `<ins class="adsbygoogle">` of 160 by 600 pixels with `data-ad-client` and `data-ad-slot`, then the include of `adsense_slot_push.js`.

**Behaviour.** It is fixed to the viewport because the site has no generic sidebar region across page types. `d-none d-xl-block` hides it below the Bootstrap `xl` breakpoint so it never competes with content on narrow screens. It appears on `/shack` too. One field alone renders nothing.

**See also.** Test `tests/test_advertising_layout.py` (`test_07_sidebar_slot_requires_both_fields_and_renders_on_shack_too`); footer entry `xpath_rendering_advertising_footer`.

### `xpath_rendering_advertising_settings` [@ANCHOR: xpath_rendering_advertising_settings]

**Purpose.** The administrator's place to switch advertising on, per website.

**Definition.** View `res_config_settings_view_form` of the `advertising` module (`views/res_config_settings_views.xml`) inherits `website.res_config_settings_view_form`; an xpath `//block[@id='website_settings']` with position `after` adds the block **Advertising** (`id="advertising_settings"`) with one setting, **Google AdSense**, and three fields: `google_adsense_client_id` (Publisher ID, placeholder `ca-pub-XXXXXXXXXXXXXXXX`), `google_adsense_footer_slot_id` and `google_adsense_sidebar_slot_id`.

**Data.** The three Char fields live on `website` (per website, empty by default) and `res.config.settings` exposes them as related fields with `readonly=False`, so Save writes them to the current website. Empty means no ads at all. The setting's help text still says "single footer banner" although a sidebar slot exists (to-do: update the text).

**See also.** Test `test_xpath_rendering_advertising_settings`; placement entries `xpath_rendering_advertising_head`, `xpath_rendering_advertising_footer`.

### `test_xpath_rendering_advertising` [@ANCHOR: test_xpath_rendering_advertising]

**Purpose.** Test anchor for the default state: an installed but unconfigured `advertising` module must put no ad-related markup on any page.

**What it does.** `TestAdvertisingLayout.test_01_no_ad_markup_when_unconfigured` (`advertising/tests/test_advertising_layout.py`, class `HamsHttpCase`, tagged `post_install`, `-at_install`) opens `/` with `url_open` and asserts HTTP 200 and that the body contains none of `adsbygoogle`, `adsense_loader` or `advertising_footer_slot`. The failure message tells a maintainer that the default must render nothing.

**Coverage.** The same anchor is cited by the `audit-ignore-view` and xpath comments in `views/website_layout.xml`. The other tests of the class cover the configured states: tests 02 to 05 and 07 (publisher id alone, footer, `/shack`, consent defaults, sidebar), each citing the placement anchors.

**Requirements.** A website with no AdSense fields set (the test database default); needs `website` installed.

**See also.** `xpath_rendering_advertising_head`, `xpath_rendering_advertising_footer`, `xpath_rendering_advertising_sidebar`.

### `test_xpath_rendering_advertising_settings` [@ANCHOR: test_xpath_rendering_advertising_settings]

**Purpose.** Test anchor proving the Advertising block is part of the website Settings form once the module is installed.

**What it does.** `test_06_xpath_rendering_settings` (`advertising/tests/test_advertising_layout.py`) loads `website.res_config_settings_view_form`, asks `res.config.settings` for its compiled form (`get_view`, language `None`), and asserts that the resulting architecture string contains `google_adsense_client_id`. A failure means the xpath on `//block[@id='website_settings']` no longer matches and the settings block disappeared.

**Scope.** It checks presence of the field in the compiled view only; it does not save values or check slot fields (those are exercised through the website model in the layout tests). It cites `xpath_rendering_advertising_settings` as the thing under test, and `views/res_config_settings_views.xml` cites this anchor from its `audit-ignore-xpath` comment.

**Requirements.** `website` and `advertising` installed; no data is created.

**See also.** `xpath_rendering_advertising_settings`.
