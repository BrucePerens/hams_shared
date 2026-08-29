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
  [@ANCHOR: external:HTTP_REACHABLE_LEAFLET].
* **Transformers.js** (v2.16.1) -- used for Edge AI processing (e.g. recognizing callsigns in
  speech). Reachable at `/external/static/src/node_modules/transformers/transformers.js`
  [@ANCHOR: external:HTTP_REACHABLE_TRANSFORMERS].

Both are also documented in the module's own in-app manual (`external/data/documentation.html`,
installed as a Knowledge article) -- this file exists alongside it because this codebase's own
anchor-coverage linter only scans `docs/`, not module `data/` directories.
