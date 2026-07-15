import os
import glob
import time
import argparse
import asyncio
import sys
from google.antigravity import Agent, LocalAgentConfig

def get_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0

async def send_alert(agent_id, message):
    alert_file = os.path.expanduser("~/.gemini/antigravity/stalled_alerts.txt")
    try:
        with open(alert_file, "a") as f:
            f.write(f"{agent_id}|{message}\n")
    except Exception as e:
        print(f"Failed to alert {agent_id}: {e}")

async def monitor(super_agent, ignatz, timeout_mins):
    try:
        brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
        timeout_secs = timeout_mins * 60
        notified_stalled = set()

        print(f"Starting Sidecar Watchdog. Monitoring {brain_dir} (Timeout: {timeout_mins}m)")
        
        while True:
            now = time.time()
            agent_dirs = glob.glob(os.path.join(brain_dir, "*"))
            
            for agent_dir in agent_dirs:
                agent_id = os.path.basename(agent_dir)
                transcript_path = os.path.join(agent_dir, ".system_generated", "logs", "transcript.jsonl")
                
                if not os.path.exists(transcript_path):
                    continue
                    
                mtime = get_mtime(transcript_path)
                age = now - mtime
                
                if age > timeout_secs:
                    if agent_id in notified_stalled:
                        continue # Already notified for this stall incident
                    
                    print(f"[{time.strftime('%X')}] Agent {agent_id} stalled/finished. Age: {age:.0f}s")
                    
                    # Notify
                    if agent_id == super_agent:
                        print("Waking up Super Agent...")
                        await send_alert(super_agent, "SYSTEM ALERT: You have been idle for too long. Please wake up and report status.")
                    elif agent_id == ignatz:
                        print("Waking up Ignatz...")
                        await send_alert(ignatz, "SYSTEM ALERT: You have been idle for too long. Please wake up and proceed with orchestration.")
                    else:
                        print(f"Notifying Ignatz about sub-agent {agent_id}...")
                        await send_alert(ignatz, f"SYSTEM ALERT: Your sub-agent {agent_id} has stopped logging for over {timeout_mins} minutes. It may have finished silently or crashed. Please verify its progress and proceed.")
                    
                    notified_stalled.add(agent_id)
                else:
                    # If the agent wakes up and updates its transcript, remove from notified list
                    if agent_id in notified_stalled:
                        notified_stalled.remove(agent_id)
                        print(f"[{time.strftime('%X')}] Agent {agent_id} resumed activity.")

            # Sleep before next poll
            await asyncio.sleep(60)
    except Exception as e:
        error_msg = f"SYSTEM ALERT: Python Sidecar crashed! Exception: {e}"
        print(error_msg)
        await send_alert(super_agent, error_msg)
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Antigravity Watchdog Sidecar")
    parser.add_argument("--super-agent", required=True, help="Conversation ID of the Super Agent")
    parser.add_argument("--ignatz", required=True, help="Conversation ID of Ignatz")
    parser.add_argument("--timeout", type=int, default=5, help="Stall timeout in minutes")
    args = parser.parse_args()
    
    asyncio.run(monitor(args.super_agent, args.ignatz, args.timeout))
