import type { LucideIcon } from "lucide-react";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface Props {
  label: string;
  value: string;
  /** Optional small comparison line under the value, e.g. "vs 243 kg baseline -- 12% lower". */
  sublabel?: string;
  tone?: "emerald" | "amber" | "red" | "sky";
  icon?: LucideIcon;
  /** "filled" renders a bold solid-color hero card (for the one or two
   * headline stats that should visually pop), "default" is the standard
   * subtle tinted tile used everywhere else. */
  variant?: "default" | "filled";
}

// "emerald" keeps its original call-site name for backward compatibility with
// every existing usage, but now maps to the dedicated --success token rather
// than --primary -- --primary is the Honeywell-red brand/action color, and
// these tiles represent positive/good metrics (savings, renewables, etc.),
// which is a status meaning, not a brand accent.
const toneClasses: Record<string, string> = {
  emerald: "text-success",
  amber: "text-warning",
  red: "text-destructive",
  sky: "text-info",
};

const toneRing: Record<string, string> = {
  emerald: "bg-success/15 text-success",
  amber: "bg-warning/15 text-warning",
  red: "bg-destructive/15 text-destructive",
  sky: "bg-info/15 text-info",
};

const toneFilled: Record<string, string> = {
  emerald: "bg-success text-success-foreground shadow-[0_10px_30px_-10px_var(--success)]",
  amber: "bg-warning text-warning-foreground shadow-[0_10px_30px_-10px_var(--warning)]",
  red: "bg-destructive text-destructive-foreground shadow-[0_10px_30px_-10px_var(--destructive)]",
  sky: "bg-info text-info-foreground shadow-[0_10px_30px_-10px_var(--info)]",
};

export default function MetricCard({ label, value, sublabel, tone = "sky", icon: Icon, variant = "default" }: Props) {
  if (variant === "filled") {
    return (
      <Card className={cn("gap-2.5 border-transparent py-4", toneFilled[tone])}>
        <div className="flex items-center justify-between px-4">
          <p className="text-xs font-medium tracking-wide opacity-80">{label}</p>
          {Icon && (
            <div className="flex size-7 shrink-0 items-center justify-center rounded-md bg-white/15">
              <Icon className="size-3.5" />
            </div>
          )}
        </div>
        <p className="px-4 text-2xl font-bold tracking-tight tabular-nums">{value}</p>
        {sublabel && <p className="px-4 text-xs opacity-75">{sublabel}</p>}
      </Card>
    );
  }

  return (
    <Card className="gap-2.5 py-4 transition-shadow hover:shadow-md">
      <div className="flex items-center justify-between px-4">
        <p className="text-xs font-medium tracking-wide text-muted-foreground">{label}</p>
        {Icon && (
          <div className={cn("flex size-7 shrink-0 items-center justify-center rounded-md", toneRing[tone])}>
            <Icon className="size-3.5" />
          </div>
        )}
      </div>
      <p className={cn("px-4 text-2xl font-semibold tracking-tight tabular-nums", toneClasses[tone])}>{value}</p>
      {sublabel && <p className="px-4 text-xs text-muted-foreground">{sublabel}</p>}
    </Card>
  );
}
