# Full Dashboard Walkthrough Script

This is the comprehensive, "explain everything" version — not the 3-minute pitch
(that's `DEMO_SCRIPT.md`). Use this for a longer explainer video that walks through
every panel, every metric, and every term in the dashboard, plus what to look at
once a simulation finishes. Record in chapters; each `##` section is a natural
cut point.

Setup before recording: `uvicorn backend.main:app` + `npm run dev`, and ideally
`data/building.db` already populated (run `python -m sim.compare_runs` at least
once so the baseline-dependent tiles have real numbers instead of "no baseline
run yet").

---

## 0. What this project is (30-60s)

"This is an autonomous smart building optimization system. Three real pieces,
wired together for real, not simulated in a slide:

- **EnergyPlus** — the actual physics simulation engine used across the real
  building industry. It's modeling a real reference office building, minute by
  minute: temperature, humidity, CO2, energy draw, solar gain, all of it.
- **A local open-source LLM** — Llama 3.2, running via Ollama, reasoning about
  that building's state every control cycle.
- **MCP — the Model Context Protocol** — the real client-server protocol that
  lets the LLM actually read the building's state and actually act on it,
  instead of just being fed a canned prompt.

Everything in this dashboard is either live data polled from a real running
simulation, or historical data logged from a real completed one. Nothing is
hand-typed or faked — I'll point out the exact evidence for that as we go."

---

## 1. Header and top controls

Point at the header bar.

"Top left: the app title and a one-line summary of the stack — EnergyPlus,
multi-agent LLM over MCP, solar PV, daylight harvesting, guardrails. Next to it,
once data exists, it names the **headline strategy** — whichever strategy's data
is currently being summarized in the metric cards below (the backend prefers a
real `llm` run if one has a baseline, otherwise `predictive`, otherwise whatever
exists).

Top right: a **Live / Disconnected** badge. This is the WebSocket connection
status — it's tailing the database for genuinely new simulation ticks as they're
written, not sending fake data."

### Run a real simulation panel

"This is how you trigger an actual simulation run, not a canned demo.

- **Strategy dropdown** — three options:
  - **Reactive**: a pure occupancy-setback rule. Occupied → heat to 21°C / cool to
    24°C. Unoccupied → setback to 17°C / 27°C. No memory, no lookahead. This is
    the baseline everything else is measured against.
  - **Predictive**: the same rule, but smarter — it looks ahead at EnergyPlus's
    own forecast weather data to pre-condition a zone before a temperature swing
    hits, and it remembers each zone's own occupancy history earlier in the run
    to pre-condition a room before people typically arrive — we call that
    'meeting-prep'.
  - **LLM agents (real MCP)**: the actual autonomous pipeline — three
    specialist AI agents plus an arbiter, making real decisions and calling
    real MCP tools. This one is slow — 15 to 30-plus minutes — because it's
    making genuine local LLM calls, not because anything is artificially
    throttled.
- **Run simulation / Stop** — starts or force-kills the backend subprocess
  actually running EnergyPlus.
- **'Clear old data first' switch** — deletes this strategy's previous rows
  before starting, so a rerun doesn't silently mix old and new data together.
- **Clear all** — wipes every strategy's data, plus the on-disk live-state
  snapshots, for a totally clean slate.
- **Run baseline comparison** — a separate job: it runs reactive, then
  predictive (and llm, if you check 'include LLM'), back to back, and stitches
  the reactive run's real energy into the other strategy's numbers. This is
  the only thing that makes the 'Energy savings vs baseline' tile show a real
  percentage instead of 'no baseline run yet' — the plain 'Run simulation'
  button never does this stitching on its own."

---

## 2. The top metric cards (run-level summary)

"These summarize the entire logged run for the headline strategy — not just
this instant, the whole thing so far."

- **Energy savings vs baseline** — percentage energy reduction vs. the stitched
  reactive baseline. Shows "no baseline run yet" honestly if no baseline exists
  for this strategy yet, instead of guessing.
- **Avg comfort deviation** — average, in °C, of how far occupied-zone
  temperature strayed from the comfort band's midpoint (22.5°C) across the
  whole run. Lower is better; it's 0 for any tick with no occupants, since
  comfort only matters when someone's there to feel it.
- **Guardrail interventions** — count of times the safety layer clipped a
  proposed setpoint because it was outside the safe range, across every zone,
  every tick.
- **Resilience events handled** — count of simulated sensor-fault events (a
  zone's temperature reading was deliberately made unreliable for a tick
  window, forcing a fallback to its neighbors' average).
- **Avg PMV when occupied (Fanger)** — Predicted Mean Vote, the real ASHRAE
  Fanger thermal comfort model. Ranges roughly -3 (cold) to +3 (hot), 0 is
  neutral. Averaged only over occupied ticks, because PMV isn't meaningful for
  an empty room.
- **Avg PPD when occupied** — Predicted Percentage Dissatisfied, the % of
  occupants the Fanger model predicts would be thermally uncomfortable at that
  PMV. It's mathematically derived from PMV, not a separate measurement.
- **Sustainability score** — a 0-100 composite of three real, measured things:
  energy savings, renewable fraction, and comfort deviation, weighted 40/30/30.
  If there's no baseline yet, it renormalizes over just renewable + comfort
  rather than guessing the missing piece.
- **Renewable energy (solar PV)** — % of total energy draw offset by the
  building's simulated solar PV generation.
- **Carbon emissions (est.)** — EnergyPlus's own carbon-equivalent meter,
  summed over the run.
- **Demand response events** — count of ticks where building draw spiked well
  above its recent rolling average, triggering a real load-shedding response
  (comfort band relaxed slightly to reduce draw).
- **Water usage** — domestic hot water, from EnergyPlus's mains water meter.

---

## 3. "Live Operations" tab — the closed-loop console

"This is the tab built specifically to make the autonomous loop watchable in
real time, not just described. Six panels, all polling the real backend every
second."

### Panel 1 — EnergyPlus Simulation Monitor

"This is the simulation engine's own status board.

- **Status badge** — Running / Idle / Completed / Failed / Stopped, with a
  pulsing dot when genuinely live.
- **CURRENT_modified_RefBldgSmallOffice.idf** — the real building model filename. IDF is
  EnergyPlus's own building-description file format.
- **Sim time** — the simulated calendar date and time inside EnergyPlus right
  now, not wall-clock time.
- **Timestep / tick** — the control-loop tick number. One tick is roughly 30-60
  simulated minutes, and it's when a control decision actually gets made — not
  every physics timestep, which would be far too frequent to reason about.
- **Active zone** — whichever zone has the highest occupancy this tick.
- **Sim speed** — real ticks-per-minute, computed from actual wall-clock
  timestamps between ticks, not an assumed number.
- **Last updated** — how long ago the last tick landed.
- **Outdoor temp / Energy now / Avg PMV / Occupancy** — this instant's real
  readings.
- **HVAC setpoints table** — every zone's current heating/cooling setpoint,
  highlighting the active zone.
- **Simulation log**, with two toggles:
  - **Pipeline** — a human-readable line per real backend event (tick started,
    MCP call made, actuator updated, etc).
  - **Raw EnergyPlus** — literally EnergyPlus's own process console output.
    This is the direct proof the engine is really running — point at this for
    5-10 seconds and say so explicitly."

### Panel 2 — Closed-Loop Data Flow (the architecture diagram, live)

"This is the architecture diagram, but alive instead of a static slide.
EnergyPlus → Backend → LLM Agents → MCP → Control Engine → EnergyPlus →
Dashboard, drawn as connected boxes. Each box glows the instant that real stage
actually executes — it's keyed off the same real event stream as the log, not a
decorative animation loop. Under reactive/predictive, only the EnergyPlus/
Backend/Control/Dashboard boxes will ever light up, honestly, because those
strategies never touch MCP or the LLM."

### Panel 3 — Raw EnergyPlus Data

"This exists specifically to prove nothing here is fabricated. Every row shows
the literal EnergyPlus Python API call — the exact function and variable name
used in the code, like `get_variable_handle(state, "Zone Mean Air Temperature",
"Core_ZN")` — right next to its live value. There's a zone picker at the top.
Below the per-zone table, the building-wide meters: `ElectricityNet:Facility`,
`Photovoltaic:ElectricityProduced`, `Carbon Equivalent:Facility`, and so on —
these are EnergyPlus's own internal meter names, copyable and greppable in the
source code."

### Panel 4 — Live Building Dashboard

"The instantaneous version of the top metric cards — energy, temperature,
humidity, CO2, occupancy, comfort deviation, PMV, HVAC status (active/idle
based on real HVAC draw), lighting %, carbon, and savings — plus two live
charts of energy and comfort deviation building up tick by tick."

### Panel 5 — Decision & Reasoning

"One card per zone, always populated regardless of strategy — this was
specifically built so it isn't just an 'LLM-only' feature.

- **Zone name, tick number**
- **LLM badge** (green, brain icon) — this zone's decision this tick came from
  the real multi-agent pipeline: specialists reasoned via Ollama, the arbiter
  resolved it and called a real MCP tool.
- **Rule-based badge** — the plain occupancy rule decided it instead — either
  because the strategy isn't `llm`, or because this tick is past the capped
  agent window and it fell back to the rule.
- **Clipped badge** (red, shield icon) — the guardrail safety layer rejected
  the proposed setpoint and forced a safe one, before EnergyPlus ever saw it.
  This can appear on an LLM decision too — that's worth pointing out, since it
  shows the safety layer catching the AI's own mistakes.
- Expand a card to see **Observation** (real threshold-derived bullets from
  this tick's sensor data), **Reasoning** (the actual specialist proposal text
  the model produced, when this was an LLM tick), **Decision** (the real
  rationale text), and **Confidence** (a rule-margin heuristic — how far
  current occupancy is from the ambiguous zero/one transition, not a machine-
  learned probability, and the dashboard says so)."

### Panel 6 — MCP Tool Execution

"Every real MCP round-trip, in order, animated in as it happens.

- **MCP resource** badge — a real MCP *resource* read, like
  `read_resource(building://recent_issues)`. Resources are read-only context
  the LLM pulls in.
- **MCP tool** badge — a real MCP *tool call*, like
  `set_zone_setpoint(zone=..., heating_c=..., cooling_c=...)`, with a
  checkmark or an X showing whether it was accepted or rejected.
- **local LLM (not MCP)** badge — the three specialists' proposals. These are
  genuine Ollama calls, but they're *not* MCP round-trips — only the arbiter
  actually talks over MCP. This dashboard is deliberately precise about that
  distinction rather than calling everything 'MCP' for effect.

If you're on reactive/predictive, this panel will honestly say MCP calls only
happen under the llm strategy."

### Panel 7 — Closed-Loop Event Stream

"The full timeline, every stage, in the order it really executed: tick start →
(llm ticks only: resource read → specialist reasoning → tool call) → actuator
update → tick complete. This is the single panel that makes the loop watchable
end to end without describing it — just point at it scrolling."

---

## 4. The other tabs — what to look at after a simulation finishes

"These five tabs hold the historical, whole-run view — what you look at once a
run is done, rather than while it's live."

### Overview

- **Energy chart** — AI-controlled vs. baseline energy, tick by tick, over the
  whole run.
- **Comfort deviation chart** — same, for comfort deviation.
- **Strategy compare** — side-by-side reactive vs predictive vs llm, whole-run
  totals. Point specifically at the **isolated LLM-active-window** box if it's
  there: "the whole-run llm number is diluted because the agent is capped to N
  ticks for latency — the rest of a long run reverts to the reactive rule. This
  isolated window compares the agent only against the identical ticks under
  reactive control — that's the real, attributable number."

### Digital Twin

"A floorplan replay of the whole run. Hover or click a zone and a real info
panel appears beside it — temperature, humidity, CO2, PMV, PPD, daylight,
lighting %, AI confidence, guardrail-clipped flag, meeting-prep/fault-fallback
tags. Use the play/scrub control below to step through the run tick by tick."

### AI Agents

- **Multi-agent LLM pipeline panel** — counts of agent-driven HVAC decisions,
  agent-driven lighting decisions, and reactive-fallback ticks, for the whole
  llm run.
- **Decision feed** — every single zone-tick decision, expandable, showing all
  three specialist proposals plus the arbiter's rationale verbatim. This is
  where you show a case where a specialist's output was malformed and got
  dropped — "the pipeline doesn't hide the LLM's mistakes, it validates and
  logs them."

### Analytics

- **Energy breakdown** — HVAC / lighting / plug loads / solar PV, as a bar
  chart, for the whole run.
- **Space utilization** — % of ticks each zone was occupied, by day vs night,
  derived from real logged occupancy — not a guess.
- **Predictive maintenance** — a runtime heuristic (estimated HVAC hours, %
  of ticks at heavy load) flagging whether a system's been working hard enough
  to warrant a look. Explicitly labeled as a heuristic from logged electricity
  draw, not real equipment telemetry.
- **What-if simulation** — slide an outdoor-temperature delta and get a fast
  linear-fit projection of the energy impact, based on the actual logged
  temperature/energy relationship from this run. Labeled as an approximation,
  not a live re-simulation.

### Resilience

"Every sensor-fault and energy-anomaly event, logged as it happened: which
zone, what tick, and whether a fallback was used. Sensor faults are a
deliberately simulated one-zone dropout with neighbor-zone fallback; energy
anomalies are a real statistical z-score outlier check against the zone's own
rolling history — not scripted."

---

## Glossary (quick reference)

- **EnergyPlus** — the real building-physics simulation engine.
- **IDF** — EnergyPlus's building-description file format (`CURRENT_modified_RefBldgSmallOffice.idf`).
- **MCP (Model Context Protocol)** — the real client-server protocol connecting
  the control loop to the LLM's tools/resources.
- **Resource vs. Tool (MCP)** — a resource is read-only context (`building://zone_state`,
  `building://recent_issues`); a tool is an action the LLM can invoke
  (`set_zone_setpoint`, `set_lighting_level`).
- **Tick** — one control decision cycle, ~30-60 simulated minutes.
- **Zone** — Core_ZN (interior, no windows) plus Perimeter_ZN_1-4 (South/East/
  North/West-facing, each with windows and daylighting).
- **PMV** — Predicted Mean Vote, Fanger thermal comfort model, -3 (cold) to +3
  (hot), 0 = neutral.
- **PPD** — Predicted Percentage Dissatisfied, derived from PMV.
- **Guardrail intervention / "clipped"** — the safety layer forced an unsafe
  proposed setpoint back into a safe range before EnergyPlus applied it.
- **Meeting-prep** — predictive strategy pre-conditioning a zone ahead of its
  own historical occupancy pattern.
- **Demand response** — shedding load (relaxing comfort band slightly) when
  draw spikes well above its recent rolling average.
- **Confidence** — a rule-margin heuristic based on how far occupancy is from
  the 0/1 transition zone, not a machine-learned probability.
- **Sustainability score** — weighted composite (40% savings / 30% renewable /
  30% comfort) renormalized when savings isn't measurable yet.
- **Baseline / stitching** — copying the reactive run's real energy into
  another strategy's `baseline_kwh` column, tick-matched, so a real % savings
  can be computed. Only `sim/compare_runs.py` (or the dashboard's "Run baseline
  comparison" button) does this.
- **Strategy** — `reactive` (rule, no memory), `predictive` (rule + weather
  lookahead + occupancy-history pre-conditioning), `llm` (real multi-agent MCP
  pipeline, capped ticks, falls back to reactive after the cap).
