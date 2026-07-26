import type {
  CompareResult,
  Decision,
  EnergyBreakdown,
  LatestDecisionsResponse,
  LiveState,
  MaintenanceInfo,
  OccupancyPattern,
  PipelineEventsResponse,
  ResilienceEvent,
  RunStatus,
  RunTick,
  StrategyName,
  Summary,
  WhatIfResult,
  ZoneTimeline,
} from "./types";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`);
  return res.json() as Promise<T>;
}

export const fetchSummary = (strategy?: string) =>
  getJson<Summary>(`/api/summary${strategy ? `?strategy=${strategy}` : ""}`);
export const fetchRunLog = (strategy?: string) =>
  getJson<RunTick[]>(`/api/run-log${strategy ? `?strategy=${strategy}` : ""}`);
export const fetchDecisions = (strategy?: string) =>
  getJson<Decision[]>(`/api/decisions${strategy ? `?strategy=${strategy}` : ""}`);
export const fetchCompare = () => getJson<CompareResult>("/api/compare");
export const fetchResilienceEvents = (strategy?: string) =>
  getJson<ResilienceEvent[]>(`/api/resilience-events${strategy ? `?strategy=${strategy}` : ""}`);
export const fetchZoneTimeline = (strategy?: string) =>
  getJson<ZoneTimeline>(`/api/zone-timeline${strategy ? `?strategy=${strategy}` : ""}`);
export const fetchEnergyBreakdown = (strategy?: string) =>
  getJson<EnergyBreakdown>(`/api/energy-breakdown${strategy ? `?strategy=${strategy}` : ""}`);
export const fetchOccupancyPattern = (strategy?: string) =>
  getJson<OccupancyPattern>(`/api/occupancy-pattern${strategy ? `?strategy=${strategy}` : ""}`);
export const fetchMaintenance = (strategy?: string) =>
  getJson<MaintenanceInfo>(`/api/maintenance${strategy ? `?strategy=${strategy}` : ""}`);
export const fetchWhatIf = (deltaC: number) =>
  getJson<WhatIfResult>(`/api/whatif?outdoor_temp_delta_c=${encodeURIComponent(deltaC)}`);
export const fetchLLMDecisions = () => getJson<Decision[]>("/api/decisions?strategy=llm");

export async function startSimulation(
  strategy: StrategyName,
  clearExisting = true,
): Promise<{ status: string; strategy: string }> {
  const res = await fetch(
    `/api/run-simulation?strategy=${encodeURIComponent(strategy)}&clear_existing=${clearExisting}`,
    { method: "POST" },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `run-simulation failed: ${res.status}`);
  }
  return res.json();
}

export async function clearData(strategy: StrategyName | "all"): Promise<{ status: string; strategy: string }> {
  const res = await fetch(`/api/data?strategy=${encodeURIComponent(strategy)}`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `clear-data failed: ${res.status}`);
  }
  return res.json();
}

export const fetchRunStatus = () => getJson<RunStatus>("/api/run-status");
export const fetchRunLogTail = (lines = 20) => getJson<{ lines: string[] }>(`/api/run-log-tail?lines=${lines}`);
export const fetchLiveState = () => getJson<LiveState>("/api/live-state");
export const fetchPipelineEvents = (sinceId: number, strategy?: StrategyName, limit = 200) =>
  getJson<PipelineEventsResponse>(
    `/api/pipeline-events?since_id=${sinceId}${strategy ? `&strategy=${strategy}` : ""}&limit=${limit}`,
  );
export const fetchLatestDecisions = () => getJson<LatestDecisionsResponse>("/api/latest-decisions");

export async function runBaselineComparison(
  includeLlm: boolean,
  clearExisting = true,
): Promise<{ status: string; strategy: string }> {
  const res = await fetch(
    `/api/run-baseline-comparison?include_llm=${includeLlm}&clear_existing=${clearExisting}`,
    { method: "POST" },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `run-baseline-comparison failed: ${res.status}`);
  }
  return res.json();
}

export async function stopSimulation(): Promise<{ status: string }> {
  const res = await fetch("/api/stop-simulation", { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `stop-simulation failed: ${res.status}`);
  }
  return res.json();
}
