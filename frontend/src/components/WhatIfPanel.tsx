import { useState } from "react";
import { TrendingDown, TrendingUp } from "lucide-react";

import { fetchWhatIf } from "../api";
import type { WhatIfResult } from "../types";
import { Button } from "./ui/button";
import { Slider } from "./ui/slider";

export default function WhatIfPanel() {
  const [delta, setDelta] = useState(5);
  const [result, setResult] = useState<WhatIfResult | null>(null);
  const [loading, setLoading] = useState(false);

  const run = async () => {
    setLoading(true);
    try {
      setResult(await fetchWhatIf(delta));
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-md space-y-4">
      <label className="block text-sm text-muted-foreground">
        What if outdoor temperature changes by{" "}
        <span className="font-medium text-foreground">
          {delta > 0 ? "+" : ""}
          {delta}°C
        </span>
        ?
      </label>
      <Slider value={[delta]} min={-10} max={10} step={1} onValueChange={([v]) => setDelta(v)} />
      <Button onClick={run} disabled={loading} variant="secondary" size="sm">
        {loading ? "Estimating…" : "Run what-if estimate"}
      </Button>
      {result && (
        <div className="rounded-lg border border-border bg-card/50 p-3">
          <p className="flex items-center gap-1.5 text-sm">
            Projected energy change:{" "}
            <span
              className={`flex items-center gap-1 font-medium ${
                result.projected_change_pct >= 0 ? "text-warning" : "text-success"
              }`}
            >
              {result.projected_change_pct >= 0 ? <TrendingUp className="size-3.5" /> : <TrendingDown className="size-3.5" />}
              {result.projected_change_pct >= 0 ? "+" : ""}
              {result.projected_change_pct}%
            </span>
          </p>
          <p className="mt-1 text-xs text-muted-foreground">{result.method}</p>
        </div>
      )}
    </div>
  );
}
