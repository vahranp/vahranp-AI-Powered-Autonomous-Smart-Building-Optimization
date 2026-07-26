import { useEffect, useMemo, useState } from "react";
import { Pause, Play } from "lucide-react";

import type { ZoneTimeline } from "../types";
import { Button } from "./ui/button";
import { Slider } from "./ui/slider";
import FloorplanView from "./FloorplanView";

export default function ReplayScrubber({ timeline }: { timeline: ZoneTimeline }) {
  const ticks = useMemo(
    () => Object.keys(timeline).map(Number).sort((a, b) => a - b),
    [timeline],
  );
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    if (!playing || ticks.length === 0) return;
    const id = setInterval(() => {
      setIndex((i) => (i + 1) % ticks.length);
    }, 400);
    return () => clearInterval(id);
  }, [playing, ticks.length]);

  if (ticks.length === 0) {
    return <p className="text-sm text-muted-foreground">No replay data yet.</p>;
  }

  const currentTick = ticks[index];
  const snapshot = timeline[String(currentTick)];

  return (
    <div>
      <FloorplanView snapshot={snapshot} />
      <div className="mt-4 flex items-center gap-3">
        <Button variant="outline" size="icon" onClick={() => setPlaying((p) => !p)}>
          {playing ? <Pause className="fill-current" /> : <Play className="fill-current" />}
        </Button>
        <Slider
          value={[index]}
          min={0}
          max={ticks.length - 1}
          step={1}
          onValueChange={([v]) => {
            setPlaying(false);
            setIndex(v);
          }}
          className="flex-1"
        />
        <span className="w-20 text-right text-xs text-muted-foreground">tick {currentTick}</span>
      </div>
    </div>
  );
}
