import { useMemo, useState } from "react";
import { FlaskConical } from "lucide-react";

import type { LiveState, LiveZoneState } from "../../types";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";

interface FieldMeta {
  key: keyof LiveZoneState;
  label: string;
  format: (v: number | null | undefined) => string;
}

const ZONE_FIELDS: FieldMeta[] = [
  { key: "temp_c", label: "Zone Mean Air Temperature", format: (v) => (v != null ? `${v.toFixed(2)} °C` : "—") },
  { key: "humidity_pct", label: "Zone Air Relative Humidity", format: (v) => (v != null ? `${v.toFixed(1)} %` : "—") },
  { key: "occupancy", label: "Zone People Occupant Count", format: (v) => (v != null ? `${v.toFixed(2)} people` : "—") },
  { key: "co2_ppm", label: "Zone Air CO2 Concentration", format: (v) => (v != null ? `${v.toFixed(1)} ppm` : "—") },
  { key: "pmv", label: "Zone Thermal Comfort Fanger Model PMV", format: (v) => (v != null ? v.toFixed(3) : "—") },
  { key: "ppd_pct", label: "Zone Thermal Comfort Fanger Model PPD", format: (v) => (v != null ? `${v.toFixed(1)} %` : "—") },
  { key: "daylight_lux", label: "Daylighting Reference Point 1 Illuminance", format: (v) => (v != null ? `${v.toFixed(1)} lux` : "n/a (core zone)") },
  { key: "heating_c", label: "Heating setpoint actuator (Schedule:Compact)", format: (v) => (v != null ? `${v.toFixed(1)} °C` : "—") },
  { key: "cooling_c", label: "Cooling setpoint actuator (Schedule:Compact)", format: (v) => (v != null ? `${v.toFixed(1)} °C` : "—") },
  { key: "lighting_pct", label: "Lighting level actuator (Schedule:Compact)", format: (v) => (v != null ? `${(v * 100).toFixed(0)} %` : "—") },
];

const BUILDING_FIELDS: { key: keyof LiveState; label: string; format: (v: number | null | undefined) => string }[] = [
  { key: "ai_kwh", label: "ElectricityNet:Facility (meter)", format: (v) => (v != null ? `${v.toFixed(3)} kWh` : "—") },
  { key: "hvac_kwh", label: "Cooling + Heating + Fans:Electricity (meters)", format: (v) => (v != null ? `${v.toFixed(3)} kWh` : "—") },
  { key: "lighting_kwh", label: "InteriorLights:Electricity (meter)", format: (v) => (v != null ? `${v.toFixed(3)} kWh` : "—") },
  { key: "plugload_kwh", label: "InteriorEquipment:Electricity (meter)", format: (v) => (v != null ? `${v.toFixed(3)} kWh` : "—") },
  { key: "pv_kwh", label: "Photovoltaic:ElectricityProduced (meter)", format: (v) => (v != null ? `${v.toFixed(3)} kWh` : "—") },
  { key: "carbon_kg", label: "Carbon Equivalent:Facility (meter)", format: (v) => (v != null ? `${v.toFixed(4)} kg` : "—") },
  { key: "water_m3", label: "MainsWater:Facility (meter)", format: (v) => (v != null ? `${v.toFixed(4)} m³` : "—") },
];

export default function RawEnergyPlusPanel({ liveState }: { liveState: LiveState }) {
  const zoneKeys = useMemo(() => Object.keys(liveState.zones ?? {}), [liveState.zones]);
  const [selectedZone, setSelectedZone] = useState<string | null>(null);
  const zone = selectedZone ?? liveState.active_zone ?? zoneKeys[0];
  const zoneState = zone ? liveState.zones?.[zone] : undefined;
  const source = liveState.eplus_api_source ?? {};

  if (!zone || !zoneState) {
    return (
      <p className="text-sm text-muted-foreground">
        No live EnergyPlus data yet — run a simulation to see the literal Python API calls and values behind every
        number on this dashboard.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <FlaskConical className="size-3.5" />
          Every value below is read straight from the EnergyPlus Python API call shown — nothing here is computed
          or guessed by this dashboard.
        </p>
        <Select value={zone} onValueChange={(v) => setSelectedZone(v)}>
          <SelectTrigger className="w-44 shrink-0">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {zoneKeys.map((z) => (
              <SelectItem key={z} value={z}>
                {z}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="max-h-64 overflow-auto rounded-lg border border-border">
        <table className="w-full text-xs">
          <thead className="sticky top-0 z-10">
            <tr className="border-b border-border bg-popover text-left text-muted-foreground">
              <th className="px-3 py-2 font-medium">EnergyPlus Python API call</th>
              <th className="px-3 py-2 text-right font-medium">Live value</th>
            </tr>
          </thead>
          <tbody>
            {ZONE_FIELDS.map((f) => (
              <tr key={f.key} className="border-b border-border/60 last:border-0 hover:bg-accent/40">
                <td className="px-3 py-2 font-mono text-[10.5px] text-muted-foreground">
                  {(source[f.key] ?? f.label).replaceAll("<zone>", zone)}
                </td>
                <td className="px-3 py-2 text-right font-mono tabular-nums text-foreground/90">
                  {f.format(zoneState[f.key] as number | null | undefined)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-[11px] text-muted-foreground">Building-wide meters (this tick)</p>
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-xs">
          <tbody>
            {BUILDING_FIELDS.map((f) => (
              <tr key={f.key} className="border-b border-border/60 last:border-0 hover:bg-accent/40">
                <td className="px-3 py-2 font-mono text-[10.5px] text-muted-foreground">{source[f.key] ?? f.label}</td>
                <td className="px-3 py-2 text-right font-mono tabular-nums text-foreground/90">
                  {f.format(liveState[f.key] as number | null | undefined)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
