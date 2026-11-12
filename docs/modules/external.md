# External Dependencies (`external`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

Local hosting of third-party JS libraries for isolated networks, so the platform stays fully
functional without access to the global internet. Vendored files are reached via Odoo's ordinary
generic static-file route (`/external/static/src/node_modules/<library>/...`), never through an
assets bundle -- deliberately not declared under an `"assets"` manifest key, so files stay
unminified (see `external/__manifest__.py`'s own comment on why: `transformers.js`'s nested ES6
template literals break under Odoo's `rjsmin` minifier).

## Libraries hosted here

* **Leaflet.js** (v1.9.4) -- used for map rendering across the site (satellite tracker, repeater
  directory, DNS map, awards dashboard). Reachable at
  `/external/static/src/node_modules/leaflet/leaflet.js` and `leaflet.css`
  (`external:HTTP_REACHABLE_LEAFLET`).
* **Transformers.js** (v2.16.1) -- used for Edge AI processing (e.g. recognizing callsigns in
  speech: the net-roster widget in `ham_events/static/src/js/net_roster_widget.js` runs a named-entity
  token-classification model over the browser's speech-recognition text, in the browser, only when the page is not
  cross-origin isolated). Reachable at `/external/static/src/node_modules/transformers/transformers.js`
  (`external:HTTP_REACHABLE_TRANSFORMERS`).

The module's `static/src/node_modules/` directory vendors other libraries too, each reached by the same route
pattern: `d3` (with `d3-geo-projection`), `topojson`, `ft8js` (WebAssembly FT8 encode/decode), `noble`
(cryptography: `ciphers`, `curves`, `hashes`) and `sipjs` (the SIP client used for AllStar calling). Only Leaflet
and Transformers.js have a reachability test and anchor. `external/fetch_assets.py` is the developer-run script that
downloads Leaflet and Transformers.js from fixed URLs and refuses any file whose SHA-256 checksum differs from the
pinned value; nothing at request time fetches from the internet.

Both Leaflet and Transformers.js are also documented in the module's own in-app manual (`external/data/documentation.html`,
installed as a Knowledge article) -- this file exists alongside it because this codebase's own
anchor-coverage linter only scans `docs/`, not module `data/` directories.

## Anchor reference

### `external:HTTP_REACHABLE_LEAFLET` [@ANCHOR: external:HTTP_REACHABLE_LEAFLET]
This anchor names the guarantee that the vendored Leaflet 1.9.4 files are served over plain HTTP by Odoo's generic static-file route, with no asset bundle. The files are `/external/static/src/node_modules/leaflet/leaflet.js` and `leaflet.css`, used by the satellite tracker, repeater directory, DNS map and awards dashboard, so isolated networks need no CDN. The anchor sits in a comment in `external/__manifest__.py`, which deliberately declares no `assets` keys: bundling would make Odoo re-minify the files.

**Interface:** two unauthenticated GET URLs; 200 with the file body expected, 404 if the file is missing. There is no code, locking or secret. **Test:** `test_01_leaflet_assets_reachable` in `external/tests/test_assets.py` fetches both URLs and checks for `Leaflet` and `.leaflet-container` in the bodies. **See also:** `external:HTTP_REACHABLE_TRANSFORMERS`.

### `external:HTTP_REACHABLE_TRANSFORMERS` [@ANCHOR: external:HTTP_REACHABLE_TRANSFORMERS]
This anchor names the guarantee that the vendored Transformers.js 2.16.1 file is reachable at `/external/static/src/node_modules/transformers/transformers.js`, served by the same generic static route. It is used for in-browser Edge AI, such as callsign recognition in the net-roster widget. The file must never be declared under an `assets` bundle key: it contains nested ES6 template literals that Odoo's `rjsmin` minifier cannot handle, which previously caused browser-tour `SyntaxError` failures.

**Interface:** one unauthenticated GET; 200 expected, 404 if missing; no code, locking or secrets. **Test:** `test_02_transformers_assets_reachable` in `external/tests/test_assets.py` requests the URL and checks for `transformers` in the body. **See also:** `external:HTTP_REACHABLE_LEAFLET`.
