import { useEffect, useRef, useState } from "react";
import { Bot, Building2, Loader2, Play, Square, Trash2 } from "lucide-react";

import {
  clearData,
  fetchRunLog,
  fetchRunLogTail,
  fetchRunStatus,
  startSimulation,
  stopSimulation,
} from "../api";
import type { RunStatus } from "../types";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Label } from "./ui/label";
import { Switch } from "./ui/switch";

const STATUS_BADGE: Record<RunStatus["status"], { label: string; variant: "success" | "outline" | "destructive" | "warning" }> = {
  idle: { label: "Idle", variant: "outline" },
  running: { label: "Running", variant: "success" },
  completed: { label: "Completed", variant: "success" },
  failed: { label: "Failed", variant: "destructive" },
  stopped: { label: "Stopped", variant: "warning" },
};

// Only ASHRAE baseline and the real LLM agent simulation are exposed here --
// the generic reactive/predictive selector and the reactive-baseline
// "Run baseline comparison" job (sim/compare_runs.py) are still real,
// working code paths (backend/api.ts untouched), just not surfaced in this
// UI since the ASHRAE-vs-LLM comparison is the only one being used.
export default function RunSimulationPanel({ onComplete }: { onComplete: () => void }) {
  const [clearExisting, setClearExisting] = useState(true);
  const [status, setStatus] = useState<RunStatus>({ status: "idle" });
  const [logLines, setLogLines] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [baselineClearing, setBaselineClearing] = useState(false);
  const [baselineStatus, setBaselineStatus] = useState<{ ticks: number; totalKwh: number } | null>(null);
  const [llmClearExisting, setLlmClearExisting] = useState(true);
  const [llmClearing, setLlmClearing] = useState(false);
  const [llmStatus, setLlmStatus] = useState<{ ticks: number; totalKwh: number } | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refreshBaselineStatus = () => {
    fetchRunLog("ashrae_baseline")
      .then((rows) =>
        setBaselineStatus(
          rows.length > 0 ? { ticks: rows.length, totalKwh: rows.reduce((sum, r) => sum + r.ai_kwh, 0) } : null,
        ),
      )
      .catch(() => {});
  };

  const refreshLlmStatus = () => {
    fetchRunLog("llm")
      .then((rows) =>
        setLlmStatus(
          rows.length > 0 ? { ticks: rows.length, totalKwh: rows.reduce((sum, r) => sum + r.ai_kwh, 0) } : null,
        ),
      )
      .catch(() => {});
  };

  useEffect(() => {
    fetchRunStatus().then(setStatus).catch(() => {});
    refreshBaselineStatus();
    refreshLlmStatus();
  }, []);

  useEffect(() => {
    if (status.status !== "running") {
      if (pollRef.current) clearInterval(pollRef.current);
      return;
    }
    pollRef.current = setInterval(async () => {
      const next = await fetchRunStatus().catch(() => null);
      if (!next) return;
      setStatus(next);
      fetchRunLogTail(10).then((r) => setLogLines(r.lines)).catch(() => {});
      if (next.status !== "running") {
        onComplete();
        refreshBaselineStatus();
        refreshLlmStatus();
      }
    }, 2000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [status.status, onComplete]);

  const runAshraeBaseline = async () => {
    setError(null);
    try {
      await startSimulation("ashrae_baseline", clearExisting);
      setStatus({ status: "running", strategy: "ashrae_baseline", elapsed_s: 0 });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const clearBaselineData = async () => {
    if (!confirm('Delete the ASHRAE baseline run data? Other strategies\' baseline comparison lines will go flat until it\'s re-run.')) return;
    setError(null);
    setBaselineClearing(true);
    try {
      await clearData("ashrae_baseline");
      setBaselineStatus(null);
      onComplete();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBaselineClearing(false);
    }
  };

  const runLlm = async () => {
    setError(null);
    try {
      await startSimulation("llm", llmClearExisting);
      setStatus({ status: "running", strategy: "llm", elapsed_s: 0 });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const clearLlmData = async () => {
    if (!confirm("Delete the LLM agent run data? This can't be undone.")) return;
    setError(null);
    setLlmClearing(true);
    try {
      await clearData("llm");
      setLlmStatus(null);
      onComplete();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLlmClearing(false);
    }
  };

  const stop = async () => {
    setError(null);
    try {
      await stopSimulation();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const clearAll = async () => {
    if (!confirm("Delete ALL logged run data (every strategy)? This can't be undone.")) return;
    setError(null);
    setClearing(true);
    try {
      await clearData("all");
      onComplete();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setClearing(false);
    }
  };

  const isRunning = status.status === "running";
  const badge = STATUS_BADGE[status.status];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        {isRunning && (
          <Button variant="destructive" onClick={stop}>
            <Square className="fill-current" />
            Stop
          </Button>
        )}

        {status.status !== "idle" && (
          <Badge variant={badge.variant} className="gap-1.5 py-1.5">
            {isRunning && <Loader2 className="size-3 animate-spin" />}
            {badge.label}
            {status.strategy && ` (${status.strategy})`}
            {status.elapsed_s != null && ` · ${status.elapsed_s.toFixed(0)}s`}
            {status.status === "failed" && ` · exit ${status.returncode}`}
          </Badge>
        )}

        {isRunning && (
          <p className="text-xs text-muted-foreground">
            This calls EnergyPlus + Ollama for real, not a canned demo — the log below streams live.
          </p>
        )}

        <div className="ml-auto flex items-center gap-4">
          <div className="flex items-center gap-2">
            <Switch checked={clearExisting} onCheckedChange={setClearExisting} disabled={isRunning} id="clear-existing" />
            <Label htmlFor="clear-existing" className="text-xs font-normal text-muted-foreground">
              Clear ASHRAE baseline's old data first
            </Label>
          </div>
          <Button variant="outline" size="sm" onClick={clearAll} disabled={isRunning || clearing}>
            <Trash2 />
            {clearing ? "Clearing…" : "Clear all"}
          </Button>
        </div>
      </div>

      <div className="space-y-2 rounded-lg border border-border bg-secondary/20 p-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <Building2 className="size-4 text-muted-foreground" />
            <p className="text-sm font-medium">ASHRAE 90.1 Baseline</p>
          </div>
          <Button variant="outline" size="sm" onClick={runAshraeBaseline} disabled={isRunning}>
            <Play className="fill-current" />
            Run ASHRAE baseline
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={clearBaselineData}
            disabled={isRunning || baselineClearing || !baselineStatus}
          >
            <Trash2 />
            {baselineClearing ? "Clearing…" : "Clear baseline data"}
          </Button>
          <Badge variant={baselineStatus ? "success" : "outline"} className="gap-1.5 py-1.5">
            {baselineStatus
              ? `${baselineStatus.ticks} ticks recorded · ${baselineStatus.totalKwh.toFixed(1)} kWh total`
              : "Not yet run"}
          </Badge>
        </div>
        <p className="text-xs text-muted-foreground">
          Fixed, code-minimum operating schedule (constant setpoints on a clock, no occupancy sensing, forecasting,
          or AI) — the industry-standard "no controls credit" reference point buildings are compared against. Run
          this once; every strategy below then gets a real, live-updating baseline comparison line, tick by tick,
          without needing a separate stitching step.
        </p>
      </div>

      <div className="space-y-2 rounded-lg border border-border bg-secondary/20 p-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <Bot className="size-4 text-muted-foreground" />
            <p className="text-sm font-medium">LLM Agent Simulation</p>
          </div>
          <Button variant="outline" size="sm" onClick={runLlm} disabled={isRunning}>
            <Play className="fill-current" />
            Run LLM simulation
          </Button>
          <Button variant="outline" size="sm" onClick={clearLlmData} disabled={isRunning || llmClearing || !llmStatus}>
            <Trash2 />
            {llmClearing ? "Clearing…" : "Clear LLM data"}
          </Button>
          <Badge variant={llmStatus ? "success" : "outline"} className="gap-1.5 py-1.5">
            {llmStatus ? `${llmStatus.ticks} ticks recorded · ${llmStatus.totalKwh.toFixed(1)} kWh total` : "Not yet run"}
          </Badge>
          <div className="ml-auto flex items-center gap-2">
            <Switch
              checked={llmClearExisting}
              onCheckedChange={setLlmClearExisting}
              disabled={isRunning}
              id="clear-existing-llm"
            />
            <Label htmlFor="clear-existing-llm" className="text-xs font-normal text-muted-foreground">
              Clear old LLM data first
            </Label>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          The real multi-agent pipeline (agents/*.py + MCP + local Ollama) — this is the "normal" simulation the
          ASHRAE baseline above is meant to be compared against. Independent of the baseline: run, clear, or re-run
          either one without touching the other. Requires Ollama running (
          <code className="rounded bg-muted px-1 py-0.5">ollama serve</code>) with the model pulled (
          <code className="rounded bg-muted px-1 py-0.5">ollama pull llama3.2:3b</code>) — check the log below if a
          run fails.
        </p>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {(isRunning || logLines.length > 0) && (
        <pre className="max-h-32 overflow-auto rounded-md border border-border bg-black/30 p-3 font-mono text-[11px] leading-relaxed text-muted-foreground">
          {logLines.length > 0 ? logLines.join("\n") : "waiting for output…"}
        </pre>
      )}
    </div>
  );
}
