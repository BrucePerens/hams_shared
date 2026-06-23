#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

# Ensure Jules CLI is installed
if ! command -v jules &> /dev/null; then
    echo "Error: Jules CLI is not installed. Please install it via npm install -g @google/jules"
    exit 1
fi

echo "Fetching active sessions for the current repository..."

# Use jules remote list to capture all active sessions, filtering by the repo
jules remote list --session | grep -v 'ID' | awk '{print $1}' | while read -r SESSION_ID; do
    if [ -n "$SESSION_ID" ]; then
        echo "Destroying Jules session ID: $SESSION_ID"
        # Terminate/delete the active session
        jules remote cancel-session --session "$SESSION_ID"
    fi
done

echo "All active Jules sessions in the repository have been cancelled."

