// 站点数据(商业化 B1):每 2 秒拉一次;事件流(/api/events)来了新东西立刻再拉。
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "./api";
import type { Alert, DeterSession, Mode, Robot, Summary } from "./model";

export interface SiteData {
  robots: Robot[];
  alerts: Alert[];
  mode: Mode | null;
  /** ``null`` = 这一次没拿到(说不清有没有驱离),不是「没有驱离」(B 阶段外审 I5)。 */
  deter: DeterSession[] | null;
  summary: Summary | null;
}

/** 跟站点的连接(B 阶段外审 I5):最后一次核心数据(狗、告警)拿成功的时刻;现在是不是拿不到。 */
export interface Conn {
  okAt: number | null;
  failingSince: number | null;
}

const POLL_MS = 2000;
/** 核心数据多久没刷新成功就算过期(两三拍)。 */
export const STALE_AFTER_MS = 6000;

/** 画面上的数据还算不算「现在」:拿不到了,或者太久没拿到新的,都算过期。 */
export function isStale(conn: Conn, now: number): boolean {
  return conn.okAt == null || conn.failingSince != null || now - conn.okAt > STALE_AFTER_MS;
}

/** 拉一次。核心(狗、告警)拿不到整个算失败;次要的(模式、驱离、电量)拿不到记成「说不清」(null)。 */
export async function fetchSite(): Promise<SiteData> {
  const [r, a, m, d, s] = await Promise.all([
    api<{ robots: Robot[] }>("GET", "/api/robots"),
    api<{ alerts: Alert[] }>("GET", "/api/alerts"),
    api<Mode>("GET", "/api/mode").catch(() => null),
    api<{ sessions: DeterSession[] }>("GET", "/api/deterrence").then((x) => x.sessions).catch(() => null),
    api<Summary>("GET", "/api/watch/summary").catch(() => null),
  ]);
  return { robots: r.robots, alerts: a.alerts, mode: m, deter: d, summary: s };
}

export function useSiteData(): [SiteData | null, () => Promise<void>, Conn] {
  const [data, setData] = useState<SiteData | null>(null);
  const [conn, setConn] = useState<Conn>({ okAt: null, failingSince: null });
  const busy = useRef(false);
  const refresh = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      setData(await fetchSite());
      setConn({ okAt: Date.now(), failingSince: null });
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401) console.warn(e);
      // 旧的数据留着(标明时刻),但一定要让界面知道它过期了
      setConn((c) => (c.failingSince != null ? c : { ...c, failingSince: Date.now() }));
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
  return [data, refresh, conn];
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
