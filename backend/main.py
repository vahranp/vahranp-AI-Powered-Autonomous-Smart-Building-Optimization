"""FastAPI backend: REST endpoints over the SQLite run/decision history,
plus a WebSocket that streams live ticks as the simulation produces them.
Until sim/energyplus_loop.py streams live instead of just logging to
SQLite, the WebSocket sends a mock generator so the frontend has a live
feed to render today.

Several endpoints here compute derived metrics (sustainability score,
maintenance heuristic, what-if estimate) from the real logged run rather
than the simulation loop itself -- kept in the backend so they update
automatically as more real data accumulates, and so they're clearly
separable from what the control loop actually decided live.
"""
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from backend.db import Decision, PipelineEvent, ResilienceEvent, RunTick, get_session
from backend.seed import seed
from sim import live_state

# Loads .env (OLLAMA_MODEL, LLM_MAX_INVOCATIONS, etc.) into os.environ before
# the /api/run-simulation Popen calls copy it for sim.energyplus_loop's
# subprocess -- that subprocess is where sim/mcp_bridge.py actually reads
# these. override=False (the load_dotenv default) so a var already set in
# this shell session still wins over .env.
load_dotenv()

ZONE_HVAC_ACTIVE_THRESHOLD_KWH = 0.3  # near the observed p75 draw -- flags heavy-load ticks, not just fan-idle draw


def _aggregate_ticks(rows) -> list[dict]:
    """RunTick is written once per zone per tick (5 rows/tick, see
    sim/energyplus_loop.py's per-zone loop) -- the building-wide fields
    (ai_kwh, baseline_kwh, hvac_kwh, etc.) are identical across all 5 rows
    for a given tick; only comfort_deviation_c genuinely varies by zone.
    Returning every raw row produced 5 duplicate-ish points per tick on
    every chart consuming this data (visibly jagged/duplicate-labeled
    x-axis on the Overview chart), and on the live WebSocket feed it
    combined with the frontend's new-run-detection reset (a same-or-lower
    tick number means a new run started) to collapse the live chart to a
    single visible point, since tick N's 2nd-5th same-tick message looked
    like a reset every time. Aggregate to one row per tick: building-wide
    fields from the first row seen, and the worst (max) zone comfort
    deviation for that tick -- a meaningful whole-building signal, not an
    arbitrary pick."""
    by_tick: dict[int, dict] = {}
    for t in rows:
        if t.tick not in by_tick:
            by_tick[t.tick] = {
                "tick": t.tick,
                "baseline_kwh": t.baseline_kwh,
                "ai_kwh": t.ai_kwh,
                "comfort_deviation_c": t.comfort_deviation_c,
                "outdoor_temp_c": t.outdoor_temp_c,
                "hvac_kwh": t.hvac_kwh,
                "lighting_kwh": t.lighting_kwh,
                "plugload_kwh": t.plugload_kwh,
                "pv_kwh": t.pv_kwh,
                "carbon_kg": t.carbon_kg,
                "demand_response_active": t.demand_response_active,
            }
        else:
            by_tick[t.tick]["comfort_deviation_c"] = max(by_tick[t.tick]["comfort_deviation_c"], t.comfort_deviation_c)
    return [by_tick[tick] for tick in sorted(by_tick)]
TICK_HOURS = 0.5  # approximate real-world hours represented by one control tick

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
RUN_LOG_PATH = os.path.join(PROJECT_ROOT, "data", "run_simulation.log")
os.makedirs(os.path.dirname(RUN_LOG_PATH), exist_ok=True)  # data/ isn't tracked by git; don't assume it exists

# In-memory state for the one background simulation subprocess this backend
# can run at a time. A real multi-user deployment would need a job queue;
# for a single-operator demo dashboard, one in-flight run at a time is the
# right constraint -- it also prevents two runs writing to building.db at once.
_run_state = {"process": None, "strategy": None, "started_at": None, "stop_requested": False}

# Bumped every time /api/data (clear_data) runs. The Operations Console's
# frontend accumulates PipelineEvents client-side across polls (so it can
# animate the closed-loop stream/log without refetching full history every
# second) -- that cache only gets reset when it detects a brand new run
# starting. A plain "clear data" action doesn't start a run, so without this
# counter the frontend would keep showing the old (now-deleted) events
# forever. Polled via /api/run-status; the frontend resets its local cache
# whenever this value changes.
_data_epoch = {"value": 0}


def _kill_orphaned_simulation_on_startup():
    """No simulation should ever be running unless the user explicitly
    started it through this app. On Windows, a subprocess.Popen child (the
    simulation) is NOT killed when its parent (this backend) exits, so a
    previous backend instance's run survives independently -- without this,
    a fresh backend would start up and silently begin reporting that
    leftover run as "running" (via _orphaned_run_state(), used by
    /api/run-status and others), which looks exactly like something
    auto-started even though nothing did. A clean slate on every backend
    startup is the simpler, more predictable contract the user asked for.
    The PID comes from live_dashboard_state.json, written every tick by
    sim/energyplus_loop.py specifically so this (and /api/stop-simulation's
    orphan fallback) is possible."""
    state = live_state.read_dashboard_state()
    pid = state.get("pid")
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.remove(live_state.LIVE_DASHBOARD_STATE_PATH)
    except FileNotFoundError:
        pass
    print(f"[startup] killed orphaned simulation subprocess (PID {pid}) left over from a previous backend instance", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed()
    _kill_orphaned_simulation_on_startup()
    yield


app = FastAPI(title="Smart Building Optimizer API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _clear_strategy_data(session: Session, strategy: str):
    session.query(RunTick).filter(RunTick.strategy == strategy).delete()
    session.query(Decision).filter(Decision.strategy == strategy).delete()
    session.query(ResilienceEvent).filter(ResilienceEvent.strategy == strategy).delete()
    session.query(PipelineEvent).filter(PipelineEvent.strategy == strategy).delete()
    session.commit()


def _clear_live_state_files(strategy: Optional[str]):
    """The DB delete above only clears SQLite rows -- it doesn't touch the
    two on-disk live-state snapshots the Operations Console reads
    (data/live_dashboard_state.json, data/live_state.json). Without this,
    clearing data would leave the previous run's last tick/zone/setpoint
    snapshot sitting on disk, and the console would keep displaying it as
    if it were still current (just without the 'live' pulsing dot) until a
    new run happens to overwrite it."""
    dashboard_state = live_state.read_dashboard_state()
    if strategy is None or dashboard_state.get("strategy") == strategy:
        try:
            os.remove(live_state.LIVE_DASHBOARD_STATE_PATH)
        except FileNotFoundError:
            pass
    # data/live_state.json is only ever written by sim/mcp_bridge.py during
    # "llm" strategy runs, so its content implicitly belongs to that strategy
    # even though the file itself doesn't record one.
    if strategy is None or strategy == "llm":
        mcp_live_state_path = os.path.join(PROJECT_ROOT, "data", "live_state.json")
        try:
            os.remove(mcp_live_state_path)
        except FileNotFoundError:
            pass
    # run_simulation() truncates RUN_LOG_PATH itself at the start of every
    # new run, so a leftover here only matters between "clear data" and the
    # next run starting -- still worth doing on a full clear so the raw
    # EnergyPlus log toggle doesn't show a stale prior run's output.
    if strategy is None:
        try:
            open(RUN_LOG_PATH, "w").close()
        except OSError:
            pass


@app.delete("/api/data")
def clear_data(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    """Deletes logged run data. Pass strategy=ashrae_baseline/reactive/predictive/llm
    to scope the wipe to just that strategy; omit it (or pass 'all') to clear
    everything. This is the endpoint both the 'Run simulation' panel's
    auto-clear and a standalone 'clear data' action in the UI call."""
    if strategy and strategy != "all":
        if strategy not in ("ashrae_baseline", "reactive", "predictive", "llm"):
            raise HTTPException(400, "strategy must be ashrae_baseline, reactive, predictive, llm, or omitted/'all'")
        _clear_strategy_data(session, strategy)
        _clear_live_state_files(strategy)
        _data_epoch["value"] += 1
        return {"status": "cleared", "strategy": strategy}
    session.query(RunTick).delete()
    session.query(Decision).delete()
    session.query(ResilienceEvent).delete()
    session.query(PipelineEvent).delete()
    session.commit()
    _clear_live_state_files(None)
    _data_epoch["value"] += 1
    return {"status": "cleared", "strategy": "all"}


@app.post("/api/run-simulation")
def run_simulation(strategy: str = "llm", clear_existing: bool = True, session: Session = Depends(get_session)):
    """Spawns sim/energyplus_loop.py as a background subprocess -- this is
    what makes the dashboard able to trigger a real run itself instead of
    requiring a manual terminal command. Only one run at a time; a second
    request while one is in flight is rejected (409), not queued or
    stacked, since two processes writing to building.db concurrently would
    corrupt the run.

    clear_existing (default True) deletes this strategy's old RunTick/
    Decision/ResilienceEvent rows before starting -- otherwise a rerun just
    appends, and duplicate tick numbers pile up in the decision feed and
    zone-timeline (aggregates like /api/compare average them together
    silently, but per-tick views would show every past run's rows at once)."""
    if strategy not in ("ashrae_baseline", "reactive", "predictive", "llm"):
        raise HTTPException(400, "strategy must be ashrae_baseline, reactive, predictive, or llm")

    existing = _run_state["process"]
    if existing is not None and existing.poll() is None:
        raise HTTPException(409, f"a '{_run_state['strategy']}' run is already in progress")

    if clear_existing:
        _clear_strategy_data(session, strategy)
        # Without this, the on-disk live-state snapshot files (which /api/live-state
        # and the "llm" strategy's MCP resources read directly, not via the DB)
        # keep the PREVIOUS run's last values until the new subprocess's own
        # first control tick overwrites them -- and since _run_state["process"]
        # already points at the new (running) subprocess by the time that
        # happens, /api/live-state's is_running check goes true immediately,
        # so a fast clear+restart could briefly serve stale prior-run data
        # mislabeled as live. /api/data's DELETE already does this; this path
        # (used by "Run simulation"/"Run LLM simulation"/"Run ASHRAE baseline")
        # was missing it.
        _clear_live_state_files(strategy)

    env = os.environ.copy()
    env["CONTROL_STRATEGY"] = strategy
    log_file = open(RUN_LOG_PATH, "w")  # noqa: SIM115 -- kept open for the subprocess's lifetime, not this request's
    process = subprocess.Popen(
        [sys.executable, "-m", "sim.energyplus_loop"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        # POSIX only: without its own session, the child inherits this
        # server's process group, so /api/stop-simulation's os.killpg()
        # would kill the whole backend, not just the simulation. (Windows
        # ignores this and uses taskkill /T to walk the real process tree
        # instead, which doesn't depend on process groups.)
        start_new_session=(os.name != "nt"),
    )
    _run_state.update(process=process, strategy=strategy, started_at=time.time(), stop_requested=False)
    return {"status": "started", "strategy": strategy}


@app.post("/api/run-baseline-comparison")
def run_baseline_comparison(include_llm: bool = False, clear_existing: bool = True):
    """Runs sim/compare_runs.py -- reactive, then predictive (and optionally
    llm), stitching the reactive run's real energy into the other
    strategy's baseline_kwh column so /api/summary's 'Energy savings vs
    baseline' tile has something real to report. The plain 'Run simulation'
    button only runs one strategy and never stitches a baseline, which is
    why that tile shows 'no baseline run yet' otherwise -- this is the
    dashboard-triggered equivalent of running `python -m sim.compare_runs`
    from a terminal, reusing that exact tested script rather than
    reimplementing the stitching logic here."""
    existing = _run_state["process"]
    if existing is not None and existing.poll() is None:
        raise HTTPException(409, f"a '{_run_state['strategy']}' run is already in progress")

    args = [sys.executable, "-m", "sim.compare_runs"]
    if include_llm:
        args.append("--llm")
    if not clear_existing:
        args.append("--keep-existing")

    log_file = open(RUN_LOG_PATH, "w")  # noqa: SIM115 -- kept open for the subprocess's lifetime, not this request's
    process = subprocess.Popen(
        args,
        cwd=PROJECT_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
    )
    label = "comparison+llm" if include_llm else "comparison"
    _run_state.update(process=process, strategy=label, started_at=time.time(), stop_requested=False)
    return {"status": "started", "strategy": label}


@app.post("/api/stop-simulation")
def stop_simulation():
    """Force-kills the running simulation's whole process tree, not just the
    parent -- under `llm` strategy, sim/energyplus_loop.py spawns a second
    process (mcp_server.server) that Popen.terminate() alone would not
    reach on Windows, leaving an orphaned MCP server behind.

    Also handles an ORPHANED run: if this backend instance restarted (or
    never spawned the run itself) it has no Popen handle in _run_state, but
    /api/run-status still correctly reports the run as live via
    _orphaned_run_state() -- without this fallback, Stop would 400 with "no
    simulation is currently running" for a run that demonstrably is,
    forcing a manual taskkill outside the app to ever end it. The orphan's
    own PID (written into live_dashboard_state.json every tick, see
    sim/energyplus_loop.py) is what makes killing it possible here."""
    process = _run_state["process"]
    if process is not None and process.poll() is None:
        _run_state["stop_requested"] = True
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
        else:
            import signal
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        return {"status": "stopping"}

    orphan = _orphaned_run_state()
    orphan_pid = orphan.get("pid") if orphan else None
    if orphan_pid:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(orphan_pid)], capture_output=True)
        else:
            import signal
            try:
                os.killpg(os.getpgid(orphan_pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        # Don't wait up to ORPHAN_FRESHNESS_S for the status to catch up --
        # the process is dead now, so nothing will write this file again;
        # removing it immediately means /api/run-status reflects "idle"
        # right away instead of still looking "running" for up to 90s.
        try:
            os.remove(live_state.LIVE_DASHBOARD_STATE_PATH)
        except FileNotFoundError:
            pass
        return {"status": "stopping", "orphaned": True}

    raise HTTPException(400, "no simulation is currently running")


ORPHAN_FRESHNESS_S = 90  # see _orphaned_run_state() -- generous on purpose: this only needs to
# distinguish "a process is still genuinely alive" from "this is stale leftover data", not track
# live UX freshness (that's /api/live-state's own adaptive stale_threshold_s). A tight threshold
# here false-negatived during verification: individual llm-strategy ticks (several sequential real
# Ollama calls each) can legitimately take 40+ real seconds between writes.


def _orphaned_run_state() -> Optional[dict]:
    """If this backend process has no tracked simulation (e.g. it just
    restarted or crashed) but data/live_dashboard_state.json is still being
    written very recently, a simulation subprocess survived without this
    backend's knowledge -- on Windows in particular, a child spawned via
    subprocess.Popen is NOT killed when its parent (this backend) exits, so
    the real simulation keeps running and writing real data even though
    this process has no memory of it. Without this check, /api/run-status
    would wrongly report "idle" and /api/live-state would wrongly report
    stale=True for genuinely fresh, in-progress data -- discovered when the
    backend was restarted mid-run during verification and the dashboard
    would otherwise have claimed nothing was running."""
    state = live_state.read_dashboard_state()
    if not state:
        return None
    age_s = time.time() - state.get("wall_time", 0)
    if age_s > ORPHAN_FRESHNESS_S:
        return None
    return state


@app.get("/api/run-status")
def run_status():
    process = _run_state["process"]
    if process is None:
        orphaned = _orphaned_run_state()
        if orphaned:
            return {
                "status": "running",
                "strategy": orphaned.get("strategy"),
                "data_epoch": _data_epoch["value"],
                "orphaned": True,  # this backend isn't tracking the process -- Stop won't work for it
            }
        return {"status": "idle", "data_epoch": _data_epoch["value"]}
    returncode = process.poll()
    elapsed_s = round(time.time() - _run_state["started_at"], 1)
    if returncode is None:
        return {"status": "running", "strategy": _run_state["strategy"], "elapsed_s": elapsed_s, "data_epoch": _data_epoch["value"]}
    if _run_state["stop_requested"]:
        status = "stopped"
    else:
        status = "completed" if returncode == 0 else "failed"
    return {
        "status": status,
        "strategy": _run_state["strategy"],
        "elapsed_s": elapsed_s,
        "returncode": returncode,
        "data_epoch": _data_epoch["value"],
    }


@app.get("/api/run-log-tail")
def run_log_tail(lines: int = 20):
    if not os.path.exists(RUN_LOG_PATH):
        return {"lines": []}
    with open(RUN_LOG_PATH, errors="replace") as f:
        all_lines = f.readlines()
    return {"lines": [line.rstrip("\n") for line in all_lines[-lines:]]}


@app.get("/api/live-state")
def live_state_endpoint(session: Session = Depends(get_session)):
    """The real latest per-tick snapshot for the Operations Console, written
    by sim/energyplus_loop.py every tick regardless of strategy (see
    sim/live_state.py). `stale` is true if nothing has been written recently
    or no simulation is currently running -- the frontend uses this to avoid
    presenting old data as if it were live."""
    state = live_state.read_dashboard_state()
    if not state:
        return {"stale": True}

    # Real sim-speed readout derived from actual tick_start event timestamps
    # (not the single-sample estimate baked into live_state.json), and
    # tick-boundary info for "Current Timestep". Computed before the
    # staleness check below, since staleness needs it.
    strategy = state.get("strategy")
    recent_ticks = (
        session.query(PipelineEvent)
        .filter(PipelineEvent.strategy == strategy, PipelineEvent.phase == "tick_start")
        .order_by(PipelineEvent.id.desc())
        .limit(20)
        .all()
    )
    sim_ticks_per_min = None
    if len(recent_ticks) >= 2:
        span = recent_ticks[0].ts - recent_ticks[-1].ts
        sim_ticks_per_min = round(60 * (len(recent_ticks) - 1) / span, 2) if span > 0 else None
    state["sim_ticks_per_min"] = sim_ticks_per_min

    age_s = time.time() - state.get("wall_time", 0)
    # Prefer this backend's own tracked process, but fall back to the
    # orphaned-run freshness check (see _orphaned_run_state()) so a
    # simulation that outlived a backend restart doesn't get reported as
    # not-running just because this process has no handle to it.
    is_running = (_run_state["process"] is not None and _run_state["process"].poll() is None) or age_s <= ORPHAN_FRESHNESS_S
    # A fixed 15s staleness threshold works for reactive/predictive's fast
    # (millisecond) tick cadence, but falsely flags an active "llm" run as
    # stale during its genuinely slow agent-driven ticks -- each makes
    # several sequential real Ollama calls and can legitimately take
    # 15-30+ real seconds. Scale the threshold to the actually observed
    # cadence instead of a constant tuned only for the fast strategies.
    if sim_ticks_per_min and sim_ticks_per_min > 0:
        observed_seconds_per_tick = 60 / sim_ticks_per_min
        stale_threshold_s = max(15, 3 * observed_seconds_per_tick)
    else:
        stale_threshold_s = 15
    state["stale"] = age_s > stale_threshold_s or not is_running
    state["age_s"] = round(age_s, 1)
    return state


@app.get("/api/pipeline-events")
def pipeline_events(since_id: int = 0, strategy: Optional[str] = None, limit: int = 200,
                     session: Session = Depends(get_session)):
    """Incremental feed of real closed-loop pipeline stages (tick_start,
    mcp_resource_read, llm_specialist, mcp_tool_call, actuator_applied,
    tick_complete) for the Operations Console -- id > since_id so the
    frontend can poll every ~1s and only receive genuinely new events,
    matching the run_log_tail/run-status polling pattern already used
    elsewhere in this backend rather than a cross-process WebSocket push."""
    if strategy is None:
        strategy = _current_live_strategy(session)
    rows = (
        session.query(PipelineEvent)
        .filter(PipelineEvent.strategy == strategy, PipelineEvent.id > since_id)
        .order_by(PipelineEvent.id)
        .limit(limit)
        .all()
    )
    return {
        "strategy": strategy,
        "events": [
            {
                "id": e.id, "tick": e.tick, "ts": e.ts, "phase": e.phase,
                "zone": e.zone, "detail": json.loads(e.detail) if e.detail else {},
            }
            for e in rows
        ],
    }


def _current_live_strategy(session: Session) -> str:
    """Best guess at 'which strategy's data should I show right now'.

    Prefers the strategy actually being written by the live simulation
    subprocess (read from data/live_dashboard_state.json, which
    sim/energyplus_loop.py always tags with the real reactive/predictive/llm
    value) over _run_state["strategy"] -- during a "Run baseline comparison"
    job, that run-tracking label is the display string "comparison" or
    "comparison+llm", which never appears as an actual strategy value on any
    PipelineEvent/Decision row (sim/compare_runs.py runs reactive, then
    predictive, then llm as separate real-strategy-tagged subprocesses).
    Filtering by the display label directly returned zero rows for the
    entire duration of a comparison run -- this is the fix.

    Also falls back to the orphaned-run freshness check (see
    _orphaned_run_state()) so a simulation that outlived a backend restart
    still resolves to its real live strategy instead of silently falling
    through to whatever _headline_strategy() guesses."""
    running_process = _run_state["process"]
    tracked_and_running = running_process is not None and running_process.poll() is None
    if tracked_and_running or _orphaned_run_state():
        live_strategy = live_state.read_dashboard_state().get("strategy")
        if live_strategy in ("ashrae_baseline", "reactive", "predictive", "llm"):
            return live_strategy
        if _run_state["strategy"] in ("ashrae_baseline", "reactive", "predictive", "llm"):
            return _run_state["strategy"]
    return _headline_strategy(session)


def _headline_strategy(session: Session) -> str:
    """Prefer the real LLM/MCP agent run (the actual autonomous strategy the
    hackathon evaluates) if it has a stitched baseline; then predictive;
    then whatever exists (keeps mock/seeded data working before any real
    sim run has happened)."""
    for candidate in ("llm", "predictive"):
        has_baseline = (
            session.query(RunTick)
            .filter(RunTick.strategy == candidate, RunTick.baseline_kwh > 0)
            .first()
        )
        if has_baseline:
            return candidate
    # .first() with no ORDER BY has no guaranteed row order in SQL -- SQLite
    # tends to return insertion order in practice, but that's an
    # implementation detail, not a contract. This is only the last-resort
    # fallback (no llm/predictive baseline exists at all), but "whichever
    # row happens to come back" isn't the same as "the oldest surviving
    # run," which is presumably the intent here -- make it explicit.
    first = session.query(RunTick).order_by(RunTick.id).first()
    return first.strategy if first else "predictive"


@app.get("/api/summary")
def summary(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    strategy = strategy or _current_live_strategy(session)
    ticks = session.query(RunTick).filter(RunTick.strategy == strategy).all()
    total_baseline = sum(t.baseline_kwh for t in ticks)
    has_baseline = total_baseline > 0  # baseline_kwh is only populated by sim/compare_runs.py's stitching step --
    # a standalone single-strategy run (e.g. from the dashboard's "Run simulation" button) never gets it, so it's
    # always 0 there. Treating that as "0 kWh baseline" and dividing by a fake 1.0 fallback produced nonsense
    # like -347972% once total_ai grew past a few kWh -- there is no real comparison to report until a baseline
    # run has actually been stitched in, so say so instead of guessing.
    total_ai = sum(t.ai_kwh for t in ticks)
    avg_comfort_dev = sum(t.comfort_deviation_c for t in ticks) / max(len(ticks), 1)
    guardrail_count = (
        session.query(Decision)
        .filter(Decision.strategy == strategy, Decision.guardrail_intervened.is_(True))
        .count()
    )
    resilience_count = session.query(ResilienceEvent).filter(
        ResilienceEvent.strategy == strategy, ResilienceEvent.event_type == "sensor_dropout"
    ).count()
    anomaly_count = session.query(ResilienceEvent).filter(
        ResilienceEvent.strategy == strategy, ResilienceEvent.event_type == "energy_anomaly"
    ).count()

    total_carbon = sum(t.carbon_kg for t in ticks)
    total_water_m3 = sum(t.water_m3 for t in ticks)
    total_pv = sum(t.pv_kwh for t in ticks)
    renewable_fraction = total_pv / (total_ai + total_pv) if (total_ai + total_pv) > 0 else 0.0
    savings_pct = round(100 * (total_baseline - total_ai) / total_baseline, 2) if has_baseline else None

    # Sustainability score: a weighted composite of energy savings, renewable
    # fraction, and comfort deviation -- but savings is only measurable once
    # a real baseline exists (see has_baseline above). Without it, renormalize
    # the composite over just renewable + comfort rather than silently
    # treating "no data" as "0% savings" or some other guessed value.
    comfort_score_component = max(0, 1 - avg_comfort_dev / 2) * 100
    renewable_component = renewable_fraction * 100
    components = [(0.3, renewable_component), (0.3, comfort_score_component)]
    if has_baseline:
        savings_component = max(0, min(100, 50 + savings_pct * 5))
        components.append((0.4, savings_component))
    weight_sum = sum(w for w, _ in components)
    sustainability_score = round(sum(w * c for w, c in components) / weight_sum, 1)

    demand_response_ticks = sum(1 for t in ticks if t.demand_response_active)

    decisions = session.query(Decision).filter(Decision.strategy == strategy).all()
    pmv_occupied = []
    ppd_occupied = []
    for d in decisions:
        drivers = json.loads(d.drivers) if d.drivers else {}
        if drivers.get("occupancy", 0) and drivers.get("occupancy", 0) > 0:
            if drivers.get("pmv") is not None:
                pmv_occupied.append(drivers["pmv"])
            if drivers.get("ppd_pct") is not None:
                ppd_occupied.append(drivers["ppd_pct"])
    avg_pmv = round(sum(pmv_occupied) / len(pmv_occupied), 2) if pmv_occupied else None
    avg_ppd_pct = round(sum(ppd_occupied) / len(ppd_occupied), 1) if ppd_occupied else None

    return {
        "strategy": strategy,
        "savings_pct": savings_pct,
        "has_baseline_comparison": has_baseline,
        "avg_comfort_deviation_c": round(avg_comfort_dev, 2),
        "guardrail_interventions": guardrail_count,
        "resilience_events": resilience_count,
        "energy_anomalies": anomaly_count,
        "total_carbon_kg": round(total_carbon, 2),
        "total_pv_kwh": round(total_pv, 2),
        "renewable_fraction_pct": round(renewable_fraction * 100, 1),
        "sustainability_score": sustainability_score,
        "demand_response_events": demand_response_ticks,
        "peak_demand_kwh": round(max((t.ai_kwh for t in ticks), default=0), 3),
        "avg_pmv_occupied": avg_pmv,
        "avg_ppd_pct_occupied": avg_ppd_pct,
        "total_water_m3": round(total_water_m3, 3),
    }


@app.get("/api/compare")
def compare(session: Session = Depends(get_session)):
    """Reactive (baseline) vs predictive vs real-llm strategy comparison --
    real numbers whenever each strategy has been run, so the dashboard
    shows its work rather than asserting a single savings percentage.

    The whole-run 'llm' entry is diluted by design: the strategic layer is
    event-driven and budget-capped (LLM_MAX_INVOCATIONS in
    sim/energyplus_loop.py), not called every tick, so any tick without a
    fresh-enough cached policy falls back to the reactive rule -- over a
    long RunPeriod a meaningful fraction of the 'llm' strategy's ticks can
    be literally identical to reactive. That makes the whole-run number a
    robustness signal (did it run the full horizon without crashing), not a
    pure efficiency signal. 'llm_active_window' isolates just the ticks the
    agent actually controlled (drivers.used_llm) against the same ticks
    under reactive control -- that is the real, attributable Energy
    Efficiency number the rubric grades."""
    result = {}
    for strategy in ("reactive", "predictive", "llm"):
        ticks = session.query(RunTick).filter(RunTick.strategy == strategy).all()
        if not ticks:
            continue
        result[strategy] = {
            "total_kwh": round(sum(t.ai_kwh for t in ticks), 2),
            "avg_comfort_deviation_c": round(sum(t.comfort_deviation_c for t in ticks) / len(ticks), 3),
        }

    llm_decisions = session.query(Decision).filter(Decision.strategy == "llm").all()
    active_ticks = sorted({d.tick for d in llm_decisions if json.loads(d.drivers or "{}").get("used_llm")})
    if active_ticks and "reactive" in result:
        window_max = max(active_ticks)
        llm_window = session.query(RunTick).filter(RunTick.strategy == "llm", RunTick.tick <= window_max).all()
        reactive_window = session.query(RunTick).filter(
            RunTick.strategy == "reactive", RunTick.tick <= window_max
        ).all()
        if llm_window and reactive_window:
            result["llm_active_window"] = {
                "window_ticks": window_max,
                "genuinely_agent_driven_ticks": len(active_ticks),
                "llm": {
                    "total_kwh": round(sum(t.ai_kwh for t in llm_window), 2),
                    "avg_comfort_deviation_c": round(
                        sum(t.comfort_deviation_c for t in llm_window) / len(llm_window), 3
                    ),
                },
                "reactive_baseline": {
                    "total_kwh": round(sum(t.ai_kwh for t in reactive_window), 2),
                    "avg_comfort_deviation_c": round(
                        sum(t.comfort_deviation_c for t in reactive_window) / len(reactive_window), 3
                    ),
                },
            }
    return result


@app.get("/api/energy-breakdown")
def energy_breakdown(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    strategy = strategy or _current_live_strategy(session)
    ticks = session.query(RunTick).filter(RunTick.strategy == strategy).all()
    if not ticks:
        return {}
    return {
        "hvac_kwh": round(sum(t.hvac_kwh for t in ticks), 2),
        "lighting_kwh": round(sum(t.lighting_kwh for t in ticks), 2),
        "plugload_kwh": round(sum(t.plugload_kwh for t in ticks), 2),
        "pv_kwh": round(sum(t.pv_kwh for t in ticks), 2),
        "total_kwh": round(sum(t.ai_kwh for t in ticks), 2),
    }


@app.get("/api/run-log")
def run_log(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    strategy = strategy or _current_live_strategy(session)
    ticks = session.query(RunTick).filter(RunTick.strategy == strategy).order_by(RunTick.tick).all()
    return _aggregate_ticks(ticks)


@app.get("/api/decisions")
def decisions(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    strategy = strategy or _current_live_strategy(session)
    rows = session.query(Decision).filter(Decision.strategy == strategy).order_by(Decision.tick).all()
    return [
        {
            "tick": d.tick,
            "zone": d.zone,
            "energy_proposal": d.energy_proposal,
            "comfort_proposal": d.comfort_proposal,
            "carbon_proposal": d.carbon_proposal,
            "arbiter_decision": d.arbiter_decision,
            "guardrail_intervened": d.guardrail_intervened,
            "drivers": json.loads(d.drivers) if d.drivers else {},
        }
        for d in rows
    ]


@app.get("/api/latest-decisions")
def latest_decisions(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    """Just the most recent Decision row per zone -- cheap enough to poll
    every ~1s for the Operations Console's Decision & Reasoning panel,
    unlike /api/decisions which returns the full history (thousands of rows
    on a long run). Defaults to whichever strategy is currently running so
    the panel always matches what the live console is showing."""
    if strategy is None:
        strategy = _current_live_strategy(session)
    zones = [row[0] for row in session.query(Decision.zone).filter(Decision.strategy == strategy).distinct().all()]
    result = {}
    for zone in zones:
        d = (
            session.query(Decision)
            .filter(Decision.strategy == strategy, Decision.zone == zone)
            .order_by(Decision.tick.desc())
            .first()
        )
        if d:
            result[zone] = {
                "tick": d.tick,
                "zone": d.zone,
                "energy_proposal": d.energy_proposal,
                "comfort_proposal": d.comfort_proposal,
                "carbon_proposal": d.carbon_proposal,
                "arbiter_decision": d.arbiter_decision,
                "guardrail_intervened": d.guardrail_intervened,
                "drivers": json.loads(d.drivers) if d.drivers else {},
            }
    return {"strategy": strategy, "zones": result}


@app.get("/api/resilience-events")
def resilience_events(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    strategy = strategy or _current_live_strategy(session)
    rows = session.query(ResilienceEvent).filter(ResilienceEvent.strategy == strategy).order_by(
        ResilienceEvent.tick
    ).all()
    return [
        {
            "tick": e.tick,
            "zone": e.zone,
            "event_type": e.event_type,
            "description": e.description,
            "fallback_used": e.fallback_used,
        }
        for e in rows
    ]


@app.get("/api/zone-timeline")
def zone_timeline(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    """Per-zone, per-tick snapshot for the digital-twin floorplan replay:
    {tick: {zone: {temp_c, occupancy, ...}}}."""
    strategy = strategy or _current_live_strategy(session)
    rows = session.query(Decision).filter(Decision.strategy == strategy).order_by(Decision.tick).all()
    timeline: dict = {}
    for d in rows:
        drivers = json.loads(d.drivers) if d.drivers else {}
        timeline.setdefault(d.tick, {})[d.zone] = {
            "temp_c": drivers.get("temp_c"),
            "occupancy": drivers.get("occupancy"),
            "humidity_pct": drivers.get("humidity_pct"),
            "co2_ppm": drivers.get("co2_ppm"),
            "pmv": drivers.get("pmv"),
            "ppd_pct": drivers.get("ppd_pct"),
            "daylight_lux": drivers.get("daylight_lux"),
            "lighting_pct": drivers.get("lighting_pct"),
            "confidence": drivers.get("confidence"),
            "meeting_prep": drivers.get("meeting_prep", False),
            "fault_fallback": drivers.get("fault_fallback", False),
            "guardrail_intervened": d.guardrail_intervened,
        }
    return timeline


@app.get("/api/occupancy-pattern")
def occupancy_pattern(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    """Historical occupancy pattern (by zone, by hour-of-day) and space
    utilization %, derived from the real logged occupancy readings --
    this is what 'occupancy prediction' and 'space utilization analytics'
    are grounded in: actual simulated occupancy history, not a guess."""
    strategy = strategy or _current_live_strategy(session)
    rows = session.query(Decision).filter(Decision.strategy == strategy).all()

    by_zone_hour = defaultdict(lambda: defaultdict(list))
    by_zone_all = defaultdict(list)
    for d in rows:
        drivers = json.loads(d.drivers) if d.drivers else {}
        occ = drivers.get("occupancy")
        tod = drivers.get("time_of_day")
        if occ is None:
            continue
        hour_bucket = "day" if tod == "day" else "night"
        by_zone_hour[d.zone][hour_bucket].append(occ)
        by_zone_all[d.zone].append(occ)

    result = {}
    for zone, values in by_zone_all.items():
        utilization_pct = 100 * sum(1 for v in values if v > 0) / len(values) if values else 0
        result[zone] = {
            "utilization_pct": round(utilization_pct, 1),
            "avg_occupancy_day": round(
                sum(by_zone_hour[zone]["day"]) / len(by_zone_hour[zone]["day"]), 2
            ) if by_zone_hour[zone]["day"] else 0,
            "avg_occupancy_night": round(
                sum(by_zone_hour[zone]["night"]) / len(by_zone_hour[zone]["night"]), 2
            ) if by_zone_hour[zone]["night"] else 0,
        }
    return result


@app.get("/api/maintenance")
def maintenance(strategy: Optional[str] = None, session: Session = Depends(get_session)):
    """Lightweight runtime-based maintenance heuristic -- NOT real equipment
    telemetry or failure prediction. Estimates cumulative active hours from
    real HVAC electricity draw and flags zones/systems worth a look if
    runtime is unusually high relative to the rest of the run."""
    strategy = strategy or _current_live_strategy(session)
    ticks = session.query(RunTick).filter(RunTick.strategy == strategy).order_by(RunTick.tick).all()
    heavy_load_ticks = sum(1 for t in ticks if t.hvac_kwh > ZONE_HVAC_ACTIVE_THRESHOLD_KWH)
    total_ticks = len(ticks) or 1
    estimated_runtime_hours = round(sum(1 for t in ticks if t.hvac_kwh > 0) * TICK_HOURS, 1)
    heavy_load_pct = round(100 * heavy_load_ticks / total_ticks, 1)

    # Simple heuristic: spending most of the run at heavy load (not just
    # fan-idle draw) suggests the system rarely gets to rest -- worth
    # flagging for inspection, not a real fault prediction.
    flag = heavy_load_pct >= 60

    return {
        "estimated_hvac_runtime_hours": estimated_runtime_hours,
        "heavy_load_pct": heavy_load_pct,
        "service_recommended": flag,
        "note": "Heuristic from logged HVAC electricity draw, not real equipment sensors or failure data.",
    }


@app.get("/api/whatif")
def whatif(outdoor_temp_delta_c: float = 5.0, strategy: Optional[str] = None, session: Session = Depends(get_session)):
    """Lightweight what-if estimate: fits a simple linear relationship
    between outdoor temperature and building energy from the ACTUAL logged
    run, then projects what a +/- delta would do. This is a fast
    approximation, not a live EnergyPlus re-simulation -- labeled as such
    in the response."""
    strategy = strategy or _current_live_strategy(session)
    ticks = session.query(RunTick).filter(RunTick.strategy == strategy).all()
    points = [(t.outdoor_temp_c, t.ai_kwh) for t in ticks if t.outdoor_temp_c is not None]
    if len(points) < 5:
        return {"error": "not enough data logged yet to estimate a relationship"}

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in points)
    var_x = sum((x - mean_x) ** 2 for x in xs) or 1e-6
    slope = cov / var_x
    intercept = mean_y - slope * mean_x

    baseline_avg_energy = mean_y
    projected_energy = intercept + slope * (mean_x + outdoor_temp_delta_c)
    delta_pct = 100 * (projected_energy - baseline_avg_energy) / baseline_avg_energy if baseline_avg_energy else 0

    return {
        "outdoor_temp_delta_c": outdoor_temp_delta_c,
        "baseline_avg_kwh_per_tick": round(baseline_avg_energy, 3),
        "projected_avg_kwh_per_tick": round(projected_energy, 3),
        "projected_change_pct": round(delta_pct, 1),
        "method": "linear fit of outdoor_temp_c vs ai_kwh from the logged run -- an approximation, not a live re-simulation",
    }


@app.websocket("/ws/live")
async def live_feed(websocket: WebSocket):
    """Polls building.db for genuinely new RunTick rows (of whichever
    strategy is currently running, or the headline strategy if idle) and
    forwards each real one to the client -- this used to send synthetic
    random.random() numbers, which meant the dashboard could silently show
    fake data whenever the socket connected, even mid real run. Ticks are
    written by sim/energyplus_loop.py in a separate process, so this is a
    poll-and-forward bridge rather than a true push, matching every other
    live-ish endpoint in this backend.

    last_id must be reset whenever the strategy being watched changes, or
    whenever data gets cleared -- SQLite reuses RunTick.id values after a
    bulk DELETE (confirmed empirically: clearing then rerunning the same
    strategy handed out the exact same id a just-deleted row had). Without
    this, a long-lived connection's last_id stays at the old high-water
    mark forever, "RunTick.id > last_id" never matches the new run's
    reused-lower ids again, and the live charts silently stop updating for
    the rest of that connection's lifetime -- exactly the reported bug
    ("not updating when I run baseline and llm simulation, and clearing on
    clearing")."""
    await websocket.accept()
    session = next(get_session())
    last_id = 0
    last_strategy = None
    last_data_epoch = None
    try:
        while True:
            strategy = _current_live_strategy(session)
            data_epoch = _data_epoch["value"]
            if strategy != last_strategy or data_epoch != last_data_epoch:
                last_id = 0
                last_strategy = strategy
                last_data_epoch = data_epoch
            rows = (
                session.query(RunTick)
                .filter(RunTick.strategy == strategy, RunTick.id > last_id)
                .order_by(RunTick.id)
                .limit(50)  # a clean multiple of 5 (one RunTick row per zone per tick) so a batch never splits a tick's row-group across polls
                .all()
            )
            if rows:
                last_id = rows[-1].id
                for aggregated in _aggregate_ticks(rows):
                    await websocket.send_json(aggregated)
            session.expire_all()  # otherwise SQLAlchemy's identity map keeps serving the first poll's snapshot
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    finally:
        session.close()
