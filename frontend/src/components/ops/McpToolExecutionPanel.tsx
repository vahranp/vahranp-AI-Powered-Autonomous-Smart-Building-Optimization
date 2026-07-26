import { useEffect, useMemo, useRef, type ReactNode } from "react";
import { BookOpenCheck, Brain, CheckCircle2, Wrench, XCircle, type LucideIcon } from "lucide-react";

import type { PipelineEventItem } from "../../types";
import { Badge } from "../ui/badge";
import { ScrollArea } from "../ui/scroll-area";
import { formatClockTime } from "./format";

function toolSignature(e: PipelineEventItem): string {
  const d = e.detail as { tool?: string; args?: Record<string, unknown> };
  const args = d.args ?? {};
  const argStr = Object.entries(args)
    .filter(([k]) => k !== "rationale")
    .map(([k, v]) => `${k}=${typeof v === "number" ? v : JSON.stringify(v)}`)
    .join(", ");
  return `${d.tool ?? "?"}(${argStr})`;
}

export default function McpToolExecutionPanel({ events }: { events: PipelineEventItem[] }) {
  const relevant = useMemo(
    () => events.filter((e) => e.phase === "mcp_resource_read" || e.phase === "mcp_tool_call" || e.phase === "llm_specialist"),
    [events],
  );
  const scrollRootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const viewport = scrollRootRef.current?.querySelector<HTMLDivElement>('[data-slot="scroll-area-viewport"]');
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  }, [relevant.length]);

  if (relevant.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        MCP resource reads and tool calls only occur while the "llm" strategy is running — start an LLM run to see
        real MCP round-trips execute here.
      </p>
    );
  }

  return (
    <ScrollArea ref={scrollRootRef} className="h-72 pr-3">
      <div className="space-y-1.5">
        {relevant.slice(-80).map((e) => {
          const t = formatClockTime(e.ts);
          if (e.phase === "mcp_resource_read") {
            const d = e.detail as { uri?: string; chars?: number };
            return (
              <Row key={e.id} icon={BookOpenCheck} tone="text-info" ok>
                <span className="text-muted-foreground">{t}</span>{" "}
                <Badge variant="info" className="mx-1">MCP resource</Badge>
                <span className="font-mono">read_resource({d.uri})</span>
              </Row>
            );
          }
          if (e.phase === "llm_specialist") {
            const d = e.detail as { agent?: string; proposal_count?: number };
            return (
              <Row key={e.id} icon={Brain} tone="text-[var(--chart-4)]" ok>
                <span className="text-muted-foreground">{t}</span>{" "}
                <Badge variant="outline" className="mx-1">local LLM (not MCP)</Badge>
                <span className="font-mono">{d.agent}_agent.propose() → {d.proposal_count} proposal(s)</span>
              </Row>
            );
          }
          const d = e.detail as { tool?: string; result?: { status?: string } };
          const ok = d.result?.status === "accepted";
          return (
            <Row key={e.id} icon={Wrench} tone="text-foreground" ok={ok}>
              <span className="text-muted-foreground">{t}</span>{" "}
              <Badge variant="success" className="mx-1">MCP tool</Badge>
              <span className="font-mono">{toolSignature(e)}</span>
            </Row>
          );
        })}
      </div>
    </ScrollArea>
  );
}

function Row({
  icon: Icon,
  tone,
  ok,
  children,
}: {
  icon: LucideIcon;
  tone: string;
  ok: boolean;
  children: ReactNode;
}) {
  return (
    <div className="ops-event-enter flex items-center gap-2 rounded-md border border-border bg-secondary/20 px-2.5 py-1.5 text-xs">
      <Icon className={`size-3.5 shrink-0 ${tone}`} />
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {ok ? <CheckCircle2 className="size-3.5 shrink-0 text-success" /> : <XCircle className="size-3.5 shrink-0 text-destructive" />}
    </div>
  );
}
