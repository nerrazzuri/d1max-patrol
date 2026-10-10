// 站点数据(商业化 B1):每 2 秒拉一次;事件流(/api/events)来了新东西立刻再拉。
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "./api";
import type { Alert, DeterSession, Mode, Robot, Summary } from "./model";

export interface SiteData {
  robots: Robot[];
  alerts: Alert[];
  mode: Mode | null;
  deter: DeterSession[];
  summary: Summary | null;
}

const POLL_MS = 2000;

export function useSiteData(): [SiteData | null, () => Promise<void>] {
  const [data, setData] = useState<SiteData | null>(null);
  const busy = useRef(false);
  const refresh = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      const [r, a, m, d, s] = await Promise.all([
        api<{ robots: Robot[] }>("GET", "/api/robots"),
        api<{ alerts: Alert[] }>("GET", "/api/alerts"),
        api<Mode>("GET", "/api/mode").catch(() => null),
        api<{ sessions: DeterSession[] }>("GET", "/api/deterrence").catch(() => ({ sessions: [] })),
        api<Summary>("GET", "/api/watch/summary").catch(() => null),
      ]);
      setData({ robots: r.robots, alerts: a.alerts, mode: m, deter: d.sessions, summary: s });
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401) console.warn(e);
    } finally {
      busy.current = false;
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    let es: EventSource | null = null;
    try {
      es = new EventSource("/api/events");
      es.onmessage = () => void refresh();
    } catch {
      es = null;
    }
    return () => {
      window.clearInterval(timer);
      es?.close();
    };
  }, [refresh]);
  return [data, refresh];
}

/** 每秒走一下的「现在」。 */
export function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

export function usePageVisible(): boolean {
  const [v, setV] = useState(() => typeof document === "undefined" || document.visibilityState !== "hidden");
  useEffect(() => {
    const on = () => setV(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return v;
}

export function hms(ms: number): string {
  return new Date(ms).toLocaleTimeString("en-GB", { hour12: false });
}

export function hm(ms: number): string {
  return hms(ms).slice(0, 5);
}
