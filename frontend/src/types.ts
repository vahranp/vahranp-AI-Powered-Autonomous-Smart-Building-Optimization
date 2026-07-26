export interface RunTick {
  tick: number;
  baseline_kwh: number;
  ai_kwh: number;
  comfort_deviation_c: number;
  outdoor_temp_c?: number;
  hvac_kwh?: number;
  lighting_kwh?: number;
  plugload_kwh?: number;
  pv_kwh?: number;
  carbon_kg?: number;
  demand_response_active?: boolean;
}

export interface DecisionDrivers {
  temp_c?: number;
  humidity_pct?: number;
  co2_ppm?: number;
  pmv?: number;
  ppd_pct?: number;
  daylight_lux?: number | null;
  zone_hvac_kwh?: number;
  occupancy?: number;
  outdoor_trend?: string;
  time_of_day?: string;
  strategy?: string;
  fault_fallback?: boolean;
  confidence?: number;
  lighting_pct?: number;
  meeting_prep?: boolean;
  demand_response_active?: boolean;
  used_llm?: boolean;
  used_llm_lighting?: boolean;
  // How stale a cached strategic policy is -- the "llm" strategy invokes
  // the LLM adaptively (event-driven), not every tick, so used_llm=true can
  // mean "issued this tick" or "issued 18 ticks ago and still cached";
  // this is what lets the dashboard show the difference honestly.
  policy_issued_at_tick?: number | null;
  // Same staleness field, tracked independently for the lighting cache --
  // an invocation can update the setpoint policy without touching lighting
  // (or vice versa), so this can legitimately differ from the field above.
  lighting_policy_issued_at_tick?: number | null;
  // Real-time occupancy-proportional scaling applied to the cached policy
  // this tick -- 1.0 means no scaling (occupancy is at/above what it was
  // when issued); lower means fewer people are here now and the applied
  // setpoint/lighting were pulled toward the cheaper fallback edge in
  // proportion, with zero additional LLM latency.
  occupancy_scale_ratio?: number | null;
  heating_c?: number;
  cooling_c?: number;
  guardrail_intervened?: boolean;
}

export interface Decision {
  tick: number;
  zone: string;
  energy_proposal: string;
  comfort_proposal: string;
  carbon_proposal: string;
  arbiter_decision: string;
  guardrail_intervened: boolean;
  drivers: DecisionDrivers;
}

export interface Summary {
  strategy: string;
  savings_pct: number | null;
  has_baseline_comparison: boolean;
  avg_comfort_deviation_c: number;
  guardrail_interventions: number;
  resilience_events: number;
  energy_anomalies: number;
  total_carbon_kg: number;
  total_pv_kwh: number;
  renewable_fraction_pct: number;
  sustainability_score: number;
  demand_response_events: number;
  peak_demand_kwh: number;
  avg_pmv_occupied: number | null;
  avg_ppd_pct_occupied: number | null;
  total_water_m3: number;
}

export interface StrategyStats {
  total_kwh: number;
  avg_comfort_deviation_c: number;
}

export interface LLMActiveWindow {
  window_ticks: number;
  genuinely_agent_driven_ticks: number;
  llm: StrategyStats;
  reactive_baseline: StrategyStats;
}

export type CompareResult = Partial<Record<"reactive" | "predictive" | "llm", StrategyStats>> & {
  llm_active_window?: LLMActiveWindow;
};

export interface ResilienceEvent {
  tick: number;
  zone: string;
  event_type: string;
  description: string;
  fallback_used: boolean;
}

export interface ZoneSnapshot {
  temp_c: number | null;
  occupancy: number | null;
  humidity_pct?: number | null;
  co2_ppm?: number | null;
  pmv?: number | null;
  ppd_pct?: number | null;
  daylight_lux?: number | null;
  lighting_pct?: number | null;
  confidence?: number | null;
  meeting_prep?: boolean;
  fault_fallback: boolean;
  guardrail_intervened: boolean;
}

export type ZoneTimeline = Record<string, Record<string, ZoneSnapshot>>; // tick -> zone -> snapshot

export interface EnergyBreakdown {
  hvac_kwh: number;
  lighting_kwh: number;
  plugload_kwh: number;
  pv_kwh: number;
  total_kwh: number;
}

export interface ZoneOccupancyPattern {
  utilization_pct: number;
  avg_occupancy_day: number;
  avg_occupancy_night: number;
}

export type OccupancyPattern = Record<string, ZoneOccupancyPattern>;

export interface MaintenanceInfo {
  estimated_hvac_runtime_hours: number;
  heavy_load_pct: number;
  service_recommended: boolean;
  note: string;
}

export interface WhatIfResult {
  outdoor_temp_delta_c: number;
  baseline_avg_kwh_per_tick: number;
  projected_avg_kwh_per_tick: number;
  projected_change_pct: number;
  method: string;
}

export type StrategyName = "ashrae_baseline" | "reactive" | "predictive" | "llm";

export interface RunStatus {
  status: "idle" | "running" | "completed" | "failed" | "stopped";
  strategy?: string; // StrategyName for a single-strategy run, or "comparison"/"comparison+llm" for a baseline-comparison run
  elapsed_s?: number;
  returncode?: number;
  data_epoch?: number; // bumped by DELETE /api/data -- the Operations Console resets its local event cache when this changes
}

export interface LiveZoneState {
  temp_c: number;
  humidity_pct: number;
  co2_ppm: number;
  pmv: number;
  ppd_pct: number;
  daylight_lux: number | null;
  occupancy: number;
  outdoor_trend: string;
  time_of_day: string;
  fault_fallback: boolean;
  confidence: number;
  lighting_pct: number;
  meeting_prep: boolean;
  demand_response_active: boolean;
  used_llm: boolean;
  used_llm_lighting: boolean;
  heating_c: number;
  cooling_c: number;
  guardrail_intervened: boolean;
}

export interface LiveState {
  stale: boolean;
  age_s?: number;
  tick?: number;
  strategy?: StrategyName;
  sim_clock?: { month: number; day: number; hour: number; minute: number };
  building_idf?: string;
  outdoor_temp_c?: number;
  ai_kwh?: number;
  hvac_kwh?: number;
  lighting_kwh?: number;
  plugload_kwh?: number;
  pv_kwh?: number;
  carbon_kg?: number;
  water_m3?: number;
  demand_response_active?: boolean;
  energy_anomaly?: boolean;
  zones?: Record<string, LiveZoneState>;
  active_zone?: string;
  wall_time?: number;
  real_seconds_per_sim_tick?: number | null;
  sim_ticks_per_min?: number | null;
  eplus_api_source?: Record<string, string>;
}

export type PipelinePhase =
  | "tick_start"
  | "mcp_resource_read"
  | "llm_specialist"
  | "mcp_tool_call"
  | "llm_pipeline_error"
  // Adaptive/event-driven strategic layer (see sim/energyplus_loop.py):
  // emitted only when a real trigger fires a background LLM invocation,
  // not every tick.
  | "llm_invocation_started"
  | "llm_policy_updated"
  | "actuator_applied"
  | "tick_complete";

export interface PipelineEventItem {
  id: number;
  tick: number;
  ts: number;
  phase: PipelinePhase;
  zone: string | null;
  detail: Record<string, unknown>;
}

export interface PipelineEventsResponse {
  strategy: StrategyName;
  events: PipelineEventItem[];
}

export interface LatestDecisionsResponse {
  strategy: string;
  zones: Record<string, Decision>;
}
