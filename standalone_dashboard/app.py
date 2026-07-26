"""Standalone, backend-free Streamlit dashboard for the FINAL logged run.

Unlike dashboard/app.py (a zero-setup mock-data fallback that reads CSVs the
real pipeline never writes) and the React app (which needs backend/main.py's
FastAPI server + a live simulation subprocess running), this reads
data/building.db DIRECTLY with plain sqlite3/pandas -- no backend process,
no live simulation, no Ollama/EnergyPlus required to just VIEW a completed
run's real results. Built specifically so the final run can be viewed, or
deployed (e.g. Streamlit Community Cloud), independent of the rest of the
stack.

Run locally:
    streamlit run standalone_dashboard/app.py

Deploy: push this repo (data/building.db is not gitignored, so it travels
with the repo) and point Streamlit Community Cloud at this file. If the DB
isn't bundled for some reason, use the "Upload a building.db" uploader in
the sidebar instead. .streamlit/config.toml sets the base dark theme; the
CSS injected below layers glassmorphism/gradient styling on top of it.

Every number on this page is read straight from data/building.db -- the
exact same SQLite file backend/main.py's REST API reads for the live React
dashboard. Nothing here is recomputed differently or hardcoded; the summary
metrics reproduce backend/main.py's /api/summary formulas in pure pandas so
this stays consistent with the live dashboard without needing it running.
"""
import json
import os
import sqlite3
import tempfile

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Smart Building — Final Results", layout="wide", page_icon="🏢")

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "building.db")

ZONES = ["Core_ZN", "Perimeter_ZN_1", "Perimeter_ZN_2", "Perimeter_ZN_3", "Perimeter_ZN_4"]
# Matches the real building geometry (core + 4 perimeter zones) and the same
# layout the React app's FloorplanView uses, so the two are visually consistent.
ZONE_LAYOUT = {
    "Perimeter_ZN_3": {"label": "North", "x0": 1, "x1": 5, "y0": 4, "y1": 5},
    "Perimeter_ZN_4": {"label": "West", "x0": 0, "x1": 1, "y0": 1, "y1": 4},
    "Core_ZN": {"label": "Core", "x0": 1, "x1": 5, "y0": 1, "y1": 4},
    "Perimeter_ZN_2": {"label": "East", "x0": 5, "x1": 6, "y0": 1, "y1": 4},
    "Perimeter_ZN_1": {"label": "South", "x0": 1, "x1": 5, "y0": 0, "y1": 1},
}

# Vibrant, consistent category palette reused across metric cards, section
# headers, and every chart so the same concept always reads as the same color.
COLORS = {
    "energy": "#34d399",     # emerald
    "comfort": "#38bdf8",    # sky
    "carbon": "#fb923c",     # orange
    "guardrail": "#fb7185",  # rose
    "sustain": "#a78bfa",    # violet
    "water": "#2dd4bf",      # teal
    "llm": "#e879f9",        # fuchsia
    "baseline": "#fbbf24",   # amber
    "neutral": "#94a3b8",    # slate
}
FONT_FAMILY = "Inter, -apple-system, BlinkMacSystemFont, sans-serif"
HEADING_FONT = "'Space Grotesk', Inter, sans-serif"


# --------------------------------------------------------------------------
# Visual theme -- glassmorphism + gradient background + modern type, layered
# on top of .streamlit/config.toml's base dark theme via CSS injection.
# --------------------------------------------------------------------------
_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700'
    '&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">'
)

# NOTE ON WHY THIS STARTS WITH <style> AND NOT <link>: CommonMark classifies
# a raw-HTML block by whatever tag starts it. A block starting with <link>
# is "type 6", which (unlike <style>/<script>/<pre>) terminates at the FIRST
# blank line -- every blank line between CSS rule groups below would kick
# the rest back into normal markdown parsing (rendered as literal visible
# text, which is exactly the bug that happened). A block starting with
# <style> is "type 1", which only ends when a line contains the literal
# </style> closing tag -- blank lines inside are preserved as raw content.
# So the font <link> tags are injected via a separate, earlier st.markdown
# call (see inject_css below) and this block starts directly with <style>.
_RAW_STYLE_BLOCK = """\
        <style>
        html, body, [class*="css"] { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; }

        /* ---- animated gradient-mesh background ---- */
        .stApp {
            background:
                radial-gradient(1200px 700px at 8% -10%, rgba(56,189,248,0.16), transparent 60%),
                radial-gradient(1000px 700px at 110% 10%, rgba(232,121,249,0.14), transparent 55%),
                radial-gradient(900px 900px at 50% 120%, rgba(52,211,153,0.10), transparent 55%),
                linear-gradient(180deg, #05070d 0%, #0a0e1a 40%, #0d1224 100%);
            background-attachment: fixed;
        }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stAppViewContainer"] { animation: fadeIn 0.5s ease; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        .block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1400px; }

        h1, h2, h3, h4, .section-title { font-family: 'Space Grotesk', Inter, sans-serif !important; letter-spacing: -0.01em; }

        /* ---- hero title ---- */
        .hero-title {
            font-family: 'Space Grotesk', Inter, sans-serif;
            font-weight: 700; font-size: 2.3rem; line-height: 1.15; margin: 0 0 0.35rem 0;
            background: linear-gradient(90deg, #67e8f9 0%, #a78bfa 45%, #f472b6 100%);
            -webkit-background-clip: text; background-clip: text; color: transparent;
        }
        .hero-caption { color: #9aa4c4; font-size: 0.92rem; margin-bottom: 0.15rem; }
        .pill-row { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.6rem; }
        .pill {
            display: inline-flex; align-items: center; gap: 0.35rem;
            padding: 0.28rem 0.75rem; border-radius: 999px; font-size: 0.78rem; font-weight: 600;
            background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); color: #cfd6ee;
            backdrop-filter: blur(8px);
        }

        /* ---- glass card (custom metric grid) ---- */
        .metric-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0.9rem; margin: 0.4rem 0 1.1rem 0; }
        @media (max-width: 1100px) { .metric-grid { grid-template-columns: repeat(2, 1fr); } }
        .glass-metric {
            position: relative; overflow: hidden;
            background: linear-gradient(160deg, rgba(255,255,255,0.07), rgba(255,255,255,0.02));
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 18px; padding: 1.05rem 1.15rem 0.95rem 1.15rem;
            backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
            box-shadow: 0 8px 24px rgba(0,0,0,0.28);
            transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
        }
        .glass-metric:hover {
            transform: translateY(-3px);
            border-color: var(--accent, rgba(255,255,255,0.22));
            box-shadow: 0 14px 34px rgba(0,0,0,0.38), 0 0 0 1px var(--accent, transparent);
        }
        .glass-metric::before {
            content: ""; position: absolute; inset: 0; border-radius: 18px; padding: 1px;
            background: linear-gradient(135deg, var(--accent, #38bdf8), transparent 55%);
            opacity: 0.55; -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
            -webkit-mask-composite: xor; mask-composite: exclude; pointer-events: none;
        }
        .glass-metric .m-icon { font-size: 1.15rem; opacity: 0.9; }
        .glass-metric .m-label {
            font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
            color: #97a1c2; margin-top: 0.45rem;
        }
        .glass-metric .m-value {
            font-family: 'Space Grotesk', Inter, sans-serif; font-weight: 700; font-size: 1.65rem;
            color: #f3f5fb; margin-top: 0.1rem; line-height: 1.15;
        }
        .glass-metric .m-value .accent { color: var(--accent, #38bdf8); }
        .glass-metric .m-sub { font-size: 0.74rem; color: #7d87a8; margin-top: 0.15rem; }

        /* ---- section headers ---- */
        .section-title {
            display: flex; align-items: center; gap: 0.55rem; font-weight: 700; font-size: 1.18rem;
            color: #eef1fb; margin: 1.4rem 0 0.55rem 0; padding-bottom: 0.55rem;
            border-bottom: 1px solid rgba(255,255,255,0.08);
            background: linear-gradient(90deg, var(--accent, #38bdf8) 0%, transparent 22%) bottom left no-repeat;
            background-size: 90px 2px;
        }
        .section-title .dot {
            width: 9px; height: 9px; border-radius: 50%; background: var(--accent, #38bdf8);
            box-shadow: 0 0 10px 2px var(--accent, #38bdf8);
        }
        .section-caption { color: #8a93b5; font-size: 0.84rem; margin: -0.3rem 0 0.9rem 0; }

        /* ---- top brand bar ---- */
        .topbar {
            display: flex; align-items: center; justify-content: space-between;
            padding: 0.6rem 0.2rem 1.1rem 0.2rem; margin-bottom: 0.4rem;
            border-bottom: 1px solid rgba(255,255,255,0.07);
        }
        .topbar .brand { display: flex; align-items: center; gap: 0.55rem; font-family: 'Space Grotesk', sans-serif; font-weight: 700; color: #e9ecfb; font-size: 1rem; }
        .topbar .brand .logo-dot {
            width: 30px; height: 30px; border-radius: 9px; display: flex; align-items: center; justify-content: center;
            background: linear-gradient(135deg, #22d3ee, #a78bfa); font-size: 1rem; box-shadow: 0 0 18px rgba(34,211,238,0.35);
        }
        .topbar .live-badge {
            display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.76rem; font-weight: 600; color: #9fe8c7;
            background: rgba(16,185,129,0.12); border: 1px solid rgba(52,211,153,0.35); padding: 0.3rem 0.7rem; border-radius: 999px;
        }
        .topbar .live-badge .blip { width: 7px; height: 7px; border-radius: 50%; background: #34d399; box-shadow: 0 0 8px 2px rgba(52,211,153,0.7); animation: pulse 1.8s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

        /* ---- glass containers (st.container(border=True)) ---- */
        [data-testid="stVerticalBlockBorderWrapper"] > div {
            background: linear-gradient(160deg, rgba(255,255,255,0.045), rgba(255,255,255,0.015));
            border-radius: 18px; border-color: rgba(255,255,255,0.09) !important;
            backdrop-filter: blur(16px);
        }

        /* ---- tabs: pill style ---- */
        [data-testid="stTabs"] [data-baseweb="tab-list"] {
            gap: 0.4rem; background: rgba(255,255,255,0.035); padding: 0.35rem;
            border-radius: 14px; border: 1px solid rgba(255,255,255,0.07);
        }
        [data-testid="stTabs"] button[role="tab"] {
            border-radius: 10px; color: #9aa4c4; font-weight: 600; font-size: 0.88rem;
            padding: 0.5rem 1rem; transition: all 0.15s ease;
        }
        [data-testid="stTabs"] button[role="tab"]:hover { color: #e6e9f5; background: rgba(255,255,255,0.05); }
        [data-testid="stTabs"] button[aria-selected="true"] {
            background: linear-gradient(120deg, rgba(56,189,248,0.22), rgba(167,139,250,0.22));
            color: #ffffff !important; box-shadow: inset 0 0 0 1px rgba(255,255,255,0.15);
        }
        [data-testid="stTabs"] [data-baseweb="tab-highlight"] { display: none; }
        [data-testid="stTabs"] [data-baseweb="tab-border"] { display: none; }

        /* ---- sidebar ---- */
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, rgba(19,26,46,0.98), rgba(10,14,26,0.98));
            border-right: 1px solid rgba(255,255,255,0.06);
        }
        /* NOT a bare `* { font-family }` -- Streamlit renders its icons
        (upload icon, etc.) as text ligatures in a dedicated "Material
        Symbols Rounded" font; overriding font-family on every descendant
        clobbers that font on icon spans too, turning the icon glyph into
        literal garbled ligature text (e.g. an "upload" icon rendering as
        the raw word "upload_file"). Exclude icon elements explicitly. */
        [data-testid="stSidebar"] *:not([data-testid="stIconMaterial"]):not([data-testid="stIconMaterial"] *) {
            font-family: 'Inter', sans-serif;
        }

        /* ---- file uploader ---- */
        /* Explicit flex + gap so the dropzone's instruction text and the
        Browse-files button always sit apart -- without this, a narrow
        sidebar can collapse their spacing enough for the two text nodes
        to render on top of each other (e.g. "Upload" overlapping "Browse
        files" into an unreadable mashed-together label). */
        [data-testid="stFileUploaderDropzone"] {
            display: flex !important; align-items: center; justify-content: space-between;
            flex-wrap: wrap; gap: 0.6rem 0.9rem;
            background: rgba(255,255,255,0.04); border: 1px dashed rgba(255,255,255,0.2) !important;
            border-radius: 14px; padding: 0.85rem 1rem;
        }
        [data-testid="stFileUploaderDropzoneInstructions"] {
            display: flex; flex-direction: column; gap: 0.15rem; min-width: 0; flex: 1 1 auto;
        }
        [data-testid="stFileUploaderDropzoneInstructions"] span {
            white-space: normal; overflow-wrap: break-word; color: #dfe3f5; font-size: 0.85rem;
        }
        [data-testid="stFileUploaderDropzoneInstructions"] small { color: #8a93b5; font-size: 0.72rem; }
        [data-testid="stFileUploaderDropzone"] button { flex-shrink: 0; white-space: nowrap; }
        [data-testid="stFileUploaderFile"] { background: rgba(255,255,255,0.04); border-radius: 10px; }

        /* ---- inputs / controls ---- */
        div[data-baseweb="select"] > div, .stMultiSelect div[data-baseweb="select"] > div {
            background: rgba(255,255,255,0.045) !important; border-color: rgba(255,255,255,0.12) !important;
            border-radius: 10px !important;
        }
        [data-testid="stSlider"] [role="slider"] { box-shadow: 0 0 0 6px rgba(34,211,238,0.18); }
        .stCheckbox { padding-top: 0.15rem; }

        /* ---- buttons ---- */
        .stButton > button, [data-testid="stDownloadButton"] button {
            border-radius: 10px; border: 1px solid rgba(255,255,255,0.14);
            background: rgba(255,255,255,0.05); font-weight: 600; transition: all 0.15s ease;
        }
        .stButton > button:hover, [data-testid="stDownloadButton"] button:hover {
            border-color: #22d3ee; box-shadow: 0 0 0 1px #22d3ee, 0 0 16px rgba(34,211,238,0.35);
            color: #22d3ee;
        }

        /* ---- dataframe / code / alerts ---- */
        [data-testid="stDataFrame"] { border-radius: 14px; overflow: hidden; border: 1px solid rgba(255,255,255,0.08); }
        div[data-testid="stCodeBlock"] pre { border-radius: 12px !important; font-family: 'JetBrains Mono', monospace !important; }
        [data-testid="stAlert"] { border-radius: 12px; backdrop-filter: blur(10px); }

        hr { border-color: rgba(255,255,255,0.08) !important; }

        /* ---- scrollbar ---- */
        ::-webkit-scrollbar { width: 10px; height: 10px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.14); border-radius: 8px; }
        ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.24); }
        </style>
        """

# NOTE: st.markdown treats any line with 4+ leading spaces as a literal
# markdown-indented code block, and a blank line can split an otherwise
# already-started raw-HTML block back into regular markdown parsing --
# so every line of injected CSS must start at column 0. The block above is
# written indented (to read cleanly as Python source); strip ALL leading
# whitespace from every line here rather than relying on the common-prefix
# behavior of textwrap.dedent(), since nested CSS rule bodies are indented
# relative to their selectors and would otherwise keep a few leading spaces.
_STYLE_BLOCK = "\n".join(line.lstrip() for line in _RAW_STYLE_BLOCK.splitlines())


def inject_css() -> None:
    st.markdown(_FONT_LINKS, unsafe_allow_html=True)
    st.markdown(_STYLE_BLOCK, unsafe_allow_html=True)


def metric_card(icon: str, label: str, value: str, color_key: str = "comfort", sub: str = "") -> str:
    accent = COLORS.get(color_key, COLORS["comfort"])
    sub_html = f'<div class="m-sub">{sub}</div>' if sub else ""
    return (
        f'<div class="glass-metric" style="--accent:{accent}">'
        f'<div class="m-icon">{icon}</div>'
        f'<div class="m-label">{label}</div>'
        f'<div class="m-value">{value}</div>'
        f"{sub_html}</div>"
    )


def render_metric_grid(cards: list[tuple[str, str, str, str, str]]) -> None:
    """cards: list of (icon, label, value, color_key, sub)."""
    html = '<div class="metric-grid">' + "".join(metric_card(*c) for c in cards) + "</div>"
    st.markdown(html, unsafe_allow_html=True)


def section_header(icon: str, title: str, color_key: str = "comfort", caption: str = "") -> None:
    accent = COLORS.get(color_key, COLORS["comfort"])
    st.markdown(
        f'<div class="section-title" style="--accent:{accent}"><span class="dot"></span>{icon} {title}</div>',
        unsafe_allow_html=True,
    )
    if caption:
        st.markdown(f'<div class="section-caption">{caption}</div>', unsafe_allow_html=True)


def themed_fig(fig: go.Figure, height: int = 360) -> go.Figure:
    """Consistent dark-glass Plotly styling applied to every chart in this app."""
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT_FAMILY, color="#c7cfe6", size=12),
        legend=dict(orientation="h", y=1.12, bgcolor="rgba(0,0,0,0)", font=dict(color="#c7cfe6")),
        hoverlabel=dict(bgcolor="#161d33", font=dict(family=FONT_FAMILY, color="#f0f2fb"), bordercolor="rgba(255,255,255,0.15)"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.1)", linecolor="rgba(255,255,255,0.12)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.1)", linecolor="rgba(255,255,255,0.12)"),
    )
    return fig


# --------------------------------------------------------------------------
# Data loading -- direct SQLite reads, no ORM/backend needed
# --------------------------------------------------------------------------
@st.cache_data(show_spinner="Loading data/building.db …")
def load_tables(db_path: str, db_mtime: float) -> dict[str, pd.DataFrame]:
    """db_mtime busts the cache automatically if the underlying file changes
    (e.g. a fresh run was logged) without needing a manual 'clear cache'."""
    con = sqlite3.connect(db_path)
    try:
        tables = {}
        for name in ("run_ticks", "decisions", "resilience_events", "pipeline_events"):
            tables[name] = pd.read_sql_query(f"SELECT * FROM {name}", con)
        return tables
    finally:
        con.close()


def _decided_by(row: pd.Series) -> str:
    """Human-readable attribution: who/what actually produced this tick's
    setpoint -- the real distinction the drivers JSON already carries
    (used_llm), not a guess."""
    if row["strategy"] == "ashrae_baseline":
        return "Fixed ASHRAE 90.1 schedule (no controls credit)"
    if row.get("used_llm"):
        issued = row.get("policy_issued_at_tick")
        age = row["tick"] - issued if issued is not None else None
        return f"LLM agent (cached policy from tick {int(issued)}, {int(age)} tick(s) old)" if issued is not None else "LLM agent"
    return "Deterministic reactive rule (no fresh LLM policy, or occupancy below the real-occupant threshold)"


def _lighting_decided_by(row: pd.Series) -> str:
    if row["strategy"] == "ashrae_baseline":
        return "Fixed schedule"
    if row.get("used_llm_lighting"):
        issued = row.get("lighting_policy_issued_at_tick")
        age = row["tick"] - issued if issued is not None else None
        return f"LLM agent (cached, {int(age)} tick(s) old)" if issued is not None else "LLM agent"
    return "Deterministic daylight/occupancy rule"


@st.cache_data(show_spinner=False)
def build_decisions_df(decisions_raw: pd.DataFrame) -> pd.DataFrame:
    df = decisions_raw.copy()
    drivers = df["drivers"].apply(lambda s: json.loads(s) if s else {})
    driver_cols = [
        "temp_c", "humidity_pct", "co2_ppm", "pmv", "ppd_pct", "daylight_lux",
        "occupancy", "outdoor_trend", "time_of_day", "confidence", "lighting_pct",
        "meeting_prep", "fault_fallback", "demand_response_active",
        "used_llm", "used_llm_lighting", "policy_issued_at_tick",
        "lighting_policy_issued_at_tick", "occupancy_scale_ratio",
        "heating_c", "cooling_c",
    ]
    for col in driver_cols:
        df[col] = drivers.apply(lambda d: d.get(col))
    df["decided_by"] = df.apply(_decided_by, axis=1)
    df["lighting_decided_by"] = df.apply(_lighting_decided_by, axis=1)
    return df


def summary_for(strategy: str, run_ticks: pd.DataFrame, decisions: pd.DataFrame,
                 resilience: pd.DataFrame) -> dict:
    """Reproduces backend/main.py's /api/summary formulas exactly, in pandas,
    so this standalone view stays numerically consistent with the live
    FastAPI-backed dashboard without needing it running."""
    ticks = run_ticks[run_ticks["strategy"] == strategy]
    if ticks.empty:
        return {}
    total_baseline = float(ticks["baseline_kwh"].sum())
    has_baseline = total_baseline > 0
    total_ai = float(ticks["ai_kwh"].sum())
    avg_comfort_dev = float(ticks["comfort_deviation_c"].mean())
    d = decisions[decisions["strategy"] == strategy]
    guardrail_count = int(d["guardrail_intervened"].sum())
    r = resilience[resilience["strategy"] == strategy]
    resilience_count = int((r["event_type"] == "sensor_dropout").sum())
    anomaly_count = int((r["event_type"] == "energy_anomaly").sum())
    total_carbon = float(ticks["carbon_kg"].sum())
    total_water = float(ticks["water_m3"].sum())
    total_pv = float(ticks["pv_kwh"].sum())
    renewable_fraction = total_pv / (total_ai + total_pv) if (total_ai + total_pv) > 0 else 0.0
    savings_pct = round(100 * (total_baseline - total_ai) / total_baseline, 2) if has_baseline else None

    comfort_score = max(0, 1 - avg_comfort_dev / 2) * 100
    renewable_score = renewable_fraction * 100
    components = [(0.3, renewable_score), (0.3, comfort_score)]
    if has_baseline:
        components.append((0.4, max(0, min(100, 50 + savings_pct * 5))))
    weight_sum = sum(w for w, _ in components)
    sustainability_score = round(sum(w * c for w, c in components) / weight_sum, 1)

    occupied = d[d["occupancy"].fillna(0) > 0]
    avg_pmv = round(occupied["pmv"].dropna().mean(), 2) if occupied["pmv"].notna().any() else None
    avg_ppd = round(occupied["ppd_pct"].dropna().mean(), 1) if occupied["ppd_pct"].notna().any() else None
    used_llm_pct = round(100 * d["used_llm"].fillna(False).mean(), 1) if strategy == "llm" and len(d) else None

    return {
        "strategy": strategy, "savings_pct": savings_pct, "has_baseline_comparison": has_baseline,
        "avg_comfort_deviation_c": round(avg_comfort_dev, 2), "guardrail_interventions": guardrail_count,
        "resilience_events": resilience_count, "energy_anomalies": anomaly_count,
        "total_carbon_kg": round(total_carbon, 2), "total_pv_kwh": round(total_pv, 2),
        "renewable_fraction_pct": round(renewable_fraction * 100, 1),
        "sustainability_score": sustainability_score,
        "demand_response_events": int(ticks["demand_response_active"].sum()),
        "peak_demand_kwh": round(float(ticks["ai_kwh"].max()), 3) if len(ticks) else 0,
        "avg_pmv_occupied": avg_pmv, "avg_ppd_pct_occupied": avg_ppd,
        "total_water_m3": round(total_water, 3),
        "total_kwh": round(total_ai, 2), "used_llm_pct": used_llm_pct,
        "n_ticks": int(ticks["tick"].nunique()),
    }


def aggregate_ticks(run_ticks: pd.DataFrame, strategy: str) -> pd.DataFrame:
    """One row per tick (RunTick is 5 rows/tick, one per zone) -- building-wide
    fields from the first row, worst-case (max) comfort deviation across zones,
    matching backend/main.py's _aggregate_ticks exactly."""
    t = run_ticks[run_ticks["strategy"] == strategy].copy()
    if t.empty:
        return t
    agg = t.groupby("tick").agg(
        baseline_kwh=("baseline_kwh", "first"), ai_kwh=("ai_kwh", "first"),
        comfort_deviation_c=("comfort_deviation_c", "max"),
        outdoor_temp_c=("outdoor_temp_c", "first"), hvac_kwh=("hvac_kwh", "first"),
        lighting_kwh=("lighting_kwh", "first"), plugload_kwh=("plugload_kwh", "first"),
        pv_kwh=("pv_kwh", "first"), carbon_kg=("carbon_kg", "first"),
        demand_response_active=("demand_response_active", "first"),
    ).reset_index().sort_values("tick")
    return agg


def tick_to_date_map(pipeline_events: pd.DataFrame, strategy: str) -> dict[int, dict]:
    ev = pipeline_events[(pipeline_events["strategy"] == strategy) & (pipeline_events["phase"] == "tick_start")]
    out = {}
    for _, row in ev.iterrows():
        try:
            detail = json.loads(row["detail"])
        except (TypeError, json.JSONDecodeError):
            continue
        sc = detail.get("sim_clock")
        if sc:
            out[int(row["tick"])] = sc
    return out


# --------------------------------------------------------------------------
inject_css()

# --------------------------------------------------------------------------
# Sidebar: data source
# --------------------------------------------------------------------------
st.sidebar.markdown(
    '<div style="font-family:\'Space Grotesk\',sans-serif;font-weight:700;font-size:1.1rem;'
    'background:linear-gradient(90deg,#67e8f9,#a78bfa);-webkit-background-clip:text;'
    'background-clip:text;color:transparent;margin-bottom:0.3rem;">🏢 Data source</div>',
    unsafe_allow_html=True,
)
uploaded = st.sidebar.file_uploader("Upload a building.db (optional)", type=["db"])
if uploaded is not None:
    tmp_path = os.path.join(tempfile.gettempdir(), "uploaded_building.db")
    with open(tmp_path, "wb") as f:
        f.write(uploaded.getbuffer())
    db_path = tmp_path
    st.sidebar.success(f"Using uploaded file ({uploaded.size / 1e6:.1f} MB)")
else:
    db_path = DEFAULT_DB_PATH
    if not os.path.exists(db_path):
        st.sidebar.error(f"No database found at {db_path} — upload a building.db file above.")
        st.stop()
    st.sidebar.caption(f"Reading `{os.path.relpath(db_path, os.path.dirname(__file__))}`")

tables = load_tables(db_path, os.path.getmtime(db_path))
run_ticks, decisions_raw, resilience, pipeline_events = (
    tables["run_ticks"], tables["decisions"], tables["resilience_events"], tables["pipeline_events"]
)

if run_ticks.empty:
    st.warning("This database has no logged runs yet.")
    st.stop()

available_strategies = sorted(run_ticks["strategy"].unique())
primary_default = "llm" if "llm" in available_strategies else available_strategies[0]
baseline_default = "ashrae_baseline" if "ashrae_baseline" in available_strategies else (
    available_strategies[0] if available_strategies[0] != primary_default else (
        available_strategies[1] if len(available_strategies) > 1 else available_strategies[0]
    )
)
st.sidebar.markdown("---")
primary_strategy = st.sidebar.selectbox(
    "AI strategy to feature", available_strategies,
    index=available_strategies.index(primary_default),
)
baseline_strategy = st.sidebar.selectbox(
    "Baseline to compare against", available_strategies,
    index=available_strategies.index(baseline_default) if baseline_default in available_strategies else 0,
)

decisions = build_decisions_df(decisions_raw)
summary = summary_for(primary_strategy, run_ticks, decisions, resilience)
baseline_summary = summary_for(baseline_strategy, run_ticks, decisions, resilience)
agg = aggregate_ticks(run_ticks, primary_strategy)
agg_baseline = aggregate_ticks(run_ticks, baseline_strategy)
clock_map = tick_to_date_map(pipeline_events, primary_strategy) or tick_to_date_map(pipeline_events, baseline_strategy)

# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
st.markdown(
    '<div class="topbar"><div class="brand"><span class="logo-dot">🏢</span>Smart Building Ops</div>'
    '<span class="live-badge"><span class="blip"></span>FINAL RUN LOGGED</span></div>',
    unsafe_allow_html=True,
)
st.markdown('<div class="hero-title">Autonomous Smart Building Optimization</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="hero-caption">Real EnergyPlus + multi-agent LLM pipeline over MCP — reading '
    '<code>data/building.db</code> directly, no backend server required</div>',
    unsafe_allow_html=True,
)
pills = [f"⚡ {primary_strategy} vs {baseline_strategy}"]
if clock_map:
    ticks_sorted = sorted(clock_map)
    first, last = clock_map[ticks_sorted[0]], clock_map[ticks_sorted[-1]]
    pills.append(f"📅 {first['month']}/{first['day']} → {last['month']}/{last['day']}")
    pills.append(f"⏱️ {len(ticks_sorted)} control ticks")
st.markdown(
    '<div class="pill-row">' + "".join(f'<span class="pill">{p}</span>' for p in pills) + "</div>",
    unsafe_allow_html=True,
)
st.markdown("<div style='height:0.9rem'></div>", unsafe_allow_html=True)

# --------------------------------------------------------------------------
# Headline metrics
# --------------------------------------------------------------------------
if not summary:
    st.warning(f"No data logged yet for strategy '{primary_strategy}'.")
    st.stop()

render_metric_grid([
    ("⚡", "Energy savings vs baseline",
     f"<span class='accent'>{summary['savings_pct']}%</span>" if summary["has_baseline_comparison"] else "—",
     "energy", "" if summary["has_baseline_comparison"] else "no baseline stitched"),
    ("🌡️", "Avg comfort deviation", f"{summary['avg_comfort_deviation_c']} °C", "comfort", ""),
    ("🛡️", "Guardrail interventions", str(summary["guardrail_interventions"]), "guardrail", "safety net exercised"),
    ("🩹", "Resilience events", str(summary["resilience_events"]), "carbon", "self-healed faults"),
    ("🧠", "LLM-driven decisions" if primary_strategy == "llm" else "Ticks logged",
     f"{summary['used_llm_pct']}%" if summary["used_llm_pct"] is not None else str(summary["n_ticks"]),
     "llm", ""),
])
render_metric_grid([
    ("🌍", "Sustainability score", f"{summary['sustainability_score']}<span style='font-size:0.95rem;color:#7d87a8'>/100</span>", "sustain", ""),
    ("☀️", "Renewable fraction (PV)", f"{summary['renewable_fraction_pct']}%", "energy", ""),
    ("🏭", "Carbon emissions", f"{summary['total_carbon_kg']}<span style='font-size:0.9rem;color:#7d87a8'> kg</span>", "carbon", "CO2e"),
    ("💧", "Water usage", f"{summary['total_water_m3']}<span style='font-size:0.9rem;color:#7d87a8'> m³</span>", "water", ""),
    ("😊", "Avg PMV (occupied)", str(summary["avg_pmv_occupied"]) if summary["avg_pmv_occupied"] is not None else "—", "comfort", "Fanger model"),
])

st.markdown("<div style='height:0.4rem'></div>", unsafe_allow_html=True)

# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
tab_overview, tab_compare, tab_twin, tab_decisions, tab_resilience, tab_raw = st.tabs(
    ["📈 Energy & Comfort", "⚖️ Strategy Comparison", "🏢 Digital Twin Replay",
     "🧠 Decision Log & Reasoning", "🛡️ Resilience & Anomalies", "📄 Raw Data Export"]
)

# ---- Energy & Comfort ------------------------------------------------------
with tab_overview:
    col1, col2 = st.columns(2)
    with col1:
        section_header("⚡", f"Energy: {primary_strategy} vs {baseline_strategy}", "energy")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=agg["tick"], y=agg["baseline_kwh"], name=baseline_strategy,
                                  line=dict(color=COLORS["baseline"], width=2)))
        fig.add_trace(go.Scatter(x=agg["tick"], y=agg["ai_kwh"], name=primary_strategy,
                                  line=dict(color=COLORS["energy"], width=2.5), fill="tozeroy",
                                  fillcolor="rgba(52,211,153,0.15)"))
        fig.update_layout(xaxis_title="tick", yaxis_title="kWh")
        st.plotly_chart(themed_fig(fig), width="stretch")
    with col2:
        section_header("🌡️", "Comfort deviation over time", "comfort")
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=agg["tick"], y=agg["comfort_deviation_c"], name=primary_strategy,
                                   line=dict(color=COLORS["comfort"], width=2.5), fill="tozeroy",
                                   fillcolor="rgba(56,189,248,0.18)"))
        fig2.add_trace(go.Scatter(x=agg_baseline["tick"], y=agg_baseline["comfort_deviation_c"],
                                   name=baseline_strategy, line=dict(color=COLORS["neutral"], width=1.5, dash="dot")))
        fig2.update_layout(xaxis_title="tick", yaxis_title="°C deviation")
        st.plotly_chart(themed_fig(fig2), width="stretch")

    col3, col4 = st.columns(2)
    with col3:
        section_header("🔋", "Energy breakdown", "carbon")
        breakdown = pd.DataFrame({
            "component": ["HVAC", "Lighting", "Plug loads", "Solar PV (offset)"],
            "kwh": [agg["hvac_kwh"].sum(), agg["lighting_kwh"].sum(), agg["plugload_kwh"].sum(),
                    -agg["pv_kwh"].sum()],
        })
        fig3 = go.Figure(go.Bar(
            x=breakdown["kwh"], y=breakdown["component"], orientation="h",
            marker=dict(color=[COLORS["carbon"], COLORS["comfort"], COLORS["sustain"], COLORS["energy"]],
                        line=dict(width=0)),
            text=breakdown["kwh"].round(1), textposition="outside",
        ))
        fig3.update_layout(xaxis_title="kWh", height=280)
        st.plotly_chart(themed_fig(fig3, height=280), width="stretch")
    with col4:
        section_header("🌤️", "Outdoor temperature vs energy draw", "llm")
        fig4 = go.Figure(go.Scatter(
            x=agg["outdoor_temp_c"], y=agg["ai_kwh"], mode="markers",
            marker=dict(color=agg["tick"], colorscale=[[0, "#38bdf8"], [0.5, "#a78bfa"], [1, "#f472b6"]],
                        size=7, line=dict(width=0), colorbar=dict(title="tick", tickfont=dict(color="#c7cfe6"))),
        ))
        fig4.update_layout(xaxis_title="outdoor °C", yaxis_title="ai_kwh", height=280)
        st.plotly_chart(themed_fig(fig4, height=280), width="stretch")

# ---- Strategy Comparison ---------------------------------------------------
with tab_compare:
    section_header("⚖️", "Head-to-head", "sustain")
    compare_df = pd.DataFrame([
        {"strategy": s, **{k: v for k, v in summary_for(s, run_ticks, decisions, resilience).items()}}
        for s in available_strategies
    ]).set_index("strategy")
    st.dataframe(
        compare_df[["total_kwh", "savings_pct", "avg_comfort_deviation_c", "guardrail_interventions",
                    "sustainability_score", "renewable_fraction_pct", "total_carbon_kg", "used_llm_pct"]],
        width="stretch",
    )

    section_header("📊", "Every strategy's energy curve", "energy")
    fig5 = go.Figure()
    palette = {"ashrae_baseline": COLORS["baseline"], "reactive": COLORS["neutral"],
               "predictive": COLORS["comfort"], "llm": COLORS["energy"]}
    for s in available_strategies:
        a = aggregate_ticks(run_ticks, s)
        fig5.add_trace(go.Scatter(x=a["tick"], y=a["ai_kwh"], name=s,
                                   line=dict(color=palette.get(s, COLORS["sustain"]), width=2)))
    fig5.update_layout(xaxis_title="tick", yaxis_title="kWh")
    st.plotly_chart(themed_fig(fig5, height=380), width="stretch")

    if primary_strategy == "llm":
        section_header("🧠", "How much of the run was genuinely LLM-driven", "llm")
        llm_d = decisions[decisions["strategy"] == "llm"]
        coverage_by_tick = llm_d.groupby("tick")["used_llm"].mean().reset_index()
        coverage_by_tick["used_llm_pct"] = coverage_by_tick["used_llm"] * 100
        fig6 = go.Figure(go.Bar(x=coverage_by_tick["tick"], y=coverage_by_tick["used_llm_pct"],
                                 marker=dict(color=COLORS["llm"], line=dict(width=0))))
        fig6.update_layout(xaxis_title="tick", yaxis_title="% zones on a fresh LLM policy", height=280)
        st.plotly_chart(themed_fig(fig6, height=280), width="stretch")
        st.caption(
            "The LLM strategy is event-driven, not called every tick (see ARCHITECTURE.md) — ticks below "
            "100% are correctly running the deterministic fallback rule, not a bug. This is the honest "
            "coverage number, not a claim of constant AI control."
        )

# ---- Digital Twin Replay ---------------------------------------------------
with tab_twin:
    section_header("🏢", f"Floorplan replay — {primary_strategy}", "sustain")
    twin_d = decisions[decisions["strategy"] == primary_strategy]
    ticks_available = sorted(twin_d["tick"].unique())
    if not ticks_available:
        st.info("No decisions logged for this strategy.")
    else:
        tick_sel = st.slider("Tick", min_value=int(min(ticks_available)), max_value=int(max(ticks_available)),
                              value=int(min(ticks_available)))
        if clock_map.get(tick_sel):
            sc = clock_map[tick_sel]
            st.caption(f"Simulated time: month {sc['month']}, day {sc['day']}, {sc['hour']:02d}:{sc['minute']:02d}")

        snapshot = twin_d[twin_d["tick"] == tick_sel].set_index("zone")
        col_plan, col_detail = st.columns([2, 1])
        with col_plan:
            with st.container(border=True):
                fig7 = go.Figure()
                for zone, box in ZONE_LAYOUT.items():
                    row = snapshot.loc[zone] if zone in snapshot.index else None
                    temp = row["temp_c"] if row is not None else None
                    occ = row["occupancy"] if row is not None else None
                    color = "#2a3150"
                    if temp is not None:
                        t_norm = max(0.0, min(1.0, (temp - 15) / (28 - 15)))
                        r = int(56 + (244 - 56) * t_norm)
                        g = int(189 + (114 - 189) * t_norm)
                        b = int(248 + (182 - 248) * t_norm)
                        color = f"rgb({r},{g},{b})"
                    fig7.add_shape(type="rect", x0=box["x0"], x1=box["x1"], y0=box["y0"], y1=box["y1"],
                                    fillcolor=color, line=dict(color="rgba(255,255,255,0.5)", width=1.5), opacity=0.92)
                    label = f"<b>{box['label']}</b><br>{temp:.1f}°C" if temp is not None else f"<b>{box['label']}</b>"
                    if occ is not None and occ > 0:
                        label += f"<br>{occ:.1f} occ"
                    fig7.add_annotation(x=(box["x0"] + box["x1"]) / 2, y=(box["y0"] + box["y1"]) / 2,
                                         text=label, showarrow=False, font=dict(color="white", size=13, family=FONT_FAMILY))
                fig7.update_xaxes(visible=False, range=[-0.2, 6.2])
                fig7.update_yaxes(visible=False, range=[-0.2, 5.2], scaleanchor="x")
                fig7.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10),
                                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
                st.plotly_chart(fig7, width="stretch")
        with col_detail:
            with st.container(border=True):
                zone_sel = st.selectbox("Inspect zone", ZONES)
                if zone_sel in snapshot.index:
                    z = snapshot.loc[zone_sel]
                    st.markdown(f"**{zone_sel}**")
                    st.write(f"Temperature: {z['temp_c']:.1f} °C" if pd.notna(z["temp_c"]) else "Temperature: —")
                    st.write(f"Occupancy: {z['occupancy']:.2f}" if pd.notna(z["occupancy"]) else "Occupancy: —")
                    st.write(f"Humidity: {z['humidity_pct']:.0f}%" if pd.notna(z["humidity_pct"]) else "Humidity: —")
                    st.write(f"CO2: {z['co2_ppm']:.0f} ppm" if pd.notna(z["co2_ppm"]) else "CO2: —")
                    st.write(f"PMV: {z['pmv']:.2f}" if pd.notna(z["pmv"]) else "PMV: —")
                    st.write(f"PPD: {z['ppd_pct']:.0f}%" if pd.notna(z["ppd_pct"]) else "PPD: —")
                    st.write(f"Daylight: {z['daylight_lux']:.0f} lux" if pd.notna(z["daylight_lux"]) else "Daylight: n/a (core zone)")
                    st.write(f"Setpoints: heat {z['heating_c']}° / cool {z['cooling_c']}°" if pd.notna(z["heating_c"]) else "")
                    st.write(f"Lighting: {z['lighting_pct']*100:.0f}%" if pd.notna(z["lighting_pct"]) else "")
                    st.write(f"Guardrail clipped: {'yes' if z['guardrail_intervened'] else 'no'}")
                    st.info(f"Decided by: {z['decided_by']}")
                    st.info(f"Lighting decided by: {z['lighting_decided_by']}")
                    if z.get("fault_fallback"):
                        st.error("Sensor fault — using neighbor-zone fallback this tick")
                else:
                    st.write("No data for this zone at this tick.")

# ---- Decision Log & Reasoning ----------------------------------------------
with tab_decisions:
    section_header("🧠", "Full per-tick, per-zone decision history", "llm",
                    "Every real setpoint/lighting decision the logged run made, who/what made it, and — "
                    "for LLM decisions — the actual specialist proposals and arbiter rationale, verbatim.")
    log_strategy = st.selectbox("Strategy", available_strategies,
                                 index=available_strategies.index(primary_strategy))
    log_d = decisions[decisions["strategy"] == log_strategy].sort_values(["tick", "zone"])

    fc1, fc2, fc3 = st.columns(3)
    zone_filter = fc1.multiselect("Zone", ZONES, default=[])
    only_llm = fc2.checkbox("Only genuinely LLM-driven decisions", value=False)
    only_clipped = fc3.checkbox("Only guardrail-clipped decisions", value=False)

    filtered = log_d
    if zone_filter:
        filtered = filtered[filtered["zone"].isin(zone_filter)]
    if only_llm:
        filtered = filtered[filtered["used_llm"] == True]  # noqa: E712
    if only_clipped:
        filtered = filtered[filtered["guardrail_intervened"] == True]  # noqa: E712

    st.dataframe(
        filtered[["tick", "zone", "decided_by", "heating_c", "cooling_c", "lighting_pct",
                  "occupancy", "pmv", "ppd_pct", "guardrail_intervened", "arbiter_decision"]],
        width="stretch", height=400,
    )
    st.caption(f"{len(filtered)} of {len(log_d)} logged decisions shown.")

    st.markdown("<div style='height:0.3rem'></div>", unsafe_allow_html=True)
    section_header("🔍", "Inspect one decision's full reasoning", "guardrail")
    if not filtered.empty:
        pick_tick = st.selectbox("Tick", sorted(filtered["tick"].unique()))
        zone_options = sorted(filtered[filtered["tick"] == pick_tick]["zone"].unique())
        pick_zone = st.selectbox("Zone", zone_options)
        row = filtered[(filtered["tick"] == pick_tick) & (filtered["zone"] == pick_zone)]
        if not row.empty:
            r = row.iloc[0]
            with st.container(border=True):
                st.markdown(f"**Tick {int(r['tick'])} · {r['zone']} · {r['decided_by']}**")
                colA, colB = st.columns(2)
                with colA:
                    st.markdown(f"`{r['temp_c']}°C → heat {r['heating_c']}° / cool {r['cooling_c']}°` · "
                                 f"lighting {r['lighting_pct']*100:.0f}%" if pd.notna(r["lighting_pct"]) else "")
                    st.markdown(f"Occupied: {r['occupancy']:.1f} people" if pd.notna(r["occupancy"]) else "Unoccupied")
                    if r["guardrail_intervened"]:
                        st.warning("⚠️ Guardrail intervened on this decision")
                with colB:
                    st.markdown(f"Confidence: {r['confidence']*100:.0f}%" if pd.notna(r["confidence"]) else "")
                    if r.get("fault_fallback"):
                        st.error("Sensor fault — neighbor-zone fallback")
                    if r.get("demand_response_active"):
                        st.info("Demand response active this tick")

                st.markdown("**⚡ Energy specialist proposal**")
                st.code(r["energy_proposal"] or "—", language="json")
                st.markdown("**🌡️ Comfort specialist proposal**")
                st.code(r["comfort_proposal"] or "—", language="json")
                st.markdown("**🌍 Carbon specialist proposal**")
                st.code(r["carbon_proposal"] or "—", language="json")
                st.markdown("**⚖️ Arbiter decision (final rationale)**")
                st.success(r["arbiter_decision"] or "—")
    else:
        st.info("No decisions match the current filters.")

# ---- Resilience & Anomalies -------------------------------------------------
with tab_resilience:
    section_header("🛡️", "Sensor faults & energy anomalies", "guardrail")
    res_strategy = st.selectbox("Strategy ", available_strategies,
                                 index=available_strategies.index(primary_strategy), key="res_strategy")
    r = resilience[resilience["strategy"] == res_strategy].sort_values("tick")
    if r.empty:
        st.info("No resilience events logged for this strategy.")
    else:
        render_metric_grid([
            ("📡", "Sensor-fault events", str(int((r["event_type"] == "sensor_dropout").sum())), "guardrail", "neighbor-zone fallback used"),
            ("📈", "Energy anomalies", str(int((r["event_type"] == "energy_anomaly").sum())), "carbon", "real z-score outliers"),
        ])
        st.dataframe(r[["tick", "zone", "event_type", "description", "fallback_used"]],
                     width="stretch", height=320)

# ---- Raw Data Export --------------------------------------------------------
with tab_raw:
    section_header("📄", "Download the complete underlying data", "sustain",
                    "Exactly what's in data/building.db for the selected strategy — for independent verification.")
    export_strategy = st.selectbox("Strategy  ", available_strategies,
                                    index=available_strategies.index(primary_strategy), key="export_strategy")
    exp_ticks = run_ticks[run_ticks["strategy"] == export_strategy]
    exp_decisions = decisions[decisions["strategy"] == export_strategy]
    exp_events = pipeline_events[pipeline_events["strategy"] == export_strategy]

    d1, d2, d3 = st.columns(3)
    d1.download_button("⬇ Download run_ticks.csv", exp_ticks.to_csv(index=False),
                        file_name=f"{export_strategy}_run_ticks.csv", mime="text/csv")
    d2.download_button("⬇ Download decisions.csv", exp_decisions.drop(columns=["drivers"]).to_csv(index=False),
                        file_name=f"{export_strategy}_decisions.csv", mime="text/csv")
    d3.download_button("⬇ Download pipeline_events.csv", exp_events.to_csv(index=False),
                        file_name=f"{export_strategy}_pipeline_events.csv", mime="text/csv")

    st.markdown("#### run_ticks (building-wide, per zone-tick)")
    st.dataframe(exp_ticks, width="stretch", height=300)
    st.markdown("#### decisions (per zone-tick, includes drivers)")
    st.dataframe(exp_decisions, width="stretch", height=300)
