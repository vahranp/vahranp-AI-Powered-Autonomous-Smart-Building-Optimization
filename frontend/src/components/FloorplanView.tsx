import { useState } from "react";
import type { ZoneSnapshot } from "../types";

const TEMP_MIN = 15;
const TEMP_MAX = 28;

function tempColor(temp: number | null | undefined): string {
  if (temp == null) return "#3f3f46"; // zinc-700, unknown
  const t = Math.max(0, Math.min(1, (temp - TEMP_MIN) / (TEMP_MAX - TEMP_MIN)));
  // sky (cold) -> red (hot), matching the --info / --destructive hues
  const cold = [56, 189, 248]; // sky-400
  const hot = [248, 113, 113]; // red-400
  const r = Math.round(cold[0] + (hot[0] - cold[0]) * t);
  const g = Math.round(cold[1] + (hot[1] - cold[1]) * t);
  const b = Math.round(cold[2] + (hot[2] - cold[2]) * t);
  return `rgb(${r},${g},${b})`;
}

interface ZoneBox {
  key: string;
  label: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

const LAYOUT: ZoneBox[] = [
  { key: "Perimeter_ZN_3", label: "North", x: 90, y: 10, w: 220, h: 70 },
  { key: "Perimeter_ZN_4", label: "West", x: 10, y: 90, w: 70, h: 220 },
  { key: "Core_ZN", label: "Core", x: 90, y: 90, w: 220, h: 220 },
  { key: "Perimeter_ZN_2", label: "East", x: 320, y: 90, w: 70, h: 220 },
  { key: "Perimeter_ZN_1", label: "South", x: 90, y: 320, w: 220, h: 70 },
];

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between border-b border-border/60 py-1 last:border-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono tabular-nums text-foreground/90">{value}</span>
    </div>
  );
}

export default function FloorplanView({ snapshot }: { snapshot: Record<string, ZoneSnapshot> | undefined }) {
  const [hovered, setHovered] = useState<string | null>(null);
  const activeKey = hovered ?? LAYOUT[2].key; // default to Core_ZN so the panel never looks empty
  const activeBox = LAYOUT.find((b) => b.key === activeKey)!;
  const activeZone = snapshot?.[activeKey];

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-[minmax(0,1fr)_15rem]">
      <svg viewBox="0 0 400 400" className="mx-auto w-full max-w-md">
        {LAYOUT.map((box) => {
          const zone = snapshot?.[box.key];
          const isActive = box.key === activeKey;
          return (
            <g
              key={box.key}
              onMouseEnter={() => setHovered(box.key)}
              onMouseLeave={() => setHovered(null)}
              onFocus={() => setHovered(box.key)}
              onBlur={() => setHovered(null)}
              tabIndex={0}
              className="cursor-pointer outline-none"
            >
              <rect
                x={box.x}
                y={box.y}
                width={box.w}
                height={box.h}
                rx={8}
                fill={tempColor(zone?.temp_c)}
                stroke={
                  isActive
                    ? "var(--foreground)"
                    : zone?.fault_fallback
                      ? "var(--destructive)"
                      : zone?.meeting_prep
                        ? "var(--info)"
                        : "rgba(255,255,255,0.2)"
                }
                strokeWidth={isActive ? 2.5 : zone?.fault_fallback || zone?.meeting_prep ? 3 : 1}
                strokeDasharray={zone?.fault_fallback ? "6 4" : undefined}
                opacity={hovered && !isActive ? 0.55 : 0.9}
                style={{ transition: "opacity 120ms ease, stroke-width 120ms ease" }}
              />
              <text x={box.x + box.w / 2} y={box.y + box.h / 2 - 6} textAnchor="middle" className="pointer-events-none fill-white text-[11px] font-medium">
                {box.label}
              </text>
              <text x={box.x + box.w / 2} y={box.y + box.h / 2 + 12} textAnchor="middle" className="pointer-events-none fill-white text-[13px] font-semibold">
                {zone?.temp_c != null ? `${zone.temp_c.toFixed(1)}°C` : "—"}
              </text>
              {zone?.occupancy != null && zone.occupancy > 0 && (
                <text x={box.x + box.w / 2} y={box.y + box.h / 2 + 28} textAnchor="middle" className="pointer-events-none fill-white/80 text-[10px]">
                  {Math.round(zone.occupancy)} occ · {zone.co2_ppm?.toFixed(0) ?? "—"} ppm
                </text>
              )}
              {zone?.fault_fallback && (
                <text x={box.x + 6} y={box.y + 16} className="pointer-events-none fill-red-100 text-[10px] font-semibold">
                  FALLBACK
                </text>
              )}
              {zone?.meeting_prep && !zone?.fault_fallback && (
                <text x={box.x + 6} y={box.y + 16} className="pointer-events-none fill-sky-100 text-[10px] font-semibold">
                  PREP
                </text>
              )}
            </g>
          );
        })}
      </svg>

      <div className="rounded-lg border border-border bg-secondary/20 p-3 text-xs">
        <p className="mb-1.5 flex items-center gap-1.5 font-medium text-foreground/90">
          {activeBox.label} <span className="font-mono text-muted-foreground">({activeBox.key})</span>
        </p>
        {!activeZone ? (
          <p className="text-muted-foreground">No data for this zone yet.</p>
        ) : (
          <div>
            <DetailRow label="Temperature" value={activeZone.temp_c != null ? `${activeZone.temp_c.toFixed(1)}°C` : "—"} />
            <DetailRow label="Humidity" value={activeZone.humidity_pct != null ? `${activeZone.humidity_pct.toFixed(0)}%` : "—"} />
            <DetailRow label="CO2" value={activeZone.co2_ppm != null ? `${activeZone.co2_ppm.toFixed(0)} ppm` : "—"} />
            <DetailRow label="PMV (Fanger)" value={activeZone.pmv != null ? activeZone.pmv.toFixed(2) : "—"} />
            <DetailRow label="PPD" value={activeZone.ppd_pct != null ? `${activeZone.ppd_pct.toFixed(0)}% dissatisfied` : "—"} />
            <DetailRow label="Daylight" value={activeZone.daylight_lux != null ? `${activeZone.daylight_lux.toFixed(0)} lux` : "—"} />
            <DetailRow label="Lighting" value={activeZone.lighting_pct != null ? `${(activeZone.lighting_pct * 100).toFixed(0)}%` : "—"} />
            <DetailRow label="AI confidence" value={activeZone.confidence != null ? `${(activeZone.confidence * 100).toFixed(0)}%` : "—"} />
            <DetailRow label="Guardrail clipped" value={activeZone.guardrail_intervened ? "yes" : "no"} />
            {activeZone.meeting_prep && <p className="mt-1.5 text-info">Meeting-prep pre-conditioning active</p>}
            {activeZone.fault_fallback && <p className="mt-1.5 text-destructive">Sensor fault — neighbor-zone fallback</p>}
          </div>
        )}
        <p className="mt-2 text-[10px] text-muted-foreground/70">Hover or focus a zone to inspect it.</p>
      </div>
    </div>
  );
}
