import { useState } from "react";
import { ChevronDown, Lightbulb, Zap } from "lucide-react";

import type { Decision } from "../types";
import { Badge } from "./ui/badge";
import { cn } from "@/lib/utils";

function ProposalBlock({ label, json, color }: { label: string; json: string; color: string }) {
  let pretty = json;
  try {
    pretty = JSON.stringify(JSON.parse(json), null, 2);
  } catch {
    /* raw text fallback, e.g. no parseable tool call */
  }
  return (
    <div>
      <p className={cn("text-xs font-medium", color)}>{label}</p>
      <pre className="mt-1 max-h-40 overflow-auto rounded-md border border-border bg-black/30 p-2 font-mono text-[11px] text-muted-foreground">
        {pretty}
      </pre>
    </div>
  );
}

export default function LLMAgentPanel({ decisions }: { decisions: Decision[] }) {
  const [openTick, setOpenTick] = useState<number | null>(null);

  const llmDriven = decisions.filter((d) => d.drivers.used_llm);
  const fallback = decisions.filter((d) => !d.drivers.used_llm);
  const lightingDriven = decisions.filter((d) => d.drivers.used_llm_lighting).length;

  if (decisions.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No "llm" strategy run logged yet. Use the "Run simulation" panel above (select "LLM agents") to exercise the
        real multi-agent MCP pipeline.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="success" className="gap-1.5">
          <Zap />
          {llmDriven.length} agent-driven (HVAC)
        </Badge>
        <Badge variant="info" className="gap-1.5">
          <Lightbulb />
          {lightingDriven} agent-driven (lighting)
        </Badge>
        <Badge variant="outline">{fallback.length} reactive-rule fallback</Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        Fallback ticks are ones with no fresh enough cached strategic policy for that zone yet (the LLM strategy is
        event-driven, not called every tick), or the LLM produced no parseable proposal that invocation — logged
        honestly, not hidden.
      </p>
      <div className="space-y-2">
        {llmDriven.map((d, i) => {
          const isOpen = openTick === d.tick;
          return (
            <div key={i} className="rounded-lg border border-border bg-card/50">
              <button
                onClick={() => setOpenTick(isOpen ? null : d.tick)}
                className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-sm hover:bg-accent/30"
              >
                <span className="min-w-0 flex-1 truncate">
                  <span className="text-muted-foreground">Tick {d.tick}</span> · {d.zone} ·{" "}
                  <span className="font-medium text-foreground">{d.arbiter_decision.slice(0, 80)}</span>
                </span>
                <ChevronDown className={cn("size-4 shrink-0 text-muted-foreground transition-transform", isOpen && "rotate-180")} />
              </button>
              {isOpen && (
                <div className="grid grid-cols-1 gap-3 border-t border-border p-4 md:grid-cols-3">
                  <ProposalBlock label="Energy specialist" json={d.energy_proposal} color="text-warning" />
                  <ProposalBlock label="Comfort specialist" json={d.comfort_proposal} color="text-info" />
                  <ProposalBlock label="Carbon specialist" json={d.carbon_proposal} color="text-[color:var(--chart-4)]" />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
