import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { RunTick } from "../types";

export default function EnergyChart({ data }: { data: RunTick[] }) {
  return (
    <ResponsiveContainer width="100%" height={280}>
      <ComposedChart data={data}>
        <defs>
          <linearGradient id="aiKwhFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--success)" stopOpacity={0.35} />
            <stop offset="100%" stopColor="var(--success)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
        <XAxis dataKey="tick" stroke="var(--muted-foreground)" tick={{ fontSize: 12, fontFamily: "var(--font-mono)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
        <YAxis stroke="var(--muted-foreground)" tick={{ fontSize: 12 }} unit=" kWh" tickLine={false} axisLine={false} />
        <Tooltip
          contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 }}
          labelStyle={{ color: "var(--muted-foreground)", marginBottom: 4 }}
          cursor={{ stroke: "var(--border)", strokeWidth: 1 }}
        />
        <Legend wrapperStyle={{ fontSize: 12, paddingTop: 8 }} iconType="line" iconSize={14} />
        <Line type="monotone" dataKey="baseline_kwh" name="Baseline" stroke="var(--warning)" dot={false} strokeWidth={2} />
        <Area type="monotone" dataKey="ai_kwh" name="AI-controlled" stroke="var(--success)" strokeWidth={2.5} fill="url(#aiKwhFill)" />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
