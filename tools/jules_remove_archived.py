import os
import sys
import requests

# 1. Retrieve the API Key from the environment variables
API_KEY = os.getenv("JULES_API_KEY")

if not API_KEY:
    print("Error: The JULES_API_KEY environment variable is not set.", file=sys.stderr)
    print("Please set it using: export JULES_API_KEY='your_key_here'", file=sys.stderr)
    sys.exit(1)

# Base configuration for the Jules REST API
BASE_URL = "https://jules.googleapis.com/v1alpha"
HEADERS = {
    "x-goog-api-key": API_KEY,
    "Content-Type": "application/json"
}

def get_archived_sessions():
    """Fetches all sessions and filters for those in the ARCHIVED state."""
    url = f"{BASE_URL}/sessions"
    archived_sessions = []
    page_token = None

    print("Fetching sessions from Jules API...")
    
    while True:
        params = {}
        if page_token:
            params["pageToken"] = page_token

        response = requests.get(url, headers=HEADERS, params=params)
        
        if response.status_code != 200:
            print(f"Error fetching sessions: {response.status_code} - {response.text}", file=sys.stderr)
            sys.exit(1)
            
        data = response.json()
        sessions = data.get("sessions", [])
        
        for session in sessions:
            # Check if the session's state is 'ARCHIVED'
            state = session.get("state")
            print(f"State: {state}")
            if state == "ARCHIVED":
                archived_sessions.append(session)
        
        # Handle pagination if there are many sessions
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return archived_sessions

def delete_session(session_name):
    """Deletes a single session permanently using its resource name."""
    # session_name is formatted as "sessions/{sessionId}"
    url = f"{BASE_URL}/{session_name}"
    
    response = requests.delete(url, headers=HEADERS)
    
    if response.status_code == 200 or response.status_code == 204:
        print(f"Successfully deleted: {session_name}")
        return True
    else:
        print(f"Failed to delete {session_name}: {response.status_code} - {response.text}", file=sys.stderr)
        return False

def main():
    # Retrieve all archived sessions
    archived = get_archived_sessions()
    
    if not archived:
        print("No archived sessions found to clean up.")
        return

    print(f"Found {len(archived)} archived session(s). Starting deletion...")
    
    success_count = 0
    for session in archived:
        # The delete endpoint requires the full resource path (e.g., 'sessions/1234567')
        resource_name = session.get("name")
        if resource_name:
            if delete_session(resource_name):
                success_count += 1
                
    print(f"\nCleanup complete. Successfully deleted {success_count}/{len(archived)} archived sessions.")

if __name__ == "__main__":
    main()

