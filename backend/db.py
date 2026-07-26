"""SQLite storage for run history, agent decisions, and resilience events.
SQLAlchemy models only -- swapping to Postgres later is a one-line change
to DATABASE_URL, no model changes needed.
"""
import os

from sqlalchemy import Boolean, Column, Float, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "building.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

# sqlite3 does not create parent directories -- on a fresh clone (no data/
# yet, since git doesn't track empty directories) this would otherwise fail
# with "unable to open database file" on the very first run.
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class RunTick(Base):
    __tablename__ = "run_ticks"

    id = Column(Integer, primary_key=True, index=True)
    tick = Column(Integer, index=True)
    strategy = Column(String, default="reactive", index=True)  # "ashrae_baseline" | "reactive" | "predictive" | "llm"
    baseline_kwh = Column(Float)
    ai_kwh = Column(Float)
    comfort_deviation_c = Column(Float)
    outdoor_temp_c = Column(Float, default=0.0)
    hvac_kwh = Column(Float, default=0.0)
    lighting_kwh = Column(Float, default=0.0)
    plugload_kwh = Column(Float, default=0.0)
    pv_kwh = Column(Float, default=0.0)
    carbon_kg = Column(Float, default=0.0)
    water_m3 = Column(Float, default=0.0)
    demand_response_active = Column(Boolean, default=False)


class Decision(Base):
    __tablename__ = "decisions"

    id = Column(Integer, primary_key=True, index=True)
    tick = Column(Integer, index=True)
    strategy = Column(String, default="reactive", index=True)
    zone = Column(String)
    energy_proposal = Column(String)
    comfort_proposal = Column(String)
    carbon_proposal = Column(String)
    arbiter_decision = Column(String)
    guardrail_intervened = Column(Boolean, default=False)
    drivers = Column(String, default="{}")  # JSON: occupancy/outdoor-trend/time-of-day/fault-fallback tags


class ResilienceEvent(Base):
    __tablename__ = "resilience_events"

    id = Column(Integer, primary_key=True, index=True)
    tick = Column(Integer, index=True)
    strategy = Column(String, default="reactive", index=True)
    zone = Column(String)
    event_type = Column(String)  # e.g. "sensor_dropout"
    description = Column(String)
    fallback_used = Column(Boolean, default=False)


class PipelineEvent(Base):
    """One row per real stage of the closed control loop, emitted live by
    sim/energyplus_loop.py (and, for MCP round-trips, sim/mcp_bridge.py) as
    each stage actually happens -- not synthesized after the fact. Polled
    incrementally by the frontend's Operations Console (id > since_id) to
    animate the EnergyPlus -> backend -> MCP -> LLM -> actuator loop as it
    really executes.

    phase is one of: tick_start, mcp_resource_read, llm_specialist,
    mcp_tool_call, actuator_applied, tick_complete. See energyplus_loop.py
    for exactly where each is emitted and what `detail` contains.
    """
    __tablename__ = "pipeline_events"

    id = Column(Integer, primary_key=True, index=True)
    tick = Column(Integer, index=True)
    strategy = Column(String, default="reactive", index=True)
    ts = Column(Float)  # time.time() when the stage actually happened
    phase = Column(String, index=True)
    zone = Column(String, nullable=True)
    detail = Column(String, default="{}")  # JSON


def init_db():
    Base.metadata.create_all(bind=engine)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
