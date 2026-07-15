# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import glob
import time
import asyncio
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Watchdog")

def get_state_file(self_agent_id):
    prefix = f"_{self_agent_id}" if self_agent_id else ""
    return os.path.expanduser(f"~/.gemini/antigravity/scratch/watchdog_state{prefix}.json")

def load_persisted_states(self_agent_id):
    state_file = get_state_file(self_agent_id)
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_persisted_states(states, self_agent_id):
    state_file = get_state_file(self_agent_id)
    os.makedirs(os.path.dirname(state_file), exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(states, f)

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
async def wait_for_agent_state_change(target_agent_ids: list[str] = None, stall_mins: int = 5, max_wait_mins: int = 0, turn_warning_limit: int = 150, self_agent_id: str = None, alert_on_idle: bool = False) -> str:
    """
    Waits until a monitored agent changes state (stalls for more than stall_mins, resumes, or finishes),
    or until all monitored agents are gone.
    If target_agent_ids is provided, only those specific conversation IDs are monitored.
    If max_wait_mins is > 0, the tool will return a 'wait-over' event after that many minutes if no state changes occur.
    If turn_warning_limit is > 0, it will return a warning if an agent's transcript line count exceeds this limit.
    """
    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    timeout_secs = stall_mins * 60
    
    def get_agent_dirs():
        dirs = []
        if target_agent_ids:
            dirs = [os.path.join(brain_dir, aid) for aid in target_agent_ids]
        else:
            dirs = glob.glob(os.path.join(brain_dir, "*"))
        if self_agent_id:
            self_dir = os.path.join(brain_dir, self_agent_id)
            if self_dir not in dirs:
                dirs.append(self_dir)
        return dirs

    def get_states(persisted_states=None):
        persisted = persisted_states or {}
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
                old_state = persisted.get(agent_id, {})
                states[agent_id] = {
                    "mtime": mtime, 
                    "is_stalled": age > timeout_secs,
                    "fsize": fsize,
                    "path": transcript_path,
                    "warned": old_state.get("warned", False)
                }
        return states

    persisted = load_persisted_states(self_agent_id)
    initial_states = persisted if persisted is not None else get_states()
    last_sizes = {aid: state["fsize"] for aid, state in initial_states.items()}
    start_time = time.time()
    
    first_iteration = True
    while True:
        if not first_iteration:
            await asyncio.sleep(5)
        first_iteration = False
        
        current_states = get_states(initial_states)
        now = time.time()
        
        if max_wait_mins > 0 and (now - start_time) >= (max_wait_mins * 60):
            save_persisted_states(current_states, self_agent_id)
            return f"wait-over: No state changes occurred within the {max_wait_mins} minute wait period."
        
        # Check for state changes and new messages
        for agent_id, state in current_states.items():
            if agent_id not in initial_states:
                save_persisted_states(current_states, self_agent_id)
                return f"New agent {agent_id} detected."
                
            init_state = initial_states[agent_id]
            mtime = state["mtime"]
            is_stalled = state["is_stalled"]
            fsize = state["fsize"]
            transcript_path = state["path"]
            
            # Check for turn limit
            if turn_warning_limit > 0 and not state.get("warned"):
                try:
                    with open(transcript_path, 'r', encoding='utf-8') as f:
                        lines = sum(1 for line in f if line.strip())
                    if lines >= turn_warning_limit:
                        state["warned"] = True
                        save_persisted_states(current_states, self_agent_id)
                        if self_agent_id and agent_id == self_agent_id:
                            return f"You (Agent {agent_id}) are approaching your turn limit ({lines} turns). You have ~50 turns remaining. ACTION REQUIRED: Gracefully finish your work, notify your Orchestrator to do the hand-over now, and exit."
                        else:
                            return f"Agent {agent_id} is approaching its turn limit ({lines} turns). It has ~50 turns remaining. ACTION REQUIRED: Instruct Agent {agent_id} to gracefully finish its work, notify you when it's ready for a hand-over, and exit. Then spawn a replacement."
                except OSError:
                    pass
            if self_agent_id and agent_id == self_agent_id:
                last_sizes[agent_id] = fsize
                continue
            
            # Check for stalled/resumed states
            if mtime > init_state["mtime"] and init_state["is_stalled"]:
                save_persisted_states(current_states, self_agent_id)
                return f"Agent {agent_id} resumed activity."
                
            if is_stalled and not init_state["is_stalled"]:
                save_persisted_states(current_states, self_agent_id)
                return f"Agent {agent_id} stalled/finished (idle for > {stall_mins}m). ACTION REQUIRED: You must immediately alert your Orchestrator via send_message."
                
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
                                if entry.get('source') == 'MODEL' and entry.get('type') == 'PLANNER_RESPONSE':
                                    tool_calls = entry.get('tool_calls', [])
                                    if alert_on_idle and not tool_calls:
                                        save_persisted_states(current_states, self_agent_id)
                                        return f"Agent {agent_id} stalled/finished (idle/finished). ACTION REQUIRED: You must immediately alert your Orchestrator via send_message."
                                    for call in tool_calls:
                                        if call.get('name') == 'send_message' or call.get('toolName') == 'send_message':
                                            save_persisted_states(current_states, self_agent_id)
                                            return f"Agent {agent_id} sent a message."
                            except json.JSONDecodeError as e:  # audit-ignore-catch-all
                                import logging
                                logging.getLogger(__name__).warning("JSONDecodeError: %s", e)
                    except OSError as e:  # audit-ignore-catch-all
                        import logging
                        logging.getLogger(__name__).warning("OSError: %s", e)
                last_sizes[agent_id] = fsize

        other_targets = [aid for aid in (target_agent_ids or []) if aid != self_agent_id]
        
        if target_agent_ids and other_targets:
            target_active_agents = sum(1 for aid, state in current_states.items() if not state.get("is_stalled") and aid in other_targets)
            if target_active_agents == 0:
                save_persisted_states(current_states, self_agent_id)
                return "All target agents are stalled, finished, or gone."
        else:
            active_agents = sum(1 for aid, state in current_states.items() if not state.get("is_stalled") and aid != self_agent_id)
            if active_agents <= 2:
                save_persisted_states(current_states, self_agent_id)
                return "All agents (except orchestrators) are gone or idle."

if __name__ == "__main__":
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(description="MCP Watchdog Server/CLI")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode")
    parser.add_argument("--target_agent_ids", type=str, nargs="*", default=None, help="Agent IDs to monitor")
    parser.add_argument("--stall_mins", type=int, default=5, help="Stall timeout in minutes")
    parser.add_argument("--max_wait_mins", type=int, default=15, help="Max wait in minutes")
    parser.add_argument("--turn_warning_limit", type=int, default=150, help="Turn warning limit")
    parser.add_argument("--self_agent_id", type=str, default=None, help="Agent ID of the monitor")
    parser.add_argument("--alert_on_idle", action="store_true", help="Instantly alert if an agent stops calling tools")
    
    args = parser.parse_args()
    
    if args.cli:
        result = asyncio.run(wait_for_agent_state_change(
            target_agent_ids=args.target_agent_ids,
            stall_mins=args.stall_mins,
            max_wait_mins=args.max_wait_mins,
            turn_warning_limit=args.turn_warning_limit,
            self_agent_id=args.self_agent_id,
            alert_on_idle=args.alert_on_idle
        ))
        print(result)
        sys.exit(0)
    else:
        mcp.run()
