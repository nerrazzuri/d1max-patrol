// 运行可视化(规划路径、轨迹、被挡 / 重规划、定位原因、定位历史)的数据与说法。
// 文字走本文件的 L(en, zh)(跟手机的 tr 一个办法),不动 i18n.ts 的词条表。
import { useEffect, useState } from "preact/hooks";
import { api } from "./api";
import { lang } from "./i18n";
import { usePageVisible } from "./data";
import type { Pt } from "./mapview";

/** 英文为主,中文可选;{k} 换成 vars[k]。读了 lang.value,换语言会跟着重画。 */
export function L(en: string, zh: string, vars?: Record<string, string | number>): string {
  let s = lang.value === "zh" ? zh : en;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
  return s;
}

export interface BlockedInfo {
  since_ms: number;
  for_s: number;
  reason: string;
  x: number | null;
  y: number | null;
  map_id: string;
  map_version: string;
}
export interface ReplanInfo { why: string; n: number; at_ms: number }
export interface NavSummary { blocked?: BlockedInfo; replan?: ReplanInfo }

export interface VizRobot extends NavSummary {
  map: { map_id: string; version: string } | null;
  path: Pt[];
  /** 这一趟任务走过的点(W41:每趟新任务开始时站点清掉;遥控器握着控制权时不记)。 */
  trail: Pt[];
  /** 轨迹从这些下标起另起一段(中间是遥控器开的、或者没记的,不连线);老站点没有。 */
  trail_cuts?: number[];
}

/** 轨迹按断点切成几段;每段至少两个点才画得出线。 */
export function trailSegs(r: Pick<VizRobot, "trail" | "trail_cuts">): Pt[][] {
  const cuts = (r.trail_cuts ?? []).filter((i) => Number.isInteger(i) && i > 0 && i < r.trail.length).sort((a, b) => a - b);
  const out: Pt[][] = [];
  let from = 0;
  for (const c of [...cuts, r.trail.length]) {
    if (c > from) out.push(r.trail.slice(from, c));
    from = Math.max(from, c);
  }
  return out.filter((seg) => seg.length >= 2);
}
export interface VizData { robots: Record<string, VizRobot> }

/** 打开地图页时每 2 秒拉一次;页面不可见就停。拿不到就保留上一份(只是少画,不影响别的)。 */
export function useRunViz(enabled: boolean, periodMs = 2000): VizData | null {
  const [data, setData] = useState<VizData | null>(null);
  const visible = usePageVisible();
  useEffect(() => {
    if (!enabled || !visible) return;
    let live = true;
    const pull = () => api<VizData>("GET", "/api/runviz").then((d) => live && setData(d)).catch(() => {});
    pull();
    const id = window.setInterval(pull, periodMs);
    return () => {
      live = false;
      window.clearInterval(id);
    };
  }, [enabled, visible, periodMs]);
  return data;
}

// ------------------------------------------------------------------ 定位历史

export interface Episode {
  id: number;
  robot_id: string;
  kind: "lost" | "degraded";
  start_ms: number;
  end_ms: number | null;
  ongoing: boolean;
  duration_s: number;
  end_how: string;
  cause: string;
  source: string;
  sigma_m: number | null;
  task_id: string;
  map_id: string;
  map_version: string;
  x: number | null;
  y: number | null;
}

export interface EpisodeQuery { robot?: string; kind?: string; since_ms?: number; until_ms?: number; map_id?: string; map_version?: string; limit?: number }

export function episodesPath(q: EpisodeQuery): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== "") p.set(k, String(v));
  const s = p.toString();
  return `/api/loc-episodes${s ? `?${s}` : ""}`;
}

export function useEpisodes(q: EpisodeQuery, enabled = true): { rows: Episode[] | null; error: string; reload: () => void } {
  const [rows, setRows] = useState<Episode[] | null>(null);
  const [error, setError] = useState("");
  const [n, setN] = useState(0);
  const path = episodesPath(q);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    setError("");
    api<{ episodes: Episode[] }>("GET", path)
      .then((d) => live && setRows(d.episodes))
      .catch((e) => {
        if (live) {
          setRows(null);
          setError(String(e instanceof Error ? e.message : e));
        }
      });
    return () => {
      live = false;
    };
  }, [path, enabled, n]);
  return { rows, error, reload: () => setN((v) => v + 1) };
}

export function endText(how: string, ongoing: boolean): string {
  if (ongoing) return L("still ongoing", "还没恢复");
  switch (how) {
    case "recovered": return L("recovered", "恢复了");
    case "became_lost": return L("became: not localized", "转成:定位丢了");
    case "became_degraded": return L("became: degraded", "转成:定位变差");
    case "offline": return L("robot went offline (end time is not the recovery time)", "狗掉线了(结束时刻不是恢复的时刻)");
    case "no_data": return L("no data from the robot (end time is not the recovery time)", "狗的数据断了(结束时刻不是恢复的时刻)");
    case "site_restart": return L("site restarted (end time is not the recovery time)", "站点重启了(结束时刻不是恢复的时刻)");
    default: return how || "—";
  }
}

export function kindText(kind: string): string {
  return kind === "lost" ? L("Not localized", "定位丢了") : L("Degraded", "定位变差");
}

export function durText(s: number): string {
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min ${s % 60} s` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

// ------------------------------------------------------------------ 狗的一行

export interface LocView {
  source?: string;
  anchored?: boolean;
  sigma_m?: number | null;
  reason?: string;
  degraded?: string;
  quality?: number | null;
  localizer?: string;
  pose?: { x: number; y: number; yaw: number } | null;
}

export type LocState = "ok" | "degraded" | "lost" | "unset" | "unknown";

/** 定位现在什么状态。老狗没报定位块是 unknown(不猜成「没设位置」)。 */
export function locState(loc: LocView | null | undefined): LocState {
  if (!loc) return "unknown";
  if (loc.anchored !== true && !loc.reason) return "unset";
  if (loc.reason) return loc.anchored === true ? "lost" : "unset";
  return loc.degraded ? "degraded" : "ok";
}

export function sourceText(source: string | undefined): string {
  switch (source) {
    case "scan_match": return L("scan matching", "点云匹配");
    case "rtk": return "RTK";
    case "fused": return L("fused", "融合");
    case "dead_reckoning": return L("dead reckoning (localizer briefly silent)", "里程推算中(定位器一时没来)");
    case "odom_identity": return L("simulated (odometry is the position)", "仿真(里程就是位置)");
    default: return source || "—";
  }
}

/** 单行说法,跟手机 locText 同一套意思:来源、偏差、变差、为什么不可信。 */
export function locLine(loc: LocView | null | undefined): string {
  const st = locState(loc);
  if (!loc || st === "unknown") return L("Localization: not reported", "定位:没上报");
  const r = loc.reason ?? "";
  if (st === "unset") return L("Localization: position not set{r}", "定位:没设位置{r}", { r: r ? ` — ${r}` : "" });
  if (st === "lost") return L("Localization unreliable: {r}", "定位不可信:{r}", { r });
  const s = typeof loc.sigma_m === "number" ? loc.sigma_m.toFixed(1) : "?";
  const base = L("Localization: {src}, error about {s} m", "定位:{src},偏差约 {s} m", { src: sourceText(loc.source), s });
  return st === "degraded" ? `${base} · ${L("degraded: {d}", "变差:{d}", { d: loc.degraded ?? "" })}` : base;
}

export function blockedText(b: BlockedInfo): string {
  return L("Blocked {n} s: {r}", "被挡 {n} 秒:{r}", { n: b.for_s, r: b.reason || L("no reason given", "没说原因") });
}

export function replanText(r: ReplanInfo): string {
  const why: Record<string, [string, string]> = {
    off_route: ["off the route", "偏离路径"],
    zones: ["zones changed", "区域换了"],
    near_nogo: ["too close to a no-go zone", "离禁行区太近"],
    detour: ["detouring around an obstacle", "绕障"],
  };
  const [en, zh] = why[r.why] ?? why.off_route;
  return L("Re-planned {n}× ({w})", "重规划 {n} 次({w})", { n: r.n, w: lang.value === "zh" ? zh : en });
}
