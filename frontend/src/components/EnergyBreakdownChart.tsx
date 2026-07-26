import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { EnergyBreakdown } from "../types";

const COLORS: Record<string, string> = {
  HVAC: "var(--warning)",
  Lighting: "var(--info)",
  "Plug loads": "var(--chart-4)",
  "Solar PV (offset)": "var(--success)",
};

export default function EnergyBreakdownChart({ breakdown }: { breakdown: EnergyBreakdown | null }) {
  if (!breakdown) return <p className="text-sm text-muted-foreground">Loading…</p>;

  const data = [
    { name: "HVAC", kwh: breakdown.hvac_kwh },
    { name: "Lighting", kwh: breakdown.lighting_kwh },
    { name: "Plug loads", kwh: breakdown.plugload_kwh },
    { name: "Solar PV (offset)", kwh: -breakdown.pv_kwh },
  ];

  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} layout="vertical" margin={{ left: 20 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" horizontal={false} />
        <XAxis type="number" stroke="var(--muted-foreground)" tick={{ fontSize: 12 }} unit=" kWh" tickLine={false} axisLine={{ stroke: "var(--border)" }} />
        <YAxis type="category" dataKey="name" stroke="var(--muted-foreground)" tick={{ fontSize: 12 }} width={100} tickLine={false} axisLine={false} />
        <Tooltip
          contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 }}
          cursor={{ fill: "var(--accent)", opacity: 0.4 }}
        />
        <Bar dataKey="kwh" radius={6}>
          {data.map((d) => (
            <Cell key={d.name} fill={COLORS[d.name]} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
