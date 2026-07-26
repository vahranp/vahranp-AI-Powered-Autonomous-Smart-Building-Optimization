import { TrendingDown, TrendingUp } from "lucide-react";

import type { CompareResult, StrategyStats } from "../types";
import { Badge } from "./ui/badge";
import { Card } from "./ui/card";

const LABELS: Record<string, string> = {
  reactive: "Reactive (baseline)",
  predictive: "Predictive (pre-conditioning)",
  llm: "LLM agents (real MCP)",
};

const DOT: Record<string, string> = {
  reactive: "bg-warning",
  predictive: "bg-info",
  llm: "bg-[color:var(--chart-4)]",
};

function DeltaLine({ reactive, other, label }: { reactive: StrategyStats; other: StrategyStats; label: string }) {
  const energyDeltaPct = (100 * (reactive.total_kwh - other.total_kwh)) / reactive.total_kwh;
  const comfortDeltaPct =
    (100 * (reactive.avg_comfort_deviation_c - other.avg_comfort_deviation_c)) / reactive.avg_comfort_deviation_c;
  const energyGood = energyDeltaPct >= 0;
  return (
    <div className="flex items-start gap-3 rounded-lg bg-secondary/40 p-3 text-sm">
      {energyGood ? (
        <TrendingDown className="mt-0.5 size-4 shrink-0 text-success" />
      ) : (
        <TrendingUp className="mt-0.5 size-4 shrink-0 text-destructive" />
      )}
      <p className="text-muted-foreground">
        <span className="font-medium text-foreground/90">{label}: </span>
        {energyDeltaPct >= 0 ? "-" : "+"}
        {Math.abs(energyDeltaPct).toFixed(2)}% energy, {comfortDeltaPct >= 0 ? "-" : "+"}
        {Math.abs(comfortDeltaPct).toFixed(1)}% comfort deviation vs. reactive baseline — same weather, same building.
      </p>
    </div>
  );
}

export default function StrategyCompare({ compare }: { compare: CompareResult }) {
  const { reactive } = compare;
  const others = (["predictive", "llm"] as const).filter((k) => compare[k]);

  if (!reactive || others.length === 0) {
    return <p className="text-sm text-muted-foreground">Run the reactive baseline plus at least one other strategy to see a comparison.</p>;
  }

  const strategies = ["reactive", ...others] as const;

  return (
    <div className="space-y-4">
      <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${strategies.length}, minmax(0, 1fr))` }}>
        {strategies.map((key) => {
          const s = compare[key]!;
          return (
            <Card key={key} className="gap-1.5 py-4">
              <div className="flex items-center gap-2 px-4">
                <span className={`size-2 rounded-full ${DOT[key]}`} />
                <p className="text-xs text-muted-foreground">{LABELS[key]}</p>
              </div>
              <p className="px-4 text-xl font-semibold tracking-tight">{s.total_kwh} kWh</p>
              <p className="px-4 text-xs text-muted-foreground">{s.avg_comfort_deviation_c}°C avg deviation</p>
            </Card>
          );
        })}
      </div>

      <div className="space-y-2">
        {others.map((key) => (
          <DeltaLine key={key} reactive={reactive} other={compare[key]!} label={`${LABELS[key]} (whole run)`} />
        ))}
      </div>

      {compare.llm_active_window && (
        <Card className="gap-2 border-info/30 bg-info/5 py-4">
          <div className="flex items-center gap-2 px-4">
            <Badge variant="info" className="gap-1">
              {compare.llm_active_window.genuinely_agent_driven_ticks} / {compare.llm_active_window.window_ticks} ticks agent-driven
            </Badge>
          </div>
          <p className="px-4 text-xs text-muted-foreground">
            Isolated to the ticks the LLM agent actually controlled — the whole-run number above is diluted by the
            long reactive-fallback tail after the capped window ends, so this is the real, attributable
            agent-vs-baseline comparison.
          </p>
          <div className="px-4">
            <DeltaLine
              reactive={compare.llm_active_window.reactive_baseline}
              other={compare.llm_active_window.llm}
              label="LLM agent (active window only)"
            />
          </div>
        </Card>
      )}
    </div>
  );
}
