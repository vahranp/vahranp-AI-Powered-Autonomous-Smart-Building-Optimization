import type { OccupancyPattern } from "../types";
import { Badge } from "./ui/badge";

export default function OccupancyPatternPanel({ pattern }: { pattern: OccupancyPattern }) {
  const zones = Object.keys(pattern);
  if (zones.length === 0) return <p className="text-sm text-muted-foreground">No occupancy data yet.</p>;

  return (
    <div className="space-y-2">
      {zones.map((zone) => {
        const p = pattern[zone];
        return (
          <div key={zone} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border px-3 py-2 text-sm">
            <span className="font-medium text-foreground/90">{zone}</span>
            <span className="text-xs text-muted-foreground">
              day avg <span className="text-foreground/80">{p.avg_occupancy_day}</span> · night avg{" "}
              <span className="text-foreground/80">{p.avg_occupancy_night}</span>
            </span>
            <Badge variant={p.utilization_pct < 30 ? "warning" : "success"}>{p.utilization_pct}% utilized</Badge>
          </div>
        );
      })}
    </div>
  );
}
