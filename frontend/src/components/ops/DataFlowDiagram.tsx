import { useMemo } from "react";

import type { PipelineEventItem, PipelinePhase } from "../../types";

interface Node {
  key: string;
  label: string;
  sub: string;
  y: number;
  phases: PipelinePhase[];
}

// Order matches the real execution sequence in sim/energyplus_loop.py: the
// three specialist agents reason locally (Ollama, not MCP) BEFORE the
// arbiter reads the MCP recent_issues resource and calls the MCP tools --
// so LLM reasoning genuinely happens first, MCP round-trips second.
const NODES: Node[] = [
  { key: "eplus1", label: "EnergyPlus", sub: "simulation engine", y: 20, phases: ["tick_start"] },
  { key: "backend", label: "Backend", sub: "FastAPI + SQLite", y: 105, phases: ["tick_start"] },
  { key: "llm", label: "LLM Agents", sub: "Ollama, multi-agent", y: 190, phases: ["llm_specialist"] },
  { key: "mcp", label: "MCP", sub: "resources + tools", y: 275, phases: ["mcp_resource_read", "mcp_tool_call"] },
  { key: "control", label: "Control Engine", sub: "guardrails + actuation", y: 360, phases: ["actuator_applied"] },
  { key: "eplus2", label: "EnergyPlus", sub: "actuators updated", y: 445, phases: ["actuator_applied"] },
  { key: "dashboard", label: "Dashboard", sub: "live console", y: 530, phases: ["tick_complete"] },
];
const BOX_W = 220;
const BOX_H = 56;
const BOX_X = 50;

function lastIdFor(events: PipelineEventItem[], phases: PipelinePhase[]): number {
  for (let i = events.length - 1; i >= 0; i--) {
    if (phases.includes(events[i].phase)) return events[i].id;
  }
  return 0;
}

export default function DataFlowDiagram({ events, live }: { events: PipelineEventItem[]; live: boolean }) {
  const highlightIds = useMemo(
    () => Object.fromEntries(NODES.map((n) => [n.key, lastIdFor(events, n.phases)])),
    [events],
  );

  return (
    <div className="space-y-2">
      <svg viewBox="0 0 320 610" className="mx-auto w-full max-w-[280px]" role="img" aria-label="Closed-loop data flow diagram">
        <defs>
          <marker id="ops-arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 Z" fill="var(--muted-foreground)" />
          </marker>
        </defs>

        {NODES.slice(0, -1).map((n, i) => {
          const next = NODES[i + 1];
          const x = BOX_X + BOX_W / 2;
          const y1 = n.y + BOX_H;
          const y2 = next.y;
          return (
            <path
              key={`arrow-${n.key}`}
              d={`M${x},${y1} L${x},${y2}`}
              stroke="var(--muted-foreground)"
              strokeOpacity={0.5}
              strokeWidth={1.5}
              className={live ? "ops-flow-line" : undefined}
              markerEnd="url(#ops-arrow)"
              fill="none"
            />
          );
        })}

        {/* loop-back: EnergyPlus(2) actuates, cycle continues into the next tick */}
        <path
          d={`M${BOX_X},${NODES[5].y + BOX_H / 2} C 10,${NODES[5].y} 10,${NODES[0].y + BOX_H / 2} ${BOX_X},${NODES[0].y + BOX_H / 2}`}
          stroke="var(--success)"
          strokeOpacity={0.4}
          strokeWidth={1.5}
          strokeDasharray="3 4"
          className={live ? "ops-flow-line" : undefined}
          markerEnd="url(#ops-arrow)"
          fill="none"
        />
        <text x={16} y={(NODES[5].y + NODES[0].y) / 2} className="fill-success" fontSize="8" textAnchor="middle" transform={`rotate(-90 16 ${(NODES[5].y + NODES[0].y) / 2})`}>
          next tick
        </text>

        {NODES.map((n) => {
          const active = highlightIds[n.key] > 0;
          return (
            <g key={highlightIds[n.key] ? `${n.key}-${highlightIds[n.key]}` : n.key} className={active && live ? "ops-node-active" : undefined}>
              <rect
                x={BOX_X}
                y={n.y}
                width={BOX_W}
                height={BOX_H}
                rx={10}
                className="fill-secondary stroke-border"
                strokeWidth={1}
              />
              <text x={BOX_X + BOX_W / 2} y={n.y + 22} textAnchor="middle" className="fill-foreground" fontSize="13" fontWeight={600}>
                {n.label}
              </text>
              <text x={BOX_X + BOX_W / 2} y={n.y + 38} textAnchor="middle" className="fill-muted-foreground" fontSize="9">
                {n.sub}
              </text>
            </g>
          );
        })}
      </svg>
      <p className="text-center text-[11px] text-muted-foreground">
        Node glow reflects real pipeline events as they happen. MCP/LLM only light up during the "llm" strategy.
      </p>
    </div>
  );
}
