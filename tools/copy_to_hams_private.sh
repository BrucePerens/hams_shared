#!/bin/bash

# Target repository path. Can be passed as the first argument.
TARGET_REPO="${1:-../hams_private}"

if [ ! -d "$TARGET_REPO" ]; then
    echo "Target directory '$TARGET_REPO' does not exist. Creating it..."
    mkdir -p "$TARGET_REPO"
fi

echo "Copying tools and documents updated in the past week to $TARGET_REPO..."

# Find files modified in the past week
git log --since="1 week ago" --name-only --format="" | sort | uniq | while read -r file; do
    # Filter for tools and documents
    if [[ "$file" == tools/* ]] || [[ "$file" == docs/* ]] || [[ "$file" == *.md ]] || [[ "$file" == *.txt ]] || [[ "$file" == *.html ]]; then
        if [ -f "$file" ]; then
            echo "Copying $file..."

            # Create target directory structure
            target_dir="$TARGET_REPO/$(dirname "$file")"
            mkdir -p "$target_dir"

            # Copy file
            cp "$file" "$TARGET_REPO/$file"
        fi
    fi
done

echo "Copy complete."
