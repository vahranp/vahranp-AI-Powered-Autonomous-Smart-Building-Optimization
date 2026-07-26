"""Real EnergyPlus Python API control loop, validated against
building_models/CURRENT_modified_RefBldgSmallOffice.idf (per-zone heating/cooling/lighting
schedules added so each can be actuated independently; solar PV, CO2
modeling, and daylighting reference points added to the building model --
see ARCHITECTURE.md for what each addition required).

Four control strategies, selectable via CONTROL_STRATEGY env var:
  - "ashrae_baseline" -- fixed, code-minimum operating schedule: constant
                    occupied-hours setpoints during a FIXED time window (not
                    real-time occupancy sensing), fixed setback outside it,
                    fixed-schedule lighting (no daylight harvesting). No
                    forecast lookahead, no demand response, no AI -- this is
                    the "no controls credit" reference point ASHRAE 90.1
                    Appendix G-style baselines use buildings' real performance
                    against. Meant to be run once as a reference; every other
                    strategy's baseline_kwh is then populated live, tick by
                    tick, from this run's real energy (see the baseline
                    lookup in on_zone_timestep below) -- NOT a full Appendix G
                    Performance Rating Method model (that would also swap the
                    HVAC system type/envelope per the building's climate zone,
                    which is out of scope here); this reproduces the specific
                    thing Appendix G baselines are known for -- crediting NO
                    adaptive/smart control -- on the same real building model.
  - "reactive"   -- occupancy-setback rule, decides only on current state.
  - "predictive" -- same rule, but nudges setpoints ahead of a forecast
                    outdoor temperature swing (EnergyPlus's own lookahead
                    weather API) and ahead of a zone's own historical
                    occupancy pattern from earlier in this same run
                    ("meeting room prep"), so the zone doesn't have to work
                    as hard reactively once the swing/arrival happens.
  - "llm"        -- hierarchical hybrid control, NOT an LLM call every tick
                    (an 8B local model takes ~35s/call x 4 calls/invocation,
                    which made per-tick invocation top out around 3-5 ticks
                    before a 100+ tick run became impractical). Two layers:

                    STRATEGIC layer (slow, event-driven): the real multi-agent
                    LLM pipeline (agents/*.py + local Ollama + the real MCP
                    server, bridged by sim/mcp_bridge.py) runs in a background
                    thread, invoked only when _evaluate_llm_trigger() below
                    detects a real reason to re-plan -- checked in priority
                    order: an occupancy STATE change (any zone crossing
                    occupied<->unoccupied -- the most decision-relevant
                    occupancy event, checked before an aggregate headcount
                    delta so it can't get diluted by four other unchanged
                    zones), a smaller cumulative occupancy shift, a weather
                    swing, a statistical energy anomaly, a zone's energy
                    drifting well above what it was when its policy was
                    issued, sustained comfort degradation, a rising
                    guardrail-intervention rate, or (last resort) a
                    confidence-decay heartbeat -- capped at LLM_MAX_INVOCATIONS
                    total real calls for the whole run. It outputs a per-zone
                    POLICY (target setpoints + rationale), not a single tick's
                    actuator command.

                    TACTICAL layer (fast, every tick): the existing
                    deterministic controller (decide_setpoints/decide_lighting,
                    the same one "reactive"/"predictive" use) applies the
                    latest cached policy to occupied zones every single tick,
                    still gated by real-time occupancy/guardrails -- an
                    unoccupied zone is ALWAYS setback regardless of any
                    cached policy, and every applied setpoint still passes
                    through guardrails/validator.py unchanged. This means the
                    simulation is NEVER blocked waiting on the LLM: EnergyPlus
                    keeps ticking at full speed on the current policy while a
                    new one computes in the background, and the new policy
                    takes over the instant it's ready.

                    Real-time occupancy-proportional scaling
                    (scale_cached_setpoints_for_occupancy/
                    scale_cached_lighting_for_occupancy): a live run showed
                    a zone dropping from several people to just one still
                    reapplying a cached policy sized for the fuller room --
                    still "occupied" (no state-crossing trigger), and
                    inference is never instant regardless of how sensitive
                    the triggers are. Rather than only trying to shrink that
                    latency further, the tactical layer now blends the
                    cached policy toward the cheaper fallback edge in direct
                    proportion to how much occupancy has actually dropped
                    since the policy was issued -- zero additional LLM
                    calls, zero latency, every tick. This is what actually
                    closes most of the energy-savings gap a stale cached
                    policy was causing, rather than just triggering more
                    often and still lagging real occupancy by however long
                    one inference call takes. See ARCHITECTURE.md for the
                    full design writeup.

Also: simulated sensor-fault injection with neighbor-zone fallback, a
statistical energy-anomaly detector, demand-response load shedding,
daylight-harvesting lighting control, and per-tick attribution covering
every quantity the dashboard surfaces (drivers JSON on each Decision row).
"""
import json
import os
import statistics
import sys
import threading
import time
from collections import defaultdict

EPLUS_DIR = os.environ.get("ENERGYPLUS_DIR", r"C:\EnergyPlusV26-1-0")
sys.path.insert(0, EPLUS_DIR)

from pyenergyplus.api import EnergyPlusAPI  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agents import arbiter, carbon_agent, comfort_agent, energy_agent  # noqa: E402
from backend.db import Decision, PipelineEvent, ResilienceEvent, RunTick, SessionLocal, init_db  # noqa: E402
from guardrails.validator import validate_setpoint  # noqa: E402
from sim import live_state  # noqa: E402
from sim.mcp_bridge import MCPAgentBridge  # noqa: E402

IDF_PATH = os.path.join(os.path.dirname(__file__), "..", "building_models", "CURRENT_modified_RefBldgSmallOffice.idf")
EPW_PATH = os.path.join(os.path.dirname(__file__), "..", "building_models", "weather.epw")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")

ZONES = ["Core_ZN", "Perimeter_ZN_1", "Perimeter_ZN_2", "Perimeter_ZN_3", "Perimeter_ZN_4"]
PERIMETER_ZONES = ["Perimeter_ZN_1", "Perimeter_ZN_2", "Perimeter_ZN_3", "Perimeter_ZN_4"]  # have windows/daylighting
ADJACENCY = {
    "Core_ZN": ["Perimeter_ZN_1", "Perimeter_ZN_2", "Perimeter_ZN_3", "Perimeter_ZN_4"],
    "Perimeter_ZN_1": ["Core_ZN", "Perimeter_ZN_2", "Perimeter_ZN_4"],
    "Perimeter_ZN_2": ["Core_ZN", "Perimeter_ZN_1", "Perimeter_ZN_3"],
    "Perimeter_ZN_3": ["Core_ZN", "Perimeter_ZN_2", "Perimeter_ZN_4"],
    "Perimeter_ZN_4": ["Core_ZN", "Perimeter_ZN_3", "Perimeter_ZN_1"],
}
CONTROL_EVERY_N_TIMESTEPS = 4  # every ~30-60 sim minutes depending on model timestep resolution
STRATEGY = os.environ.get("CONTROL_STRATEGY", "reactive")  # "ashrae_baseline" | "reactive" | "predictive" | "llm"

OCCUPIED_HEAT_C, OCCUPIED_COOL_C = 21.0, 24.0
# SETBACK_HEAT_C was 17.0 -- BELOW guardrails/validator.py's own
# SAFE_HEATING_RANGE_C floor of 18.0. That meant every single unoccupied
# heating-setback tick, for every strategy that reaches this constant
# (reactive, predictive, and "llm"'s reactive fallback), got silently
# clipped to 18.0 -- the REAL applied value was always 18.0 regardless, so
# this change alters zero simulated physics. What it removes is false-
# positive guardrail_intervened noise: that flag is supposed to mean "a
# proposal was genuinely risky and got corrected," not "this constant
# always trips the same clip by construction." Verified as the dominant
# contributor to a measured 137 guardrail interventions on a real run.
SETBACK_HEAT_C, SETBACK_COOL_C = 18.0, 27.0
PRECONDITION_MARGIN_C = 1.0
COLD_SNAP_THRESHOLD_C = -2.0
HEAT_WAVE_THRESHOLD_C = 2.0
LOOKAHEAD_HOURS = 3

# ashrae_baseline's fixed occupied window, taken from the building's own real
# design occupancy schedule (BLDG_OCC_SCH in the IDF: weekday ramp starts
# 06:00, tapers off after 20:00) -- a FIXED clock window, not a live occupancy
# read, is the whole point of a baseline. Setback is 18.0/28.0C, not the
# tighter 17.0/27.0C the reactive/predictive rule uses, because those exact
# values sit right at guardrails/validator.py's SAFE_HEATING_RANGE_C /
# SAFE_COOLING_RANGE_C bounds (18.0-24.0 / 22.0-28.0) -- picking values
# already inside that envelope means this fixed schedule never trips
# guardrail_intervened, which should mean "the AI/rule proposed something
# risky", not "this is what a static schedule always does".
ASHRAE_BASELINE_OCC_START_HOUR, ASHRAE_BASELINE_OCC_END_HOUR = 6, 20
ASHRAE_BASELINE_SETBACK_HEAT_C, ASHRAE_BASELINE_SETBACK_COOL_C = 18.0, 28.0

LIGHTING_OCCUPIED_PCT = 0.9
LIGHTING_UNOCCUPIED_PCT = 0.05
DAYLIGHT_HARVEST_LUX_THRESHOLD = 400.0  # above this, dim lighting proportionally
# For a zone with no daylight sensor (no windows, e.g. Core_ZN), occupant
# COUNT is the only real signal available for how bright it needs to be --
# 2.0 reuses the same "clearly occupied" threshold compute_confidence()
# already treats as unambiguous (below it, a fractional headcount is still
# mid-transition/sparse), rather than inventing a new arbitrary number.
LIGHTING_FULL_OCCUPANCY_REF = 2.0

DEMAND_RESPONSE_WINDOW = 12
DEMAND_RESPONSE_MULTIPLIER = 1.4
DEMAND_RESPONSE_RELIEF_C = 1.0

ANOMALY_MIN_HISTORY = 12
ANOMALY_Z_THRESHOLD = 2.5

# Simulated sensor fault: one zone's readings are treated as unreliable for
# a tick window, forcing a fallback to neighbor-zone estimates.
FAULT_ZONE = "Perimeter_ZN_2"
FAULT_TICK_START, FAULT_TICK_END = 20, 30

# --- Adaptive LLM invocation policy (see the "llm" strategy docstring above) ---
# LLM_MAX_INVOCATIONS is the real cost cap: total real LLM calls for the
# whole run (each invocation = 4 sequential calls: 3 specialists + arbiter,
# plus up to one arbiter retry). Measured on this project's hardware (RTX
# 3050 4GB, llama3.1:8b, partial GPU offload): ~120-230s/invocation.
# Raised to 45 -- the explicit goal became "the LLM should genuinely decide
# ~90% of ticks, not the reactive fallback," which needs both (a) enough
# budget that a policy essentially never goes untrusted purely from running
# out of invocations (the freshness check below treats any cached policy as
# trusted indefinitely WHILE budget remains, regardless of age) and (b) an
# invocation available basically any time a real trigger fires across the
# whole occupied window, not just the first ~25. ~45 x ~175s avg = ~130 min
# worst case, explicitly accepted -- a longer-running but far more
# genuinely agent-driven simulation, not a demo-speed compromise.
LLM_MAX_INVOCATIONS = int(os.environ.get("LLM_MAX_INVOCATIONS", "45"))
# Confidence-decay floor: if nothing else has triggered a re-plan in this
# many ticks, invoke anyway -- a policy this stale is assumed to have decayed
# confidence even with no explicit event. This is a FLOOR among several
# trigger types below, not the scheduling mechanism itself. Occupancy-driven
# triggers (plus zone_never_covered) are expected to dominate, so this is
# mainly the safety net for an otherwise-quiet stretch.
#
# Raised back from 8 to 16 -- 8 was set too low relative to how many ticks a
# SINGLE invocation's own compute consumes while its thread is alive and
# paced: at LLM_TICK_PACING_S=40 and ~150-230s/invocation, one invocation
# alone spans roughly 4-6 paced ticks. With heartbeat at 8, by the time an
# invocation finished, only 2-4 ticks of "nothing else changed" headroom
# remained before heartbeat alone re-fired -- combined with
# LLM_MIN_COOLDOWN_TICKS being trivially already satisfied by then (compute
# time alone exceeded it), invocations ended up firing almost back-to-back
# with no real gap, so the tactical loop's thread-alive-gated adaptive
# pacing (see on_zone_timestep) stayed slow almost continuously after the
# first invocation -- a real run showed only the pre-occupancy ticks running
# fast, everything after staying at ~2 ticks/min. 16 leaves genuine
# breathing room after a typical invocation completes for ticks to run at
# full EnergyPlus speed (no thread alive, no sleep) until something ACTUALLY
# warrants a re-plan -- occupancy/weather/comfort/guardrail/coverage
# triggers remain fully sensitive and can still fire immediately when truly
# needed; only the "nothing else happened, just refresh because it's been a
# while" backstop is properly spaced out now.
LLM_HEARTBEAT_TICKS = int(os.environ.get("LLM_HEARTBEAT_TICKS", "16"))
# Rate limit: minimum ticks between invocations even if multiple triggers
# fire at once, so a noisy tick doesn't burn several invocations back to
# back. Raised from 1 to 4 -- 1 was trivially already satisfied by the time
# a typical invocation's own ~4-6-tick compute window finished, so it
# imposed no real gap at all; 4 guarantees at least a few genuinely fast,
# unpaced ticks between invocations regardless of which trigger fires next,
# without meaningfully delaying a real urgent trigger (occupancy change,
# zone never covered, etc.) beyond a handful of ticks.
LLM_MIN_COOLDOWN_TICKS = int(os.environ.get("LLM_MIN_COOLDOWN_TICKS", "4"))
# Once the invocation budget is exhausted, a cached policy older than this
# (ticks since it was issued) is no longer trusted -- the tactical layer
# reverts to the plain reactive rule rather than keep reapplying an
# increasingly stale strategic decision for the rest of the run.
LLM_POLICY_STALENESS_TICKS = LLM_HEARTBEAT_TICKS * 2

# Trigger thresholds -- each is a real, cheap, deterministic check computed
# every tick from data already being read; ANY one crossing fires a
# re-plan (see _evaluate_llm_trigger). Occupancy is checked FIRST and most
# sensitively: a zone's real, applied setpoint (and lighting) should
# actually track who's in the building, not just refresh on a timer or a
# big aggregate swing.
#
# Was 2.0, then 1.5 -- a real run showed a zone going from several people
# down to one (still > 0, so no zero-crossing) kept reapplying a cached
# lighting/setpoint policy sized for the fuller room, because the aggregate
# delta never crossed even 1.5. 0.5 means even a MINOR headcount shift
# (one person arriving or leaving) reliably re-triggers, at the direct cost
# of many more invocations -- an explicitly accepted tradeoff here.
LLM_OCCUPANCY_DELTA_TRIGGER = 0.5
LLM_COMFORT_PMV_TRIGGER = 0.5
LLM_COMFORT_PPD_TRIGGER = 15.0
LLM_COMFORT_SUSTAIN_TICKS = 2  # hysteresis: must breach for N consecutive ticks, not one noisy sample
LLM_GUARDRAIL_RATE_TRIGGER = 3  # interventions within the window below
LLM_GUARDRAIL_RATE_WINDOW = 5  # ticks
# A zone's real energy draw drifting well above what it was when its
# current policy was issued is a faster, more targeted signal than waiting
# for the z-score anomaly detector's full rolling-history threshold to
# trip -- catches "this specific cached policy has become inefficient as
# conditions shifted" even when building-wide energy isn't a statistical
# outlier yet.
LLM_ENERGY_DRIFT_RATIO = 1.25  # current tick ai_kwh >= 1.25x the ai_kwh recorded at the last invocation

# Real-time, zero-LLM-latency floor for scale_cached_lighting_for_occupancy
# -- a genuine remaining occupant is never dropped below 30% lighting just
# because they're the only one left in the zone.
LLM_LIGHTING_OCCUPANCY_FLOOR_PCT = 0.3

# decide_setpoints() (reactive/predictive's rule) treats ANY occupancy > 0
# as "occupied, condition to 21-24C" -- including a schedule-ramp reading of
# e.g. 0.05, which represents 5% of a zone's design occupancy, not a real
# person. That's reactive/predictive's own long-standing behavior and isn't
# changed here. But the "llm" strategy's REACTIVE FALLBACK (used on every
# tick without a fresh cached policy -- the majority of ticks) doesn't need
# to inherit that same eagerness: treating occupancy below this threshold as
# "not really occupied" for fallback purposes avoids wastefully fully
# conditioning a zone during the schedule's ramp-up/ramp-down edges, a real
# contributor to the fallback using more energy than ashrae_baseline's fixed
# clock does during those same edge hours.
LLM_FALLBACK_OCCUPANCY_THRESHOLD = 0.5

# Only ~12 invocations ever exist for a whole run, and each one only
# realistically covers a handful of zones (an 8B local model doesn't
# reliably call set_zone_setpoint for all 5 every time) -- meaning most
# zone-ticks under "llm" strategy run the FALLBACK, not a cached policy.
# If that fallback used the exact same 21/24C reactive/predictive already
# use, "llm" strategy's achievable savings would be capped near reactive's
# own (already measured at <1%) for the vast majority of the run, no matter
# how good the minority of real LLM-covered ticks are.
#
# This was originally 20.5/25.5C (COMFORT_STANDARD_TEXT's full ASHRAE 55
# Category B edges) but a real measured run showed that backfired -- savings
# went from +5.13% to -2.43% versus baseline. Only 1 of 4 invocations that
# run actually produced a real policy, so the fallback (not the prompts) is
# what dominated the whole run's energy use, and 25.5C cooling likely didn't
# reduce total HVAC energy the way it would in a drier climate -- cooling
# coils do latent (dehumidification) work fairly independent of the
# dry-bulb setpoint in humid conditions (New Delhi has a real monsoon
# season), so a wider cooling deadband bought little or nothing there while
# still nudging heating energy up. 20.75/24.75C is a smaller, safer step
# beyond reactive/predictive's 21/24C -- re-verify savings before widening
# further rather than assuming more is always better.
LLM_FALLBACK_OCCUPIED_HEAT_C, LLM_FALLBACK_OCCUPIED_COOL_C = 20.75, 24.75

# Multi-rate control loop pacing: this small building model computes a
# control tick in milliseconds, far faster than a ~120-230s LLM invocation --
# without this, EnergyPlus can finish an entire 100-tick run before even one
# background invocation completes, so no tick ever actually gets to use a
# cached policy. 0 disables pacing (useful for fast iteration/testing where
# you don't need policies to actually land mid-run).
#
# Raised from 15 to 40 -- this, not LLM_MAX_INVOCATIONS or
# LLM_HEARTBEAT_TICKS, turned out to be the actual ceiling on how much of a
# run is genuinely LLM-driven. A real run with a 45-invocation budget and an
# 8-tick heartbeat still only completed 4 invocations (55% used_llm
# coverage) -- because only ONE invocation is ever in flight at a time (see
# the thread-alive gate in on_zone_timestep), the true limit is simply how
# many ~175s invocations fit inside (occupied_ticks x LLM_TICK_PACING_S) of
# real time, and at 15s/tick that's only ~1080s across a ~72-tick occupied
# window -- room for ~6 at most, budget and triggers notwithstanding. At
# 40s/tick that window becomes ~2880s, room for ~15-20+ (more with the
# specialist-call parallelization above cutting per-invocation latency), a
# real structural fix rather than tuning knobs that were never the
# bottleneck. Direct cost: a 100-tick run's wall-clock floor goes from ~25
# min to ~67 min purely from pacing, before any invocation compute -- an
# explicitly accepted tradeoff for genuine coverage over demo speed.
LLM_TICK_PACING_S = float(os.environ.get("LLM_TICK_PACING_S", "40.0"))

# Optional hard stop on total simulation length, applied only to "llm" and
# "ashrae_baseline" -- NOT reactive/predictive, which stay full-RunPeriod by
# default. Calls api.runtime.stop_simulation() to end the whole EnergyPlus
# run early once the tick cap is hit. For "llm", the adaptive invocation
# policy above can trigger anywhere across the run, not just a fixed head
# window, so capping total run length is what actually bounds a demo run's
# wall-clock time; for "ashrae_baseline", setting this to the same value
# keeps the two runs' tick ranges aligned for a direct comparison instead of
# the baseline covering the full RunPeriod while the capped normal run only
# covers the first slice of it.
MAX_SIM_TICKS = os.environ.get("MAX_SIM_TICKS")
MAX_SIM_TICKS = int(MAX_SIM_TICKS) if MAX_SIM_TICKS else None
# Rebalanced from {0.4, 0.4, 0.2} then {0.5, 0.3, 0.2} -- a real run measured
# average PMV of -0.04 and PPD of 6.8% (both far better than the comfort
# standard requires), confirming there was still headroom to tilt further
# toward energy without actually risking comfort; the arbiter's own prompt
# still hard-constrains every proposal to stay inside the comfort range
# regardless of these weights, so this tilts emphasis, not the safety floor.
AGENT_WEIGHTS = {"energy": 0.55, "comfort": 0.30, "carbon": 0.15}
# This used to be "21.0-24.0C" -- IDENTICAL to OCCUPIED_HEAT_C/OCCUPIED_COOL_C,
# the exact fixed targets "reactive"/"predictive"/"ashrae_baseline" already
# use. That gave the comfort specialist (and by extension the arbiter, which
# weighs its proposal) literally zero room to ever suggest anything wider or
# cheaper than what the dumbest rule-based strategy already does -- the best
# a comfort-constrained proposal could ever achieve was parity, never a real
# saving. This was a real, verified root cause of "llm" strategy showing
# negative savings vs baseline: the AI was never being ASKED to save energy
# within its comfort mandate, only to match a rule it could never beat.
# 20.5-25.5C is ASHRAE 55 Category B (PMV within +/-0.5 for typical
# business-casual clothing) -- a real, defensible standard, not loosened
# just to win the comparison.
COMFORT_STANDARD_TEXT = (
    "Acceptable operative temperature range when occupied: 20.5-25.5C (ASHRAE 55 Category B, "
    "PMV within +/-0.5 for typical business-casual clothing) -- humidity 30-60%, CO2 below "
    "1000 ppm. DEFAULT to heating_c near 20.5-21.0C and cooling_c near 25.0-25.5C -- the cheap "
    "end of this range, not the middle. A prior real run measured average PMV of -0.04 and PPD "
    "of 6.8%, both far better than required, meaning proposals have been sitting too close to "
    "the middle of this range rather than its edges -- push further toward the edges, only "
    "pulling back for a zone whose own PMV/PPD data actually shows it needs it."
)

# Literal EnergyPlus Python API calls that produce each dashboard field --
# copied verbatim from the get_variable_handle/get_actuator_handle/
# get_meter_handle calls a few hundred lines below, not paraphrased. Sent
# once in live_dashboard_state.json so the Operations Console can show
# judges exactly which real EnergyPlus API call each number came from,
# rather than asking them to take it on faith.
EPLUS_API_SOURCE = {
    "temp_c": 'exchange.get_variable_handle(state, "Zone Mean Air Temperature", <zone>)',
    "humidity_pct": 'exchange.get_variable_handle(state, "Zone Air Relative Humidity", <zone>)',
    "occupancy": 'exchange.get_variable_handle(state, "Zone People Occupant Count", <zone>)',
    "co2_ppm": 'exchange.get_variable_handle(state, "Zone Air CO2 Concentration", <zone>)',
    "pmv": 'exchange.get_variable_handle(state, "Zone Thermal Comfort Fanger Model PMV", "<zone> People")',
    "ppd_pct": 'exchange.get_variable_handle(state, "Zone Thermal Comfort Fanger Model PPD", "<zone> People")',
    "daylight_lux": 'exchange.get_variable_handle(state, "Daylighting Reference Point 1 Illuminance", "<zone> Daylighting Control")',
    "zone_hvac_kwh": 'exchange.get_variable_handle(state, "Zone Air System Sensible Heating/Cooling Energy", <zone>)',
    "heating_c": 'exchange.get_actuator_handle(state, "Schedule:Compact", "Schedule Value", "<zone> HTGSETP_SCH")',
    "cooling_c": 'exchange.get_actuator_handle(state, "Schedule:Compact", "Schedule Value", "<zone> CLGSETP_SCH")',
    "lighting_pct": 'exchange.get_actuator_handle(state, "Schedule:Compact", "Schedule Value", "<zone> LIGHT_SCH")',
    "ai_kwh": 'exchange.get_meter_handle(state, "ElectricityNet:Facility")',
    "hvac_kwh": 'exchange.get_meter_handle(state, "Cooling:Electricity" + "Heating:Electricity" + "Fans:Electricity")',
    "lighting_kwh": 'exchange.get_meter_handle(state, "InteriorLights:Electricity")',
    "plugload_kwh": 'exchange.get_meter_handle(state, "InteriorEquipment:Electricity")',
    "pv_kwh": 'exchange.get_meter_handle(state, "Photovoltaic:ElectricityProduced")',
    "carbon_kg": 'exchange.get_meter_handle(state, "Carbon Equivalent:Facility")',
    "water_m3": 'exchange.get_meter_handle(state, "MainsWater:Facility")',
}


def time_of_day_bucket(hour: int) -> str:
    return "day" if 6 <= hour < 18 else "night"


def get_forecast_trend(api, state):
    """Net outdoor temperature change over the next LOOKAHEAD_HOURS, using
    EnergyPlus's own forward weather data -- a real lookahead, not a
    fabricated prediction."""
    try:
        cur_hour = api.exchange.hour(state)
        ts = api.exchange.zone_time_step_number(state)
        current_outdoor = api.exchange.today_weather_outdoor_dry_bulb_at_time(state, cur_hour, ts)
        target_hour = cur_hour + LOOKAHEAD_HOURS
        if target_hour <= 24:
            forecast_outdoor = api.exchange.today_weather_outdoor_dry_bulb_at_time(state, target_hour, ts)
        else:
            forecast_outdoor = api.exchange.tomorrow_weather_outdoor_dry_bulb_at_time(state, target_hour - 24, ts)
        return forecast_outdoor - current_outdoor
    except Exception:
        return None


def compute_confidence(occupancy: float) -> float:
    """A rule-margin heuristic, NOT an ML probability: how far the current
    reading is from the ambiguous 0-1 occupant transition zone. Clearly
    occupied or clearly empty -> high confidence; a fractional headcount
    (mid-transition) -> lower confidence in the setback/comfort decision."""
    if occupancy <= 0 or occupancy >= 2:
        return 0.95
    return round(0.6 + 0.35 * abs(occupancy - 1), 2)


class OccupancyHistory:
    """Causal (past-only) per-zone, per-hour occupancy pattern accumulated
    live during the run -- used for occupancy prediction, space utilization,
    and 'meeting room prep' (pre-conditioning ahead of a zone's own typical
    arrival time). Nothing here ever looks at future ticks."""

    def __init__(self):
        self.by_zone_hour = defaultdict(lambda: defaultdict(list))
        self.all_ticks = defaultdict(list)

    def record(self, zone: str, hour: int, occupancy: float):
        self.by_zone_hour[zone][hour].append(occupancy)
        self.all_ticks[zone].append(occupancy)

    def typical_occupancy(self, zone: str, hour: int) -> float | None:
        history = self.by_zone_hour[zone][hour]
        return sum(history) / len(history) if history else None

    def utilization_pct(self, zone: str) -> float:
        ticks = self.all_ticks[zone]
        if not ticks:
            return 0.0
        return 100 * sum(1 for o in ticks if o > 0) / len(ticks)


occupancy_history = OccupancyHistory()
building_energy_history: list[float] = []
equipment_runtime = {"heating_active_ticks": 0, "cooling_active_ticks": 0}
recent_guardrail_events: list[dict] = []  # rolling feedback for the LLM's self-correction loop
RECENT_ISSUES_WINDOW = 10

tick_start_wall_time: list[float] = []  # last N (phase="tick_start") wall-clock timestamps, for a real sim-speed readout

# --- Strategic-layer policy cache + async invocation state (STRATEGY == "llm" only) ---
# current_policy/current_lighting_policy are the TACTICAL layer's only
# window into what the LLM has decided -- a plain dict, read every tick by
# the fast deterministic control path, written only when a background
# invocation completes. This IS the "policy generation instead of per-tick
# actions" + "decision caching" design: the LLM is never on the hot path.
current_policy: dict[str, dict] = {}  # zone -> {heating_c, cooling_c, rationale, issued_at_tick}
current_lighting_policy: dict[str, dict] = {}  # zone -> {pct, issued_at_tick, issued_at_occupancy}
policy_state = {
    "invocation_count": 0,
    "last_invocation_tick": 0,
    "last_invocation_occ_snapshot": {},  # zone -> occupancy, as of the last invocation
    "last_invocation_trend_label": "unknown",
    "last_invocation_ai_kwh": 0.0,  # building-wide ai_kwh as of the last invocation, for the energy-drift trigger
    "comfort_breach_streak": 0,
    "thread": None,  # the in-flight threading.Thread, or None
    "last_proposal_texts": {},  # specialist/arbiter text from the last completed invocation, for the Decision feed
}
# A background thread computes an LLM decision asynchronously (see
# _llm_worker) so EnergyPlus's per-tick callback is NEVER blocked on
# inference -- it only ever reads/writes _llm_result_box under this lock,
# both from the worker (on completion) and the main thread (on consumption).
_llm_lock = threading.Lock()
_llm_result_box: dict = {}


def log_pipeline_event(db, tick: int, phase: str, detail: dict, zone: str | None = None):
    db.add(PipelineEvent(tick=tick, strategy=STRATEGY, ts=time.time(), phase=phase, zone=zone, detail=json.dumps(detail)))


def _classify_trend(forecast_trend_c: float | None) -> str:
    if forecast_trend_c is None:
        return "unknown"
    if forecast_trend_c <= COLD_SNAP_THRESHOLD_C:
        return "falling"
    if forecast_trend_c >= HEAT_WAVE_THRESHOLD_C:
        return "rising"
    return "steady"


def decide_setpoints(zone: str, occupancy: float, forecast_trend_c: float | None, hour: int,
                      demand_response_active: bool, occupied_heat_c: float = OCCUPIED_HEAT_C,
                      occupied_cool_c: float = OCCUPIED_COOL_C) -> tuple[float, float, str, bool]:
    """Returns (heating_c, cooling_c, outdoor_trend_label, meeting_prep_applied).

    occupied_heat_c/occupied_cool_c default to the plain reactive/predictive
    targets (21/24C) -- "llm" strategy's fallback path passes wider,
    still-real-comfort-standard values instead (see
    LLM_FALLBACK_OCCUPIED_HEAT_C below) so the majority of ticks that don't
    have a fresh cached strategic policy aren't stuck matching the exact
    same targets as the dumb rule the AI strategy is meant to beat."""
    trend_label = _classify_trend(forecast_trend_c)

    meeting_prep_applied = False
    if occupancy > 0:
        heat_c, cool_c = occupied_heat_c, occupied_cool_c
    else:
        heat_c, cool_c = SETBACK_HEAT_C, SETBACK_COOL_C
        if STRATEGY == "predictive":
            typical = occupancy_history.typical_occupancy(zone, hour)
            if typical is not None and typical >= 1.0:
                # This zone/hour combo has historically been occupied on
                # earlier days of this run -- pre-condition before arrival.
                heat_c, cool_c = occupied_heat_c, occupied_cool_c
                meeting_prep_applied = True

    if STRATEGY == "predictive" and occupancy > 0:
        if trend_label == "falling":
            heat_c += PRECONDITION_MARGIN_C
        elif trend_label == "rising":
            cool_c -= PRECONDITION_MARGIN_C

    if demand_response_active and occupancy > 0:
        # Shed load during a demand spike: relax the comfort band slightly
        # rather than ignoring the event entirely.
        heat_c -= DEMAND_RESPONSE_RELIEF_C
        cool_c += DEMAND_RESPONSE_RELIEF_C

    return heat_c, cool_c, trend_label, meeting_prep_applied


def decide_setpoints_ashrae_baseline(hour: int) -> tuple[float, float]:
    """ASHRAE 90.1 Appendix G-style baseline operation: a FIXED time-of-day
    schedule, never real-time occupancy sensing, forecast lookahead, demand
    response, or AI -- the industry-standard 'no controls credit' comparison
    point. Uses the building's own weekday occupied-hours envelope
    (BLDG_OCC_SCH, 06:00-20:00) as the fixed occupied window."""
    if ASHRAE_BASELINE_OCC_START_HOUR <= hour < ASHRAE_BASELINE_OCC_END_HOUR:
        return OCCUPIED_HEAT_C, OCCUPIED_COOL_C
    return ASHRAE_BASELINE_SETBACK_HEAT_C, ASHRAE_BASELINE_SETBACK_COOL_C


def decide_lighting_ashrae_baseline(hour: int) -> float:
    """Same fixed-schedule philosophy as decide_setpoints_ashrae_baseline --
    no daylight harvesting credit, since a baseline doesn't get credit for
    controls it doesn't model."""
    if ASHRAE_BASELINE_OCC_START_HOUR <= hour < ASHRAE_BASELINE_OCC_END_HOUR:
        return LIGHTING_OCCUPIED_PCT
    return LIGHTING_UNOCCUPIED_PCT


def _daylight_harvest_ratio(daylight_lux: float | None) -> float:
    """Fraction of full occupied brightness (LIGHTING_OCCUPIED_PCT) that
    current daylight alone justifies -- 1.0 when there's no daylight sensor
    (Core_ZN has no windows) or it's dark, down to ~0.33 in bright daylight.
    Factored out of decide_lighting() so the real-time cached-policy scaling
    below can apply the identical curve to a live reading, instead of a
    second hand-tuned formula that could drift from this one."""
    if daylight_lux is None:
        return 1.0
    if daylight_lux >= DAYLIGHT_HARVEST_LUX_THRESHOLD:
        return max(0.1, LIGHTING_OCCUPIED_PCT - 0.6) / LIGHTING_OCCUPIED_PCT
    fraction_harvested = daylight_lux / DAYLIGHT_HARVEST_LUX_THRESHOLD
    return (LIGHTING_OCCUPIED_PCT - 0.5 * fraction_harvested) / LIGHTING_OCCUPIED_PCT


def decide_lighting(occupancy: float, daylight_lux: float | None) -> float:
    """Daylight harvesting for perimeter zones (real Daylighting:Controls
    illuminance reading); occupancy-count scaling for a windowless zone
    (Core_ZN has no windows, no daylight_lux, no harvesting -- occupant
    COUNT is the only real signal available there). A real run showed a
    single remaining occupant getting the exact same full LIGHTING_OCCUPIED_PCT
    as a packed room under the old binary occupied/unoccupied treatment --
    scale proportionally toward LIGHTING_FULL_OCCUPANCY_REF instead, floored
    so a genuine lone occupant still gets usable light, not darkness."""
    if occupancy <= 0:
        return LIGHTING_UNOCCUPIED_PCT
    if daylight_lux is None:
        fraction = min(1.0, occupancy / LIGHTING_FULL_OCCUPANCY_REF)
        return LLM_LIGHTING_OCCUPANCY_FLOOR_PCT + (LIGHTING_OCCUPIED_PCT - LLM_LIGHTING_OCCUPANCY_FLOOR_PCT) * fraction
    return LIGHTING_OCCUPIED_PCT * _daylight_harvest_ratio(daylight_lux)


def _occupancy_ratio(current_occupancy: float, issued_at_occupancy: float) -> float:
    """1.0 if the zone has as many or more people as when its cached policy
    was issued; shrinks toward 0.0 as real occupancy falls below that,
    capturing "the room has emptied out somewhat since this was decided"
    as a plain number the tactical layer can act on immediately."""
    if issued_at_occupancy <= 0:
        return 1.0
    return min(1.0, current_occupancy / issued_at_occupancy)


def _daylight_ratio_relative(current_lux: float | None, issued_at_lux: float | None) -> float:
    """1.0 if daylight hasn't meaningfully changed since the cached policy
    was issued; > 1.0 if it's gotten DARKER since (brighten the cached
    value back up), < 1.0 if it's gotten BRIGHTER since (dim it further).
    Not capped at 1.0 like _occupancy_ratio -- unlike more people showing up
    (which triggers a fresh re-plan via occupancy_shift anyway), a genuine
    darkness increase has no equivalent trigger of its own, so this needs
    to carry the signal in both directions with zero latency."""
    current_ratio = _daylight_harvest_ratio(current_lux)
    issued_ratio = _daylight_harvest_ratio(issued_at_lux)
    if issued_ratio <= 0:
        return 1.0
    return current_ratio / issued_ratio


def scale_cached_setpoints_for_occupancy(policy_entry: dict, current_occupancy: float) -> tuple[float, float]:
    """The single biggest real gap found in this strategy: a zone dropping
    from several people to just one is still 'occupied' (no state-crossing
    trigger, and the cumulative delta may not cross the threshold either),
    so the cached policy -- sized for the fuller room -- kept being
    reapplied verbatim until the NEXT LLM invocation landed, 120-230s
    later. Comparing the live baseline_kwh line against the llm line
    during a real occupancy drop showed exactly this: measurable energy
    lost purely to inference latency, not to a bad decision.

    This runs every tick with zero LLM latency: as the zone's current
    occupancy falls below what it was when the cached policy was issued,
    blend the applied setpoint toward LLM_FALLBACK_OCCUPIED_HEAT_C/COOL_C
    (the same real ASHRAE-comfort-band-edge values the fallback already
    uses) in direct proportion -- never further than that, so a genuine
    remaining occupant still gets real, comfort-standard-compliant
    conditioning, just at the cheaper end rather than whatever the fuller
    room's policy happened to specify."""
    ratio = _occupancy_ratio(current_occupancy, policy_entry.get("issued_at_occupancy", current_occupancy))
    heat_c = policy_entry["heating_c"] + (1 - ratio) * (LLM_FALLBACK_OCCUPIED_HEAT_C - policy_entry["heating_c"])
    cool_c = policy_entry["cooling_c"] + (1 - ratio) * (LLM_FALLBACK_OCCUPIED_COOL_C - policy_entry["cooling_c"])
    return heat_c, cool_c


def scale_cached_lighting_for_occupancy(cached_lighting_pct: float, policy_entry: dict, current_occupancy: float,
                                         daylight_lux: float) -> float:
    """Same real-time, zero-latency principle as
    scale_cached_setpoints_for_occupancy, applied to lighting: lighting
    scales down proportionally with real occupancy immediately, every tick,
    without waiting for a new strategic decision.
    LLM_LIGHTING_OCCUPANCY_FLOOR_PCT keeps a genuine remaining occupant from
    ever being dropped to near-darkness.

    A cached policy goes stale on DAYLIGHT the same way it does on
    occupancy -- a zone the arbiter set to 80% during a dim tick has no
    reason to stay at 80% once real daylight climbs past the harvesting
    threshold many ticks later, but nothing was re-checking that between
    invocations (verified live: a zone showed 525 lux -- well above
    DAYLIGHT_HARVEST_LUX_THRESHOLD -- while still applying an 80% cached
    level from 35 ticks earlier).

    Bidirectional, unlike the occupancy scaling above: an earlier version
    only ever dimmed further as daylight rose, never brightened back up as
    it fell, reasoning that a lower cached value might be a genuine
    zone-specific judgment call (e.g. glare) worth preserving. A real
    two-day comparison disproved that in practice -- a zone dimmed
    correctly at midday (bright) then stayed frozen at that same dim level
    all evening as it got dark, versus an equivalent zone the next day
    whose cache happened to be anchored to a dark hour and correctly
    stayed bright. Same building, same schedule, wildly different lighting
    energy, purely from cache-issuance timing luck -- not a real decision
    difference. The arbiter's prompt gives daylight as the ONLY stated
    reason to deviate from full brightness, so there's no other genuine
    judgment being protected by staying anchored to stale conditions in
    either direction: scale the cached value by how daylight NOW compares
    to daylight when the policy was issued (both directions), the same way
    occupancy scaling already tracks change since issuance -- just not
    capped at 1.0, since darker-than-issuance is just as real a signal as
    brighter-than-issuance.

    Windowless zones (daylight_lux is None, e.g. Core_ZN) never reach this
    function at all -- see the used_llm_lighting gate in the tactical loop
    below. Three real bugs in a row (cached at an unjustified 30% despite 8
    occupants; pinned at 90% despite occupancy dropping to 0.4; pinned at
    90% for a single occupant that never triggered any relative-change
    scaling) all traced back to the same root cause: trying to reconcile an
    LLM-cached VALUE for a zone the LLM has no real per-tick signal
    (daylight) to reason from in the first place. decide_lighting()'s
    occupancy-count curve already gives windowless zones real-time,
    zero-latency, occupancy-proportional lighting with no staleness risk at
    all -- there was never a genuine need for the strategic layer to be in
    this loop for them. Caller guarantees current_occupancy > 0 and
    daylight_lux is not None here (used_llm_lighting requires both)."""
    occ_ratio = _occupancy_ratio(current_occupancy, policy_entry.get("issued_at_occupancy", current_occupancy))
    occ_scaled = max(LLM_LIGHTING_OCCUPANCY_FLOOR_PCT, min(cached_lighting_pct, cached_lighting_pct * occ_ratio))
    daylight_relative = _daylight_ratio_relative(daylight_lux, policy_entry.get("issued_at_daylight_lux"))
    return max(LLM_LIGHTING_OCCUPANCY_FLOOR_PCT, min(LIGHTING_OCCUPIED_PCT, occ_scaled * daylight_relative))


def run_llm_agent_pipeline(bridge: MCPAgentBridge, zones: dict, energy: dict, carbon_and_weather: dict,
                            recent_issues: list, trigger_reason: str = "") -> tuple[dict, dict, dict]:
    """One combined STRATEGIC decision cycle -- called from a background
    thread (see _llm_worker), never from the EnergyPlus callback thread
    directly. zones is pre-filtered to occupied zones only by the caller
    (nothing for the strategic layer to decide about an unoccupied zone --
    the tactical layer always setbacks those regardless of policy).
    recent_issues is a POINT-IN-TIME COPY the caller took before spawning
    this thread, not a live reference to the module-global list the main
    thread keeps mutating -- reading a shared mutable list from two threads
    without that copy is a real race (this project's SQLAlchemy session is
    already configured check_same_thread=False for a similar cross-thread
    reason, but that doesn't cover plain Python lists).

    Returns (decisions: {zone: {heating_c, cooling_c, rationale}},
    lighting_decisions: {zone: pct}, proposal_texts). Falls back gracefully
    (empty decisions) if a specialist returns no parseable proposal, logged
    as raw_text for debugging, not hidden."""
    bridge.write_live_state(zones, energy, carbon_and_weather, recent_issues=recent_issues)

    comfort_standard = (
        f"{COMFORT_STANDARD_TEXT} This strategic review was triggered by: {trigger_reason}."
        if trigger_reason else COMFORT_STANDARD_TEXT
    )
    # The 3 specialists are called sequentially, not concurrently -- this
    # project runs entirely against a local Ollama server (no hosted LLM API
    # is used anywhere, by design), and a real run on this project's GPU
    # (RTX 3050, 4GB VRAM) crashed a whole invocation with
    # httpx.RemoteProtocolError ("Server disconnected without sending a
    # response") the moment 3 simultaneous 8B-model inference requests were
    # fired at it: the ollama client itself is thread-safe (a shared
    # httpx.Client handles concurrent connections fine), but the local
    # server ran out of headroom trying to actually serve three at once and
    # dropped one mid-request. Sequential is the only reliable option
    # against a single local model server -- see LLM_TICK_PACING_S's
    # adaptive sleep and the specialist-count-independent triggers above for
    # where the real coverage gains come from instead.
    energy_result = energy_agent.propose(bridge, zones, energy)
    comfort_result = comfort_agent.propose(bridge, zones, comfort_standard)
    carbon_result = carbon_agent.propose(bridge, zones, carbon_and_weather)

    decisions, lighting_decisions, arbiter_text = arbiter.resolve(
        bridge, zones,
        json.dumps(energy_result), json.dumps(comfort_result), json.dumps(carbon_result),
        AGENT_WEIGHTS,
    )
    proposal_texts = {
        "energy": json.dumps(energy_result),
        "comfort": json.dumps(comfort_result),
        "carbon": json.dumps(carbon_result),
        "arbiter": arbiter_text,
    }
    return decisions, lighting_decisions, proposal_texts


def _evaluate_llm_trigger(tick: int, occ_now: dict, trend_label: str, energy_anomaly: bool,
                           guardrail_hits_recent: int, ai_kwh: float) -> str | None:
    """Adaptive scheduling: decides whether the strategic LLM layer should be
    re-invoked THIS tick. Cheap and deterministic -- runs every tick
    regardless of the answer. Any one of several real signals can trigger;
    LLM_MIN_COOLDOWN_TICKS prevents thrashing if several fire close together,
    LLM_MAX_INVOCATIONS caps total real inference cost for the run. Returns
    a trigger reason string, or None.

    Occupancy is checked first and most sensitively -- a real run showed
    energy savings suffering specifically because the strategy wasn't
    re-planning around actual occupancy changes often enough. Two separate
    occupancy checks, not one: a STATE transition (any zone crossing
    occupied<->unoccupied -- someone arrived or the room emptied) is checked
    before the cumulative headcount-delta check, because a big change in one
    zone can get diluted in a sum across all five and a state transition is
    the single most decision-relevant occupancy event regardless of
    magnitude."""
    if not any(v > 0 for v in occ_now.values()):
        return None  # nothing occupied -- no strategic decision to make, deterministic setback is already optimal
    if policy_state["invocation_count"] >= LLM_MAX_INVOCATIONS:
        return None
    ticks_since_last = tick - policy_state["last_invocation_tick"]
    if policy_state["invocation_count"] > 0 and ticks_since_last < LLM_MIN_COOLDOWN_TICKS:
        return None
    if policy_state["invocation_count"] == 0:
        return "initial_strategy"  # bootstrap: no policy exists yet

    # A zone can be occupied right now and STILL have never received a real
    # policy -- the arbiter doesn't reliably cover every zone in one pass
    # (see arbiter.py's own retry), so a zone can fall through every prior
    # invocation's gaps. Without this, that zone just sits on the reactive
    # fallback for the rest of the run, silently capping how much of the
    # occupied window is ever genuinely LLM-driven regardless of how many
    # other triggers fire -- none of them guarantee THIS zone gets covered
    # next time. Checked with the same priority as the bootstrap trigger
    # since it's the same situation at the per-zone level: real, current
    # occupancy with zero LLM judgment behind it.
    occupied_uncovered = [z for z, occ in occ_now.items() if occ > 0 and z not in current_policy]
    if occupied_uncovered:
        return "zone_never_covered"

    occ_state_changed = any(
        (occ_now.get(z, 0.0) > 0) != (policy_state["last_invocation_occ_snapshot"].get(z, 0.0) > 0)
        for z in occ_now
    )
    if occ_state_changed:
        return "occupancy_state_change"
    occ_delta = sum(
        abs(occ_now.get(z, 0.0) - policy_state["last_invocation_occ_snapshot"].get(z, 0.0)) for z in occ_now
    )
    if occ_delta >= LLM_OCCUPANCY_DELTA_TRIGGER:
        return "occupancy_shift"
    if trend_label != policy_state["last_invocation_trend_label"] and trend_label in ("falling", "rising"):
        return "weather_swing"
    if energy_anomaly:
        return "energy_anomaly"
    baseline_kwh = policy_state["last_invocation_ai_kwh"]
    if baseline_kwh > 0 and ai_kwh >= LLM_ENERGY_DRIFT_RATIO * baseline_kwh:
        return "energy_drift"
    if policy_state["comfort_breach_streak"] >= LLM_COMFORT_SUSTAIN_TICKS:
        return "comfort_degradation"
    if guardrail_hits_recent >= LLM_GUARDRAIL_RATE_TRIGGER:
        return "guardrail_rate"
    if ticks_since_last >= LLM_HEARTBEAT_TICKS:
        return "heartbeat"
    return None


def _llm_worker(bridge: MCPAgentBridge, zones_payload: dict, energy_payload: dict, carbon_payload: dict,
                 recent_issues: list, trigger_reason: str, tick: int):
    """Runs entirely on a background thread. Only ever touches the bridge
    (its own private asyncio loop + MCP session) and the agent propose()
    functions -- never the EnergyPlus api/state, never the SQLAlchemy
    session, so there is nothing here that needs to coordinate with the
    main callback thread except handing back the final result. bridge's
    on_event callback is reassigned to a closure over a LOCAL list (not the
    module-level pending-events list a previous per-tick design used) so
    concurrently-arriving events from two invocations could never interleave
    -- moot in practice since only one invocation is ever in flight at a
    time (the main thread won't start a second while this one's thread is
    still alive), but correct regardless of that invariant holding."""
    local_events: list[tuple[str, dict]] = []
    bridge._on_event = lambda phase, detail: local_events.append((phase, detail))
    try:
        decisions, lighting_decisions, proposal_texts = run_llm_agent_pipeline(
            bridge, zones_payload, energy_payload, carbon_payload, recent_issues, trigger_reason)
        with _llm_lock:
            _llm_result_box.clear()
            _llm_result_box.update(
                ok=True, decisions=decisions, lighting_decisions=lighting_decisions,
                proposal_texts=proposal_texts, trigger_reason=trigger_reason, tick=tick,
                events=local_events, done=True,
            )
    except Exception as e:
        with _llm_lock:
            _llm_result_box.clear()
            _llm_result_box.update(
                ok=False, error=f"{type(e).__name__}: {e}", trigger_reason=trigger_reason, tick=tick,
                events=local_events, done=True,
            )


def detect_energy_anomaly(current_kwh: float) -> bool:
    """Real statistical outlier check (z-score vs rolling history), not a
    scripted event -- independent of the sensor-fault injection above."""
    if len(building_energy_history) < ANOMALY_MIN_HISTORY:
        return False
    mean = statistics.mean(building_energy_history)
    stdev = statistics.pstdev(building_energy_history) or 1e-6
    z = (current_kwh - mean) / stdev
    return abs(z) >= ANOMALY_Z_THRESHOLD


class Handles:
    resolved = False
    temp = {}
    humidity = {}
    occupancy = {}
    co2 = {}
    pmv = {}
    ppd = {}
    daylight = {}  # perimeter zones only
    zone_heat_energy = {}
    zone_cool_energy = {}
    heat_actuator = {}
    cool_actuator = {}
    light_actuator = {}
    meter_electricity = None
    meter_cooling = None
    meter_heating = None
    meter_fans = None
    meter_lighting = None
    meter_plugload = None
    meter_pv = None
    meter_carbon = None
    meter_water = None


def run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    init_db()
    db = SessionLocal()

    bridge = None
    if STRATEGY == "llm":
        # on_event is reassigned per-invocation by _llm_worker (a closure over
        # that invocation's own local event list) before any real call
        # happens -- this default is never actually exercised, just a safe
        # no-op placeholder until the first invocation starts.
        bridge = MCPAgentBridge(on_event=lambda phase, detail: None)
        bridge.start()

    api = EnergyPlusAPI()
    state = api.state_manager.new_state()
    tick_counter = {"n": 0}

    # Reporting-only variables EnergyPlus won't compute unless explicitly
    # requested before the run starts (confirmed via diagnostic: occupancy
    # silently read as 0.0 for an entire run until this was added).
    for zone in ZONES:
        api.exchange.request_variable(state, "Zone People Occupant Count", zone)
        api.exchange.request_variable(state, "Zone Air Relative Humidity", zone)
        api.exchange.request_variable(state, "Zone Air CO2 Concentration", zone)
        api.exchange.request_variable(state, "Zone Thermal Comfort Fanger Model PMV", f"{zone} People")
        api.exchange.request_variable(state, "Zone Thermal Comfort Fanger Model PPD", f"{zone} People")
        api.exchange.request_variable(state, "Zone Air System Sensible Heating Energy", zone)
        api.exchange.request_variable(state, "Zone Air System Sensible Cooling Energy", zone)
    for zone in PERIMETER_ZONES:
        api.exchange.request_variable(state, "Daylighting Reference Point 1 Illuminance", f"{zone} Daylighting Control")

    def on_zone_timestep(state):
        if not api.exchange.api_data_fully_ready(state):
            return
        if api.exchange.warmup_flag(state):
            return

        if not Handles.resolved:
            for zone in ZONES:
                Handles.temp[zone] = api.exchange.get_variable_handle(state, "Zone Mean Air Temperature", zone)
                Handles.humidity[zone] = api.exchange.get_variable_handle(state, "Zone Air Relative Humidity", zone)
                Handles.occupancy[zone] = api.exchange.get_variable_handle(state, "Zone People Occupant Count", zone)
                Handles.co2[zone] = api.exchange.get_variable_handle(state, "Zone Air CO2 Concentration", zone)
                Handles.pmv[zone] = api.exchange.get_variable_handle(
                    state, "Zone Thermal Comfort Fanger Model PMV", f"{zone} People")
                Handles.ppd[zone] = api.exchange.get_variable_handle(
                    state, "Zone Thermal Comfort Fanger Model PPD", f"{zone} People")
                Handles.zone_heat_energy[zone] = api.exchange.get_variable_handle(
                    state, "Zone Air System Sensible Heating Energy", zone)
                Handles.zone_cool_energy[zone] = api.exchange.get_variable_handle(
                    state, "Zone Air System Sensible Cooling Energy", zone)
                Handles.heat_actuator[zone] = api.exchange.get_actuator_handle(
                    state, "Schedule:Compact", "Schedule Value", f"{zone} HTGSETP_SCH")
                Handles.cool_actuator[zone] = api.exchange.get_actuator_handle(
                    state, "Schedule:Compact", "Schedule Value", f"{zone} CLGSETP_SCH")
                Handles.light_actuator[zone] = api.exchange.get_actuator_handle(
                    state, "Schedule:Compact", "Schedule Value", f"{zone} LIGHT_SCH")
            for zone in PERIMETER_ZONES:
                Handles.daylight[zone] = api.exchange.get_variable_handle(
                    state, "Daylighting Reference Point 1 Illuminance", f"{zone} Daylighting Control")

            Handles.meter_electricity = api.exchange.get_meter_handle(state, "ElectricityNet:Facility")
            Handles.meter_cooling = api.exchange.get_meter_handle(state, "Cooling:Electricity")
            Handles.meter_heating = api.exchange.get_meter_handle(state, "Heating:Electricity")
            Handles.meter_fans = api.exchange.get_meter_handle(state, "Fans:Electricity")
            Handles.meter_lighting = api.exchange.get_meter_handle(state, "InteriorLights:Electricity")
            Handles.meter_plugload = api.exchange.get_meter_handle(state, "InteriorEquipment:Electricity")
            Handles.meter_pv = api.exchange.get_meter_handle(state, "Photovoltaic:ElectricityProduced")
            Handles.meter_carbon = api.exchange.get_meter_handle(state, "Carbon Equivalent:Facility")
            Handles.meter_water = api.exchange.get_meter_handle(state, "MainsWater:Facility")

            bad = [n for n, h in {**Handles.temp, **Handles.heat_actuator, **Handles.cool_actuator,
                                   **Handles.light_actuator}.items() if h == -1]
            if bad or Handles.meter_electricity == -1:
                raise RuntimeError(f"Unresolved EnergyPlus handles (fix before trusting output): {bad}")
            Handles.resolved = True

        tick_counter["n"] += 1
        if tick_counter["n"] % CONTROL_EVERY_N_TIMESTEPS != 0:
            return

        tick = tick_counter["n"] // CONTROL_EVERY_N_TIMESTEPS

        if (
            MAX_SIM_TICKS is not None
            and STRATEGY in ("llm", "ashrae_baseline")
            and tick > MAX_SIM_TICKS
        ):
            api.runtime.stop_simulation(state)
            return

        if STRATEGY == "llm" and LLM_TICK_PACING_S > 0:
            # This small building model computes a control tick in
            # milliseconds -- EnergyPlus can blow through all 100 ticks
            # before even ONE ~230s background LLM invocation finishes,
            # which would mean every cached policy arrives after the run has
            # already ended and never actually reaches an actuator (verified
            # empirically: a 100-tick run with no pacing completed in ~90s,
            # one invocation triggered at tick 10 and only landed via the
            # end-of-run capture path in on_zone_timestep's caller, having
            # influenced zero ticks). A real BMS's control loop runs at a
            # real-world cadence for the same reason a thermostat doesn't
            # recompute its setpoint a million times a second just because
            # the CPU can -- this sleep gives the tactical loop a comparable,
            # deliberately-paced cadence so the async strategic layer has
            # genuine wall-clock room to compute and be adopted mid-run.
            #
            # Adaptive, not constant: only sleep while a background
            # invocation is actually in flight. There's nothing to wait for
            # on a tick where no thread is alive -- the ~28 pre-occupancy
            # ticks, and every quiet stretch between invocations where the
            # cached policy is still fresh and no new trigger has fired, were
            # burning the full LLM_TICK_PACING_S every tick for zero benefit
            # under constant pacing. Ticking fast there doesn't risk missing
            # a trigger (_evaluate_llm_trigger still runs every control tick
            # regardless of pacing) and doesn't cost the in-flight invocation
            # any real time (that invocation still gets LLM_TICK_PACING_S of
            # real time per tick for as long as its own thread stays alive).
            # Net effect: the same or more invocations fit in significantly
            # less total wall-clock time than pacing every tick uniformly.
            thread_alive = policy_state["thread"] is not None and policy_state["thread"].is_alive()
            if thread_alive:
                time.sleep(LLM_TICK_PACING_S)

        hour = api.exchange.hour(state)
        hour_bucket = time_of_day_bucket(hour)
        forecast_trend_c = get_forecast_trend(api, state)
        outdoor_temp_c = api.exchange.today_weather_outdoor_dry_bulb_at_time(
            state, hour, api.exchange.zone_time_step_number(state))

        energy_j = api.exchange.get_meter_value(state, Handles.meter_electricity)
        ai_kwh = energy_j / 3_600_000
        hvac_kwh = sum(api.exchange.get_meter_value(state, h) for h in
                        (Handles.meter_cooling, Handles.meter_heating, Handles.meter_fans)) / 3_600_000
        lighting_kwh = api.exchange.get_meter_value(state, Handles.meter_lighting) / 3_600_000
        plugload_kwh = api.exchange.get_meter_value(state, Handles.meter_plugload) / 3_600_000
        pv_kwh = api.exchange.get_meter_value(state, Handles.meter_pv) / 3_600_000
        carbon_kg = api.exchange.get_meter_value(state, Handles.meter_carbon)
        water_m3 = api.exchange.get_meter_value(state, Handles.meter_water) if Handles.meter_water != -1 else 0.0

        try:
            sim_clock = {
                "month": api.exchange.month(state), "day": api.exchange.day_of_month(state),
                "hour": hour, "minute": api.exchange.minutes(state),
            }
        except Exception:
            sim_clock = {"hour": hour}
        tick_wall_time = time.time()
        tick_start_wall_time.append(tick_wall_time)
        del tick_start_wall_time[:-20]
        log_pipeline_event(db, tick, "tick_start", {
            "sim_clock": sim_clock, "outdoor_temp_c": round(outdoor_temp_c, 2),
            "ai_kwh": round(ai_kwh, 3), "hvac_kwh": round(hvac_kwh, 3),
            "lighting_kwh": round(lighting_kwh, 3), "pv_kwh": round(pv_kwh, 3),
            "carbon_kg": round(carbon_kg, 4), "wall_time": tick_wall_time,
        })

        # Live baseline lookup: if an ashrae_baseline run has already been
        # completed (a separate earlier process/run, its rows already
        # committed to SQLite), pull ITS real energy for this exact tick
        # number -- both runs share the same weather file and RunPeriod, so
        # tick N means the same simulated moment in both. This makes the
        # dashboard's baseline comparison line real and populated live, tick
        # by tick, as THIS run streams in, rather than requiring a separate
        # post-hoc stitching pass (sim/compare_runs.py) after the whole run
        # finishes -- and rather than sitting flat at 0.0 the whole time, as
        # a plain (non-comparison) run always did before this.
        baseline_kwh_for_tick = 0.0
        if STRATEGY != "ashrae_baseline":
            baseline_rows = db.query(RunTick.ai_kwh).filter(
                RunTick.strategy == "ashrae_baseline", RunTick.tick == tick
            ).all()
            if baseline_rows:
                baseline_kwh_for_tick = sum(r[0] for r in baseline_rows) / len(baseline_rows)

        # Demand response: shed load if this tick's building draw spikes
        # well above its recent rolling average.
        demand_response_active = False
        if len(building_energy_history) >= DEMAND_RESPONSE_WINDOW:
            recent_avg = sum(building_energy_history[-DEMAND_RESPONSE_WINDOW:]) / DEMAND_RESPONSE_WINDOW
            if recent_avg > 0 and ai_kwh > DEMAND_RESPONSE_MULTIPLIER * recent_avg:
                demand_response_active = True
        energy_anomaly = detect_energy_anomaly(ai_kwh)
        building_energy_history.append(ai_kwh)
        if energy_anomaly:
            db.add(ResilienceEvent(
                tick=tick, strategy=STRATEGY, zone="(building)", event_type="energy_anomaly",
                description=f"Building electricity draw {ai_kwh:.2f} kWh is a statistical outlier vs recent history",
                fallback_used=False,
            ))

        # First pass: read every zone's real telemetry.
        raw_temp, raw_occ, raw_humidity, raw_co2, raw_zone_hvac, raw_daylight = {}, {}, {}, {}, {}, {}
        raw_pmv, raw_ppd = {}, {}
        for zone in ZONES:
            raw_temp[zone] = api.exchange.get_variable_value(state, Handles.temp[zone])
            raw_occ[zone] = api.exchange.get_variable_value(state, Handles.occupancy[zone])
            raw_humidity[zone] = api.exchange.get_variable_value(state, Handles.humidity[zone])
            raw_co2[zone] = api.exchange.get_variable_value(state, Handles.co2[zone])
            raw_pmv[zone] = api.exchange.get_variable_value(state, Handles.pmv[zone])
            raw_ppd[zone] = api.exchange.get_variable_value(state, Handles.ppd[zone])
            zone_heat_j = api.exchange.get_variable_value(state, Handles.zone_heat_energy[zone])
            zone_cool_j = api.exchange.get_variable_value(state, Handles.zone_cool_energy[zone])
            raw_zone_hvac[zone] = (zone_heat_j + zone_cool_j) / 3_600_000
            raw_daylight[zone] = (
                api.exchange.get_variable_value(state, Handles.daylight[zone]) if zone in PERIMETER_ZONES else None
            )
            occupancy_history.record(zone, hour, raw_occ[zone])

        any_heating_active = any(raw_temp[z] < OCCUPIED_HEAT_C for z in ZONES if raw_occ[z] > 0) or hvac_kwh > 0
        if any_heating_active:
            equipment_runtime["heating_active_ticks"] += 1

        trend_label_now = _classify_trend(forecast_trend_c)

        if STRATEGY == "llm":
            # 1. Consume a completed background invocation, if one finished
            # since the last tick. Never blocks -- if nothing's done yet,
            # this is just a lock+dict-check, and the tactical layer below
            # keeps using whatever policy is already cached.
            result = None
            with _llm_lock:
                if _llm_result_box.get("done"):
                    result = dict(_llm_result_box)
                    _llm_result_box.clear()
            if result is not None:
                policy_state["thread"] = None
                for phase, detail in result["events"]:
                    log_pipeline_event(db, result["tick"], phase, detail)
                if result["ok"]:
                    for z, d in result["decisions"].items():
                        current_policy[z] = {
                            "heating_c": d["heating_c"], "cooling_c": d["cooling_c"],
                            "rationale": d.get("rationale", ""), "issued_at_tick": result["tick"],
                            # Real occupancy THIS zone had when the policy was issued -- lets the
                            # tactical layer detect "fewer people are here now than when this was
                            # decided" every tick, with zero LLM latency, instead of blindly
                            # reapplying a policy sized for a fuller room. See
                            # _scale_cached_policy_for_occupancy.
                            "issued_at_occupancy": raw_occ.get(z, 0.0),
                        }
                    for z, pct in result["lighting_decisions"].items():
                        # Tracked independently from current_policy: an
                        # invocation can (and did, in a real run) call
                        # set_zone_setpoint for every zone while calling
                        # set_lighting_level for none of them, or vice versa.
                        # Before this, the tactical layer trusted a cached
                        # lighting % as "fresh" purely because the CO-CACHED
                        # setpoint policy was fresh -- so a lighting value
                        # left over from an invocation many ticks earlier
                        # than the setpoint policy's own issued_at_tick was
                        # silently treated as current. See
                        # scale_cached_lighting_for_occupancy.
                        current_lighting_policy[z] = {
                            "pct": pct, "issued_at_tick": result["tick"],
                            "issued_at_occupancy": raw_occ.get(z, 0.0),
                            # Real daylight_lux THIS zone had when the policy
                            # was issued -- lets scale_cached_lighting_for_occupancy
                            # track how daylight has changed (either direction)
                            # since issuance, with zero LLM latency, instead of
                            # a cached value staying anchored to whatever
                            # brightness happened to exist when it was decided.
                            "issued_at_daylight_lux": raw_daylight.get(z),
                        }
                    policy_state["last_proposal_texts"] = result["proposal_texts"]
                    policy_state["invocation_count"] += 1
                    policy_state["last_invocation_tick"] = result["tick"]
                    policy_state["last_invocation_occ_snapshot"] = dict(raw_occ)
                    policy_state["last_invocation_trend_label"] = trend_label_now
                    policy_state["last_invocation_ai_kwh"] = ai_kwh
                    policy_state["comfort_breach_streak"] = 0
                    log_pipeline_event(db, tick, "llm_policy_updated", {
                        "trigger": result["trigger_reason"], "invocation_number": policy_state["invocation_count"],
                        "zones_updated": list(result["decisions"].keys()), "issued_at_tick": result["tick"],
                    })
                else:
                    print(f"[llm pipeline] invocation FAILED (trigger={result['trigger_reason']}): "
                          f"{result['error']}", flush=True)
                    log_pipeline_event(db, tick, "llm_pipeline_error",
                                        {"trigger": result["trigger_reason"], "error": result["error"]})

            # 2. Hysteresis bookkeeping for the comfort-degradation trigger --
            # must breach for LLM_COMFORT_SUSTAIN_TICKS consecutive ticks,
            # not one noisy sample, before it counts.
            occupied_pmvs = [raw_pmv[z] for z in ZONES if raw_occ[z] > 0]
            occupied_ppds = [raw_ppd[z] for z in ZONES if raw_occ[z] > 0]
            comfort_breach_now = bool(occupied_pmvs) and (
                max(abs(p) for p in occupied_pmvs) > LLM_COMFORT_PMV_TRIGGER
                or max(occupied_ppds) > LLM_COMFORT_PPD_TRIGGER
            )
            policy_state["comfort_breach_streak"] = (
                policy_state["comfort_breach_streak"] + 1 if comfort_breach_now else 0
            )
            guardrail_hits_recent = sum(1 for e in recent_guardrail_events if tick - e["tick"] <= LLM_GUARDRAIL_RATE_WINDOW)

            # 3. If no invocation is currently in flight, check whether a
            # real trigger fires; if so, launch the strategic layer on a
            # background thread and move on immediately -- this tick's
            # actuation (below) always uses whatever policy is ALREADY
            # cached, never waits on this.
            if policy_state["thread"] is None or not policy_state["thread"].is_alive():
                trigger_reason = _evaluate_llm_trigger(
                    tick, raw_occ, trend_label_now, energy_anomaly, guardrail_hits_recent, ai_kwh)
                if trigger_reason:
                    occupied_zones = [z for z in ZONES if raw_occ[z] > 0]
                    zones_payload = {
                        z: {
                            "temp_c": round(raw_temp[z], 2), "occupancy": raw_occ[z],
                            "humidity_pct": round(raw_humidity[z], 1), "co2_ppm": round(raw_co2[z], 1),
                            "pmv": round(raw_pmv[z], 2), "ppd_pct": round(raw_ppd[z], 1),
                            "daylight_lux": round(raw_daylight[z], 1) if raw_daylight[z] is not None else None,
                        }
                        for z in occupied_zones
                    }
                    energy_payload = {"ai_kwh_this_tick": round(ai_kwh, 3), "hvac_kwh": round(hvac_kwh, 3),
                                       "trigger_reason": trigger_reason}
                    carbon_payload = {
                        "carbon_kg_this_tick": round(carbon_kg, 4), "outdoor_temp_c": round(outdoor_temp_c, 2),
                        "outdoor_trend": forecast_trend_c, "trigger_reason": trigger_reason,
                    }
                    recent_issues_snapshot = list(recent_guardrail_events)  # copy -- see run_llm_agent_pipeline docstring
                    policy_state["thread"] = threading.Thread(
                        target=_llm_worker,
                        args=(bridge, zones_payload, energy_payload, carbon_payload,
                              recent_issues_snapshot, trigger_reason, tick),
                        daemon=True,
                    )
                    policy_state["thread"].start()
                    log_pipeline_event(db, tick, "llm_invocation_started", {
                        "trigger": trigger_reason, "invocation_number": policy_state["invocation_count"] + 1,
                        "occupied_zones": occupied_zones,
                    })

        # Second pass: apply fault injection/fallback, decide, actuate, log.
        zone_snapshots = {}  # zone -> full driver snapshot + applied setpoints, for live_state.json / PipelineEvent
        for zone in ZONES:
            is_faulty = zone == FAULT_ZONE and FAULT_TICK_START <= tick <= FAULT_TICK_END
            if is_faulty:
                neighbors = ADJACENCY[zone]
                fallback_temp = sum(raw_temp[n] for n in neighbors) / len(neighbors)
                temp_c = fallback_temp
                db.add(ResilienceEvent(
                    tick=tick, strategy=STRATEGY, zone=zone, event_type="sensor_dropout",
                    description=(f"Simulated sensor fault on {zone}; falling back to neighbor-zone "
                                 f"estimate {fallback_temp:.2f}C from {neighbors}"),
                    fallback_used=True,
                ))
            else:
                temp_c = raw_temp[zone]
            occupancy = raw_occ[zone]
            daylight_lux = raw_daylight[zone]

            # Tactical layer: consults the CACHED strategic policy (if any,
            # and if still fresh) every tick -- this is the only place the
            # "llm" strategy ever reads current_policy/current_lighting_policy.
            # An unoccupied zone is ALWAYS the deterministic setback,
            # regardless of any cached policy (the strategic layer only ever
            # tunes occupied-zone targets, see run_llm_agent_pipeline).
            policy_entry = current_policy.get(zone)
            policy_fresh = policy_entry is not None and (
                policy_state["invocation_count"] < LLM_MAX_INVOCATIONS
                or (tick - policy_entry["issued_at_tick"]) <= LLM_POLICY_STALENESS_TICKS
            )
            # fallback_occupancy computed unconditionally (not just inside the
            # setpoint fallback branch below) so the lighting fallback can
            # reuse the exact same zeroing -- see used_llm's threshold below
            # for why this matters.
            fallback_occupancy = (
                occupancy if not (STRATEGY == "llm" and occupancy < LLM_FALLBACK_OCCUPANCY_THRESHOLD) else 0.0
            )
            # occupancy >= LLM_FALLBACK_OCCUPANCY_THRESHOLD, not occupancy > 0:
            # a real run showed a zone with occupancy=0.18-0.40 (BLDG_OCC_SCH's
            # smooth ramp-down tail, not a real remaining occupant) still being
            # treated as "occupied enough" to trust a cached policy -- and
            # scale_cached_setpoints_for_occupancy only ever relaxes toward
            # LLM_FALLBACK_OCCUPIED_HEAT_C/COOL_C (20.75/24.75, still an
            # OCCUPIED comfort target), never all the way to true setback
            # (18/27), because it has no way to know the occupancy reading
            # itself is schedule noise rather than a genuine sparse occupant.
            # LLM_FALLBACK_OCCUPANCY_THRESHOLD already exists to catch exactly
            # this for the fallback rule -- it just wasn't applied to the gate
            # that decides whether the cached policy is trusted AT ALL. Two
            # real nights compared: Jan21 (52.8% used_llm coverage) lost
            # 20-52% more energy than baseline overnight; Jan22 (75.0%
            # coverage, i.e. MORE ticks routed through this same bug) lost up
            # to 93.8% more -- coverage going up made this specific bug worse,
            # not better, until this threshold closes it.
            used_llm = STRATEGY == "llm" and policy_fresh and occupancy >= LLM_FALLBACK_OCCUPANCY_THRESHOLD

            if STRATEGY == "ashrae_baseline":
                heat_c, cool_c = decide_setpoints_ashrae_baseline(hour)
                trend_label = "fixed_schedule"
                meeting_prep = False
            elif used_llm:
                heat_c, cool_c = scale_cached_setpoints_for_occupancy(policy_entry, occupancy)
                if demand_response_active:
                    # A cached strategic policy has no way to know a demand
                    # spike is happening right now (it wasn't in the payload
                    # when the policy was issued, and won't be again for
                    # potentially many ticks) -- without this, a zone under a
                    # fresh LLM policy silently skipped DR shedding entirely
                    # while reactive/predictive zones got real relief via
                    # decide_setpoints() below. Same relief magnitude as the
                    # fallback rule uses, so this doesn't introduce a new
                    # comfort tradeoff, just closes the gap for llm-covered zones.
                    heat_c -= DEMAND_RESPONSE_RELIEF_C
                    cool_c += DEMAND_RESPONSE_RELIEF_C
                trend_label = "policy_cached_scaled"
                meeting_prep = False
            else:
                if STRATEGY == "llm":
                    heat_c, cool_c, trend_label, meeting_prep = decide_setpoints(
                        zone, fallback_occupancy, forecast_trend_c, hour, demand_response_active,
                        occupied_heat_c=LLM_FALLBACK_OCCUPIED_HEAT_C, occupied_cool_c=LLM_FALLBACK_OCCUPIED_COOL_C,
                    )
                else:
                    heat_c, cool_c, trend_label, meeting_prep = decide_setpoints(
                        zone, fallback_occupancy, forecast_trend_c, hour, demand_response_active)

            validated = validate_setpoint(zone, heat_c, cool_c)
            api.exchange.set_actuator_value(state, Handles.heat_actuator[zone], validated["heating_c"])
            api.exchange.set_actuator_value(state, Handles.cool_actuator[zone], validated["cooling_c"])

            if validated["guardrail_intervened"]:
                recent_guardrail_events.append({
                    "tick": tick, "zone": zone,
                    "proposed": {"heating_c": heat_c, "cooling_c": cool_c},
                    "clipped_to": {"heating_c": validated["heating_c"], "cooling_c": validated["cooling_c"]},
                    "via_llm": used_llm,
                })
                del recent_guardrail_events[:-RECENT_ISSUES_WINDOW]

            # Own freshness check, separate from the setpoint policy's
            # policy_fresh -- a real run showed one invocation call
            # set_zone_setpoint for every zone while calling
            # set_lighting_level for NONE of them. Gating lighting trust on
            # the setpoint policy's freshness meant a lighting % left over
            # from a much earlier invocation got applied as if it were as
            # fresh as the (genuinely fresh) setpoint policy sitting next to
            # it in the drivers -- same silent-staleness bug the daylight
            # ceiling above fixes for VALUE, this fixes for TRUST.
            lighting_entry = current_lighting_policy.get(zone)
            lighting_policy_fresh = lighting_entry is not None and (
                policy_state["invocation_count"] < LLM_MAX_INVOCATIONS
                or (tick - lighting_entry["issued_at_tick"]) <= LLM_POLICY_STALENESS_TICKS
            )
            # daylight_lux is not None excludes windowless zones (Core_ZN)
            # entirely -- see scale_cached_lighting_for_occupancy's docstring
            # for why three real bugs in a row all traced back to trying to
            # trust an LLM-cached lighting value for a zone with no daylight
            # signal to ground it. decide_lighting()'s occupancy-count curve
            # already handles these zones with zero latency and zero
            # staleness risk, so the strategic layer's cached value (however
            # fresh) is simply never consulted for lighting there.
            #
            # Same LLM_FALLBACK_OCCUPANCY_THRESHOLD gate as used_llm above,
            # same reason -- a schedule-tail occupancy reading (0.18, 0.40)
            # shouldn't be trusted as "occupied enough" for a cached lighting
            # value either.
            used_llm_lighting = (
                STRATEGY == "llm" and lighting_policy_fresh
                and occupancy >= LLM_FALLBACK_OCCUPANCY_THRESHOLD and daylight_lux is not None
            )
            if STRATEGY == "ashrae_baseline":
                lighting_pct = decide_lighting_ashrae_baseline(hour)
            elif used_llm_lighting:
                scaled_lighting = scale_cached_lighting_for_occupancy(
                    lighting_entry["pct"], lighting_entry, occupancy, daylight_lux)
                lighting_pct = max(0.0, min(1.0, scaled_lighting))
            else:
                # fallback_occupancy (computed once, above, alongside used_llm)
                # -- reused here so a schedule-tail reading gets the same true
                # "not really occupied" treatment for lighting that it already
                # gets for heating/cooling, not just a partial dim based on a
                # raw fractional number that was never a real occupant.
                lighting_pct = decide_lighting(fallback_occupancy, daylight_lux)
            api.exchange.set_actuator_value(state, Handles.light_actuator[zone], lighting_pct)

            confidence = compute_confidence(occupancy)

            drivers = {
                "temp_c": round(temp_c, 2),
                "humidity_pct": round(raw_humidity[zone], 1),
                "co2_ppm": round(raw_co2[zone], 1),
                "pmv": round(raw_pmv[zone], 2),
                "ppd_pct": round(raw_ppd[zone], 1),
                "daylight_lux": round(daylight_lux, 1) if daylight_lux is not None else None,
                "zone_hvac_kwh": round(raw_zone_hvac[zone], 3),
                "occupancy": occupancy,
                "outdoor_trend": trend_label,
                "time_of_day": hour_bucket,
                "strategy": STRATEGY,
                "fault_fallback": is_faulty,
                "confidence": confidence,
                "lighting_pct": round(lighting_pct, 2),
                "meeting_prep": meeting_prep,
                "demand_response_active": demand_response_active,
                "used_llm": used_llm,
                "used_llm_lighting": used_llm_lighting,
                # Honesty fields for the dashboard: "used_llm" alone can't
                # distinguish a policy issued this tick from one cached 18
                # ticks ago -- surface exactly how stale it is rather than
                # implying a fresh decision every tick.
                "policy_issued_at_tick": policy_entry["issued_at_tick"] if used_llm and policy_entry else None,
                # Same honesty field, tracked independently for lighting --
                # can legitimately differ from policy_issued_at_tick above
                # (an invocation can update one cache without the other).
                "lighting_policy_issued_at_tick": (
                    lighting_entry["issued_at_tick"] if used_llm_lighting and lighting_entry else None
                ),
                # Real-time occupancy-proportional scaling ratio applied to the
                # cached policy this tick (1.0 = no scaling, i.e. occupancy is
                # at/above what it was when the policy was issued; lower means
                # fewer people are here now and the setpoint/lighting were
                # pulled toward the cheaper fallback edge accordingly). See
                # scale_cached_setpoints_for_occupancy.
                "occupancy_scale_ratio": (
                    round(_occupancy_ratio(occupancy, policy_entry.get("issued_at_occupancy", occupancy)), 2)
                    if used_llm and policy_entry else None
                ),
                # Applied setpoints -- stored on Decision.drivers (not just the
                # live-state snapshot) so the Decision & Reasoning panel can show
                # a reliable before/after for every strategy, not just llm.
                "heating_c": validated["heating_c"],
                "cooling_c": validated["cooling_c"],
                "guardrail_intervened": validated["guardrail_intervened"],
            }
            zone_snapshots[zone] = dict(drivers)

            db.add(RunTick(
                tick=tick,
                strategy=STRATEGY,
                baseline_kwh=baseline_kwh_for_tick,
                ai_kwh=ai_kwh,
                comfort_deviation_c=abs(temp_c - (OCCUPIED_HEAT_C + OCCUPIED_COOL_C) / 2) if occupancy > 0 else 0.0,
                outdoor_temp_c=outdoor_temp_c,
                hvac_kwh=hvac_kwh,
                lighting_kwh=lighting_kwh,
                plugload_kwh=plugload_kwh,
                pv_kwh=pv_kwh,
                carbon_kg=carbon_kg,
                water_m3=water_m3,
                demand_response_active=demand_response_active,
            ))

            if used_llm:
                last_texts = policy_state["last_proposal_texts"]
                energy_proposal_text = last_texts.get("energy", "")
                comfort_proposal_text = last_texts.get("comfort", "")
                carbon_proposal_text = last_texts.get("carbon", "")
                policy_age = tick - policy_entry["issued_at_tick"]
                occ_ratio = _occupancy_ratio(occupancy, policy_entry.get("issued_at_occupancy", occupancy))
                base_explanation = (
                    policy_entry.get("rationale")
                    or last_texts.get("arbiter", "")
                    or f"heat={validated['heating_c']}C cool={validated['cooling_c']}C (LLM policy, no rationale text)"
                )
                scaling_note = (
                    f", occupancy-scaled to {occ_ratio*100:.0f}% of the {policy_entry['issued_at_occupancy']:.1f}-person "
                    f"level it was issued for (now {occupancy:.1f})"
                    if occ_ratio < 0.999 else ""
                )
                explanation = (
                    f"{base_explanation} [cached strategic policy issued at tick {policy_entry['issued_at_tick']}, "
                    f"{policy_age} tick(s) ago{scaling_note}]"
                )
            elif STRATEGY == "ashrae_baseline":
                energy_proposal_text = "(ASHRAE baseline: fixed schedule, no controls credit)"
                comfort_proposal_text = "(ASHRAE baseline: fixed schedule, no controls credit)"
                carbon_proposal_text = "(ASHRAE baseline: fixed schedule, no controls credit)"
                schedule_state = (
                    f"occupied hours {ASHRAE_BASELINE_OCC_START_HOUR:02d}:00-{ASHRAE_BASELINE_OCC_END_HOUR:02d}:00"
                    if ASHRAE_BASELINE_OCC_START_HOUR <= hour < ASHRAE_BASELINE_OCC_END_HOUR
                    else "unoccupied-hours setback"
                )
                explanation = (
                    f"heat={validated['heating_c']}C cool={validated['cooling_c']}C, lighting={lighting_pct*100:.0f}% "
                    f"(ASHRAE 90.1-style fixed schedule, {schedule_state} -- no occupancy sensing, forecast, "
                    f"demand response, or AI credit)"
                )
            else:
                reason_bits = [f"occupancy={occupancy:.1f}"]
                if meeting_prep:
                    reason_bits.append("pre-conditioned ahead of historical arrival time")
                if demand_response_active:
                    reason_bits.append("demand response active, comfort band relaxed")
                if STRATEGY == "llm":
                    if occupancy <= 0:
                        reason_bits.append("unoccupied -- deterministic setback (strategic policy only tunes occupied zones)")
                    elif policy_entry is None:
                        reason_bits.append("no strategic policy issued yet, reactive fallback")
                    elif not policy_fresh:
                        reason_bits.append(
                            f"cached policy from tick {policy_entry['issued_at_tick']} is stale "
                            f"(invocation budget of {LLM_MAX_INVOCATIONS} exhausted), reactive fallback"
                        )
                energy_proposal_text = "(rule-based placeholder, not yet agent-driven)"
                comfort_proposal_text = "(rule-based placeholder, not yet agent-driven)"
                carbon_proposal_text = "(rule-based placeholder, not yet agent-driven)"
                explanation = (
                    f"heat={validated['heating_c']}C cool={validated['cooling_c']}C, lighting={lighting_pct*100:.0f}% "
                    f"({', '.join(reason_bits)}, trend={trend_label}{', FALLBACK' if is_faulty else ''})"
                )

            db.add(Decision(
                tick=tick,
                strategy=STRATEGY,
                zone=zone,
                energy_proposal=energy_proposal_text,
                comfort_proposal=comfort_proposal_text,
                carbon_proposal=carbon_proposal_text,
                arbiter_decision=explanation,
                guardrail_intervened=validated["guardrail_intervened"],
                drivers=json.dumps(drivers),
            ))

        active_zone = max(zone_snapshots, key=lambda z: zone_snapshots[z]["occupancy"])
        guardrail_hits = sum(1 for z in zone_snapshots.values() if z["guardrail_intervened"])
        log_pipeline_event(db, tick, "actuator_applied", {
            "zones": {z: {"heating_c": s["heating_c"], "cooling_c": s["cooling_c"], "lighting_pct": s["lighting_pct"],
                          "used_llm": s["used_llm"]} for z, s in zone_snapshots.items()},
            "guardrail_interventions": guardrail_hits,
        })
        log_pipeline_event(db, tick, "tick_complete", {"active_zone": active_zone})

        # Real sim-speed readout: real seconds elapsed per real sim tick,
        # from actual tick_start wall-clock timestamps recorded this run --
        # not a fabricated/assumed rate.
        sim_seconds_per_tick = None
        if len(tick_start_wall_time) >= 2:
            span = tick_start_wall_time[-1] - tick_start_wall_time[0]
            sim_seconds_per_tick = span / (len(tick_start_wall_time) - 1) if span > 0 else None

        live_state.write_dashboard_state({
            "tick": tick,
            "strategy": STRATEGY,
            # This subprocess's own PID -- lets the backend's /api/stop-simulation
            # kill an ORPHANED run (one this backend instance never spawned, e.g.
            # after a backend restart mid-run) instead of only ever being able to
            # stop a run it tracked in memory itself. See backend/main.py.
            "pid": os.getpid(),
            "sim_clock": sim_clock,
            "building_idf": "CURRENT_modified_RefBldgSmallOffice.idf",
            "outdoor_temp_c": round(outdoor_temp_c, 2),
            "ai_kwh": round(ai_kwh, 3),
            "hvac_kwh": round(hvac_kwh, 3),
            "lighting_kwh": round(lighting_kwh, 3),
            "plugload_kwh": round(plugload_kwh, 3),
            "pv_kwh": round(pv_kwh, 3),
            "carbon_kg": round(carbon_kg, 4),
            "water_m3": round(water_m3, 4),
            "demand_response_active": demand_response_active,
            "energy_anomaly": energy_anomaly,
            "zones": zone_snapshots,
            "active_zone": active_zone,
            "wall_time": tick_wall_time,
            "real_seconds_per_sim_tick": sim_seconds_per_tick,
            "eplus_api_source": EPLUS_API_SOURCE,
        })

        db.commit()

    api.runtime.callback_begin_zone_timestep_after_init_heat_balance(state, on_zone_timestep)
    try:
        exit_code = api.runtime.run_energyplus(state, ["-w", EPW_PATH, "-d", OUTPUT_DIR, "-r", IDF_PATH])
    finally:
        # EnergyPlus may finish (naturally, or via MAX_SIM_TICKS's
        # stop_simulation()) while the strategic layer's background thread
        # is still mid-invocation -- on_zone_timestep won't fire again to
        # consume its result, so do it here rather than silently discard
        # real compute that already happened. join() first so we're not
        # racing the worker thread's writes to _llm_result_box.
        thread = policy_state.get("thread")
        if thread is not None and thread.is_alive():
            thread.join(timeout=300)
        with _llm_lock:
            final_result = dict(_llm_result_box) if _llm_result_box.get("done") else None
            _llm_result_box.clear()
        if final_result is not None:
            for phase, detail in final_result["events"]:
                log_pipeline_event(db, final_result["tick"], phase, detail)
            if final_result["ok"]:
                log_pipeline_event(db, final_result["tick"], "llm_policy_updated", {
                    "trigger": final_result["trigger_reason"],
                    "zones_updated": list(final_result["decisions"].keys()),
                    "note": "captured after simulation end",
                })
            else:
                log_pipeline_event(db, final_result["tick"], "llm_pipeline_error", {
                    "trigger": final_result["trigger_reason"], "error": final_result["error"],
                })
            db.commit()
        db.close()
        if bridge is not None:
            bridge.stop()

    if exit_code != 0:
        # run_energyplus() returns nonzero on a fatal EnergyPlus error (e.g.
        # an invalid setpoint pair) but does NOT raise a Python exception --
        # discovered when a real run crashed and the wrapping subprocess.run
        # (in sim/compare_runs.py) still reported success because THIS
        # process's own exit code was 0. Surface it so callers relying on
        # subprocess exit codes (compare_runs.py's check=True, the backend's
        # /api/run-simulation) actually see the failure.
        raise RuntimeError(f"EnergyPlus exited with code {exit_code} -- check sim/output/eplusout.err")


if __name__ == "__main__":
    run()
