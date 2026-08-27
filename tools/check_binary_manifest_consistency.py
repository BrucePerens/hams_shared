#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Checks that hams_shared/tools/binary_manifest.json and
binary_downloader/data/binary_manifest_data.xml agree with each other.

Real bug this guards against, found 2026-08-27 bumping kopia/etcd/cloudflared: the two files hold
the same url/checksum/archive_type/extract_member data in two different formats, but nothing kept
them in sync. binary_manifest.json is what dependency_watch.json / check_dependency_releases.py
track; binary_manifest_data.xml is the actual seed data binary_downloader.mixin._download_and_extract()
reads via the ORM at runtime. It is easy to update one and forget the other -- a bump that "looks"
done (the tracked JSON says so) can leave production downloading the OLD binary indefinitely,
silently. This check parses both and fails loudly on any mismatch or one-sided entry.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET


def load_json_manifest(json_path):
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_xml_manifest(xml_path):
    tree = ET.parse(xml_path)
    entries = {}
    for record in tree.getroot().iter("record"):
        if record.get("model") != "binary.manifest":
            continue
        fields = {f.get("name"): (f.text or "") for f in record.findall("field")}
        name = fields.get("name")
        if not name:
            continue
        entries[name] = {
            "url": fields.get("url", ""),
            "checksum": fields.get("checksum", ""),
            "type": fields.get("archive_type", ""),
            "extract_member": fields.get("extract_member", ""),
        }
    return entries


def compare(json_manifest, xml_manifest):
    errors = []
    json_keys = set(json_manifest.keys())
    xml_keys = set(xml_manifest.keys())

    for missing in sorted(json_keys - xml_keys):
        errors.append(
            f"'{missing}' is in binary_manifest.json but has no matching <record> "
            f"(name='{missing}') in binary_manifest_data.xml -- binary_downloader's runtime seed "
            f"data will never see this entry."
        )
    for missing in sorted(xml_keys - json_keys):
        errors.append(
            f"'{missing}' is in binary_manifest_data.xml but not in binary_manifest.json -- "
            f"dependency_watch.json's upstream-version tracking has nothing to check it against."
        )

    for key in sorted(json_keys & xml_keys):
        json_entry = json_manifest[key]
        xml_entry = xml_manifest[key]
        # extract_member is only meaningful for tar.gz-type entries; a plain binary entry
        # legitimately omits it in the JSON side (see cloudflared), so only compare it when
        # the JSON side actually declares one.
        fields_to_check = ["url", "checksum", "type"]
        if json_entry.get("extract_member"):
            fields_to_check.append("extract_member")
        for field in fields_to_check:
            json_val = json_entry.get(field, "")
            xml_val = xml_entry.get(field, "")
            if json_val != xml_val:
                errors.append(
                    f"'{key}'.{field} differs: binary_manifest.json has {json_val!r}, "
                    f"binary_manifest_data.xml has {xml_val!r}."
                )
    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: check_binary_manifest_consistency.py <repo_root>")
        sys.exit(1)

    repo_root = sys.argv[1]
    json_path = os.path.join(repo_root, "hams_shared", "tools", "binary_manifest.json")
    xml_path = os.path.join(
        repo_root, "binary_downloader", "data", "binary_manifest_data.xml"
    )

    if not os.path.isfile(json_path) or not os.path.isfile(xml_path):
        # Not every checkout has both hams_open (binary_downloader) and hams_shared side by
        # side under the same root -- nothing to compare in that case, not an error.
        sys.exit(0)

    json_manifest = load_json_manifest(json_path)
    xml_manifest = load_xml_manifest(xml_path)
    errors = compare(json_manifest, xml_manifest)

    if errors:
        print("❌ Binary Manifest Consistency Violations:")
        for error in errors:
            print(f"  ❌  ERROR: {error}")
        print(f"\nTotal Errors (Binary Manifest Consistency): {len(errors)}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
