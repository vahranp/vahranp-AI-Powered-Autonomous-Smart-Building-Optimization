import { useMemo } from "react";
import {
  Cloud,
  Droplets,
  Flame,
  Gauge,
  Lightbulb,
  Thermometer,
  TrendingUp,
  Users,
  Wind,
  Zap,
} from "lucide-react";

import type { LiveState, RunTick } from "../../types";
import EnergyChart from "../EnergyChart";
import ComfortChart from "../ComfortChart";
import MetricCard from "../MetricCard";

function average(values: number[]): number | null {
  return values.length > 0 ? values.reduce((a, b) => a + b, 0) / values.length : null;
}

export default function LiveDashboardPanel({
  liveState,
  chartData,
  savingsPct,
}: {
  liveState: LiveState;
  chartData: RunTick[];
  savingsPct: number | null;
}) {
  const zones = useMemo(() => Object.values(liveState.zones ?? {}), [liveState.zones]);
  const avgTemp = average(zones.map((z) => z.temp_c));
  const avgHumidity = average(zones.map((z) => z.humidity_pct));
  const avgCo2 = average(zones.map((z) => z.co2_ppm));
  // PMV is only a meaningful comfort signal for occupied zones (matching
  // /api/summary's avg_pmv_occupied and the Simulation Monitor panel's own
  // calculation) -- averaging in empty zones here too used to make this
  // tile disagree with the Simulation Monitor panel's PMV for the same tick
  // whenever occupancy was uneven across zones.
  const occupiedZones = zones.filter((z) => z.occupancy > 0);
  const avgPmv = average((occupiedZones.length > 0 ? occupiedZones : zones).map((z) => z.pmv));
  const avgLighting = average(zones.map((z) => z.lighting_pct));
  const totalOccupancy = zones.reduce((sum, z) => sum + z.occupancy, 0);
  const hvacActive = (liveState.hvac_kwh ?? 0) > 0.05;
  const lastComfortDev = chartData.length > 0 ? chartData[chartData.length - 1].comfort_deviation_c : null;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-4">
        <MetricCard icon={Zap} label="Energy" value={liveState.ai_kwh != null ? `${liveState.ai_kwh.toFixed(2)} kWh` : "—"} tone="amber" />
        <MetricCard icon={Thermometer} label="Temperature" value={avgTemp != null ? `${avgTemp.toFixed(1)}°C` : "—"} tone="sky" />
        <MetricCard icon={Droplets} label="Humidity" value={avgHumidity != null ? `${avgHumidity.toFixed(0)}%` : "—"} tone="sky" />
        <MetricCard icon={Wind} label="CO2" value={avgCo2 != null ? `${avgCo2.toFixed(0)} ppm` : "—"} tone="emerald" />
        <MetricCard icon={Users} label="Occupancy" value={zones.length > 0 ? `${totalOccupancy.toFixed(0)}` : "—"} tone="sky" />
        <MetricCard icon={Gauge} label="Comfort deviation" value={lastComfortDev != null ? `${lastComfortDev.toFixed(2)}°C` : "—"} tone="sky" />
        <MetricCard icon={Gauge} label="PMV" value={avgPmv != null ? avgPmv.toFixed(2) : "—"} tone="amber" />
        <MetricCard icon={Flame} label="HVAC status" value={hvacActive ? "Active" : "Idle"} tone={hvacActive ? "amber" : "emerald"} />
        <MetricCard icon={Lightbulb} label="Lighting" value={avgLighting != null ? `${(avgLighting * 100).toFixed(0)}%` : "—"} tone="amber" />
        <MetricCard icon={Cloud} label="Carbon" value={liveState.carbon_kg != null ? `${liveState.carbon_kg.toFixed(2)} kg` : "—"} tone="red" />
        <MetricCard icon={TrendingUp} label="Savings vs baseline" value={savingsPct != null ? `${savingsPct}%` : "no baseline run"} tone="emerald" />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <p className="mb-1.5 text-xs text-muted-foreground">Energy: AI-controlled vs baseline (live)</p>
          <EnergyChart data={chartData} />
        </div>
        <div>
          <p className="mb-1.5 text-xs text-muted-foreground">Comfort deviation (live)</p>
          <ComfortChart data={chartData} />
        </div>
      </div>
    </div>
  );
}
