import { AlertTriangle, RadioTower } from "lucide-react";

import type { ResilienceEvent } from "../types";
import { Badge } from "./ui/badge";
import { ScrollArea } from "./ui/scroll-area";

export default function ResiliencePanel({ events }: { events: ResilienceEvent[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">No resilience events logged.</p>;
  }

  const sensorFaults = events.filter((e) => e.event_type === "sensor_dropout");
  const anomalies = events.filter((e) => e.event_type === "energy_anomaly");

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <Badge variant="destructive" className="gap-1.5">
          <RadioTower />
          {sensorFaults.length} sensor-fault events
        </Badge>
        <Badge variant="warning" className="gap-1.5">
          <AlertTriangle />
          {anomalies.length} energy anomalies
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        Neighbor-zone fallback used for sensor faults (no manual intervention); anomalies are a real z-score check
        vs. rolling history.
      </p>
      <ScrollArea className="h-40 rounded-lg border">
        <div className="divide-y divide-border">
          {events.map((e, i) => (
            <div key={i} className="px-3 py-2 text-xs text-muted-foreground">
              <span className={e.event_type === "energy_anomaly" ? "font-medium text-warning" : "font-medium text-destructive"}>
                tick {e.tick}
              </span>{" "}
              — {e.description}
            </div>
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
