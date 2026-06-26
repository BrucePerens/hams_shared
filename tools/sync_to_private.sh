#!/usr/bin/env bash
# ==============================================================================
# Synchronizes architectural files, tools, and documentation from hams_community
# to hams_private. Community module code is intentionally ignored, but their 
# architectural documents located in docs/modules/ are synchronized.
# ==============================================================================

set -e

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <path_to_hams_community> <path_to_hams_private>"
    echo "Example: $0 . ../hams_private"
    exit 1
fi

COMMUNITY_DIR="$1"
PRIVATE_DIR="$2"

if [ ! -f "$COMMUNITY_DIR/AGENTS.md" ]; then
    echo "Error: Source directory '$COMMUNITY_DIR' does not appear to be the hams_community root (missing AGENTS.md)."
    exit 1
fi

if [ ! -d "$PRIVATE_DIR" ]; then
    echo "Error: Target directory '$PRIVATE_DIR' does not exist."
    exit 1
fi

echo "============================================================"
echo "Syncing hams_community -> hams_private"
echo "Source: $COMMUNITY_DIR"
echo "Target: $PRIVATE_DIR"
echo "============================================================"

echo "[*] Syncing tools/ ..."
# We use rsync without --delete to ensure we don't destroy private-specific tools
rsync -av --exclude='__pycache__' "$COMMUNITY_DIR/tools/" "$PRIVATE_DIR/tools/"

echo "[*] Syncing docs/ ..."
# We use rsync without --delete to ensure we don't destroy private-specific docs
# This automatically includes docs/modules/<module-name>.md
rsync -av "$COMMUNITY_DIR/docs/" "$PRIVATE_DIR/docs/"

echo "[*] Syncing root architectural & configuration files ..."
for file in AGENTS.md requirements.txt .gitignore; do
    if [ -f "$COMMUNITY_DIR/$file" ]; then
        cp -pv "$COMMUNITY_DIR/$file" "$PRIVATE_DIR/$file"
        echo "Copied $file"
    fi
done

echo "============================================================"
echo "[+] Sync Complete!"
echo "Note: Community module directories were intentionally ignored."
echo "Only their architectural documentation in docs/modules/ was synced."
