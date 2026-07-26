"""Seeds SQLite with mock run/decision/resilience data so the API and
frontend are demoable before sim/energyplus_loop.py has been run. Safe to
call repeatedly -- no-ops once rows already exist.

Kept in sync with every field the real pipeline populates (drivers JSON
keys, RunTick columns, ResilienceEvent.strategy) -- an earlier version only
had the original handful of fields, so a cold-start dashboard (before any
real run) showed blank/zero for every panel added since then (PMV/PPD,
CO2, lighting, confidence, energy breakdown, water, carbon, LLM usage).
"""
import json

import numpy as np

from backend.db import Decision, ResilienceEvent, RunTick, SessionLocal, init_db

ZONES = ["Core_ZN", "Perimeter_ZN_1", "Perimeter_ZN_2", "Perimeter_ZN_3", "Perimeter_ZN_4"]


def _seed_strategy(session, rng, strategy: str, energy_factor: float, comfort_factor: float):
    ticks = np.arange(72)  # 3 days at hourly control cadence, matching the real sim's cadence
    reactive_baseline = 10 + 3 * np.sin(ticks / 24 * 2 * np.pi) + rng.normal(0, 0.4, len(ticks))
    strategy_kwh = reactive_baseline * energy_factor + rng.normal(0, 0.2, len(ticks))
    comfort_dev = np.abs(rng.normal(0.5 * comfort_factor, 0.15, len(ticks)))
    outdoor_temp = 2 + 6 * np.sin(ticks / 24 * 2 * np.pi) + rng.normal(0, 1, len(ticks))

    for t, base, val, dev, outdoor in zip(ticks, reactive_baseline, strategy_kwh, comfort_dev, outdoor_temp):
        hvac = val * 0.4
        lighting = val * 0.15
        plugload = val * 0.3
        pv = max(0.0, 0.5 * np.sin((t % 24) / 24 * 2 * np.pi))
        session.add(RunTick(
            tick=int(t),
            strategy=strategy,
            baseline_kwh=float(base) if strategy in ("predictive", "llm") else 0.0,
            ai_kwh=float(val),
            comfort_deviation_c=float(dev),
            outdoor_temp_c=float(outdoor),
            hvac_kwh=float(hvac),
            lighting_kwh=float(lighting),
            plugload_kwh=float(plugload),
            pv_kwh=float(pv),
            carbon_kg=float(val * 0.35),
            water_m3=float(abs(rng.normal(0.01, 0.005))),
            demand_response_active=bool(val > reactive_baseline.mean() * 1.3),
        ))

    mock_decisions = [
        (10, "Core_ZN", "hold setback, unoccupied", {
            "temp_c": 18.2, "occupancy": 0, "outdoor_trend": "steady", "time_of_day": "night",
            "humidity_pct": 35.0, "co2_ppm": 420.0, "pmv": -1.1, "ppd_pct": 32.0, "daylight_lux": None,
            "confidence": 0.95, "lighting_pct": 0.05, "used_llm": False, "used_llm_lighting": False,
            "meeting_prep": False, "demand_response_active": False,
        }, False),
        (30, "Perimeter_ZN_1", "occupied comfort band applied", {
            "temp_c": 21.4, "occupancy": 3, "outdoor_trend": "falling", "time_of_day": "day",
            "humidity_pct": 42.0, "co2_ppm": 610.0, "pmv": -0.2, "ppd_pct": 8.0, "daylight_lux": 320.0,
            "confidence": 0.9, "lighting_pct": 0.6, "used_llm": False, "used_llm_lighting": False,
            "meeting_prep": False, "demand_response_active": False,
        }, False),
        (31, "Perimeter_ZN_2", "pre-heat margin applied ahead of cold snap", {
            "temp_c": 20.1, "occupancy": 2, "outdoor_trend": "falling", "time_of_day": "day",
            "humidity_pct": 40.0, "co2_ppm": 580.0, "pmv": -0.4, "ppd_pct": 10.0, "daylight_lux": 210.0,
            "confidence": 0.85, "lighting_pct": 0.7, "used_llm": strategy == "llm", "used_llm_lighting": False,
            "meeting_prep": True, "demand_response_active": False,
        }, False),
    ]
    for tick, zone, decision, drivers, guardrail in mock_decisions:
        session.add(Decision(
            tick=tick, strategy=strategy, zone=zone,
            energy_proposal="(seeded mock data)",
            comfort_proposal="(seeded mock data)",
            carbon_proposal="(seeded mock data)",
            arbiter_decision=decision,
            guardrail_intervened=guardrail,
            drivers=json.dumps({**drivers, "strategy": strategy, "fault_fallback": False, "zone_hvac_kwh": 0.3}),
        ))

    session.add(ResilienceEvent(
        tick=22, strategy=strategy, zone="Perimeter_ZN_2", event_type="sensor_dropout",
        description="Simulated sensor fault; fell back to neighbor-zone estimate 18.1C",
        fallback_used=True,
    ))
    session.add(ResilienceEvent(
        tick=45, strategy=strategy, zone="(building)", event_type="energy_anomaly",
        description="Building electricity draw was a statistical outlier vs recent history",
        fallback_used=False,
    ))


def seed():
    init_db()
    session = SessionLocal()
    try:
        if session.query(RunTick).count() > 0:
            return

        rng = np.random.default_rng(0)
        _seed_strategy(session, rng, "reactive", energy_factor=1.0, comfort_factor=1.0)
        _seed_strategy(session, rng, "predictive", energy_factor=0.94, comfort_factor=0.85)

        session.commit()
    finally:
        session.close()


if __name__ == "__main__":
    seed()
