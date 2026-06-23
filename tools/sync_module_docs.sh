#!/bin/bash
# Automatically syncs module README.md files to the docs/modules/ directory.
# Discovers module names dynamically from their directory structures.

set -e

DOCS_DIR="docs/modules"
mkdir -p "$DOCS_DIR"

echo "Syncing README.md files..."

find . -type f -name "README.md" -not -path "./docs/*" -not -path "./README.md" | while read -r doc_file; do
    mod_dir=$(dirname "$doc_file")
    mod_name=$(basename "$mod_dir")

    # Skip top-level directories that aren't Odoo modules
    if [ ! -f "$mod_dir/__manifest__.py" ]; then
        continue
    fi

    cp "$doc_file" "$DOCS_DIR/${mod_name}.md"
    echo "  -> Synced: $mod_name.md"
done

echo "Sync complete."
