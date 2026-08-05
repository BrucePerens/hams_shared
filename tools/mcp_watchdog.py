# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import glob
import time
import asyncio
import json
import re
import pyinotify
import logging
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Watchdog")
logger = logging.getLogger("mcp_watchdog")

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

class WatchdogEventHandler(pyinotify.ProcessEvent):
    def __init__(self, file_changed_event, changed_agents_set, expected_file=None):
        self.file_changed_event = file_changed_event
        self.changed_agents_set = changed_agents_set
        self.expected_file = expected_file

    def process_IN_MODIFY(self, event):
        self._handle(event)

    def process_IN_CREATE(self, event):
        self._handle(event)

    def _handle(self, event):
        if self.expected_file and event.pathname == self.expected_file:
            self.file_changed_event.set()

        if not event.pathname.endswith("transcript.jsonl"):
            return
        
        parts = event.path.split(os.sep)
        try:
            sysgen_idx = parts.index('.system_generated')
            agent_id = parts[sysgen_idx - 1]
            self.changed_agents_set.add(agent_id)
            self.file_changed_event.set()
        except ValueError:
            pass


def parse_family_tree(brain_dir, self_agent_id):
    parent_map = {}
    for agent_dir in glob.glob(os.path.join(brain_dir, "*")):
        agent_id = os.path.basename(agent_dir)
        transcript_path = os.path.join(agent_dir, ".system_generated", "logs", "transcript.jsonl")
        if not os.path.exists(transcript_path): continue
        try:
            with open(transcript_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if 'conversationId' in line and 'Created the following subagents' in line:
                        try:
                            entry = json.loads(line)
                            if entry.get("type") != "EPHEMERAL_MESSAGE":
                                content = entry.get("content", "")
                                if '"conversationId"' in content and "Created the following subagents" in content:
                                    matches = re.findall(r'"conversationId":\s*"([^"]+)"', content)
                                    for child_id in matches:
                                        parent_map[child_id] = agent_id
                        except:
                            pass
        except:
            pass
            
    root_id = self_agent_id
    while root_id in parent_map:
        root_id = parent_map[root_id]
        
    active_family = {root_id}
    def add_children(node):
        for child, parent in parent_map.items():
            if parent == node and child not in active_family:
                active_family.add(child)
                add_children(child)
    add_children(root_id)
    return active_family, parent_map, root_id

@mcp.tool()
async def wait_for_agent_state_change(target_agent_ids: list[str] = None, stall_mins: int = 5, max_wait_mins: int = 0, turn_warning_limit: int = 150, self_agent_id: str = None, alert_on_idle: bool = False, ignore_idle_for_ids: list[str] = None) -> str:
    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    timeout_secs = stall_mins * 60
    
    # Event-driven sync
    file_changed_event = asyncio.Event()
    changed_agents_set = set()
    
    wm = pyinotify.WatchManager()
    handler = WatchdogEventHandler(file_changed_event, changed_agents_set)
    notifier = pyinotify.AsyncioNotifier(wm, asyncio.get_event_loop(), default_proc_fun=handler)
    # rec=True and auto_add=True will recursively watch existing and automatically watch new directories
    wm.add_watch(brain_dir, pyinotify.IN_MODIFY | pyinotify.IN_CREATE, rec=True, auto_add=True)

    persisted = load_persisted_states(self_agent_id) or {}
    current_states = {}
    parent_map = {}
    root_id = None
    
    # Initial setup function
    def rebuild_tracked_agents():
        nonlocal parent_map, root_id
        if target_agent_ids:
            tracked = set(target_agent_ids)
            if self_agent_id:
                tracked.add(self_agent_id)
            return tracked
        elif self_agent_id:
            active_family, p_map, r_id = parse_family_tree(brain_dir, self_agent_id)
            parent_map = p_map
            root_id = r_id
            return active_family
        else:
            dirs = glob.glob(os.path.join(brain_dir, "*"))
            tracked = {os.path.basename(d) for d in dirs}
            if self_agent_id: tracked.add(self_agent_id)
            return tracked

    # Bootstrap initial state
    tracked_agents = rebuild_tracked_agents()
    now = time.time()
    for agent_id in tracked_agents:
        transcript_path = os.path.join(brain_dir, agent_id, ".system_generated", "logs", "transcript.jsonl")
        mtime = get_mtime(transcript_path)
        fsize = get_fsize(transcript_path)
        old_state = persisted.get(agent_id, {})
        current_states[agent_id] = {
            "mtime": mtime,
            "is_stalled": (now - mtime) > timeout_secs if mtime > 0 else False,
            "fsize": fsize,
            "path": transcript_path,
            "warned": old_state.get("warned", False)
        }

    last_sizes = {aid: state["fsize"] for aid, state in current_states.items()}
    start_time = time.time()
    
    while True:
        now = time.time()
        
        # Calculate dynamic wait timeout for the closest stall event
        min_time_until_stall = None
        for aid, state in current_states.items():
            if not state["is_stalled"] and state["mtime"] > 0:
                time_until = (state["mtime"] + timeout_secs) - now
                if min_time_until_stall is None or time_until < min_time_until_stall:
                    min_time_until_stall = time_until
                    
        # Max wait limit check
        max_wait_timeout = None
        if max_wait_mins > 0:
            max_wait_timeout = (start_time + (max_wait_mins * 60)) - now
            if max_wait_timeout <= 0:
                notifier.stop()
                save_persisted_states(current_states, self_agent_id)
                return f"wait-over: No state changes occurred within the {max_wait_mins} minute wait period."
                
        # Resolve final sleep timeout
        sleep_timeout = 3600.0 # Default fallback
        if min_time_until_stall is not None:
            sleep_timeout = max(0.1, min_time_until_stall)
        if max_wait_timeout is not None:
            sleep_timeout = min(sleep_timeout, max(0.1, max_wait_timeout))
            
        try:
            await asyncio.wait_for(file_changed_event.wait(), timeout=sleep_timeout)
            file_changed_event.clear()
        except asyncio.TimeoutError:
            pass # Timeout reached, meaning a stall threshold was crossed or max_wait ended!

        now = time.time()
        
        # Process newly changed agents from inotify
        if changed_agents_set:
            changed_ids = list(changed_agents_set)
            changed_agents_set.clear()
            
            # Rebuild tree if necessary (only needed if self_agent_id is monitoring a family)
            if self_agent_id and not target_agent_ids:
                tracked_agents = rebuild_tracked_agents()
            
            for agent_id in changed_ids:
                if agent_id not in tracked_agents:
                    continue # Ignore agents outside our scope
                    
                transcript_path = os.path.join(brain_dir, agent_id, ".system_generated", "logs", "transcript.jsonl")
                mtime = get_mtime(transcript_path)
                fsize = get_fsize(transcript_path)
                
                if agent_id not in current_states:
                    current_states[agent_id] = {
                        "mtime": mtime,
                        "is_stalled": False,
                        "fsize": fsize,
                        "path": transcript_path,
                        "warned": False
                    }
                    notifier.stop()
                    save_persisted_states(current_states, self_agent_id)
                    return f"New agent {agent_id} detected."
                    
                current_states[agent_id]["mtime"] = mtime
                current_states[agent_id]["fsize"] = fsize
                current_states[agent_id]["is_stalled"] = False

        # Evaluate states for alerts
        for agent_id, state in current_states.items():
            init_state = persisted.get(agent_id, {})
            mtime = state["mtime"]
            is_stalled = state["is_stalled"]
            fsize = state["fsize"]
            transcript_path = state["path"]
            
            # Check turn limit
            if turn_warning_limit > 0 and not state.get("warned") and fsize > 0:
                try:
                    if time.time() - mtime > 86400:
                        state["warned"] = True
                        continue
                        
                    with open(transcript_path, 'r', encoding='utf-8') as f:
                        lines = sum(1 for line in f if line.strip())
                    if False and lines >= turn_warning_limit:
                        state["warned"] = True
                        notifier.stop()
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
                
            # Stalled Check
            new_is_stalled = (now - mtime) > timeout_secs if mtime > 0 else False
            if new_is_stalled and not state["is_stalled"]:
                state["is_stalled"] = True
                notifier.stop()
                save_persisted_states(current_states, self_agent_id)
                return f"Agent {agent_id} stalled/finished (idle for > {stall_mins}m). ACTION REQUIRED: You must immediately alert your Orchestrator to investigate the subagent logs for potential software bugs or SKILL.md issues, and correct them."
            
            # Heartbeat check – ensure agents are still alive via heartbeat file
            HEARTBEAT_PATH = os.path.expanduser("~/workspace/tmp/agent_heartbeats.log")
            try:
                hb_mtime = os.path.getmtime(HEARTBEAT_PATH)
                if now - hb_mtime > stall_mins * 60:
                    notifier.stop()
                    save_persisted_states(current_states, self_agent_id)
                    return f"Heartbeat stale (no agent heartbeat in > {stall_mins}m). ACTION REQUIRED: Investigate agents and ensure heartbeats are being written."
            except OSError:
                # Heartbeat file may not exist yet – ignore
                pass
                
            # Resumed Check
            if not new_is_stalled and init_state.get("is_stalled"):
                notifier.stop()
                save_persisted_states(current_states, self_agent_id)
                return f"Agent {agent_id} resumed activity."
                
            # Content Change Check (sent messages)
            last_size = last_sizes.get(agent_id, init_state.get("fsize", 0))
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
                                    is_orchestrator = (agent_id in parent_map.values()) or (agent_id == root_id)
                                    if (ignore_idle_for_ids is None or agent_id not in ignore_idle_for_ids) and not is_orchestrator:
                                        notifier.stop()
                                        save_persisted_states(current_states, self_agent_id)
                                        return f"Agent {agent_id} stalled/finished (idle/finished). ACTION REQUIRED: You must immediately alert your Orchestrator to investigate the subagent logs for potential software bugs or SKILL.md issues, and correct them."
                                for call in tool_calls:
                                    if call.get('name') == 'send_message' or call.get('toolName') == 'send_message':
                                        notifier.stop()
                                        save_persisted_states(current_states, self_agent_id)
                                        return f"Agent {agent_id} sent a message."
                        except json.JSONDecodeError:
                            pass
                except OSError:
                    pass
            last_sizes[agent_id] = fsize

        # Exit condition: all monitored agents are inactive
        other_targets = [aid for aid in (target_agent_ids or []) if aid != self_agent_id]
        if target_agent_ids and other_targets:
            target_active_agents = sum(1 for aid, state in current_states.items() if not state.get("is_stalled") and aid in other_targets)
            if target_active_agents == 0:
                notifier.stop()
                save_persisted_states(current_states, self_agent_id)
                return "All target agents are stalled, finished, or gone."
        else:
            active_agents = sum(1 for aid, state in current_states.items() if not state.get("is_stalled") and aid != self_agent_id)
            if active_agents == 0:
                notifier.stop()
                save_persisted_states(current_states, self_agent_id)
                return "All agents (except orchestrators) are gone or idle."

@mcp.tool()
async def wait_for_result(target_agent_id: str, expected_file: str = None, timeout_mins: int = 15, self_agent_id: str = None, turn_warning_limit: int = 150) -> str:
    """
    Wait for a specific agent to finish a task, send a message, or for a file to be ready.
    Returns immediately when the condition is met, if the target agent stalls, or if you receive a message.
    """
    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    timeout_secs = timeout_mins * 60
    
    file_changed_event = asyncio.Event()
    changed_agents_set = set()
    
    wm = pyinotify.WatchManager()
    handler = WatchdogEventHandler(file_changed_event, changed_agents_set, expected_file=expected_file)
    notifier = pyinotify.AsyncioNotifier(wm, asyncio.get_event_loop(), default_proc_fun=handler)
    
    wm.add_watch(brain_dir, pyinotify.IN_MODIFY | pyinotify.IN_CREATE, rec=True, auto_add=True)
    if expected_file:
        expected_dir = os.path.dirname(expected_file)
        if os.path.exists(expected_dir):
            wm.add_watch(expected_dir, pyinotify.IN_MODIFY | pyinotify.IN_CREATE)
            
    transcript_path = os.path.join(brain_dir, target_agent_id, ".system_generated", "logs", "transcript.jsonl")
    
    start_time = time.time()
    last_mtime = get_mtime(transcript_path)
    last_size = get_fsize(transcript_path)
    
    self_transcript_path = os.path.join(brain_dir, self_agent_id, ".system_generated", "logs", "transcript.jsonl") if self_agent_id else None
    self_last_size = get_fsize(self_transcript_path) if self_transcript_path else 0
    warned = False
    
    while True:
        now = time.time()
        
        # Check expected file
        if expected_file and os.path.exists(expected_file) and os.path.getsize(expected_file) > 0:
            notifier.stop()
            return f"Condition met: {expected_file} is ready."
            
        # Check timeout
        if now - start_time > timeout_secs:
            notifier.stop()
            return f"Wait timeout reached after {timeout_mins} minutes."
            
        # Check self for messages
        if self_transcript_path:
            self_fsize = get_fsize(self_transcript_path)
            if self_fsize > self_last_size:
                notifier.stop()
                return "You received a message."
                
        # Wait for events
        time_left = max(0.1, timeout_secs - (now - start_time))
        try:
            await asyncio.wait_for(file_changed_event.wait(), timeout=time_left)
            file_changed_event.clear()
        except asyncio.TimeoutError:
            pass
            
        # Re-check self for messages
        if self_transcript_path:
            self_fsize = get_fsize(self_transcript_path)
            if self_fsize > self_last_size:
                notifier.stop()
                return "You received a message."
                
        # Re-check agent state
        mtime = get_mtime(transcript_path)
        fsize = get_fsize(transcript_path)
        
        if mtime > 0 and (time.time() - mtime) > timeout_secs:
            notifier.stop()
            return f"Agent {target_agent_id} stalled (idle for > {timeout_mins}m)."
            
        # Check turn warning limit
        if turn_warning_limit > 0 and not warned and fsize > 0:
            try:
                with open(transcript_path, 'r', encoding='utf-8') as f:
                    lines = sum(1 for line in f if line.strip())
                if lines >= turn_warning_limit:
                    warned = True
                    notifier.stop()
                    return f"Agent {target_agent_id} is approaching its turn limit ({lines} turns). It has ~50 turns remaining. ACTION REQUIRED: Instruct Agent {target_agent_id} to gracefully finish its work, notify you when it's ready for a hand-over, and exit. Then spawn a replacement."
            except OSError:
                pass
                
        if fsize > last_size:
            try:
                with open(transcript_path, 'r', encoding='utf-8') as f:
                    f.seek(last_size)
                    new_content = f.read()
                    
                for line in new_content.strip().split('\\n'):
                    if not line: continue
                    try:
                        entry = json.loads(line)
                        if entry.get('source') == 'MODEL' and entry.get('type') == 'PLANNER_RESPONSE':
                            tool_calls = entry.get('tool_calls', [])
                            if not tool_calls:
                                notifier.stop()
                                return f"Agent {target_agent_id} finished without sending a message."
                            for call in tool_calls:
                                if call.get('name') == 'send_message' or call.get('toolName') == 'send_message':
                                    notifier.stop()
                                    return f"Agent {target_agent_id} sent a message."
                    except json.JSONDecodeError:
                        pass
            except OSError:
                pass
            last_size = fsize

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
    parser.add_argument("--ignore_idle_for_ids", type=str, nargs="*", default=None, help="Agent IDs to ignore for alert_on_idle")
    
    args = parser.parse_args()
    
    if args.cli:
        result = asyncio.run(wait_for_agent_state_change(
            target_agent_ids=args.target_agent_ids,
            stall_mins=args.stall_mins,
            max_wait_mins=args.max_wait_mins,
            turn_warning_limit=args.turn_warning_limit,
            self_agent_id=args.self_agent_id,
            alert_on_idle=args.alert_on_idle,
            ignore_idle_for_ids=args.ignore_idle_for_ids
        ))
        print(result)
        sys.exit(0)
    else:
        mcp.run()
