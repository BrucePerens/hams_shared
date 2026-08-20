# flake8: noqa
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import glob
import time
import asyncio
import json
import re
import socket
import queue as stdlib_queue
import threading
import functools
import pyinotify
import logging
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("mcp_watchdog")

SESSION_REGISTRY = {}

# --- In-process message queues (replaces the old per-call AF_UNIX socket
# rendezvous). Since this server now runs as ONE shared process for every
# agent (Claude, Gemini's Conductor, Gemini's subagents), send_ipc_message
# and wait_for_inbox can just be a dict of Queue: sends never block on a
# listener existing yet, and messages queue up in order if nobody is
# listening yet. _QUEUE_META tracks enough per-queue state (last send/receive
# time, whether a receiver is currently blocked) for queue_status to tell
# apart a dead/never-started receiver from a dead/never-started sender.
#
# Deliberately stdlib threading.Queue, not asyncio.Queue: FastMCP's
# streamable-http transport hardcodes its own Starlette lifespan
# (`lambda app: self.session_manager.run()`) and does not invoke the
# `lifespan=` constructor argument the way its docstring suggests -- there is
# no reliable hook to start an asyncio-native background listener bound to
# the *same* event loop the HTTP transport ends up running on. A thread-safe
# stdlib Queue sidesteps the question entirely: the legacy bridge below runs
# in its own plain thread with a blocking accept loop, wait_for_inbox reaches
# it via run_in_executor, and none of this cares which transport (stdio,
# sse, streamable-http) or event loop the MCP server itself is using.
_QUEUES: dict[str, "stdlib_queue.Queue"] = {}
_QUEUE_META: dict[str, dict] = {}
_QUEUE_LOCK = threading.Lock()

def _get_queue(name: str):
    with _QUEUE_LOCK:
        if name not in _QUEUES:
            _QUEUES[name] = stdlib_queue.Queue()
            _QUEUE_META[name] = {
                "created_at": time.time(),
                "last_put_at": None,
                "last_get_at": None,
                "waiting_since": None,
                "waiters": 0,
            }
        return _QUEUES[name]

# --- Legacy bridge for plain (non-MCP-agent) Python processes ---
# narrative_daemon.py, accessory_daemon.py, and friends are ordinary
# subprocesses, not MCP clients -- they can't call send_ipc_message as a
# tool. This ONE persistent Unix-domain socket (bound once when the server
# process starts, never rebound per-call, unlike the old per-queue-name
# sockets) lets them keep pushing messages in with a tiny, dependency-free
# client (see daemon_utils.send_ipc_message). Wire format: connect, write
# "<queue_name>\n<content>", close -- one connection is one message, same
# framing convention the old per-queue sockets used. Runs in its own daemon
# thread, started once from __main__ before the MCP server itself starts.
_BRIDGE_SOCK_PATH = os.path.expanduser("~/workspace/tmp/mcp_watchdog_ipc.sock")

def _handle_bridge_conn(conn):
    try:
        chunks = []
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks).decode("utf-8", errors="replace")
        if "\n" not in raw:
            logger.warning("[legacy bridge] dropped message with no queue-name header")
            return
        queue_name, content = raw.split("\n", 1)
        queue_name = queue_name.strip()
        if queue_name.startswith("/"):
            queue_name = queue_name[1:]
        if not queue_name:
            return
        q = _get_queue(queue_name)
        q.put(content)
        _QUEUE_META[queue_name]["last_put_at"] = time.time()
    except Exception as e:
        logger.warning(f"[legacy bridge] error handling client: {e}")
    finally:
        conn.close()

def _run_legacy_bridge():
    if os.path.exists(_BRIDGE_SOCK_PATH):
        try:
            os.remove(_BRIDGE_SOCK_PATH)
        except OSError:
            pass
    os.makedirs(os.path.dirname(_BRIDGE_SOCK_PATH), exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(_BRIDGE_SOCK_PATH)
    srv.listen(16)
    logger.info(f"[legacy bridge] listening on {_BRIDGE_SOCK_PATH}")
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=_handle_bridge_conn, args=(conn,), daemon=True).start()

def start_legacy_bridge():
    threading.Thread(target=_run_legacy_bridge, daemon=True).start()

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

def is_agent_dead(transcript_path):
    try:
        if not os.path.exists(transcript_path):
            return True
        with open(transcript_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line: continue
            try:
                entry = json.loads(line)
                if entry.get("type") in ("CANCELLATION", "TERMINATION", "ERROR", "KILLED"):
                    return True
                if entry.get("status") in ("CANCELLED", "KILLED", "FAILED", "ERROR", "TERMINATED"):
                    return True
                if entry.get("source") == "SYSTEM" and entry.get("type") == "SYSTEM_MESSAGE":
                    content = entry.get("content", "").lower()
                    if "killed" in content or "cancelled" in content or "terminated" in content:
                        return True
                if entry.get("source") == "MODEL" and entry.get("type") == "PLANNER_RESPONSE":
                    if not entry.get("tool_calls", []):
                        return True
                    return False
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return False

@mcp.tool()
async def wait_for_agent_state_change(target_agent_ids: list[str] = None, stall_mins: int = 5, max_wait_mins: int = 0, turn_warning_limit: int = 150, self_agent_id: str = None, alert_on_idle: bool = False, ignore_idle_for_ids: list[str] = None, heartbeat_file: str = None) -> str:
    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    timeout_secs = stall_mins * 60
    
    # Event-driven sync
    file_changed_event = asyncio.Event()
    changed_agents_set = set()
    
    wm = pyinotify.WatchManager()
    handler = WatchdogEventHandler(file_changed_event, changed_agents_set)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    notifier = pyinotify.AsyncioNotifier(wm, loop, default_proc_fun=handler)
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
                if is_agent_dead(transcript_path):
                    state["is_stalled"] = True
                    save_persisted_states(current_states, self_agent_id)
                    continue
                else:
                    state["is_stalled"] = True
                    notifier.stop()
                    save_persisted_states(current_states, self_agent_id)
                    return f"Agent {agent_id} stalled/finished (idle for > {stall_mins}m). ACTION REQUIRED: You must immediately alert your Orchestrator to investigate the subagent logs for potential software bugs or SKILL.md issues, and correct them."

            
            # Heartbeat check – ensure agents are still alive via heartbeat file
            hb_files = []
            if heartbeat_file:
                hb_files = glob.glob(os.path.expanduser(heartbeat_file))
            else:
                hb_files = glob.glob(os.path.expanduser("~/workspace/tmp/agent_heartbeats_*.log"))
                if not hb_files:
                    hb_files = [os.path.expanduser("~/workspace/tmp/agent_heartbeats.log")]
                    
            for hb_path in hb_files:
                try:
                    hb_mtime = os.path.getmtime(hb_path)
                    if now - hb_mtime > stall_mins * 60:
                        notifier.stop()
                        save_persisted_states(current_states, self_agent_id)
                        return f"Heartbeat stale (no agent heartbeat in > {stall_mins}m for {hb_path}). ACTION REQUIRED: Investigate agents and ensure heartbeats are being written."
                except OSError:
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
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    notifier = pyinotify.AsyncioNotifier(wm, loop, default_proc_fun=handler)
    
    wm.add_watch(brain_dir, pyinotify.IN_MODIFY | pyinotify.IN_CREATE, rec=True, auto_add=True)
    if expected_file:
        expected_dir = os.path.dirname(expected_file)
        if os.path.exists(expected_dir):
            wm.add_watch(expected_dir, pyinotify.IN_MODIFY | pyinotify.IN_CREATE | pyinotify.IN_MOVED_TO)
            
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
            if is_agent_dead(transcript_path):
                return f"Agent {target_agent_id} is dead/finished."
            else:
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
                    
                for line in new_content.strip().split('\n'):
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

@mcp.tool()
async def wait_for_all_complete(
    agent_ids: list[str],
    output_files: list[str] = None,
    timeout_mins: int = 30,
    self_agent_id: str = None
) -> str:
    """
    Blocks until all specified agents have reached terminal state (idle, killed, cancelled, or errored).
    Returns a JSON string containing the completion status of each agent and optional file outputs.
    """
    brain_dir = os.path.expanduser('~/.gemini/antigravity/brain')
    completed = set()
    
    # Initial check
    for aid in agent_ids:
        transcript = os.path.join(brain_dir, aid, '.system_generated/logs/transcript.jsonl')
        if is_agent_dead(transcript):
            completed.add(aid)
            
    if len(completed) < len(agent_ids):
        wm = pyinotify.WatchManager()
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        changed_set = set()

        handler = WatchdogEventHandler(event, changed_set)
        notifier = pyinotify.AsyncioNotifier(wm, loop, default_proc_fun=handler)
        wm.add_watch(brain_dir, pyinotify.IN_MODIFY | pyinotify.IN_CREATE, rec=True, auto_add=True)

        start_time = time.time()
        timeout_secs = timeout_mins * 60
        try:
            while len(completed) < len(agent_ids):
                elapsed = time.time() - start_time
                if elapsed >= timeout_secs:
                    break
                    
                for aid in agent_ids:
                    if aid not in completed:
                        transcript = os.path.join(brain_dir, aid, '.system_generated/logs/transcript.jsonl')
                        if is_agent_dead(transcript):
                            completed.add(aid)
                            
                if len(completed) == len(agent_ids):
                    break
                    
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            notifier.stop()

    results = {
        "agents": {aid: "COMPLETED" if aid in completed else "TIMED_OUT" for aid in agent_ids}
    }
    
    if output_files:
        outputs = []
        for file_path in output_files:
            try:
                with open(file_path, 'r') as f:
                    outputs.append({
                        "file": file_path,
                        "content": f.read()
                    })
            except Exception as e:
                outputs.append({
                    "file": file_path,
                    "error": str(e)
                })
        results["outputs"] = outputs
        
    return json.dumps(results, indent=2)

@mcp.tool()
def send_ipc_message(queue_name: str, content: str) -> str:
    """
    Deliver a message to whatever wait_for_inbox(queue_name=...) call is, or
    later will be, blocked on this queue name. Delivery is immediate and
    in-process (a thread-safe Queue keyed by queue_name) -- there is no OS
    socket involved and no need to retry waiting for a listener to appear: if
    nobody is receiving yet, the message just waits in order until they do.
    Use queue_status(queue_name) to check whether anyone is actually
    receiving.
    """
    if queue_name.startswith('/'):
        queue_name = queue_name[1:]
    q = _get_queue(queue_name)
    q.put(content)
    _QUEUE_META[queue_name]["last_put_at"] = time.time()
    return "Message sent successfully."

@mcp.tool()
def queue_status(queue_name: str) -> str:
    """
    Diagnose a queue without consuming from it: how many messages are
    pending, whether a receiver is currently blocked inside wait_for_inbox,
    and how long since any send/receive activity. Use this to distinguish
    "a receiver is waiting but the sender is idle/dead" from "messages are
    piling up because nobody is receiving" -- from either side alone, both
    look identical (silence), and this pipeline has been bitten by exactly
    that ambiguity before.
    """
    if queue_name.startswith('/'):
        queue_name = queue_name[1:]
    if queue_name not in _QUEUE_META:
        return json.dumps({
            "queue": queue_name,
            "exists": False,
            "detail": "No message has ever been sent or waited for on this queue name.",
        })
    meta = _QUEUE_META[queue_name]
    q = _QUEUES[queue_name]
    now = time.time()
    pending = q.qsize()
    waiting = meta["waiters"] > 0
    result = {
        "queue": queue_name,
        "exists": True,
        "pending_messages": pending,
        "receiver_currently_waiting": waiting,
        "seconds_since_last_send": (now - meta["last_put_at"]) if meta["last_put_at"] else None,
        "seconds_since_last_receive": (now - meta["last_get_at"]) if meta["last_get_at"] else None,
        "seconds_since_waiter_arrived": (now - meta["waiting_since"]) if meta["waiting_since"] else None,
    }
    if pending > 0 and not waiting:
        result["diagnosis"] = "Messages are piling up with nobody waiting to receive them -- likely a dead or never-started receiver."
    elif waiting and pending == 0:
        wait_s = result["seconds_since_waiter_arrived"] or 0
        if wait_s > 120:
            result["diagnosis"] = f"A receiver has been waiting {wait_s:.0f}s with nothing sent -- likely a dead or never-started sender."
        else:
            result["diagnosis"] = "A receiver is waiting; no message yet, but it hasn't been long."
    else:
        result["diagnosis"] = "Looks healthy."
    return json.dumps(result, indent=2)

@mcp.tool()
async def wait_for_fatal_events(session_id: str, target_agent_ids: list) -> str:
    """
    Blocks indefinitely until a fatal event occurs (e.g. a managed daemon crashes, 
    or a target agent stalls for > 5 minutes without writing to its transcript).
    Returns the fatal error string.
    """
    import asyncio
    import os
    import time
    
    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    
    while True:
        # 1. Check if any background daemons crashed
        if session_id in SESSION_REGISTRY and SESSION_REGISTRY[session_id]["dead"]:
            return f"DAEMON_CRASH: {SESSION_REGISTRY[session_id]['error']}"
            
        # 2. Check if any agents stalled
        now = time.time()
        for aid in target_agent_ids:
            transcript = os.path.join(brain_dir, aid, '.system_generated/logs/transcript.jsonl')
            if os.path.exists(transcript):
                mtime = os.path.getmtime(transcript)
                if now - mtime > 300: # 5 minutes
                    return f"AGENT_STALL: Agent {aid} has stalled (no activity for > 5 minutes)."
                    
        # 3. Check SLA expectations (files not produced in time)
        if session_id in SESSION_REGISTRY:
            for exp in list(SESSION_REGISTRY[session_id].get("expectations", [])):
                if os.path.exists(exp["file"]):
                    SESSION_REGISTRY[session_id]["expectations"].remove(exp) # Met!
                elif now > exp["deadline"]:
                    SESSION_REGISTRY[session_id]["expectations"].remove(exp) # Fire alarm and remove
                    return f"MISSING_OUTPUT: The expected file {exp['file']} was not produced in {exp['timeout']} minutes. Interrogate the responsible agent."
                    
        await asyncio.sleep(5)

@mcp.tool()
async def spawn_managed_daemons(session_id: str, commands_json: str) -> str:
    """
    Spawn and monitor a set of background Python daemons for a session.
    commands_json should be a JSON array of dicts: [{"name": "daemon1", "cmd": ["python3", "script.py", "--arg"]}]
    """
    import asyncio
    import json
    
    try:
        commands = json.loads(commands_json)
    except Exception as e:
        return f"Invalid JSON format for commands: {e}"
        
    if session_id in SESSION_REGISTRY:
        return f"Session {session_id} is already managed."
        
    SESSION_REGISTRY[session_id] = {"dead": False, "error": "", "procs": [], "expectations": []}
    
    async def monitor_proc(proc, name):
        code = await proc.wait()
        if code != 0 and not SESSION_REGISTRY[session_id]["dead"]:
            SESSION_REGISTRY[session_id]["dead"] = True
            SESSION_REGISTRY[session_id]["error"] = f"Daemon '{name}' (PID {proc.pid}) crashed with exit code {code}."
            for p in SESSION_REGISTRY[session_id]["procs"]:
                if p != proc and p.returncode is None:
                    try:
                        p.terminate()
                    except ProcessLookupError:
                        pass
    
    try:
        for item in commands:
            name = item.get("name", "unknown")
            cmd = item.get("cmd", [])
            if not cmd: continue
            
            log_file = os.path.expanduser(f"~/workspace/tmp/{name}_{session_id}.log")
            out_f = open(log_file, "a")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=out_f,
                stderr=out_f
            )
            SESSION_REGISTRY[session_id]["procs"].append(proc)
            asyncio.create_task(monitor_proc(proc, name))
            
        return f"Successfully spawned {len(SESSION_REGISTRY[session_id]['procs'])} daemons for session {session_id}."
    except Exception as e:
        SESSION_REGISTRY[session_id]["dead"] = True
        SESSION_REGISTRY[session_id]["error"] = f"Failed to spawn daemons: {e}"
        return SESSION_REGISTRY[session_id]["error"]

@mcp.tool()
async def wait_for_inbox(queue_name: str, timeout_mins: int = 15, self_agent_id: str = None, writer_agent_id: str = None, writer_pid: int = None, session_id: str = None) -> str:
    """
    Wait for the specified inbox message on an in-process queue. Returns
    immediately if a message is already pending (sent before this call
    started). If writer_pid or session_id identifies a dead writer, returns
    WRITER_DEAD so the agent can stop polling instead of waiting out the full
    timeout for a sender that will never come.
    """
    if queue_name.startswith('/'):
        queue_name = queue_name[1:]

    q = _get_queue(queue_name)
    meta = _QUEUE_META[queue_name]
    with _QUEUE_LOCK:
        meta["waiters"] += 1
        if meta["waiting_since"] is None:
            meta["waiting_since"] = time.time()

    timeout_secs = timeout_mins * 60
    check_interval = 5  # Check writer liveness every 5 seconds
    elapsed = 0.0
    loop = asyncio.get_running_loop()
    try:
        while elapsed < timeout_secs:
            wait_time = min(check_interval, timeout_secs - elapsed)
            try:
                # q.get(timeout=...) is a blocking stdlib call -- run it in the
                # default executor so it doesn't block the event loop while
                # other tool calls (queue_status, send_ipc_message, the
                # watchdog file-monitoring tools) need to keep running.
                raw_str = await loop.run_in_executor(None, functools.partial(q.get, timeout=wait_time))
                meta["last_get_at"] = time.time()

                try:
                    payload = json.loads(raw_str)
                    if isinstance(payload, dict) and "_meta_expectation" in payload:
                        exp = payload["_meta_expectation"]
                        if session_id and session_id in SESSION_REGISTRY:
                            SESSION_REGISTRY[session_id].setdefault("expectations", []).append({
                                "file": exp["file"],
                                "timeout": exp.get("timeout_mins", 60),
                                "deadline": time.time() + exp.get("timeout_mins", 60) * 60
                            })
                        return payload.get("prompt", raw_str)
                except Exception:
                    pass

                return raw_str
            except stdlib_queue.Empty:
                elapsed += wait_time
                if session_id and session_id in SESSION_REGISTRY:
                    if SESSION_REGISTRY[session_id]["dead"]:
                        return f"WRITER_DEAD: {SESSION_REGISTRY[session_id]['error']} You MUST stop polling and exit immediately."
                elif writer_pid:
                    try:
                        os.kill(writer_pid, 0)
                    except ProcessLookupError:
                        return f"WRITER_DEAD: The writer process (PID {writer_pid}) is no longer running. You MUST stop polling and exit immediately."
                    except PermissionError:
                        pass
        return "Timeout waiting for inbox message."
    finally:
        with _QUEUE_LOCK:
            meta["waiters"] = max(0, meta["waiters"] - 1)
            if meta["waiters"] == 0:
                meta["waiting_since"] = None

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
    parser.add_argument("--heartbeat_file", type=str, default=None, help="Path to heartbeat log to monitor")
    parser.add_argument("--transport", type=str, default="stdio", choices=["stdio", "sse", "streamable-http"],
                         help="MCP transport when not in --cli mode. Defaults to stdio for backward compatibility "
                              "with existing configs that launch this script directly via command/args (Claude "
                              "Code's and Antigravity's current mcp_watchdog entries both do this, with no "
                              "--transport flag -- defaulting this to streamable-http would make every one of those "
                              "launches start ITS OWN competing HTTP server instead of talking stdio, breaking them "
                              "outright). Pass --transport streamable-http explicitly to run the one shared server "
                              "instance multiple clients then connect to via URL instead of a spawn command.")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on for streamable-http/sse transports")

    args = parser.parse_args()

    if args.cli:
        result = asyncio.run(wait_for_agent_state_change(
            target_agent_ids=args.target_agent_ids,
            stall_mins=args.stall_mins,
            max_wait_mins=args.max_wait_mins,
            turn_warning_limit=args.turn_warning_limit,
            self_agent_id=args.self_agent_id,
            alert_on_idle=args.alert_on_idle,
            ignore_idle_for_ids=args.ignore_idle_for_ids,
            heartbeat_file=args.heartbeat_file
        ))
        print(result)
        sys.exit(0)
    else:
        if args.transport == "streamable-http":
            # Only the one deliberately-launched shared instance should own
            # the legacy bridge socket -- a per-client stdio instance (what
            # Claude Code's and Antigravity's current configs both spawn) is
            # ephemeral and per-process, not the shared queue store, and
            # binding the same fixed path here would silently steal it from
            # whichever process is actually playing that role.
            start_legacy_bridge()
            mcp.settings.port = args.port
        elif args.transport == "sse":
            mcp.settings.port = args.port
        mcp.run(transport=args.transport)
