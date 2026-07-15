import os
import glob
import time
import asyncio
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Sidecar Watchdog")

def get_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0

def get_fsize(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0

@mcp.tool()
async def wait_for_agent_state_change(target_agent_ids: list[str] = None, timeout_mins: int = 5, max_wait_mins: int = 0) -> str:
    """
    Waits until a monitored agent changes state (stalls for more than timeout_mins, resumes, or finishes),
    or until all monitored agents are gone.
    If target_agent_ids is provided, only those specific conversation IDs are monitored.
    If max_wait_mins is > 0, the tool will return a 'wait-over' event after that many minutes if no state changes occur.
    """
    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    timeout_secs = timeout_mins * 60
    
    def get_agent_dirs():
        if target_agent_ids:
            return [os.path.join(brain_dir, aid) for aid in target_agent_ids]
        return glob.glob(os.path.join(brain_dir, "*"))

    def get_states():
        states = {}
        now = time.time()
        for agent_dir in get_agent_dirs():
            if not os.path.exists(agent_dir):
                continue
            agent_id = os.path.basename(agent_dir)
            transcript_path = os.path.join(agent_dir, ".system_generated", "logs", "transcript.jsonl")
            if os.path.exists(transcript_path):
                mtime = get_mtime(transcript_path)
                age = now - mtime
                fsize = get_fsize(transcript_path)
                states[agent_id] = {
                    "mtime": mtime, 
                    "is_stalled": age > timeout_secs,
                    "fsize": fsize,
                    "path": transcript_path
                }
        return states

    initial_states = get_states()
    last_sizes = {aid: state["fsize"] for aid, state in initial_states.items()}
    start_time = time.time()
    
    while True:
        await asyncio.sleep(5)
        current_states = get_states()
        now = time.time()
        
        if max_wait_mins > 0 and (now - start_time) >= (max_wait_mins * 60):
            return f"wait-over: No state changes occurred within the {max_wait_mins} minute wait period."
        
        # Check for state changes and new messages
        for agent_id, state in current_states.items():
            if agent_id not in initial_states:
                return f"New agent {agent_id} detected."
                
            init_state = initial_states[agent_id]
            mtime = state["mtime"]
            is_stalled = state["is_stalled"]
            fsize = state["fsize"]
            transcript_path = state["path"]
            
            # Check for stalled/resumed states
            if mtime > init_state["mtime"] and init_state["is_stalled"]:
                return f"Agent {agent_id} resumed activity."
                
            if is_stalled and not init_state["is_stalled"]:
                return f"Agent {agent_id} stalled/finished (idle for > {timeout_mins}m)."
                
            # Check for newly sent messages
            last_size = last_sizes.get(agent_id, init_state["fsize"])
            if fsize != last_size:
                if fsize > last_size:
                    try:
                        with open(transcript_path, 'r', encoding='utf-8') as f:
                            f.seek(last_size)
                            new_content = f.read()
                            
                        for line in new_content.strip().split('\n'):
                            if not line: continue
                            try:
                                entry = json.loads(line)
                                if 'tool_calls' in entry:
                                    for call in entry['tool_calls']:
                                        if call.get('name') == 'send_message' or call.get('toolName') == 'send_message':
                                            return f"Agent {agent_id} sent a message."
                            except json.JSONDecodeError:
                                pass
                    except OSError:
                        pass
                last_sizes[agent_id] = fsize

        active_agents = sum(1 for state in current_states.values() if not state["is_stalled"])
        
        if target_agent_ids and active_agents == 0:
            return "All target agents are stalled, finished, or gone."
            
        if not target_agent_ids and active_agents <= 2:
            return "All agents (except orchestrators) are gone or idle."

if __name__ == "__main__":
    mcp.run()
