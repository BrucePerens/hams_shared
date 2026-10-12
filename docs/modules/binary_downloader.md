# Binary Downloader Module (`binary_downloader`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

The **Binary Downloader** is a secure, database-backed orchestration module designed to provide static executable dependencies (e.g., `kopia`, `etcd`, `cloudflared`) to other Odoo subsystems. It implements a robust lifecycle management system for external tools while maintaining strict security standards.

---

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for developers, LLMs, and System Integrators.
</system_role>

<security_design>
## 1. Security Design
* **DB-Backed Manifests:** Download targets and cryptographic SHA-256 checksums are stored in the `binary.manifest` model, preventing reliance on insecure flat-file manifests.
* **Least Privilege:** Executes downloads and installations under the dedicated `user_binary_downloader_service` service account. The module is fully compliant with the **Zero-Sudo** mandate (no `.sudo()`; privileged work runs as a dedicated, narrowly scoped service account -- defined in [zero_sudo.md](zero_sudo.md), accounts catalogued in [service_accounts.md](../service_accounts.md)).
* **Integrity Enforcement:** Verifies SHA-256 hashes before moving binaries to the execution path (`hams_bin`). For an archive, the hash checked is that of the whole downloaded archive. A download larger than 1 GiB is aborted before its checksum is checked.
* **Concurrency Protection:** Implements PostgreSQL **advisory locks** (via `pg_advisory_xact_lock`) during the installation process to prevent race conditions and file corruption when multiple Odoo workers trigger installations simultaneously.
* **Archive Security (Tar/Zip Slip):** "Tar Slip"/"Zip Slip" is an attack in which an archive member's path (for example `../../x`) makes extraction write outside the intended directory. Only one member is ever extracted (`.tar.gz` and `.zip`): the first whose name equals `extract_member` or ends in `/<extract_member>`. Its bytes are written to the computed target filename in `hams_bin`, so the member's own path never decides where the file lands. If the selected member is a symbolic or hard link, installation is refused.
* **Timeouts:** Both network requests have a 15 second timeout (HEAD and GET alike) to prevent resource exhaustion and hanging threads. A failed HEAD request is only logged; the GET request decides success.
* **Download Restrictions:** Only `https://` URLs are accepted (when a manifest or version is saved and again at download time). Before connecting, the host name must resolve only to public addresses, and the same check is applied to wherever a redirect lands; loopback, link-local, private-use, multicast and reserved addresses are rejected. This blocks Server-Side Request Forgery (SSRF), where a manifest URL would make the Odoo server fetch from internal services.
* **Permissions:** Target directory (`hams_bin`) and binaries are set to `0o750` to restrict execution and access.
* **Multi-Company Isolation:** Binaries are stored in one shared directory (`hams_bin`), with version-aware filenames `<name>_<id>` (`<id>` is the first 16 hex digits of the SHA-256 of `<name>_<checksum>`, so a new checksum gives a new file). `binary.manifest` has a `company_id`; `ensure_executable` uses the current company's manifest first and falls back only to a global one (no company), never to another company's. Per-website versions are handled by `binary.tenant.link` and `binary.version` (see the README).
</security_design>

<api>
## 2. API Reference

### `binary.manifest` model
The primary interface for dependency resolution.

#### `ensure_executable(cmd_name)`
`[@ANCHOR: binary_ensure_executable]`

Resolves and ensures a binary is available and executable. Returns the absolute path to the binary. It looks up the manifest for `cmd_name` (current company first, then global) as the module's service account, then returns that manifest's file in Odoo's private binary directory (`hams_bin`) if it is already there and valid; otherwise it attempts an automatic download and installation. It deliberately does not consult the system `PATH`, so a same-named program elsewhere on the server cannot shadow the verified copy.

**Parameters:**
- `cmd_name` (str): The name of the command to ensure (e.g., `"kopia"`).

**Returns:**
- Absolute path (str) to the verified executable.

**Raises:**
- `ValidationError`: If the command name is invalid (empty, contains slashes or backslashes, or is `.` or `..`). Manifest constraints (e.g., missing `extract_member` for tarballs) raise `ValidationError` when the manifest is saved, not here.
- `UserError`: If the manifest is missing, the platform is unsupported (anything but Linux on x86_64, aarch64 or armv7l), checksum/integrity checks fail, the download is refused by the restrictions above, or the download or extraction fails.

#### `_compute_is_installed()`
`[@ANCHOR: binary_compute_installed]`

Tracks whether a binary is available in the system `PATH` or `hams_bin` and has appropriate execution permissions (a program on the `PATH` counts as installed here even though `ensure_executable` never uses it).

#### `action_install()`
`[@ANCHOR: binary_action_install]`

Triggers manual installation via the UI; it raises `UserError` unless the user is in the Binary Downloader Manager group or is an administrator.

* **Logic:**
    1. Checks if the binary is already available and valid.
    2. If not, downloads, verifies checksum, and extracts/installs if necessary to `<data_dir>/hams_bin/` (`data_dir` from the Odoo configuration, default `/var/lib/odoo`) by calling `ensure_executable`.
</api>

<usage>
## 3. Usage Example
```python
# To be called by other modules needing a binary dependency
bin_path = self.env["binary.manifest"].ensure_executable("kopia")
# Verified by [@ANCHOR: test_binary_manifest_standard]

# Verified by [@ANCHOR: test_binary_manifest_integration]
subprocess.run([bin_path, "--version"], check=True)
```
</usage>

<stories_and_journeys>
## 4. Architectural Stories & Journeys

For detailed narratives and end-to-end workflows, refer to the following:

### Stories
* [Binary Resolution](hams_shared/docs/stories/binary_resolution.md)
* [UI Installation](hams_shared/docs/stories/ui_installation.md)
* [Installation Status Check](hams_shared/docs/stories/is_installed_check.md)

### Journeys
* [Automated Provisioning Flow](hams_shared/docs/journeys/auto_provisioning_flow.md)
</stories_and_journeys>

<semantic_anchors>
## 5. Semantic Anchors
- `[@ANCHOR: binary_ensure_executable]` - Core binary resolution method.

- `[@ANCHOR: binary_compute_installed]` - Installation status computation.

- `[@ANCHOR: binary_action_install]` - UI installation trigger.

- `[@ANCHOR: UX_BINARY_INSTALL]` - UI elements for installation.

- `[@ANCHOR: test_binary_manifest_standard]` - Standard unit tests.

- `[@ANCHOR: test_binary_manifest_integration]` - Unmocked physical integration tests.

- `[@ANCHOR: test_binary_install_tour]` - UI tour for binary installation.

- `[@ANCHOR: test_binary_manifest_views]` - View rendering tests.
</semantic_anchors>
