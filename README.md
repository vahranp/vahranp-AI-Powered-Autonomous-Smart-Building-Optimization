# Eco-Loop Building Agents — Autonomous Smart Building Optimization

Closed-loop Building Management System running on a real EnergyPlus simulation:
EnergyPlus (Python API) <-> control loop (reactive / predictive rule-based strategies,
or the real multi-agent LLM pipeline over MCP) <-> SQLite <-> FastAPI backend <->
React dashboard (digital-twin replay, strategy comparison, resilience feed, live
agent negotiation feed).

## Deliverables mapping (hackathon submission)

1. **Fully functional source code** — this repository. `sim/` (EnergyPlus wrapper + control
   loop), `agents/` + `mcp_server/` + `sim/mcp_bridge.py` (LLM orchestration + communication bus),
   `backend/` + `frontend/` (dashboard).
2. **Building models** — `building_models/BASELINE_original_unmodified_RefBldgSmallOffice.idf`
   (untouched original) and `building_models/CURRENT_modified_RefBldgSmallOffice.idf` (the
   modified version actually run — see `ARCHITECTURE.md`'s "Building model additions" section
   for exactly what changed and why).
3. **Quantitative savings dashboard** — the running dashboard's "Reactive vs predictive vs LLM"
   panel, backed by `/api/compare`; see "Reading the savings numbers correctly" below before
   quoting a percentage.
4. **System architecture document** — `ARCHITECTURE.md`, with explicit sections for tool-calling
   architecture, prompt engineering strategy, prompt latency management, and handling lengthy
   simulation logs (the exact four items the rubric's deliverable #4 asks for).
5. **PoC demo video + presentation** — `DEMO_SCRIPT.md` has a timed 3-minute script and slide
   content mapped to the rubric's categories; recording/template-filling is a manual step.

## Reading the savings numbers correctly

The "llm" strategy is event-driven, not called every tick (see
`ARCHITECTURE.md`) — `LLM_MAX_INVOCATIONS` caps the total number of real strategic
re-plans for the whole run (each one several sequential local LLM calls), and a tick without a
fresh-enough cached policy falls back to the reactive rule. That means **`/api/compare`'s whole-run
`llm` entry can be diluted** — a fraction of a long run's ticks may be the reactive-rule fallback,
identical to the `reactive` baseline. That number demonstrates *robustness* (the system ran the
full horizon without crashing), not pure *efficiency*.

The real, attributable **Energy Efficiency** number is `/api/compare`'s **`llm_active_window`**
field — it isolates just the ticks the agent actually controlled against the identical ticks under
pure reactive control, both stitched from the same weather/building/occupancy. The dashboard's
comparison panel surfaces this explicitly, labeled separately from the whole-run numbers.

## Project layout

- `building_models/` — `CURRENT_modified_RefBldgSmallOffice.idf` (modified: per-zone
  setpoint/lighting schedules, solar PV, CO2/IAQ, daylighting, Fanger PMV/PPD, 14-day Jan
  RunPeriod starting on a Monday) + `BASELINE_original_unmodified_RefBldgSmallOffice.idf`
  (untouched original) + weather (EPW)
- `sim/energyplus_loop.py` — the real control loop: reads live zone state, decides setpoints
  under one of three strategies (`CONTROL_STRATEGY` env var: `reactive` | `predictive` | `llm`),
  injects a scripted sensor fault with neighbor-zone fallback, logs everything to SQLite
- `sim/compare_runs.py` — runs reactive + predictive (and `--llm` optionally) back-to-back and
  stitches a real baseline comparison for each
- `sim/mcp_bridge.py` — bridges the synchronous EnergyPlus loop to the async MCP client session
  and the Ollama tool-calling agent pipeline (spawns `mcp_server/server.py` as a real subprocess)
- `mcp_server/server.py` — real MCP server: resources read live building state from
  `data/live_state.json` (written by the loop each tick), including `building://recent_issues`
  (the self-correction resource: recent guardrail interventions + a live tail of EnergyPlus's own
  error log); tools (`set_zone_setpoint`, `set_lighting_level`) are the arbiter's actual actuation path
- `agents/` — energy / comfort / carbon specialist agents (forced structured output via a local
  tool schema) + arbiter (calls the REAL MCP tools, sourced live from the server's `list_tools()`,
  and reads the self-correction resource before deciding)
- `guardrails/` — validates/clips every actuator command before it reaches EnergyPlus, including a
  minimum heating/cooling deadband (see "A real crash and its fix" below) — applies regardless of
  which strategy produced the proposal, LLM included
- `backend/` — FastAPI: REST endpoints over run/decision/resilience history (SQLite), the
  dashboard-triggered `/api/run-simulation` background-run endpoint, + a WebSocket streaming live ticks
- `frontend/` — React + TypeScript + Tailwind + Recharts dashboard: "Run simulation" trigger panel,
  savings vs baseline, comfort chart, digital-twin floorplan + tick replay scrubber,
  reactive-vs-predictive-vs-llm comparison (whole-run and isolated-window), resilience/anomaly
  feed, energy breakdown, occupancy pattern, maintenance heuristic, what-if estimator, live LLM
  agent-negotiation panel, decision feed
- `dashboard/` — original Streamlit prototype, kept as a zero-setup fallback view (`python -m streamlit run dashboard/app.py`)
- `data/` — `building.db` (SQLite) + `live_state.json` + `run_simulation.log` (ephemeral, written each run)

## A real crash and its fix (worth reading before you demo)

An early `llm`-strategy run **crashed EnergyPlus outright**: the guardrail clipped heating and
cooling setpoints to their own independent safe ranges but never checked them *against each
other*, so a proposal like heating=24°C/cooling=22°C passed validation individually while being
physically invalid together. EnergyPlus raises a severe error and **terminates the whole
simulation** for this (`DualSetPointWithDeadBand`), and — separately — `sim/energyplus_loop.py`
wasn't checking `run_energyplus()`'s return code, so the crash didn't even surface as a failure;
`sim/compare_runs.py` printed "Done." on a silently truncated run. Both are fixed now
(`guardrails/validator.py` enforces a minimum 2°C deadband between the clipped values;
`sim/energyplus_loop.py` raises if EnergyPlus's exit code is nonzero) and verified against the
exact input that caused the crash before rerunning the real pipeline. Full writeup in
`ARCHITECTURE.md`.

## Current status

**Working and verified against real EnergyPlus runs** (5-zone reference office, 14-day Jan period starting Monday):

*Core control (three real strategies, `CONTROL_STRATEGY` env var):*
- `reactive` — occupancy-setback rule
- `predictive` — pre-conditions ahead of a forecast temperature swing (EnergyPlus's own lookahead weather API) and ahead of a zone's own historical occupancy pattern from earlier in the run ("meeting room prep")
- `llm` — real multi-agent pipeline: three specialists (Ollama tool-calling, `llama3.2:3b`) propose, the arbiter reads a real self-correction resource then resolves and **actually calls the real MCP tools** (`set_zone_setpoint`/`set_lighting_level`) over a genuine stdio client-server connection to `mcp_server/server.py`, verified via captured real rationale text. Capped at `LLM_MAX_TICKS` (default 55, tuned to reach real occupied-hours behavior — occupancy in this model doesn't start until tick ~46) because each tick is several sequential local LLM calls; falls back to the reactive rule for the rest of the run, logged as such (`drivers.used_llm`). Honest caveat: a 3B model run locally on limited hardware sometimes returns malformed tool arguments (a stringified nested JSON, a `null` numeric field) — the bridge validates and drops those rather than crashing or silently miscontrolling a zone.
- Guardrail safety layer (with the heating/cooling deadband fix above), simulated sensor-fault injection + self-healing neighbor-zone fallback, statistical energy-anomaly detection, and demand-response load shedding all apply identically regardless of strategy.

*Building model additions* (each required real IDF modeling or a real EnergyPlus API discovery, not just a dashboard card):
- Rooftop solar PV (`Generator:Photovoltaic` + inverter + distribution) — real generation from `Photovoltaic:ElectricityProduced`
- CO2/indoor-air-quality modeling (`ZoneAirContaminantBalance`) — real per-zone ppm from actual occupant CO2 generation
- Daylighting reference points in all 4 window-facing zones — real per-zone illuminance (lux)
- Real Fanger PMV/PPD thermal comfort (already enabled on every `People` object in this reference building — just needed requesting)
- Real domestic water usage (`MainsWater:Facility`)
- Autonomous per-zone lighting control with daylight harvesting
- Real EnergyPlus carbon-equivalent meter, HVAC/lighting/plug-load sub-metering, per-zone HVAC energy breakdown

*Dashboard:* a **"Run simulation" trigger panel** (pick a strategy, run it live from the browser, no terminal needed), digital-twin floorplan (hover a zone for temp/humidity/CO2/PMV/PPD/daylight/lighting/AI-confidence) with tick replay scrubber, energy/comfort charts, strategy comparison (whole-run + isolated LLM-active-window), energy breakdown chart, occupancy pattern & space utilization, predictive-maintenance heuristic, what-if estimator, resilience/anomaly feed, live multi-agent LLM panel showing real specialist proposals + arbiter rationale, decision feed.

**Explicitly a heuristic, not a claim of real ML/telemetry** (labeled as such in the API and UI):
- "AI confidence" is a rule-margin heuristic, not a model probability
- "Predictive maintenance" is a runtime-hours estimate from logged HVAC electricity draw, not real equipment sensors
- "What-if simulation" is a linear fit against the logged run, not a live EnergyPlus re-simulation
- "Sustainability score" is a weighted blend of three measured components, not an external benchmark

**Not real yet:**
- The dashboard's live WebSocket feed (`/ws/live`) is still a mock generator, not the actual sim's tick stream

**Intentionally left out** (would require components with nothing real behind them in a single-building EnergyPlus simulation): voice control, security/access-control integration, multi-building management.

## Setup order (do NOT skip validation steps — verify each piece works alone first)

1. Install EnergyPlus (energyplus.net). Confirm you can run the reference IDF headless:
   `energyplus -w building_models/weather.epw -d sim/output -r building_models/CURRENT_modified_RefBldgSmallOffice.idf`
2. `pip install -r requirements.txt`
3. Install Ollama (ollama.com), pull a small tool-calling model (`ollama pull llama3.2:3b` — chosen for speed on limited hardware; a larger model will reason better but costs more wall-clock time per tick) and make sure `ollama serve` is running
4. Run the full comparison: `python -m sim.compare_runs --llm` (reactive + predictive take seconds each; the `llm` strategy's wall-clock time depends on `LLM_MAX_INVOCATIONS`, `LLM_TICK_PACING_S`, and your machine — often 30-90+ minutes — this is the single command that produces every number the dashboard shows)
5. Bring up the dashboard (see below) against that real data — or use its own "Run simulation" panel to trigger any of the three strategies again without a terminal

## Running the dashboard

Two terminals, both from the project root (the backend needs `backend` importable as a package):

```
# terminal 1 — backend (auto-seeds data/building.db if empty; leaves real data alone if present)
python -m uvicorn backend.main:app --reload --port 8000

# terminal 2 — frontend
cd frontend
npm run dev
```

Open the printed Vite URL (default `http://localhost:5173`).

See `ARCHITECTURE.md` for the data-flow diagram, design rationale, and the four documentation
sections the rubric's deliverable #4 asks for (tool-calling architecture, prompt engineering,
latency management, log handling). See `DEMO_SCRIPT.md` for the video script and presentation content.
