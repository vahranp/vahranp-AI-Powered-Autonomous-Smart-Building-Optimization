const MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function formatSimClock(clock?: { month: number; day: number; hour: number; minute: number }): string {
  if (!clock) return "—";
  const hour = clock.hour % 24;
  const minute = Math.min(59, clock.minute);
  return `${MONTHS[clock.month] ?? ""} ${clock.day}, ${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
}

export function formatClockTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

export function timeAgo(ageS?: number): string {
  if (ageS == null) return "—";
  if (ageS < 1) return "just now";
  if (ageS < 60) return `${ageS.toFixed(0)}s ago`;
  return `${(ageS / 60).toFixed(1)}m ago`;
}
