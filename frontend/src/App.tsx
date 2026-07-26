import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Bot,
  Building2,
  Cloud,
  Droplet,
  Gauge,
  Leaf,
  ShieldAlert,
  Sun,
  Thermometer,
  Zap,
} from "lucide-react";

import {
  fetchCompare,
  fetchDecisions,
  fetchEnergyBreakdown,
  fetchLLMDecisions,
  fetchMaintenance,
  fetchOccupancyPattern,
  fetchResilienceEvents,
  fetchRunLog,
  fetchRunStatus,
  fetchSummary,
  fetchZoneTimeline,
} from "./api";
import ComfortChart from "./components/ComfortChart";
import EnergyBreakdownChart from "./components/EnergyBreakdownChart";
import EnergyChart from "./components/EnergyChart";
import LLMAgentPanel from "./components/LLMAgentPanel";
import MaintenancePanel from "./components/MaintenancePanel";
import MetricCard from "./components/MetricCard";
import NegotiationFeed from "./components/NegotiationFeed";
import OccupancyPatternPanel from "./components/OccupancyPatternPanel";
import OperationsConsole from "./components/ops/OperationsConsole";
import ReplayScrubber from "./components/ReplayScrubber";
import ResiliencePanel from "./components/ResiliencePanel";
import RunSimulationPanel from "./components/RunSimulationPanel";
import SectionCard from "./components/SectionCard";
import StrategyCompare from "./components/StrategyCompare";
import { Badge } from "./components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
import WhatIfPanel from "./components/WhatIfPanel";
import { useLiveFeed } from "./hooks/useLiveFeed";
import type {
  CompareResult,
  Decision,
  EnergyBreakdown,
  MaintenanceInfo,
  OccupancyPattern,
  ResilienceEvent,
  RunTick,
  Summary,
  ZoneTimeline,
} from "./types";

export default function App() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [runLog, setRunLog] = useState<RunTick[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [compare, setCompare] = useState<CompareResult>({});
  const [resilienceEvents, setResilienceEvents] = useState<ResilienceEvent[]>([]);
  const [zoneTimeline, setZoneTimeline] = useState<ZoneTimeline>({});
  const [energyBreakdown, setEnergyBreakdown] = useState<EnergyBreakdown | null>(null);
  const [occupancyPattern, setOccupancyPattern] = useState<OccupancyPattern>({});
  const [maintenance, setMaintenance] = useState<MaintenanceInfo | null>(null);
  const [llmDecisions, setLlmDecisions] = useState<Decision[]>([]);
  const [baselineCarbonKg, setBaselineCarbonKg] = useState<number | null>(null);
  const { liveTicks, connected } = useLiveFeed();

  // Every panel below except the explicit multi-strategy ones (StrategyCompare,
  // the ASHRAE comparison tile) is pinned to the "llm" strategy explicitly,
  // rather than letting the backend's _current_live_strategy/_headline_strategy
  // heuristic guess which run to show. That heuristic is well-behaved for the
  // common path (ASHRAE baseline run once, then LLM run after) but has a real
  // fallback edge: if the "llm" run has no stitched baseline yet and no
  // "predictive" data exists, it silently falls through to whatever the very
  // FIRST RunTick row in the whole table happens to be (verified: that's
  // "ashrae_baseline" in this DB) -- with zero indication in the UI that it
  // happened. Since these tabs (Digital Twin, Decision feed, Resilience,
  // Analytics) exist specifically to show the LLM agent's behavior, pinning
  // removes that ambiguity entirely instead of trusting a best-guess fallback.
  const LLM_STRATEGY = "llm";

  const refreshAll = useCallback(() => {
    fetchSummary(LLM_STRATEGY).then(setSummary).catch(console.error);
    fetchRunLog(LLM_STRATEGY).then(setRunLog).catch(console.error);
    fetchDecisions(LLM_STRATEGY).then(setDecisions).catch(console.error);
    fetchCompare().then(setCompare).catch(console.error);
    fetchResilienceEvents(LLM_STRATEGY).then(setResilienceEvents).catch(console.error);
    fetchZoneTimeline(LLM_STRATEGY).then(setZoneTimeline).catch(console.error);
    fetchEnergyBreakdown(LLM_STRATEGY).then(setEnergyBreakdown).catch(console.error);
    fetchOccupancyPattern(LLM_STRATEGY).then(setOccupancyPattern).catch(console.error);
    fetchMaintenance(LLM_STRATEGY).then(setMaintenance).catch(console.error);
    fetchLLMDecisions().then(setLlmDecisions).catch(console.error);
    // The raw carbon number alone doesn't tell judges whether it's good --
    // the ASHRAE baseline run (if one exists) is the real reference point,
    // same as the energy-savings tile already compares against.
    fetchSummary("ashrae_baseline")
      .then((s) => setBaselineCarbonKg(s.total_carbon_kg > 0 ? s.total_carbon_kg : null))
      .catch(() => setBaselineCarbonKg(null));
  }, []);

  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  // The "Live Operations" tab and the two Overview charts already update
  // live (1s polling / WebSocket) -- but refreshAll() itself previously
  // only ran once on mount and once when a run finished, so every other
  // tab (Strategy Compare, AI Agents, Analytics, Resilience, Digital Twin)
  // stayed frozen at page-load data for the entire duration of a run.
  // Poll run-status and re-run refreshAll() periodically while something
  // is actually running so those tabs stay reasonably current too --
  // every 8s rather than every 1s, since these endpoints return full
  // history (thousands of rows on a long run), not a single latest value.
  useEffect(() => {
    const id = setInterval(async () => {
      const status = await fetchRunStatus().catch(() => null);
      if (status?.status === "running") refreshAll();
    }, 8000);
    return () => clearInterval(id);
  }, [refreshAll]);

  const chartData = useMemo(
    () => (liveTicks.length > 0 ? liveTicks : runLog),
    [liveTicks, runLog],
  );

  return (
    <div className="min-h-svh bg-background text-foreground">
      <div className="mx-auto max-w-[1400px] px-6 py-8 md:px-10">
        <header className="mb-8 flex flex-wrap items-center justify-between gap-4 border-b border-border pb-6">
          <div className="flex items-center gap-4">
            <div className="flex size-12 items-center justify-center rounded-lg bg-primary/12 text-primary ring-1 ring-primary/20">
              <Building2 className="size-6" />
            </div>
            <div>
              <h1 className="font-heading text-2xl font-bold tracking-tight sm:text-[1.75rem]">
                Autonomous Smart Building Optimization
              </h1>
              <p className="mt-1 text-sm text-muted-foreground">
                EnergyPlus &middot; multi-agent LLM over MCP &middot; solar PV &middot; daylight harvesting &middot; guardrails
                {summary && (
                  <>
                    {" "}
                    &middot; headline strategy: <span className="text-foreground/80">{summary.strategy}</span>
                  </>
                )}
              </p>
            </div>
          </div>
          <Badge variant={connected ? "success" : "outline"} className="gap-1.5 py-1.5">
            <span className={`size-1.5 rounded-full ${connected ? "bg-success ops-live-dot" : "bg-muted-foreground"}`} />
            {connected ? "Live" : "Disconnected"}
          </Badge>
        </header>

        <SectionCard title="Run a real simulation" className="mb-8">
          <RunSimulationPanel onComplete={refreshAll} />
        </SectionCard>

        <div className="mb-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <MetricCard
            icon={Zap}
            label="Energy savings vs baseline"
            value={!summary ? "…" : summary.has_baseline_comparison ? `${summary.savings_pct}%` : "no baseline run yet"}
            tone="emerald"
            variant="filled"
          />
          <MetricCard icon={Thermometer} label="Avg comfort deviation" value={summary ? `${summary.avg_comfort_deviation_c} °C` : "…"} tone="sky" />
          <MetricCard icon={ShieldAlert} label="Guardrail interventions" value={summary ? `${summary.guardrail_interventions}` : "…"} tone="amber" />
          <MetricCard icon={AlertTriangle} label="Resilience events handled" value={summary ? `${summary.resilience_events}` : "…"} tone="red" />
        </div>

        <div className="mb-4 grid grid-cols-2 gap-4 sm:grid-cols-2">
          <MetricCard icon={Gauge} label="Avg PMV when occupied (Fanger)" value={summary?.avg_pmv_occupied != null ? summary.avg_pmv_occupied.toFixed(2) : "…"} tone="sky" />
          <MetricCard icon={Activity} label="Avg PPD when occupied" value={summary?.avg_ppd_pct_occupied != null ? `${summary.avg_ppd_pct_occupied}% dissatisfied` : "…"} tone="amber" />
        </div>

        <div className="mb-8 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
          <MetricCard icon={Leaf} label="Sustainability score" value={summary ? `${summary.sustainability_score}/100` : "…"} tone="emerald" />
          <MetricCard icon={Sun} label="Renewable energy (solar PV)" value={summary ? `${summary.renewable_fraction_pct}%` : "…"} tone="sky" />
          <MetricCard
            icon={Cloud}
            label="Carbon emissions (est.)"
            value={summary ? `${summary.total_carbon_kg} kg CO2e` : "…"}
            sublabel={
              summary && baselineCarbonKg
                ? `vs ${baselineCarbonKg.toFixed(1)} kg ASHRAE baseline (${(
                    (100 * (baselineCarbonKg - summary.total_carbon_kg)) / baselineCarbonKg
                  ).toFixed(1)}% ${summary.total_carbon_kg <= baselineCarbonKg ? "lower" : "higher"})`
                : "run an ASHRAE baseline to compare"
            }
            tone="amber"
          />
          <MetricCard icon={Zap} label="Demand response events" value={summary ? `${summary.demand_response_events}` : "…"} tone="red" />
          <MetricCard icon={Droplet} label="Water usage (domestic hot water)" value={summary ? `${summary.total_water_m3} m³` : "…"} tone="sky" />
        </div>

        <Tabs defaultValue="operations">
          <TabsList>
            <TabsTrigger value="operations">Live Operations</TabsTrigger>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="twin">Digital Twin</TabsTrigger>
            <TabsTrigger value="agents">AI Agents</TabsTrigger>
            <TabsTrigger value="analytics">Analytics</TabsTrigger>
            <TabsTrigger value="resilience">Resilience</TabsTrigger>
          </TabsList>

          <TabsContent value="operations">
            <OperationsConsole
              chartData={chartData}
              savingsPct={summary?.savings_pct ?? null}
            />
          </TabsContent>

          <TabsContent value="overview" className="space-y-6">
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <SectionCard title="Energy: AI-controlled vs baseline">
                <EnergyChart data={chartData} />
              </SectionCard>
              <SectionCard title="Comfort deviation over time">
                <ComfortChart data={chartData} />
              </SectionCard>
            </div>
            <SectionCard title="Reactive vs predictive vs LLM strategy">
              <StrategyCompare compare={compare} />
            </SectionCard>
          </TabsContent>

          <TabsContent value="twin">
            <SectionCard
              title="Digital twin — replay the LLM agent run"
              description="Always the LLM strategy's logged run, not whichever strategy last happened to run. Hover a zone for temperature, humidity, CO2, PMV/PPD, daylight, lighting, and AI confidence."
            >
              <ReplayScrubber timeline={zoneTimeline} />
            </SectionCard>
          </TabsContent>

          <TabsContent value="agents" className="space-y-6">
            <SectionCard title="Multi-agent LLM pipeline" description="Real MCP tool calls, not simulated in-process." icon={Bot}>
              <LLMAgentPanel decisions={llmDecisions} />
            </SectionCard>
            <SectionCard title="Decision feed" description="Explainable AI: every setpoint change with its rationale (LLM strategy).">
              <NegotiationFeed decisions={decisions} />
            </SectionCard>
          </TabsContent>

          <TabsContent value="analytics" className="space-y-6">
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
              <SectionCard title="Energy breakdown" description="HVAC / lighting / plug loads / solar (LLM strategy)">
                <EnergyBreakdownChart breakdown={energyBreakdown} />
              </SectionCard>
              <SectionCard title="Space utilization" description="Occupancy pattern by zone (LLM strategy)">
                <OccupancyPatternPanel pattern={occupancyPattern} />
              </SectionCard>
              <SectionCard title="Predictive maintenance" description="Runtime heuristic, not real telemetry">
                <MaintenancePanel info={maintenance} />
              </SectionCard>
            </div>
            <SectionCard title="What-if simulation">
              <WhatIfPanel />
            </SectionCard>
          </TabsContent>

          <TabsContent value="resilience">
            <SectionCard title="Sensor faults &amp; energy anomalies" description="Self-healing fallback and statistical outlier detection (LLM strategy).">
              <ResiliencePanel events={resilienceEvents} />
            </SectionCard>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
