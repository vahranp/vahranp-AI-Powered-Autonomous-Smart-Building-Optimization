import { useState } from "react";
import { ChevronDown, ShieldAlert } from "lucide-react";

import type { Decision } from "../types";
import { cn } from "@/lib/utils";
import { Badge } from "./ui/badge";
import { ScrollArea } from "./ui/scroll-area";

function Row({ d }: { d: Decision }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-border bg-card/50">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left hover:bg-accent/30"
      >
        <span className="min-w-0 flex-1 truncate text-sm text-foreground/90">
          <span className="text-muted-foreground">Tick {d.tick}</span> · {d.zone} ·{" "}
          <span className="font-medium text-foreground">{d.arbiter_decision}</span>
        </span>
        <div className="flex shrink-0 items-center gap-2">
          {d.guardrail_intervened && (
            <Badge variant="destructive" className="gap-1">
              <ShieldAlert />
              guardrail
            </Badge>
          )}
          <ChevronDown className={cn("size-4 text-muted-foreground transition-transform", open && "rotate-180")} />
        </div>
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-border px-4 py-3 text-sm text-muted-foreground">
          <p><span className="font-medium text-warning">Energy agent:</span> {d.energy_proposal}</p>
          <p><span className="font-medium text-info">Comfort agent:</span> {d.comfort_proposal}</p>
          <p><span className="font-medium text-[color:var(--chart-4)]">Carbon agent:</span> {d.carbon_proposal}</p>
          <p><span className="font-medium text-foreground/90">Arbiter:</span> {d.arbiter_decision}</p>
        </div>
      )}
    </div>
  );
}

export default function NegotiationFeed({ decisions }: { decisions: Decision[] }) {
  if (decisions.length === 0) {
    return <p className="text-sm text-muted-foreground">No decisions logged yet.</p>;
  }
  return (
    <ScrollArea className="h-[28rem] pr-3">
      <div className="space-y-2">
        {decisions.map((d, i) => (
          <Row key={i} d={d} />
        ))}
      </div>
    </ScrollArea>
  );
}
