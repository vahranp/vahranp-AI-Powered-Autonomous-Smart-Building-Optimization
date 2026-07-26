import { useEffect, useMemo, useRef, useState } from "react";
import { Building2, Gauge, Radio, TerminalSquare } from "lucide-react";

import { fetchRunLogTail } from "../../api";
import type { LiveState, PipelineEventItem, RunStatus } from "../../types";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { ScrollArea } from "../ui/scroll-area";
import { formatClockTime, formatSimClock, timeAgo } from "./format";

const STATUS_LABEL: Record<RunStatus["status"], string> = {
  idle: "Idle",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  stopped: "Stopped",
};

function logLineFor(e: PipelineEventItem): string {
  const t = formatClockTime(e.ts);
  switch (e.phase) {
    case "tick_start": {
      const d = e.detail as { sim_clock?: { month: number; day: number; hour: number; minute: number }; outdoor_temp_c?: number };
      return `${t}  EnergyPlus tick ${e.tick} — ${formatSimClock(d.sim_clock)}, outdoor ${d.outdoor_temp_c ?? "—"}°C`;
    }
    case "mcp_resource_read": {
      const d = e.detail as { uri?: string };
      return `${t}  MCP resource read: ${d.uri ?? "?"}`;
    }
    case "llm_specialist": {
      const d = e.detail as { agent?: string; proposal_count?: number };
      return `${t}  ${d.agent ?? "specialist"} agent proposed ${d.proposal_count ?? 0} zone setpoint(s)`;
    }
    case "mcp_tool_call": {
      const d = e.detail as { tool?: string; args?: { zone?: string } };
      return `${t}  MCP tool call: ${d.tool ?? "?"}(${d.args?.zone ?? ""})`;
    }
    case "llm_invocation_started": {
      const d = e.detail as { trigger?: string; invocation_number?: number };
      return `${t}  Strategic LLM invocation #${d.invocation_number ?? "?"} triggered (${d.trigger ?? "?"})`;
    }
    case "llm_policy_updated": {
      const d = e.detail as { trigger?: string; zones_updated?: string[] };
      return `${t}  Strategic policy updated (${d.trigger ?? "?"}) — ${d.zones_updated?.length ?? 0} zone(s)`;
    }
    case "actuator_applied": {
      const d = e.detail as {
        zones?: Record<string, { used_llm?: boolean }>;
        guardrail_interventions?: number;
      };
      const zoneStates = d.zones ? Object.values(d.zones) : [];
      const llmCount = zoneStates.filter((z) => z.used_llm).length;
      const total = zoneStates.length;
      const driverText = total === 0 ? "" : llmCount === 0 ? ` (${total}/${total} reactive rule)` : ` (${llmCount}/${total} LLM policy)`;
      return `${t}  EnergyPlus actuators updated${driverText} — ${d.guardrail_interventions ?? 0} guardrail clips`;
    }
    case "tick_complete":
      return `${t}  Tick ${e.tick} complete — dashboard synced`;
    default:
      return `${t}  ${e.phase}`;
  }
}

export default function SimulationMonitorPanel({
  liveState,
  runStatus,
  events,
}: {
  liveState: LiveState;
  runStatus: RunStatus;
  events: PipelineEventItem[];
}) {
  const isRunning = runStatus.status === "running";
  const zones = liveState.zones ?? {};
  const zoneEntries = Object.entries(zones);

  const avgPmv = useMemo(() => {
    const occupied = zoneEntries.filter(([, z]) => z.occupancy > 0);
    const pool = occupied.length > 0 ? occupied : zoneEntries;
    if (pool.length === 0) return null;
    return pool.reduce((sum, [, z]) => sum + z.pmv, 0) / pool.length;
  }, [zoneEntries]);

  const totalOccupancy = zoneEntries.reduce((sum, [, z]) => sum + z.occupancy, 0);

  const logLines = useMemo(
    () => events.slice(-60).map((e) => ({ id: e.id, text: logLineFor(e) })),
    [events],
  );
  const scrollRootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const viewport = scrollRootRef.current?.querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]');
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  }, [logLines.length]);

  // Raw EnergyPlus process stdout (warmup, timestep, and error/warning
  // lines EnergyPlus itself prints) -- direct evidence the engine is really
  // running, not just the derived pipeline log above.
  const [logView, setLogView] = useState<"pipeline" | "raw">("pipeline");
  const [rawLines, setRawLines] = useState<string[]>([]);
  const rawScrollRootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (logView !== "raw") return;
    let cancelled = false;
    const poll = () => {
      fetchRunLogTail(40)
        .then((r) => {
          if (!cancelled) setRawLines(r.lines);
        })
        .catch(() => {});
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [logView]);
  useEffect(() => {
    const viewport = rawScrollRootRef.current?.querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]');
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  }, [rawLines.length]);

  const live = isRunning && !liveState.stale;
  // A "Run baseline comparison" job marches through reactive -> predictive ->
  // llm as separate real subprocesses under one run-status label ("comparison"
  // / "comparison+llm"), so that label alone never tells you which of the
  // three is actually executing right now. liveState.strategy is written by
  // whichever subprocess is currently ticking, so it's the one field that's
  // always accurate -- show it plainly rather than leaving that ambiguous.
  const isComparisonJob = (runStatus.strategy ?? "").startsWith("comparison");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Building2 className="size-4 text-muted-foreground" />
          <span className="font-mono text-sm">{liveState.building_idf ?? "CURRENT_modified_RefBldgSmallOffice.idf"}</span>
        </div>
        <div className="flex items-center gap-1.5">
          {liveState.strategy && (
            <Badge variant="outline" className="py-1.5">
              {isComparisonJob ? "currently running: " : "strategy: "}
              {liveState.strategy}
            </Badge>
          )}
          <Badge variant={live ? "success" : isRunning ? "warning" : "outline"} className="gap-1.5 py-1.5">
            {live && <span className="ops-live-dot size-1.5 rounded-full bg-success" />}
            {STATUS_LABEL[runStatus.status]}
            {runStatus.elapsed_s != null && ` · ${runStatus.elapsed_s.toFixed(0)}s`}
          </Badge>
        </div>
      </div>
      {isComparisonJob && (
        <p className="text-[11px] text-muted-foreground">
          Baseline comparison job — runs reactive → predictive → {runStatus.strategy?.includes("llm") ? "llm" : "(llm skipped)"} in
          sequence. Every panel below reflects whichever of those is actually executing right now.
        </p>
      )}

      <div className="grid grid-cols-2 gap-x-4 gap-y-2.5 rounded-lg border border-border bg-secondary/30 p-3 text-xs sm:grid-cols-3">
        <Field label="Sim time" value={formatSimClock(liveState.sim_clock)} />
        <Field label="Timestep / tick" value={liveState.tick != null ? `#${liveState.tick}` : "—"} />
        <Field label="Active zone" value={liveState.active_zone ?? "—"} />
        <Field label="Sim speed" value={liveState.sim_ticks_per_min ? `${liveState.sim_ticks_per_min.toFixed(0)} ticks/min` : "—"} />
        <Field label="Last updated" value={timeAgo(liveState.age_s)} />
        <Field label="Outdoor temp" value={liveState.outdoor_temp_c != null ? `${liveState.outdoor_temp_c.toFixed(1)}°C` : "—"} />
        <Field label="Energy now" value={liveState.ai_kwh != null ? `${liveState.ai_kwh.toFixed(2)} kWh` : "—"} />
        <Field label="Avg PMV" value={avgPmv != null ? avgPmv.toFixed(2) : "—"} />
        <Field label="Occupancy" value={zoneEntries.length > 0 ? `${totalOccupancy.toFixed(0)} people` : "—"} />
      </div>

      {zoneEntries.length > 0 && (
        <div>
          <p className="mb-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <Gauge className="size-3.5" /> HVAC setpoints
          </p>
          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {zoneEntries.map(([zone, z]) => (
              <div
                key={zone}
                className={`flex items-center justify-between rounded-md border border-border px-2.5 py-1.5 text-xs ${
                  zone === liveState.active_zone ? "bg-accent" : "bg-secondary/20"
                }`}
              >
                <span className="truncate font-mono text-[11px]">{zone}</span>
                <span className="tabular-nums text-muted-foreground">
                  {z.heating_c.toFixed(1)}° / {z.cooling_c.toFixed(1)}°C
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <div className="mb-1.5 flex items-center justify-between">
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            {logView === "pipeline" ? <Radio className="size-3.5" /> : <TerminalSquare className="size-3.5" />}
            {logView === "pipeline" ? "Simulation log" : "EnergyPlus console (raw process output)"}
          </p>
          <div className="flex gap-1">
            <Button size="sm" variant={logView === "pipeline" ? "secondary" : "ghost"} className="h-6 px-2 text-[11px]" onClick={() => setLogView("pipeline")}>
              Pipeline
            </Button>
            <Button size="sm" variant={logView === "raw" ? "secondary" : "ghost"} className="h-6 px-2 text-[11px]" onClick={() => setLogView("raw")}>
              Raw EnergyPlus
            </Button>
          </div>
        </div>
        {logView === "pipeline" ? (
          <ScrollArea ref={scrollRootRef} className="h-40 rounded-md border border-border bg-black/30">
            <div className="p-2.5 font-mono text-[11px] leading-relaxed text-muted-foreground">
              {logLines.length === 0 ? (
                <p>waiting for simulation output…</p>
              ) : (
                logLines.map((l) => (
                  <div key={l.id} className="ops-event-enter whitespace-nowrap">
                    {l.text}
                  </div>
                ))
              )}
              {isRunning && <span className="ops-caret">▍</span>}
            </div>
          </ScrollArea>
        ) : (
          <ScrollArea ref={rawScrollRootRef} className="h-40 rounded-md border border-border bg-black/30">
            <div className="p-2.5 font-mono text-[11px] leading-relaxed text-muted-foreground">
              {rawLines.length === 0 ? (
                <p>waiting for EnergyPlus process output…</p>
              ) : (
                rawLines.map((line, i) => (
                  <div key={i} className="whitespace-pre-wrap">
                    {line}
                  </div>
                ))
              )}
              {isRunning && <span className="ops-caret">▍</span>}
            </div>
          </ScrollArea>
        )}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground/70">{label}</p>
      <p className="font-mono tabular-nums">{value}</p>
    </div>
  );
}
