# System Architecture

Closed-loop Building Management System: EnergyPlus (real physics simulation) → a control
strategy (fixed-schedule baseline, deterministic rules, or a real multi-agent LLM pipeline over
MCP) → SQLite → FastAPI backend → React dashboard (live operations console, digital-twin replay,
strategy comparison). A standalone Streamlit dashboard (no backend required) is also included for
offline/deployed viewing of a completed run — see "Standalone dashboard" near the end.

This document reflects the system **as it actually runs today**, after a full round of real-run
debugging (every fix below was driven by a genuine observed bug in a real EnergyPlus + Ollama run,
not a hypothetical). Where earlier design iterations are relevant to a decision, they're described
as history within the relevant section, not as separate/competing designs.

## End-to-end pipeline

```
EnergyPlus (pyenergyplus API, per-zone-timestep callback, sim/energyplus_loop.py)
        |
        |-- reads: zone temp/humidity/occupancy/CO2/PMV/PPD/daylight, outdoor drybulb + forward lookahead
        |-- CONTROL_STRATEGY env var selects the decision path:
        |     "ashrae_baseline" -> fixed code-minimum schedule, no controls credit (the reference point)
        |     "reactive"/"predictive" -> decide_setpoints() rule (still real code, not surfaced in the UI)
        |     "llm" -> sim/mcp_bridge.py orchestrates the real multi-agent pipeline (see below)
        |-- simulated sensor-fault injection on one zone/tick-window -> neighbor-zone fallback
        v
guardrails/validator.py  -- clips/rejects unsafe setpoints, logs interventions (applies to every
        |                    strategy's output identically, including the LLM's)
        v
EnergyPlus actuators (per-zone setpoint/lighting schedules overridden, simulation advances)
        v
data/building.db (SQLite: run_ticks, decisions [+ drivers attribution], resilience_events,
        |                  pipeline_events -- one row per real closed-loop stage)
        |-- also: data/live_dashboard_state.json, written every tick for the Operations Console
        v
backend/main.py (FastAPI) -- REST + WebSocket over building.db
        v
frontend/ (React + TypeScript + Tailwind + shadcn/ui + Recharts) -- Live Operations, Overview,
        |   Digital Twin, AI Agents, Analytics, Resilience tabs. Only the ASHRAE-baseline and
        |   LLM-agent run controls are exposed (reactive/predictive selection and the
        |   reactive-baseline "run comparison" job are still real, working code paths, just not
        |   surfaced in this UI).
        v
dashboard/app.py (Streamlit, mock-data fallback) and the standalone final-results Streamlit
    dashboard (real data, no backend, see bottom of this doc) as alternates to the React app.
```

Every closed-loop stage the Operations Console displays (`tick_start`, `mcp_resource_read`,
`llm_specialist`, `mcp_tool_call`, `llm_invocation_started`, `llm_policy_updated`,
`actuator_applied`, `tick_complete`) is emitted at its real call site as it actually happens, into
the `pipeline_events` table -- not synthesized from the final result afterward.

## Control strategies

- **`ashrae_baseline`** -- fixed, code-minimum operating schedule: constant setpoints during a
  FIXED time window (`06:00`-`20:00`, from the building's own occupancy schedule), fixed setback
  outside it, fixed-schedule lighting (no daylight harvesting, no occupancy sensing). No forecast
  lookahead, no demand response, no AI. This is the "no controls credit" reference point every
  other strategy's `baseline_kwh` is stitched against, tick-for-tick, live as a run streams in.
- **`reactive`** -- occupancy-setback rule, decides only on current state.
- **`predictive`** -- same rule, plus pre-conditioning ahead of a forecast outdoor swing
  (EnergyPlus's own lookahead weather API) and ahead of a zone's own historical occupancy pattern
  from earlier in the same run ("meeting room prep").
- **`llm`** -- the real autonomous strategy this project evaluates: a hierarchical hybrid of a
  slow, event-driven multi-agent LLM layer and a fast, deterministic per-tick tactical layer. The
  rest of this document is mostly about this strategy.

## The "llm" strategy: hierarchical hybrid control

The LLM is **never called every tick**. An early design tried that (4 sequential local model
calls per tick) and measured **~230s/tick** -- at that rate more than 3-5 ticks made a 100+ tick
run impractical. The current design separates two rates:

```
                    STRATEGIC LAYER (slow, event-driven)
                    Background thread, spawned only when a real trigger fires
                    ┌─────────────────────────────────────────────────┐
                    │  agents/energy_agent.py    ─┐                     │
                    │  agents/comfort_agent.py    ├─► agents/arbiter.py │
                    │  agents/carbon_agent.py     ─┘   (real MCP tools) │
                    │                                                   │
                    │  Input:  occupied zones only + WHY this           │
                    │          invocation was triggered                 │
                    │  Output: {zone: {heating_c, cooling_c, rationale}}│
                    │          + {zone: lighting_pct} -- a POLICY, not   │
                    │          a single tick's command                  │
                    └───────────────┬───────────────────────────────────┘
                                    │ writes (under lock), once complete
                                    ▼
                    current_policy / current_lighting_policy
                    (plain dicts, module state -- issued_at_tick,
                     issued_at_occupancy, issued_at_daylight_lux)
                                    │ read every tick
                                    ▼
┌───────────────────────────────────────────────────────────────────────┐
│              TACTICAL LAYER (fast, every control tick)                   │
│  on_zone_timestep() -- the real EnergyPlus per-tick callback              │
│                                                                            │
│  1. Consume a completed background result, if any (non-blocking)          │
│  2. Evaluate triggers (_evaluate_llm_trigger) -- cheap, deterministic,     │
│     runs every tick regardless of pacing                                  │
│  3. Per zone: apply the cached policy (if fresh AND occupancy is real,     │
│     not schedule noise) with zero-latency real-time scaling, OR the        │
│     deterministic reactive rule / ASHRAE fixed schedule                    │
│  4. guardrails/validator.py clips the result (same safety envelope as      │
│     every other strategy)                                                  │
│  5. Actuate via EnergyPlus API, log Decision/RunTick/PipelineEvent          │
└───────────────────────────────────────────────────────────────────────┘
```

**The simulation is never blocked on the LLM.** The tactical layer is a synchronous callback
invoked by the EnergyPlus C++ runtime; the strategic layer runs in a `threading.Thread`, touching
only the MCP bridge and agent `propose()`/`resolve()` calls -- never EnergyPlus's `api`/`state`
objects, never the SQLAlchemy session -- coordinated through a single locked result box
(`_llm_result_box` + `_llm_lock`).

### When the strategic layer re-plans (event triggers, not a fixed interval)

`_evaluate_llm_trigger()` runs every control tick, cheap and deterministic. **Any one** of these
firing (subject to a cooldown and a hard budget) triggers a re-plan, checked in priority order:

| Trigger | Condition | Why |
|---|---|---|
| `initial_strategy` | No policy exists yet | Bootstrap |
| `zone_never_covered` | An occupied zone has never received a real policy | The arbiter doesn't reliably cover every zone in one pass (see below) -- without this, a zone that fell through every prior invocation's gaps just stays on the fallback forever, capping how much of the run is ever genuinely LLM-driven regardless of how many other triggers fire |
| `occupancy_state_change` | Any zone crosses occupied↔unoccupied | The single most decision-relevant occupancy event, checked before an aggregate delta so it can't get diluted by four unchanged zones |
| `occupancy_shift` | Σ\|Δoccupancy\| across zones ≥ 0.5 since last invocation | Smaller cumulative headcount changes |
| `weather_swing` | Outdoor trend label changes to falling/rising | Forecast conditions the policy assumed have changed |
| `energy_anomaly` | Real z-score outlier vs rolling history | The current policy isn't handling something well |
| `energy_drift` | Current tick's `ai_kwh` ≥ 1.25× what it was when the policy was issued | Faster, more targeted than waiting for the building-wide anomaly detector |
| `comfort_degradation` | \|PMV\| > 0.5 or PPD > 15% for **2 consecutive ticks** (hysteresis) | Sustained, not momentary, discomfort |
| `guardrail_rate` | ≥3 guardrail interventions in the last 5 ticks | The cached policy is repeatedly proposing something unsafe |
| `heartbeat` | 16 ticks since last invocation, nothing else fired | Confidence-decay floor for an otherwise-quiet stretch |

A **cooldown** (`LLM_MIN_COOLDOWN_TICKS=4`) and a **hard budget** (`LLM_MAX_INVOCATIONS=45`) bound
total cost. The heartbeat and cooldown values matter more than they look: a single invocation's
own compute consumes ~4-6 ticks of paced time on this hardware, so a heartbeat set too close to
that (an earlier value of 8 was tried and reverted) leaves almost no genuinely idle stretch between
invocations -- the tactical loop ends up paced almost continuously instead of only while a real
invocation is in flight. 16/4 leaves real breathing room while keeping urgent triggers fully
responsive.

### Decision caching and real-time zero-latency scaling

`current_policy` / `current_lighting_policy` are the tactical layer's only window into what the
LLM decided -- read every tick, written only when a background invocation completes. A policy is
trusted (`policy_fresh`) if the invocation budget isn't exhausted yet, or -- once it is -- if it's
less than `LLM_POLICY_STALENESS_TICKS` (32) ticks old.

Between invocations, real occupancy and daylight keep changing, and waiting for the next
invocation to react would waste the latency budget on stale decisions. Two zero-LLM-latency
mechanisms close that gap every tick:

- **`scale_cached_setpoints_for_occupancy`** -- as a zone's real occupancy falls below what it was
  when its policy was issued, blend the applied setpoint toward the cheap-end fallback values in
  direct proportion. A genuine remaining occupant still gets real conditioning, just cheaper.
- **`scale_cached_lighting_for_occupancy`** -- the lighting equivalent, with one important
  refinement over an earlier version: daylight scaling is **bidirectional**. A zone dimmed
  correctly at midday (bright) must be able to brighten back up as it gets dark, not just dim
  further as it gets brighter. A real two-day comparison run caught this: the same building, same
  occupancy schedule, produced wildly different evening lighting energy on two consecutive days
  purely because one day's cache happened to be anchored to a bright moment and the other's to a
  dark one -- not a real decision difference. The fix tracks how live daylight compares to daylight
  *at policy issuance* (`issued_at_daylight_lux`, mirroring `issued_at_occupancy`) and scales the
  cached value in both directions, the same way occupancy scaling already does for headcount.

**A windowless zone (Core_ZN, no daylight sensor) never goes through the LLM lighting cache at
all.** Three real bugs in a row on this exact zone -- cached at an unjustified 30% despite 8
occupants; pinned at 90% despite occupancy dropping to 0.4; pinned at 90% for a single occupant --
all traced back to the same root cause: trying to reconcile a cached *value* for a zone the model
has no real per-tick signal (daylight) to reason about in the first place. `decide_lighting()`'s
own occupancy-count curve (`LIGHTING_FULL_OCCUPANCY_REF=2.0` people as the "fully justified"
reference, floored so a lone occupant still gets usable light) gives this zone real-time,
zero-latency, correctly-scaling lighting with no staleness risk -- there was never a genuine need
for the strategic layer to be in the loop for it, so it isn't anymore. The arbiter's prompt was
updated to match: it's told explicitly not to bother calling `set_lighting_level` for a
null-`daylight_lux` zone, since any value it proposed would be ignored.

**Occupancy-threshold gating, applied consistently.** `LLM_FALLBACK_OCCUPANCY_THRESHOLD=0.5`
exists because a zone's occupancy reading is a smooth schedule ramp, not a step function -- a
reading of `0.18` is schedule noise (the tail of the ramp), not a real occupant. This threshold
was originally applied only inside the deterministic fallback rule; a real two-night comparison
run showed it was **not** applied to the gate that decides whether to trust a cached LLM policy at
all, so a schedule-tail reading still counted as "occupied enough," and the cached-policy scaling
never relaxes all the way to true setback (it only eases toward `LLM_FALLBACK_OCCUPIED_HEAT_C/
COOL_C`, itself still an "occupied comfort" target). One night lost 20-52% more energy than
baseline overnight; the next lost up to 93.8% more, because higher LLM coverage from other fixes
meant *more* ticks were routed through this same gap. `used_llm` and `used_llm_lighting` now both
require `occupancy >= LLM_FALLBACK_OCCUPANCY_THRESHOLD`, not just `occupancy > 0` -- a schedule-tail
reading now always falls through to the fallback rule, which already computes true setback
correctly, for both heating/cooling and lighting.

**Demand response** (load-shed on a real statistical spike vs rolling building-wide history) is
applied identically whether a zone is on the fallback rule or a fresh cached policy -- an earlier
version only shed load in the fallback path, so a zone under an active LLM policy silently missed
DR relief during exactly the highest-value spike windows.

## Tool-calling architecture

Two distinct patterns, deliberately different for different reasons:

- **Specialists (energy/comfort/carbon) use a *local* forced-structured-output tool**
  (`propose_setpoints` in `sim/mcp_bridge.py`) -- not a real MCP tool. Its only job is reliable
  parseable JSON out of a local model; nothing is executed when it's "called."
- **The arbiter's tools are the REAL MCP tools**, sourced live from `mcp_server/server.py`'s
  `list_tools()` at connection time (not hand-copied schemas that could drift). When the arbiter
  emits `set_zone_setpoint` or `set_lighting_level`, `sim/mcp_bridge.py` invokes it over the actual
  MCP `ClientSession` -- verified via the subprocess's own `Processing request of type
  CallToolRequest` log line, not simulated in-process. `mcp_server/server.py` validates the zone
  name and returns a genuine accept/reject response. The self-correction resource
  (`building://recent_issues`) is real too: it parses actual recent guardrail interventions plus a
  live tail of EnergyPlus's own `eplusout.err`, and the arbiter's system prompt explicitly tells it
  to use that feedback to avoid repeating a clipped proposal.

This split means only the agent that actually *acts* (the arbiter) touches the real protocol; the
agents that only *advise* don't need to. Every real model call, on both sides of this split, goes
through **local Ollama only** -- no hosted/third-party LLM API is used anywhere in this project, so
the building's live sensor state never leaves the machine. (An earlier iteration supported a
hosted Groq provider; it was removed entirely -- see "Reliability hardening" below for why.)

## Prompt engineering strategy

- **Single-objective framing per specialist** -- each system prompt tells the model to care about
  *only* its one objective ("You care only about energy cost, not comfort or carbon"). A single
  prompt juggling three objectives produces vague, unverifiable reasoning; three narrow prompts
  produce a transcript where each tradeoff is attributable to a specific concern.
- **All zones in one call, not one call per zone** -- keeps latency tractable (the alternative, 4
  calls × 5 zones, would make even a short run take tens of minutes).
- **Self-correction context appended to the user turn, not the system prompt** -- the arbiter's
  system prompt stays stable/cacheable describing its role once; the volatile, tick-specific
  `recent_issues` feedback goes in the user turn each call.
- **Explicit numeric-literal enforcement** -- a real run showed the model emitting pseudo-code as a
  value (`"heating_c": 21.0 if (22.04 - 21.02) > 0.5 else 18.5`) instead of computing a number.
  Every specialist and the arbiter now explicitly states "heating_c and cooling_c must be plain
  numbers, never a formula or expression."
- **Explicit once-per-zone enforcement** -- a real run showed the arbiter calling
  `set_zone_setpoint` *twice* for the same zone in one turn, with the second (contradicting) call
  silently overwriting a sane first one via last-write-wins caching -- including one case where the
  second call inverted heating above cooling (the exact pattern that once crashed EnergyPlus, see
  "Guardrail layer" below). The prompt now explicitly says "call each tool EXACTLY ONCE per zone,"
  and the code independently rejects any `set_zone_setpoint` call whose pair doesn't maintain the
  safe deadband, before it ever reaches the cache -- prompt and code both fixing the same failure
  mode, since a 3B/8B model won't always follow the instruction.
- **Explicit null-signal handling** -- a windowless zone's `daylight_lux` is `null`, not "low." An
  earlier prompt version left the model to infer what null means, and it sometimes guessed wrong.
  The prompt now spells out that null means no daylight to harvest at all, not a reason to dim (and,
  per above, this zone is no longer even asked for a lighting call at all).
- **Evidence-driven "push toward the cheap end"** -- every specialist's default anchors near the
  cheap edge of its safe/comfort range, not the midpoint, based on a real measured run showing
  average PMV of -0.04 and PPD of 6.8% -- both far inside the required comfort band, meaning
  earlier proposals had real headroom to save more without a genuine comfort cost.
- **Defensive parsing, not defensive prompting** -- the prompt asks for the right thing; the code
  (`_normalize_proposals`, the arbiter's per-call validation) doesn't trust that it always gets it,
  and drops malformed entries rather than crashing or letting bad data reach an actuator.

## Prompt latency management

Each invocation is 3 specialist calls + 1 arbiter call, **sequential**, not concurrent. Running
the three specialists concurrently was tried (they're mutually independent, and the `ollama`
Python client is itself thread-safe for concurrent connections) -- but a real run on this project's
hardware (RTX 3050, 4GB VRAM) crashed a whole invocation with `httpx.RemoteProtocolError` the
moment 3 simultaneous 8B-model inference requests hit the local Ollama server: the client handles
concurrency fine, the server ran out of headroom trying to actually serve three at once. Sequential
is the only reliable option against a single local model server; this is also *why* the project
stays local-only rather than adding back a hosted provider purely to unlock safe parallelism --
see "Reliability hardening."

The real levers that made a 100-tick run's LLM coverage practical instead of budget-starved:

1. **Occupied-zones-only prompts** -- nothing for the strategic layer to decide about a zone the
   tactical layer will setback regardless, so the payload (and token count) shrinks on every call.
2. **Adaptive pacing, not constant** -- `LLM_TICK_PACING_S` (40s) only applies while a background
   invocation is actually alive (`thread_alive` checked every tick), not on every tick
   unconditionally. A constant-pacing version wasted the full sleep on the ~28 pre-occupancy ticks
   and every quiet stretch between invocations; adaptive pacing lets those ticks run at full
   EnergyPlus speed (sub-millisecond) while still giving an in-flight invocation exactly as much
   real time as before. Net effect: the same or more invocations complete in significantly less
   total wall-clock time.
3. **Heartbeat/cooldown tuned to actually create idle gaps** (see the trigger table above) --
   without this, invocations chained back-to-back with no real gap, and the adaptive pacing above
   degenerates into looking like constant pacing again.
4. **`LLM_MAX_INVOCATIONS` as the real cost cap** (45) -- and a policy is trusted indefinitely
   while budget remains, so raising this doesn't just add more calls, it removes staleness
   pressure on every existing cached policy too.
5. **`OLLAMA_KEEP_ALIVE=30m`** -- Ollama unloads an idle model after 5 minutes by default; gaps
   between invocations can exceed that, forcing a cold reload mid-run for no benefit.
6. **`LLM_TEMPERATURE=0.2`** -- lower than Ollama's ~0.7-0.8 default, chosen because this is a
   structured-output/consistency task, not creative generation; lower temperature measurably
   reduces the malformed-tool-call rate the defensive parsing above has to clean up.

Model size (`llama3.1:8b`, partial GPU offload) was chosen for latency on this hardware, not just
capability; the tradeoff (occasional malformed tool arguments) is handled by validation, not by
ignoring it.

## Reliability hardening (all from real observed failures)

- **Specialist retry-on-empty** -- a real run showed each specialist (energy/comfort/carbon)
  returning zero usable proposals in roughly 40-60% of invocations. For energy (the
  highest-weighted specialist), that meant its voice was silent for well over half the expensive
  calls, with nothing recovering it. One bounded retry, explicit that only the tool call is wanted,
  recovers real signal without materially changing invocation cost.
- **Connection-error resilience** -- both `specialist_propose()` and `arbiter_decide()` now catch
  a transient connection failure (`httpx.RemoteProtocolError` and similar) and treat it the same as
  "model returned nothing usable," letting the existing retry recover it instead of the exception
  aborting the whole invocation.
- **Arbiter zone-completeness retry** -- a real run showed the arbiter covering only 1-2 of 5
  zones per invocation for setpoints, and separately, covering setpoints for every zone while
  calling `set_lighting_level` for *none* of them (or vice versa). One targeted retry, naming
  exactly which zones/tools were missed, is tracked independently for each -- and the lighting
  check correctly excludes windowless zones, which are never expected to get a lighting call at
  all (see above), so it doesn't spuriously retry every single invocation chasing a call that's
  correct to skip.
- **Invalid-pair rejection before caching** -- `set_zone_setpoint` calls whose heating/cooling pair
  doesn't maintain the safe deadband are rejected before they ever reach `current_policy`, not just
  clipped at actuation time. Caching an invalid pair would mean the zone's "genuine LLM decision"
  for the policy's entire lifetime (up to ~40 ticks) is actually just the guardrail's forced
  correction on every tick it's reapplied, never a real judgment call.
- **No hosted LLM provider** -- an earlier version supported Groq as an alternate, faster provider.
  It was removed entirely: this project runs exclusively against local Ollama, so the building's
  live sensor state never leaves the machine, and there's one code path to reason about instead of
  two providers with different concurrency/rate-limit/latency characteristics.

## Handling lengthy simulation logs

EnergyPlus produces large `.err`/`.csv`/`.eso` files, and a multi-day run's console output can run
to hundreds of lines. Two places this is handled, both tail-only:

- **`building://recent_issues`** (the self-correction resource) reads `eplusout.err` directly but
  only extracts `Warning`/`Severe` lines and returns the last 8 -- the arbiter gets the signal, not
  the full log.
- **`GET /api/run-log-tail`** reads the last N lines of the subprocess's redirected stdout for the
  live "Run simulation" log panel.
- **Subprocess output is redirected to a file, never a `PIPE` left undrained** -- a multi-week
  run's console output would otherwise fill an undrained pipe buffer and deadlock the subprocess.

## Guardrail layer

A deterministic, unit-tested safety net between reasoning and actuation, applied identically
regardless of which strategy produced a proposal.

**This is not hypothetical**: an early `llm`-strategy run actually crashed EnergyPlus. The
guardrail clipped heating and cooling to their own independent safe ranges (`18-24°C` heating,
`22-28°C` cooling) but never checked the two *against each other* -- the model proposed heating=24,
cooling=22 for the same zone, both individually "valid," but heating ≥ cooling. EnergyPlus doesn't
warn about this, it raises a fatal error and **terminates the entire simulation**
(`DualSetPointWithDeadBand: Effective heating set-point higher than effective cooling set-point`).
Fixed with a minimum 2°C deadband check on the *clipped* pair, not just each value independently --
and `sim/energyplus_loop.py`'s `run()` now checks `run_energyplus()`'s return code, since EnergyPlus's
fatal termination didn't raise a Python exception on its own; a silently truncated run used to still
report exit code 0.

## Building model additions

- **Solar PV** (`Generator:Photovoltaic` + inverter + distribution, on `Attic_roof_south`) -- real
  generation data queried via the `Photovoltaic:ElectricityProduced` meter.
- **CO2 / IAQ** (`ZoneAirContaminantBalance`) -- requires an explicit outdoor CO2 schedule; leaving
  it blank is a severe error in this EnergyPlus version.
- **Daylighting** (`Daylighting:ReferencePoint` + `Daylighting:Controls` per window-facing zone) --
  the real output-variable key is the `Daylighting:Controls` object's name, not the zone name;
  discovered by requesting the variable with a wildcard key and reading the real CSV header.
- **Per-zone lighting schedules** -- the stock IDF shared one lighting schedule across all zones;
  duplicated per zone so each can be actuated independently.
- **Real thermal comfort (Fanger PMV/PPD)** -- every `People` object already had
  `Thermal Comfort Model 1 Type = FANGER`; the output-variable key trap here is the *People
  object's* name (`"Core_ZN People"`), not the zone name.
- **Real water usage** -- the reference building already includes a domestic hot water system;
  `MainsWater:Facility` resolves as a real meter with no IDF changes needed.

Non-obvious EnergyPlus API facts worth remembering: (1) the facility-wide electricity meter is
`ElectricityNet:Facility`, not `Electricity:Facility` (the latter returns an unresolvable handle in
this version); (2) reporting-only variables (`Zone People Occupant Count`, etc.) silently read as
0.0 for an entire run unless `request_variable()` is called before `run_energyplus` starts, every
run; (3) several variables are keyed by the *specific model object that computes them*, not the
zone name -- when in doubt, request with a wildcard key and read the real CSV column header.

## Heuristic vs. real (read before quoting these)

- **AI confidence score** -- a rule-margin heuristic (`compute_confidence`): high when occupancy is
  unambiguous, lower near the 0-1 occupant transition. Not a model's predicted probability.
- **Predictive maintenance** -- estimates runtime hours from logged HVAC electricity draw crossing
  a threshold. Not real equipment sensors or a trained failure model.
- **What-if simulation** -- fits a linear relationship between outdoor temperature and energy from
  the actual logged run. A fast approximation, not a live EnergyPlus re-simulation.
- **Sustainability score** -- a weighted blend of three things actually measured (energy savings,
  renewable fraction, comfort deviation), not an external benchmark.

## Predictive pre-conditioning (`predictive` strategy)

`get_forecast_trend()` reads EnergyPlus's own forward weather data a few hours ahead -- a real
lookahead, not a fabricated forecast. When a cold snap or heat spike is detected within the
lookahead window, the predictive strategy nudges the setpoint before the swing arrives.

## Fault injection and self-healing

One zone's temperature reading is treated as unreliable for a scripted tick window, simulating a
stuck sensor. The control loop falls back to the average of the zone's real neighbors
(`ADJACENCY` map, mirroring the building's actual core+perimeter layout) rather than crashing or
acting on bad data. Every fallback is logged as a `ResilienceEvent`, surfaced in the dashboard.

## Digital-twin floorplan + replay scrubber

`/api/zone-timeline` reshapes the decision log into `{tick: {zone: snapshot}}`, and the frontend's
`ReplayScrubber` + `FloorplanView` let you scrub through an entire logged run and see an SVG
floorplan (core + 4 perimeter zones, matching the real building geometry) color-coded live by
temperature, with fault-fallback and guardrail badges -- built from the same real per-zone data as
every other panel.

## Why multi-agent instead of one LLM call

A single LLM asked to "balance energy, comfort, and carbon" tends to produce vague, unverifiable
reasoning. Splitting into three specialists with opposed incentives, resolved by an explicit
arbiter, produces a transcript where the tradeoff is visible and auditable.

## Why FastAPI + React instead of just Streamlit

Streamlit re-renders top to bottom on every interaction and has no real notion of a live-pushed
event. A WebSocket from FastAPI to React lets the live feed and charts update incrementally as
ticks arrive. The backend/frontend split also keeps the AI core (`agents/`, `mcp_server/`,
`guardrails/`, `sim/`) pure Python and untouched by the presentation layer.

## Why SQLite instead of Postgres

The models in `backend/db.py` are plain SQLAlchemy with no SQLite-specific types, so switching
`DATABASE_URL` later is a one-line change. SQLite avoids a service/container to manage for a
single-writer demo; not a scalability statement.

## Final measured results (this project's actual last full run)

100-tick run, January 21-23 (Chicago winter), `llm` vs `ashrae_baseline`, same weather/building/
occupancy schedule for both:

- **Energy savings: 11.28%** (634.79 kWh vs 715.47 kWh)
- **`used_llm` coverage: 51.4%** of all zone-ticks (the rest correctly run the deterministic
  fallback -- see the trigger/staleness design above for why 100% was never the goal; genuine
  coverage on occupied, non-schedule-noise ticks is what the occupancy-threshold fix protects)
- **Average comfort deviation: 0.93°C** (max 4.70°C during a real, logged transient)
- **6 guardrail interventions** across 500 logged decisions -- the safety net exists and is
  genuinely exercised (not zero), each one a real proposal caught and clipped before it reached an
  actuator, not a claim of a perfectly-behaved model (see "Guardrail layer")

These numbers are live in `data/building.db` and are what both the React dashboard and the
standalone Streamlit dashboard below actually read -- not hardcoded anywhere.

## Standalone dashboard

`standalone_dashboard/app.py` is a second, independently deployable Streamlit app that reads
`data/building.db` directly (no FastAPI backend, no live simulation subprocess required) -- built
specifically so the final results can be viewed or deployed (e.g. Streamlit Community Cloud)
without standing up the full stack. It bundles the final summary metrics, energy/comfort/carbon
charts, the ASHRAE-vs-LLM comparison, the complete per-tick decision log (reasoning text and which
agent/rule made each call), and a digital-twin floorplan replay across the full logged run. See
that file's own module docstring for exactly what it reads and how to run/deploy it.

## Build order and why

1. Validate EnergyPlus + Ollama + MCP each in isolation -- the highest-risk integration is
   EnergyPlus's Python API, so prove it works against a stock reference building before writing any
   agent code.
2. Build the MCP server against a mocked building state first, so agent development isn't blocked
   on EnergyPlus.
3. Wire `sim/energyplus_loop.py` to replace the mock state once both sides work independently.
4. Add the ASHRAE baseline run last, once the AI loop is stable, so both runs share identical
   weather/occupancy inputs for a fair comparison.
5. Redesign the per-tick LLM call into the event-driven hierarchical hybrid once per-tick latency
   proved impractical at real scale.
6. Full real-run debugging pass: every fix in "Decision caching," "Reliability hardening," and
   "Prompt engineering strategy" above came from an actual observed bug in a real logged run, not
   a hypothetical -- diagnosed from `data/building.db`, fixed, and re-verified numerically before
   moving to the next one.
