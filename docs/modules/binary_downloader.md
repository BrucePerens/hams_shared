# Binary Downloader Module (`binary_downloader`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

The **Binary Downloader** is a secure, database-backed orchestration module designed to provide static executable dependencies (e.g., `kopia`, `etcd`, `cloudflared`) to other Odoo subsystems. It implements a robust lifecycle management system for external tools while maintaining strict security standards.

---

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for developers, LLMs, and System Integrators.
</system_role>

<security_design>
## 1. Security Design
* **DB-Backed Manifests:** Download targets and cryptographic SHA-256 checksums are stored in the `binary.manifest` model, preventing reliance on insecure flat-file manifests.
* **Least Privilege:** Executes downloads and installations under the dedicated `user_binary_downloader_service` service account. The module is fully compliant with the **Zero-Sudo** mandate.
* **Integrity Enforcement:** Verifies SHA-256 hashes before moving binaries to the execution path (`hams_bin`).
* **Concurrency Protection:** Implements PostgreSQL **advisory locks** (via `pg_advisory_xact_lock`) during the installation process to prevent race conditions and file corruption when multiple Odoo workers trigger installations simultaneously.
* **Archive Security (Tar/Zip Slip):** Implements strict path validation and member name sanitization during archive extraction (both `.tar.gz` and `.zip`). Extracted files are strictly confined to the `hams_bin` directory. Symbolic links and hard links within archives are strictly forbidden.
* **Timeouts:** All network operations have strict timeouts (15s for HEAD, 600s for GET) to prevent resource exhaustion and hanging threads.
* **Permissions:** Target directory (`hams_bin`) and binaries are set to `0o750` to restrict execution and access.
* **Multi-Website Isolation:** While binaries are stored in a shared system directory, their use is orchestrated via Odoo's standard security model, ensuring that only authorized services can trigger or access specific dependencies.
</security_design>

<api>
## 2. API Reference

### `binary.manifest` model
The primary interface for dependency resolution.

#### `ensure_executable(cmd_name)`
`[@ANCHOR: binary_ensure_executable]`

Resolves and ensures a binary is available and executable. Returns the absolute path to the binary. It first checks the system `PATH`, then Odoo's private binary directory (`hams_bin`). If not found, it attempts an automatic download and installation.

**Parameters:**
- `cmd_name` (str): The name of the command to ensure (e.g., `"kopia"`).

**Returns:**
- Absolute path (str) to the verified executable.

**Raises:**
- `ValidationError`: If the command name is invalid (contains slashes, etc.) or if constraints fail (e.g., missing `extract_member` for tarballs).
- `UserError`: If the manifest is missing, the platform is unsupported, or checksum/integrity checks fail.

#### `_compute_is_installed()`
`[@ANCHOR: binary_compute_installed]`

Tracks whether a binary is available in the system `PATH` or `hams_bin` and has appropriate execution permissions.

#### `action_install()`
`[@ANCHOR: binary_action_install]`

Triggers manual installation via the UI.

* **Logic:**
    1. Checks if the binary is already available and valid.
    2. If not, downloads, verifies checksum, and extracts/installs if necessary to `/var/lib/odoo/hams_bin/`.
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
* [Binary Resolution](docs/stories/binary_resolution.md)
* [UI Installation](docs/stories/ui_installation.md)
* [Installation Status Check](docs/stories/is_installed_check.md)

### Journeys
* [Automated Provisioning Flow](docs/journeys/auto_provisioning_flow.md)
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
