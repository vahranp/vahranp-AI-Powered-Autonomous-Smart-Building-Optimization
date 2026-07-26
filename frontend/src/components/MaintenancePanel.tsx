import { AlertCircle, CheckCircle2 } from "lucide-react";

import type { MaintenanceInfo } from "../types";
import { Progress } from "./ui/progress";
import { Skeleton } from "./ui/skeleton";

export default function MaintenancePanel({ info }: { info: MaintenanceInfo | null }) {
  if (!info) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-8 w-full" />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-sm">
        <span className="text-muted-foreground">Estimated HVAC runtime</span>
        <span className="font-medium">{info.estimated_hvac_runtime_hours} hrs</span>
      </div>
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">Heavy-load ticks</span>
          <span className="font-medium">{info.heavy_load_pct}%</span>
        </div>
        <Progress value={info.heavy_load_pct} indicatorClassName={info.heavy_load_pct >= 60 ? "bg-warning" : "bg-success"} />
      </div>
      <div
        className={`flex items-center gap-2 rounded-lg px-3 py-2 text-xs ${
          info.service_recommended ? "bg-warning/15 text-warning" : "bg-success/15 text-success"
        }`}
      >
        {info.service_recommended ? <AlertCircle className="size-3.5" /> : <CheckCircle2 className="size-3.5" />}
        {info.service_recommended ? "Service inspection recommended" : "No service flag raised"}
      </div>
      <p className="text-xs text-muted-foreground/70">{info.note}</p>
    </div>
  );
}
