"""Runs the reactive strategy, then the predictive strategy (and optionally
the real LLM/MCP strategy), over the same weather/RunPeriod, then stitches
the reactive run's energy into each other strategy's baseline_kwh column
(matched by tick) so /api/summary and /api/compare report a real comparison
instead of a placeholder -- this is what proves the quantitative kWh
reduction the hackathon rubric asks for, for whichever strategy is being
graded (predictive rule, or the actual autonomous LLM agent).

Each strategy's old data is cleared before it reruns (matching the
dashboard's "Run simulation" default) -- otherwise reruns just append,
and duplicate tick numbers pile up in per-tick views (aggregates like
/api/compare average duplicates together silently, but the decision feed
and zone-timeline would show every past run's rows at once). Pass
--keep-existing to append instead.

Usage:
    python -m sim.compare_runs                  # reactive + predictive (~10s)
    python -m sim.compare_runs --llm            # also runs the llm strategy (~15-30+ min)
    python -m sim.compare_runs --keep-existing   # don't clear old data first
"""
import os
import subprocess
import sys

from backend.db import Decision, PipelineEvent, ResilienceEvent, RunTick, SessionLocal, init_db
from sim import live_state

PYTHON = sys.executable
LOOP_MODULE = "sim.energyplus_loop"


def clear_strategy_data(strategy: str):
    db = SessionLocal()
    try:
        db.query(RunTick).filter(RunTick.strategy == strategy).delete()
        db.query(Decision).filter(Decision.strategy == strategy).delete()
        db.query(ResilienceEvent).filter(ResilienceEvent.strategy == strategy).delete()
        db.query(PipelineEvent).filter(PipelineEvent.strategy == strategy).delete()
        db.commit()
    finally:
        db.close()
    # Also clear the on-disk live-state snapshots for this strategy, same as
    # backend/main.py's clear_data -- otherwise the Operations Console could
    # briefly show a stale leftover snapshot from a previous run of this
    # strategy before the fresh run's first tick overwrites it.
    dashboard_state = live_state.read_dashboard_state()
    if dashboard_state.get("strategy") == strategy:
        try:
            os.remove(live_state.LIVE_DASHBOARD_STATE_PATH)
        except FileNotFoundError:
            pass
    if strategy == "llm":
        mcp_live_state_path = os.path.join(os.path.dirname(__file__), "..", "data", "live_state.json")
        try:
            os.remove(mcp_live_state_path)
        except FileNotFoundError:
            pass


def run_strategy(strategy: str):
    env = os.environ.copy()
    env["CONTROL_STRATEGY"] = strategy
    subprocess.run([PYTHON, "-m", LOOP_MODULE], env=env, check=True)


def stitch_baseline_for(strategy: str):
    """Copies reactive-run energy into `strategy`'s baseline_kwh column,
    matched by tick number (both runs share the same RunPeriod/cadence, so
    tick N means the same simulated moment in both)."""
    db = SessionLocal()
    try:
        reactive_by_tick = {}
        for row in db.query(RunTick).filter(RunTick.strategy == "reactive").all():
            reactive_by_tick.setdefault(row.tick, []).append(row.ai_kwh)

        rows = db.query(RunTick).filter(RunTick.strategy == strategy).all()
        for row in rows:
            matches = reactive_by_tick.get(row.tick)
            if matches:
                row.baseline_kwh = sum(matches) / len(matches)
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
    include_llm = "--llm" in sys.argv
    clear_first = "--keep-existing" not in sys.argv

    if clear_first:
        print("Clearing old reactive/predictive" + ("/llm" if include_llm else "") + " data...")
        clear_strategy_data("reactive")
        clear_strategy_data("predictive")
        if include_llm:
            clear_strategy_data("llm")

    print("Running reactive strategy...")
    run_strategy("reactive")
    print("Running predictive strategy...")
    run_strategy("predictive")
    print("Stitching baseline energy into predictive rows...")
    stitch_baseline_for("predictive")

    if include_llm:
        print("Running real LLM/MCP agent strategy (this takes a while)...")
        run_strategy("llm")
        print("Stitching baseline energy into llm rows...")
        stitch_baseline_for("llm")

    print("Done.")
