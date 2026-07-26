import { useEffect, useRef, useState } from "react";
import { fetchLatestDecisions, fetchLiveState, fetchPipelineEvents, fetchRunStatus } from "../api";
import type { Decision, LiveState, PipelineEventItem, RunStatus } from "../types";

const MAX_EVENTS = 400;
const POLL_MS = 1000;

/** Polls the real backend (not a WebSocket, matching the rest of this app's
 * live-ish endpoints) for the latest per-tick snapshot and the incremental
 * pipeline-event stream, so the Operations Console panels can animate the
 * actual closed loop as it executes. since_id resets whenever a fresh run
 * is detected (any transition into "running"), since a new run's
 * PipelineEvent ids can restart from 1 after a table clear -- and also
 * whenever run-status's data_epoch changes, since a plain "clear data"
 * action (no new run) still deletes every PipelineEvent row server-side,
 * and without this the local cache below would keep showing the deleted
 * events forever. */
export function useOperationsConsole() {
  const [liveState, setLiveState] = useState<LiveState>({ stale: true });
  const [events, setEvents] = useState<PipelineEventItem[]>([]);
  const [runStatus, setRunStatus] = useState<RunStatus>({ status: "idle" });
  const [latestDecisions, setLatestDecisions] = useState<Record<string, Decision>>({});
  const sinceIdRef = useRef(0);
  const prevStatusRef = useRef<RunStatus["status"] | null>(null);
  const prevEpochRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const [state, status] = await Promise.all([fetchLiveState(), fetchRunStatus()]);
        if (cancelled) return;
        setLiveState(state);
        setRunStatus(status);

        const freshRun = status.status === "running" && prevStatusRef.current !== "running";
        const dataCleared = status.data_epoch != null && prevEpochRef.current != null && status.data_epoch !== prevEpochRef.current;
        if (freshRun || dataCleared) {
          sinceIdRef.current = 0;
          setEvents([]);
          setLatestDecisions({});
        }
        prevStatusRef.current = status.status;
        if (status.data_epoch != null) prevEpochRef.current = status.data_epoch;

        const page = await fetchPipelineEvents(sinceIdRef.current, undefined, 200);
        if (cancelled) return;
        if (page.events.length > 0) {
          sinceIdRef.current = page.events[page.events.length - 1].id;
          setEvents((prev) => [...prev, ...page.events].slice(-MAX_EVENTS));
        }

        const decisionsPage = await fetchLatestDecisions();
        if (cancelled) return;
        setLatestDecisions(decisionsPage.zones);
      } catch {
        // transient fetch failure -- next poll retries, nothing to surface here
      }
    };

    poll();
    const id = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return { liveState, events, runStatus, latestDecisions };
}
