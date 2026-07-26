import { Bot, FlaskConical, GitBranch, MonitorCog, Radar, Wrench, Zap } from "lucide-react";

import type { RunTick } from "../../types";
import { useOperationsConsole } from "../../hooks/useOperationsConsole";
import SectionCard from "../SectionCard";
import AgentConsolePanel from "./AgentConsolePanel";
import ClosedLoopTimeline from "./ClosedLoopTimeline";
import DataFlowDiagram from "./DataFlowDiagram";
import LiveDashboardPanel from "./LiveDashboardPanel";
import McpToolExecutionPanel from "./McpToolExecutionPanel";
import RawEnergyPlusPanel from "./RawEnergyPlusPanel";
import SimulationMonitorPanel from "./SimulationMonitorPanel";

export default function OperationsConsole({
  chartData,
  savingsPct,
}: {
  chartData: RunTick[];
  savingsPct: number | null;
}) {
  const { liveState, events, runStatus, latestDecisions } = useOperationsConsole();
  const live = runStatus.status === "running" && !liveState.stale;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <SectionCard
          title="EnergyPlus Simulation Monitor"
          description="The real simulation engine behind every decision."
          icon={MonitorCog}
          className="ops-glass ops-panel-glow xl:col-span-2"
        >
          <SimulationMonitorPanel liveState={liveState} runStatus={runStatus} events={events} />
        </SectionCard>

        <SectionCard title="Closed-Loop Data Flow" description="EnergyPlus → Backend → LLM → MCP → Control → EnergyPlus → Dashboard" icon={GitBranch} className="ops-glass ops-panel-glow">
          <DataFlowDiagram events={events} live={live} />
        </SectionCard>
      </div>

      <SectionCard
        title="Raw EnergyPlus Data"
        description="The literal EnergyPlus Python API call behind every number — proof nothing here is fabricated."
        icon={FlaskConical}
        className="ops-glass ops-panel-glow"
      >
        <RawEnergyPlusPanel liveState={liveState} />
      </SectionCard>

      <SectionCard title="Live Building Dashboard" description="Instantaneous building state, polled every second from the real run." icon={Zap} className="ops-glass ops-panel-glow">
        <LiveDashboardPanel liveState={liveState} chartData={chartData} savingsPct={savingsPct} />
      </SectionCard>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard
          title="Decision & Reasoning"
          description="What changed, per zone, and why — real for every strategy, not just LLM."
          icon={Bot}
          className="ops-glass ops-panel-glow"
        >
          <AgentConsolePanel latestDecisions={latestDecisions} />
        </SectionCard>
        <SectionCard title="MCP Tool Execution" description="Every real MCP resource read and tool call, as it happens." icon={Wrench} className="ops-glass ops-panel-glow">
          <McpToolExecutionPanel events={events} />
        </SectionCard>
      </div>

      <SectionCard title="Closed-Loop Event Stream" description="Every real stage of the autonomous control loop, in order." icon={Radar} className="ops-glass ops-panel-glow">
        <ClosedLoopTimeline events={events} />
      </SectionCard>
    </div>
  );
}
