# Story: Downloading, Sharing, and Retiring Third-Party Binaries

As a **System Administrator**, I want the platform to **fetch, verify, and share third-party binaries
across tenants** on its own, so that I don't have to manually install command-line dependencies (like
`kopia`) on every machine, and so that a stale or duplicate binary never lingers on disk after the last
record referencing it is gone.

## Scenario: Registering and Installing a Binary

An admin creates a `binary.manifest` record naming the binary, its download URL, checksum, and archive
type.

- Only `https://` URLs are accepted [@ANCHOR: binary_downloader:binary_manifest_check_url_scheme].

- The binary's own name can never contain a path-traversal character [@ANCHOR: binary_downloader:binary_manifest_check_name_no_slashes].

- A `tar.gz`/`zip` archive must name the specific member to extract [@ANCHOR: binary_downloader:binary_manifest_check_extract_member].

`action_install()` triggers `ensure_executable()`, which resolves the manifest (current company first,
then the global fallback) and calls the shared download engine -- the same engine handles raw
binaries, tarballs, and zips, verifying the SHA-256 checksum and refusing symlinked archive members
[@ANCHOR: binary_downloader:binary_utils_download_and_extract].

`_compute_is_installed()` reports whether the binary is already on `PATH` or already present in the
shared `hams_bin` pool, using a stable, checksum-derived filename [@ANCHOR: binary_downloader:binary_utils_get_target_filename].

## Scenario: Publishing a New Version and Notifying Tenants

A `binary.version` record captures one specific upstream release, with the identical URL/name/
extract-member validation as the manifest itself.

- Same URL-scheme check [@ANCHOR: binary_downloader:binary_version_check_url_scheme].

- Same no-slashes-in-the-identifier check, on the version number instead of the name [@ANCHOR: binary_downloader:binary_version_check_version_no_slashes].

- Same extract-member-required-for-archives check [@ANCHOR: binary_downloader:binary_version_check_extract_member].

`_get_central_path()` derives a deterministic, checksum-based path in the shared central pool
   [@ANCHOR: binary_downloader:binary_version_get_central_path], and `_compute_is_downloaded()` reports
   whether that exact file is already present and executable
   [@ANCHOR: binary_downloader:binary_version_compute_is_downloaded].
3. Once a new version is downloaded to the pool, `action_notify_tenants()` walks every tenant currently
   pinned to an older version of the same manifest, batching `pager.incident` alerts per company so each
   tenant's own on-call rotation is notified through their own PagerDuty routing
   [@ANCHOR: binary_downloader:binary_version_action_notify_tenants].

## Scenario: A Tenant Adopts, Upgrades, and Retires a Binary

1. A `binary.tenant.link` ties one website/tenant to one active version of one manifest. Its symlink
   path is computed from the tenant's own namespaced directory and the manifest's name, refusing to
   compute a path at all if that name were ever somehow unsafe -- a defense-in-depth check independent
   of the manifest's own name constraint
   [@ANCHOR: binary_downloader:binary_tenant_link_compute_symlink_path].
2. Creating the link immediately applies the real OS-level symlink pointing at the tenant's chosen
   version [@ANCHOR: binary_downloader:binary_tenant_link_create]; changing which version is active
   re-applies it, but only when `active_version_id` itself actually changed, not on every write
   [@ANCHOR: binary_downloader:binary_tenant_link_write].
3. `action_upgrade_to_latest()` repoints a tenant at the newest release of its manifest, or reports
   "already up to date" if there's nothing newer
   [@ANCHOR: binary_downloader:binary_tenant_link_action_upgrade_to_latest].
4. Deleting the link removes the real on-disk symlink, not just the database row
   [@ANCHOR: binary_downloader:binary_tenant_link_unlink].

## Scenario: Retiring a Binary Without Breaking a Sibling That Shares Its Bytes

Because two different manifests (or manifest/version pairs) can legitimately share the exact same
underlying binary content (an identical checksum), deleting one must never delete the shared on-disk
file out from under the other.

- `binary.manifest.unlink()` counts how many other manifest/version rows still reference its checksum before ever asking the shared cleanup helper to remove the file [@ANCHOR: binary_downloader:binary_manifest_unlink].

- `binary.version.unlink()` runs the identical dedup-by-checksum count [@ANCHOR: binary_downloader:binary_version_unlink].

- The shared cleanup helper itself only ever removes the on-disk file once both callers agree nothing else references it [@ANCHOR: binary_downloader:binary_utils_unlink_binary_file].
