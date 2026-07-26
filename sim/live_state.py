"""Shared read/write for data/live_dashboard_state.json -- the real, latest
per-tick snapshot the Operations Console's live panels poll.

Deliberately dependency-free (stdlib only) so backend/main.py can import it
directly without pulling in sim.energyplus_loop's pyenergyplus/mcp imports.

Kept separate from data/live_state.json (owned by sim/mcp_bridge.py and read
by mcp_server.py's MCP resources) -- that file's schema is the actual MCP
context contract the LLM agents depend on, only written during "llm"
strategy ticks. This one is written every tick regardless of strategy, so
the dashboard has a live snapshot even for reactive/predictive runs.
"""
import json
import os
import time

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
LIVE_DASHBOARD_STATE_PATH = os.path.join(PROJECT_ROOT, "data", "live_dashboard_state.json")
os.makedirs(os.path.dirname(LIVE_DASHBOARD_STATE_PATH), exist_ok=True)


def write_dashboard_state(state: dict) -> None:
    """Called from EnergyPlus's own ctypes callback (on_zone_timestep) --
    an uncaught exception here doesn't propagate as a normal Python
    exception (ctypes can't pass it back into the C caller; it just prints
    "Exception ignored" and the callback returns early), which means the
    REST of that callback -- including db.commit() for every RunTick/
    Decision this tick already added -- silently never runs. Confirmed for
    real: os.replace() intermittently raised PermissionError on Windows
    (another process transiently holding the destination file open, e.g.
    the backend's own concurrent read of this same file) mid-run. Retrying
    briefly, then giving up and skipping just this tick's dashboard
    snapshot, is far safer than letting that abort the tick's DB commit --
    the next tick overwrites this file a few seconds later regardless."""
    tmp_path = LIVE_DASHBOARD_STATE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f)
    for attempt in range(3):
        try:
            os.replace(tmp_path, LIVE_DASHBOARD_STATE_PATH)  # atomic on both Windows and POSIX -- avoids a reader seeing a half-written file
            return
        except PermissionError:
            if attempt == 2:
                return  # give up silently -- next tick's write will supersede this one anyway
            time.sleep(0.05)


def read_dashboard_state() -> dict:
    if not os.path.exists(LIVE_DASHBOARD_STATE_PATH):
        return {}
    try:
        with open(LIVE_DASHBOARD_STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
