import { useState, type ReactNode } from "react";
import { ArrowRight, Brain, ChevronDown, Eye, Gauge, ShieldAlert } from "lucide-react";

import type { Decision } from "../../types";
import { Badge } from "../ui/badge";
import { Progress } from "../ui/progress";

function buildObservations(d: Decision): string[] {
  const drv = d.drivers;
  const obs: string[] = [];
  if (drv.pmv != null && drv.pmv > 0.5) obs.push(`PMV above target (+${drv.pmv.toFixed(2)})`);
  if (drv.pmv != null && drv.pmv < -0.5) obs.push(`PMV below target (${drv.pmv.toFixed(2)})`);
  if ((drv.occupancy ?? 0) > 0) obs.push(`Occupied (${drv.occupancy!.toFixed(1)} people)`);
  else obs.push("Unoccupied");
  if (drv.outdoor_trend === "rising") obs.push("Outdoor temperature rising");
  if (drv.outdoor_trend === "falling") obs.push("Outdoor temperature falling");
  if (drv.co2_ppm != null && drv.co2_ppm > 800) obs.push(`CO2 elevated (${drv.co2_ppm.toFixed(0)} ppm)`);
  if (drv.daylight_lux != null && drv.daylight_lux > 400) obs.push(`High daylight (${drv.daylight_lux.toFixed(0)} lux)`);
  if (drv.demand_response_active) obs.push("Demand response active");
  if (drv.fault_fallback) obs.push("Sensor fault — neighbor fallback");
  return obs;
}

function tryPrettyProposal(raw: string): string {
  try {
    const parsed = JSON.parse(raw);
    if (parsed?.proposals?.length) {
      return parsed.proposals
        .map((p: { zone?: string; heating_c?: number; cooling_c?: number; reason?: string }) =>
          `${p.zone}: heat ${p.heating_c}°C / cool ${p.cooling_c}°C — ${p.reason ?? ""}`)
        .join("\n");
    }
    return raw;
  } catch {
    return raw;
  }
}

function ZoneCard({ zone, d }: { zone: string; d: Decision }) {
  const [open, setOpen] = useState(false);
  const drv = d.drivers;
  const usedLlm = !!drv.used_llm;
  const confidencePct = drv.confidence != null ? Math.round(drv.confidence * 100) : null;
  const observations = buildObservations(d);
  const heat = drv.heating_c;
  const cool = drv.cooling_c;

  return (
    <div className="rounded-lg border border-border bg-secondary/20">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left hover:bg-accent/30">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-mono text-xs font-medium">{zone}</span>
          <Badge variant="outline" className="shrink-0 text-[10px]">tick {d.tick}</Badge>
          {usedLlm ? (
            <Badge variant="success" className="shrink-0 gap-1 text-[10px]"><Brain className="size-3" />LLM</Badge>
          ) : (
            <Badge variant="secondary" className="shrink-0 text-[10px]">rule-based</Badge>
          )}
          {d.guardrail_intervened && (
            <Badge variant="destructive" className="shrink-0 gap-1 text-[10px]"><ShieldAlert className="size-3" />clipped</Badge>
          )}
        </div>
        <ChevronDown className={`size-3.5 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      <div className="flex items-center gap-1.5 px-3 pb-2 text-[11px] text-muted-foreground">
        <span>{drv.temp_c != null ? `${drv.temp_c.toFixed(1)}°C` : "—"}</span>
        <ArrowRight className="size-3" />
        <span className="font-medium text-foreground/90">
          {heat != null && cool != null ? `heat ${heat.toFixed(1)}° / cool ${cool.toFixed(1)}°C` : "—"}
        </span>
        {drv.lighting_pct != null && <span>· lighting {(drv.lighting_pct * 100).toFixed(0)}%</span>}
      </div>

      {open && (
        <div className="space-y-3 border-t border-border px-3 py-3 text-xs">
          <Section icon={Eye} title="Observation" tone="text-info">
            <ul className="space-y-1">
              {observations.map((o) => (
                <li key={o} className="flex gap-2">
                  <span className="text-info">•</span> {o}
                </li>
              ))}
            </ul>
          </Section>

          {usedLlm && d.energy_proposal && (
            <Section icon={Brain} title="Reasoning (specialist proposals)" tone="text-warning">
              <pre className="whitespace-pre-wrap font-sans text-[11px] leading-relaxed text-muted-foreground">
                {tryPrettyProposal(d.energy_proposal)}
              </pre>
            </Section>
          )}

          <Section icon={Gauge} title="Decision" tone="text-success">
            <p className="text-[11px] leading-relaxed text-foreground/90">{d.arbiter_decision}</p>
          </Section>

          {confidencePct != null && (
            <div>
              <div className="mb-1 flex items-center justify-between text-[10px] text-muted-foreground">
                <span>Confidence</span>
                <span className="font-mono">{confidencePct}%</span>
              </div>
              <Progress value={confidencePct} indicatorClassName={confidencePct >= 80 ? "bg-success" : "bg-warning"} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function AgentConsolePanel({ latestDecisions }: { latestDecisions: Record<string, Decision> }) {
  const zones = Object.keys(latestDecisions).sort();
  if (zones.length === 0) {
    return <p className="text-sm text-muted-foreground">No decisions logged yet — run a simulation to see per-zone reasoning here.</p>;
  }
  return (
    <div className="space-y-2">
      {zones.map((zone) => (
        <ZoneCard key={zone} zone={zone} d={latestDecisions[zone]} />
      ))}
    </div>
  );
}

function Section({ icon: Icon, title, tone, children }: { icon: typeof Eye; title: string; tone: string; children: ReactNode }) {
  return (
    <div>
      <p className={`mb-1 flex items-center gap-1.5 text-[11px] font-medium ${tone}`}>
        <Icon className="size-3.5" /> {title}
      </p>
      <div className="text-muted-foreground">{children}</div>
    </div>
  );
}
