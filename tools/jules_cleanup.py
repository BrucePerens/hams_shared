#! /usr/bin/env python3
import os
import sys
import requests

# 1. Setup API configuration variables
# Retrieve your token from https://jules.google.com/settings
API_KEY = os.environ.get("JULES_API_KEY")
BASE_URL = "https://jules.googleapis.com/v1alpha/sessions"

if not API_KEY:
    print("Error: The JULES_API_KEY environment variable is not set.", file=sys.stderr)
    print("Please set it using: export JULES_API_KEY='your_key_here'", file=sys.stderr)
    sys.exit(1)

# Pass authentication via the required x-goog-api-key header
headers = {
    "x-goog-api-key": API_KEY,
    "Content-Type": "application/json"
}

def clean_completed_sessions():
    print("Fetching sessions from Jules API...")
    try:
        response = requests.get(BASE_URL, headers=headers)
        response.raise_for_status()
        data = response.json()
        # print(f"Response: {data}")
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch sessions: {e}", file=sys.stderr)
        sys.exit(1)
        
    sessions = data.get("sessions", [])
    if not sessions:
        print("No active or historical sessions found.")
        return

    completed_count = 0
    
    # 2. Iterate through sessions and isolate completed tasks
    for session in sessions:
        session_id = session.get("id")
        # Completed sessions are reported as "DONE" or "COMPLETED" in the REST API payload
        status = session.get("state").upper()
        
        if status in ["DONE", "COMPLETED"]:
            print(f"Clearing Completed Session ID: {session_id}...")
            
            # 3. Request task deletion from the session layout
            delete_url = f"{BASE_URL}/{session_id}"
            try:
                del_response = requests.delete(delete_url, headers=headers)
                if del_response.status_code in [200, 204]:
                    print(f"Successfully removed session {session_id}.")
                    completed_count += 1
                else:
                    print(f"Failed to delete {session_id}: HTTP {del_response.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"Error executing delete request for {session_id}: {e}")

    print(f"\nTask cleanup complete. Total tasks archived/cleared: {completed_count}")

if __name__ == "__main__":
    clean_completed_sessions()

