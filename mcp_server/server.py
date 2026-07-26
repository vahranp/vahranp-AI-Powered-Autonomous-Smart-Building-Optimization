"""MCP server exposing live EnergyPlus building state as resources and
control actions as tools. Runs as a standalone stdio subprocess, spawned by
sim/mcp_bridge.py at the start of an "llm" strategy run and kept alive for
the whole run.

State is shared across the process boundary via data/live_state.json:
sim/energyplus_loop.py (the MCP *client*, running in the same process as
EnergyPlus) writes the current tick's real zone telemetry there before
asking the agents to reason about it; this server's resources read it back
fresh on every call. Tools don't need to persist anything server-side --
the calling client receives the tool's return value directly over the MCP
connection and is the one that actually applies it to EnergyPlus.
"""
import json
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("smart-building")

LIVE_STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "live_state.json")
EPLUS_ERR_PATH = os.path.join(os.path.dirname(__file__), "..", "sim", "output", "eplusout.err")

VALID_ZONES = {"Core_ZN", "Perimeter_ZN_1", "Perimeter_ZN_2", "Perimeter_ZN_3", "Perimeter_ZN_4"}


def _read_live_state() -> dict:
    if not os.path.exists(LIVE_STATE_PATH):
        return {}
    with open(LIVE_STATE_PATH) as f:
        return json.load(f)


def _tail_eplus_errors(max_lines: int = 8) -> list[str]:
    """Real log parsing, not a summary written by the control loop: reads
    EnergyPlus's own error file directly and extracts Warning/Severe lines.
    This is the file-parsing/error-extraction tool the LLM actually uses."""
    if not os.path.exists(EPLUS_ERR_PATH):
        return []
    with open(EPLUS_ERR_PATH, errors="replace") as f:
        lines = f.readlines()
    flagged = [line.strip() for line in lines if "Warning" in line or "Severe" in line]
    return flagged[-max_lines:]


@mcp.resource("building://zone_state")
def zone_state() -> dict:
    """Current temperature/humidity/CO2/occupancy/daylight per zone, from the live EnergyPlus run."""
    return _read_live_state().get("zones", {})


@mcp.resource("building://energy_meter")
def energy_meter() -> dict:
    """This tick's building electricity draw and HVAC/lighting/plug-load breakdown."""
    return _read_live_state().get("energy", {})


@mcp.resource("building://carbon_forecast")
def carbon_forecast() -> dict:
    """This tick's real EnergyPlus carbon-equivalent reading and outdoor weather trend."""
    return _read_live_state().get("carbon_and_weather", {})


@mcp.resource("building://comfort_standard")
def comfort_standard() -> str:
    """ASHRAE 55-derived comfort band used to ground agent reasoning.

    NOTE: kept as a literal string, not imported from sim/energyplus_loop.py's
    COMFORT_STANDARD_TEXT, because importing that module here would pull in
    the EnergyPlus API dependency for a lightweight MCP resource that isn't
    even in the active reasoning path (comfort_agent.py gets the real text
    passed directly by the caller, not via this resource). Must be kept in
    sync by hand -- this previously drifted to the OLD, narrower 21.0-24.0C
    band (identical to OCCUPIED_HEAT_C/OCCUPIED_COOL_C), the exact value the
    project's own tuning history identifies as a verified root cause of the
    "llm" strategy showing negative savings before it was widened. Unread by
    any client today, but a landmine if ever wired in."""
    return (
        "Acceptable operative temperature range when occupied: 20.5-25.5C (ASHRAE 55 Category B, "
        "PMV within +/-0.5 for typical business-casual clothing), humidity 30-60%, CO2 below 1000 ppm."
    )


@mcp.resource("building://recent_issues")
def recent_issues() -> str:
    """Self-correction feedback: recent guardrail interventions (proposed vs
    clipped setpoint per zone, from the live control loop) combined with a
    real tail of EnergyPlus's own error/warning log (parsed directly from
    eplusout.err, not summarized by the control loop). The arbiter reads
    this before deciding, so it can course-correct against its own recent
    mistakes rather than repeating them tick after tick."""
    guardrail_events = _read_live_state().get("recent_issues", [])
    eplus_lines = _tail_eplus_errors()

    parts = []
    if guardrail_events:
        parts.append("Recent guardrail interventions (your proposals that got clipped):")
        for e in guardrail_events[-5:]:
            parts.append(
                f"  tick {e['tick']} {e['zone']}: proposed heat={e['proposed']['heating_c']} "
                f"cool={e['proposed']['cooling_c']} -> clipped to "
                f"heat={e['clipped_to']['heating_c']} cool={e['clipped_to']['cooling_c']}"
                f"{' (via LLM)' if e.get('via_llm') else ''}"
            )
    else:
        parts.append("No recent guardrail interventions.")

    if eplus_lines:
        parts.append("Recent EnergyPlus warnings/errors:")
        parts.extend(f"  {line}" for line in eplus_lines)

    return "\n".join(parts)


@mcp.tool()
def set_zone_setpoint(zone: str, heating_c: float, cooling_c: float, rationale: str) -> dict:
    """Set final heating/cooling setpoints for a zone. Called by the arbiter once specialist
    proposals are resolved. Still subject to guardrail validation by the caller before EnergyPlus applies it."""
    if zone not in VALID_ZONES:
        return {"status": "rejected", "reason": f"unknown zone {zone}, must be one of {sorted(VALID_ZONES)}"}
    return {"zone": zone, "heating_c": heating_c, "cooling_c": cooling_c, "rationale": rationale, "status": "accepted"}


@mcp.tool()
def set_lighting_level(zone: str, pct: float, rationale: str) -> dict:
    """Set final lighting level (0.0-1.0 fraction) for a zone."""
    if zone not in VALID_ZONES:
        return {"status": "rejected", "reason": f"unknown zone {zone}, must be one of {sorted(VALID_ZONES)}"}
    return {"zone": zone, "pct": pct, "rationale": rationale, "status": "accepted"}


if __name__ == "__main__":
    mcp.run()
