import { useEffect, useRef, useState } from "react";
import type { RunTick } from "../types";

const MAX_POINTS = 60;
const RECONNECT_DELAY_MS = 2000;

export function useLiveFeed() {
  const [liveTicks, setLiveTicks] = useState<RunTick[]>([]);
  const [connected, setConnected] = useState(false);
  const lastTickRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (cancelled) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${proto}://localhost:8000/ws/live`);

      socket.onopen = () => setConnected(true);
      socket.onclose = () => {
        // Without this, any transient disconnect (a backend restart, a
        // network blip, the laptop sleeping) permanently froze liveTicks at
        // whatever it last held -- every run started after that point would
        // silently fail to update the live charts at all, since nothing
        // ever tried to re-open the socket. Reconnect on a short delay
        // until the component unmounts.
        setConnected(false);
        if (!cancelled) reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
      };
      socket.onmessage = (event) => {
        const tick: RunTick = JSON.parse(event.data);
        // Tick numbers only ever increase within one run -- a same-or-lower
        // tick number than the last one we saw means a new run started (or
        // /ws/live switched to a different strategy's ticks). Without this,
        // the previous run's tail ticks stay in the buffer and get plotted
        // alongside the new run's first few ticks as if they were one
        // continuous series (duplicate/non-monotonic x-axis labels, and a
        // baseline that appears to cliff-drop at the seam since a fresh
        // unstitched run's baseline_kwh is 0 while the old one wasn't).
        const isNewRun = lastTickRef.current != null && tick.tick <= lastTickRef.current;
        lastTickRef.current = tick.tick;
        setLiveTicks((prev) => [...(isNewRun ? [] : prev.slice(-(MAX_POINTS - 1))), tick]);
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, []);

  return { liveTicks, connected };
}
