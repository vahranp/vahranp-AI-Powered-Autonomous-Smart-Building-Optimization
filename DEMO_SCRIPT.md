# PoC Demo Video Script (target: 3:00)

Records against the live dashboard (`npm run dev` + `uvicorn backend.main:app`) with `data/building.db`
already populated from a full `python -m sim.compare_runs --llm` run. Screen-record the browser; no
narration script is mandatory but suggested lines are included.

**Run `sim.compare_runs` (not just the dashboard's single-strategy "Run simulation" button) before
recording.** Only `compare_runs.py` stitches a real reactive baseline into `baseline_kwh`, which is
what the "Energy savings vs baseline" and sustainability-score tiles need — a standalone single-
strategy run leaves `baseline_kwh` at 0 and those tiles honestly report "no baseline run yet"
instead of a number, which you don't want mid-demo.

## 0:00–0:20 — Problem + approach (title card or talking head)

"Buildings are ~40% of global energy use, and most run on rigid rule-based schedules. We built a
closed loop where EnergyPlus is the physics simulation, an open-source LLM (Llama 3.2, running
locally via Ollama) is the reasoning layer, and MCP is the real communication protocol between
them — not a mocked integration."

## 0:20–1:40 — Show the live loop, not just the result (stay on one tab)

Everything in this section happens on the **"Live Operations" tab** — no window/tab switching,
so the whole closed loop is visible on one screen the entire time.

1. Point at the **"Run a real simulation"** panel, select **llm** strategy (optionally lower
   `MAX_SIM_TICKS` and/or `LLM_TICK_PACING_S` beforehand, e.g. `set MAX_SIM_TICKS=20`, so the
   agent-driven window finishes in a few minutes instead of the full run), click **Run simulation**.
2. Briefly switch to the **"Raw EnergyPlus"** toggle inside the Simulation Monitor panel (or the
   actual EnergyPlus terminal/console window if running outside the dashboard) for 5-10 seconds —
   point at the simulation date/timestep advancing in real EnergyPlus output, then switch back to
   **"Pipeline"**: "that's the actual simulation engine's own console, not a mock — you can watch
   simulated time advance."
3. Point at the **Data Flow diagram** (top right): "this is the real architecture, not a slide —
   each node glows the instant that stage actually executes: EnergyPlus feeds the backend, the
   LLM agents reason locally, the arbiter calls real MCP tools, EnergyPlus applies the result, the
   loop repeats."
4. Point at the **Closed-Loop Event Stream** panel scrolling live: "every line here is a real
   pipeline stage as it happens — tick start, LLM specialist reasoning, MCP tool call, actuator
   update, tick complete — nothing is replayed or staged."
5. Point at the **MCP Tool Execution** panel: "these are the actual `set_zone_setpoint` /
   `set_lighting_level` calls going out over a real MCP client-server connection, with their real
   accepted/rejected result — not simulated in-process function calls."
6. Point at the **AI Agent Console**: "Observation, Reasoning, Decision, Confidence — all derived
   from this tick's real driver data and the model's actual proposal text, not scripted."

*(The run can be sped up in editing since even a shortened LLM window takes a couple minutes —
say so on screen: "sped up Nx, nothing hidden.")*

## 1:40–2:00 — Data flowing live from EnergyPlus to the LLM (detail)

1. Switch to the **"AI Agents"** tab, expand a tick in the **decision feed**.
2. Point at the three specialist proposals (energy/comfort/carbon) — show they're genuine JSON
   the model produced, including a case where one is malformed/dropped (`raw_text` visible) —
   say: "the pipeline doesn't hide the LLM's mistakes, it validates and logs them."
3. Point at a guardrail-intervened row: "the agent proposed something out of safe bounds; the
   guardrail clipped it before EnergyPlus ever saw it — this applies identically whether the
   rule-based or the LLM strategy produced the value." Mention self-correction if a captured
   rationale shows it ("adjusted after a prior clip"), or state it: "the arbiter reads a real MCP
   resource each tick containing its own recent guardrail interventions and a live tail of
   EnergyPlus's error log, and is instructed to correct course rather than repeat the mistake."

## 2:00–2:25 — Digital twin

1. Switch to the **"Digital Twin"** tab, hover a zone to show live PMV/PPD/CO2/temperature feeding
   the same decision the AI Agent Console showed a moment ago.

## 2:25–2:50 — Quantified savings, honestly framed

1. Open **Reactive vs predictive vs LLM comparison** panel.
2. Point at the **isolated LLM-active-window** box specifically (not just the whole-run number) —
   say: "the whole-run number is diluted because we cap the LLM to N ticks per run for latency;
   this isolated window is the real, attributable comparison: the agent vs. the identical baseline
   over the exact same ticks."
3. State the real percentage on screen (energy % + comfort deviation %) from whatever the current
   run produced — **do not use a number from a previous run**, read it live off the dashboard.
4. Point at PMV/PPD metric cards: "comfort wasn't sacrificed for the energy number — here's the
   real Fanger thermal comfort model, not a synthetic proxy."

## 2:50–3:00 — Close

"Full source, both the baseline and modified IDF files, and the architecture doc are in the repo.
Everything shown here is a real run, not a recorded once-and-cherry-picked result — the 'Run
simulation' button in the dashboard triggers the exact same pipeline live."

---

# Presentation slide content (map into your provided template)

## Slide: Problem & Approach
- Buildings ≈ 40% of global energy; rule-based BMS can't adapt to real-time weather/occupancy/grid signals
- Approach: EnergyPlus (physics) + local open-source LLM (Llama 3.2 via Ollama) + MCP (real protocol, not a mock)

## Slide: Architecture
- Use the ASCII diagrams from `ARCHITECTURE.md` directly (top-of-file pipeline diagram + the
  "real multi-agent LLM pipeline" diagram) — both are accurate to the shipped code, not aspirational
- Call out: 3 specialists (single-objective prompts) → arbiter (real MCP tool calls) → guardrail →
  EnergyPlus actuator, closing with the self-correction resource read

## Slide: Closed-Loop Mechanics
- Feedback: zone temp, humidity, CO2, PMV/PPD, energy, carbon, weather — all real EnergyPlus output variables
- Reasoning: 3 single-objective specialists + arbiter resolving weighted tradeoffs
- Control: real MCP `set_zone_setpoint`/`set_lighting_level` tool calls
- Forward injection: applied via `set_actuator_value` in the same live EnergyPlus instance, same tick

## Slide: Quantified Results
- Report the **isolated LLM-active-window** number (agent vs. identical-tick baseline), not the
  diluted whole-run number — pull live from `/api/compare`'s `llm_active_window` field
- Pair with comfort: avg PMV/PPD and comfort-deviation delta over the same window, showing energy
  wasn't saved at comfort's expense
- Separately report whole-run robustness: N ticks / M days executed with zero crashes, X guardrail
  interventions all safely clipped

## Slide: Agentic Autonomy
- Real MCP client-server round trip (not in-process function calls) — cite the verified
  `list_tools()`/`call_tool()` mechanism
- Self-correction loop: `building://recent_issues` resource combining guardrail history + a live
  EnergyPlus error-log tail
- Honest tradeoff disclosure: small model (`llama3.2:3b`) chosen for latency on constrained
  hardware; defensive parsing handles its occasional malformed output rather than hiding it

## Slide: Engineering Rigor / Lessons
- Three real EnergyPlus API discoveries made by testing, not documentation-reading alone:
  `ElectricityNet:Facility` vs `Electricity:Facility`, `request_variable()` needed for
  reporting-only variables, and object-specific output-variable keys (Daylighting:Controls name,
  People object name) discovered via wildcard-then-read-CSV-header
- Prompt latency managed via one combined per-tick call (not per-zone) + a capped agent window
- Lengthy logs handled via tail-only parsing (both the self-correction resource and the
  dashboard's log viewer), and subprocess output redirected to a file rather than an undrained pipe
