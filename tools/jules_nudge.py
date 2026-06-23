#! /usr/bin/env python3
import os
import sys
import time
import requests
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - [Jules Nudger] - %(levelname)s - %(message)s"
)

# Jules API Base URL
JULES_API_BASE_URL = "https://jules.googleapis.com/v1alpha"

def get_headers():
    """Retrieve the API key and construct request headers."""
    api_key = os.environ.get("JULES_API_KEY")
    if not api_key:
        raise ValueError("JULES_API_KEY environment variable is missing. Please set it.")
    return {
        "X-Goog-Api-Key": api_key,
        "Content-Type": "application/json"
    }

def list_sessions(headers):
    """List all current Jules sessions."""
    url = f"{JULES_API_BASE_URL}/sessions"
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json().get("sessions", [])
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch sessions: {e}")
        return []

def send_message(session_name, prompt, headers):
    """Send a text message to a specific Jules session."""
    url = f"{JULES_API_BASE_URL}/{session_name}:sendMessage"
    payload = {"prompt": prompt}
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        logging.info(f"https://jules.app/{session_name}: Sent message '{prompt}'.")
    except requests.exceptions.RequestException as e:
        logging.error(f"https://jules.app/{session_name}: Failed to send message. Error: {e}")

def approve_plan(session_name, headers):
    """Approve the current plan for a Jules session."""
    url = f"{JULES_API_BASE_URL}/{session_name}:approvePlan"
    try:
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        logging.info(f"https://jules.app/{session_name}: Successfully approved plan.")
    except requests.exceptions.RequestException as e:
        logging.error(f"https://jules.app/{session_name}: Failed to approve plan. Error: {e}")

def process_session(session, headers):
    """Determine the session state and take the appropriate action."""
    session_name = session.get("name")
    state = session.get("state") 

    if not session_name or not state:
        return

    match state:
        case "AWAITING_USER_FEEDBACK":
            send_message(
                session_name, 
                "The user is not available for input. Make the decision autonomously, to the best of your ability.", 
                headers
            )
    
        case "AWAITING_PLAN_APPROVAL":
            approve_plan(session_name, headers)
    
        case "PAUSED" | "FAILED" | "STUCK":
            if "Jules encountered an error when cloning the repo" in str(session):
                logging.info(f"https://jules.app/{session_name}: Skipping 'Proceed' because a repo cloning error was detected.")
            else:
                send_message(session_name, "Proceed", headers)
    
        case "QUEUED" | "IN_PROGRESS" | "PR_READY":
            pass

        case "PR_SUBMITTED" | "COMPLETED":
            pass
    
        case _:
            logging.warning(f"https://jules.app/{session_name} is in unexpected state '{state}'.")

def main():
    logging.info("Starting Jules Nudger... (Press Ctrl-C to stop)")
    
    try:
        headers = get_headers()
    except ValueError as e:
        logging.critical(e)
        sys.exit(1)

    try:
        while True:
            sessions = list_sessions(headers)
            
            for session in sessions:
                process_session(session, headers)
                
            time.sleep(60)
            
    except KeyboardInterrupt:
        # This block catches the Ctrl-C (SIGINT)
        logging.info("\nCtrl-C received. Cleaning up...")
        # (Insert any explicit cleanup tasks here if needed, like closing persistent database connections)
        logging.info("Jules Nudger safely shut down.")
        sys.exit(0)

if __name__ == "__main__":
    main()
