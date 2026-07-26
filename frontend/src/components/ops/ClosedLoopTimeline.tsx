import { useEffect, useMemo, useRef } from "react";
import {
  Activity,
  AlertTriangle,
  Brain,
  CheckCircle2,
  Database,
  Sparkles,
  SlidersHorizontal,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";

import type { PipelineEventItem, PipelinePhase } from "../../types";
import { ScrollArea } from "../ui/scroll-area";
import { formatClockTime } from "./format";

const PHASE_META: Record<PipelinePhase, { icon: LucideIcon; label: string; tone: string; dot: string }> = {
  tick_start: { icon: Activity, label: "EnergyPlus — simulation step", tone: "text-info", dot: "bg-info" },
  mcp_resource_read: { icon: Database, label: "MCP resource read", tone: "text-info", dot: "bg-info" },
  llm_specialist: { icon: Brain, label: "LLM specialist reasoning", tone: "text-[var(--chart-4)]", dot: "bg-[var(--chart-4)]" },
  mcp_tool_call: { icon: Wrench, label: "MCP tool call — control action", tone: "text-success", dot: "bg-success" },
  llm_pipeline_error: { icon: AlertTriangle, label: "LLM pipeline failed — reactive fallback", tone: "text-destructive", dot: "bg-destructive" },
  // Adaptive strategic layer: these two only appear when a real trigger
  // fires a background LLM invocation, not every tick -- see
  // sim/energyplus_loop.py's _evaluate_llm_trigger.
  llm_invocation_started: { icon: Zap, label: "Strategic LLM invocation triggered", tone: "text-[var(--chart-4)]", dot: "bg-[var(--chart-4)]" },
  llm_policy_updated: { icon: Sparkles, label: "Strategic policy updated", tone: "text-success", dot: "bg-success" },
  actuator_applied: { icon: SlidersHorizontal, label: "EnergyPlus actuators updated", tone: "text-warning", dot: "bg-warning" },
  tick_complete: { icon: CheckCircle2, label: "Tick complete — dashboard synced", tone: "text-success", dot: "bg-success" },
};

function describe(e: PipelineEventItem): string {
  switch (e.phase) {
    case "tick_start":
      return `Tick ${e.tick}`;
    case "mcp_resource_read": {
      const d = e.detail as { uri?: string };
      return d.uri ?? "";
    }
    case "llm_specialist": {
      const d = e.detail as { agent?: string };
      return `${d.agent ?? ""} agent`;
    }
    case "mcp_tool_call": {
      const d = e.detail as { tool?: string; args?: { zone?: string } };
      return `${d.tool ?? ""}(${d.args?.zone ?? ""})`;
    }
    case "llm_pipeline_error": {
      const d = e.detail as { error_type?: string; error?: string };
      return `${d.error_type ?? "Error"}: ${d.error ?? ""}`;
    }
    case "llm_invocation_started": {
      const d = e.detail as { trigger?: string; invocation_number?: number };
      return `trigger: ${d.trigger ?? "?"}${d.invocation_number ? ` (invocation #${d.invocation_number})` : ""}`;
    }
    case "llm_policy_updated": {
      const d = e.detail as { trigger?: string; zones_updated?: string[] };
      return `trigger: ${d.trigger ?? "?"}, ${d.zones_updated?.length ?? 0} zone(s) updated`;
    }
    case "actuator_applied": {
      // The backend already tags each zone with used_llm here -- this event
      // fires every tick regardless of whether a fresh/cached LLM policy or
      // the reactive rule actually drove it, and previously always said
      // "all zones" either way, which read as generic/rule-based even on a
      // tick the Decision panel correctly marked LLM. Surfacing the real
      // split closes that gap instead of the two panels silently disagreeing.
      const d = e.detail as {
        zones?: Record<string, { used_llm?: boolean }>;
        guardrail_interventions?: number;
      };
      const zoneStates = d.zones ? Object.values(d.zones) : [];
      const llmCount = zoneStates.filter((z) => z.used_llm).length;
      const total = zoneStates.length;
      const driverText =
        total === 0 ? "all zones" : llmCount === 0 ? `${total}/${total} via reactive rule` : `${llmCount}/${total} via LLM policy`;
      return d.guardrail_interventions ? `${driverText} · ${d.guardrail_interventions} guardrail clip(s)` : driverText;
    }
    case "tick_complete": {
      const d = e.detail as { active_zone?: string };
      return d.active_zone ? `active zone: ${d.active_zone}` : "";
    }
    default:
      return "";
  }
}

export default function ClosedLoopTimeline({ events }: { events: PipelineEventItem[] }) {
  const shown = useMemo(() => events.slice(-80), [events]);
  const scrollRootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const viewport = scrollRootRef.current?.querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]');
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  }, [shown.length]);

  if (shown.length === 0) {
    return <p className="text-sm text-muted-foreground">No closed-loop activity yet — run a simulation to watch it stream here.</p>;
  }

  return (
    <ScrollArea ref={scrollRootRef} className="h-[26rem] pr-3">
      <div className="relative space-y-0.5 border-l border-border pl-4">
        {shown.map((e) => {
          const meta = PHASE_META[e.phase];
          const Icon = meta.icon;
          return (
            <div key={e.id} className="ops-event-enter relative py-1.5">
              <span className={`absolute -left-[21px] top-2.5 size-2 rounded-full ${meta.dot}`} />
              <div className="flex items-baseline gap-2 text-xs">
                <Icon className={`size-3.5 shrink-0 ${meta.tone}`} />
                <span className={`font-medium ${meta.tone}`}>{meta.label}</span>
                <span className="text-muted-foreground/70">{describe(e)}</span>
                <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground/60">{formatClockTime(e.ts)}</span>
              </div>
            </div>
          );
        })}
      </div>
    </ScrollArea>
  );
}
