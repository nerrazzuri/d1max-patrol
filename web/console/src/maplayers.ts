// 地图页的图层规则(W44 第一期,设计 §4.3)。不碰 DOM,好测。
import { hm } from "./data";
import { t } from "./i18n";
import type { Pt } from "./mapview";
import { forGuard, type Alert } from "./model";

export type Layer = "zones" | "cameras" | "route" | "standby" | "events" | "stale" | "nogo";
export const LAYERS: Layer[] = ["zones", "cameras", "route", "standby", "events", "stale", "nogo"];
/** 禁行区默认关(保安平时不用看),别的默认开。 */
export const DEFAULT_LAYERS: ReadonlySet<Layer> = new Set(LAYERS.filter((l) => l !== "nogo"));
const LAYERS_KEY = "d1max.maplayers";

export function loadLayers(): Set<Layer> {
  try {
    const raw = JSON.parse(localStorage.getItem(LAYERS_KEY) ?? "null");
    if (Array.isArray(raw)) return new Set(raw.filter((x): x is Layer => (LAYERS as string[]).includes(x)));
  } catch {
    /* 读不到就用默认 */
  }
  return new Set(DEFAULT_LAYERS);
}

export function saveLayers(s: Set<Layer>): void {
  try {
    localStorage.setItem(LAYERS_KEY, JSON.stringify([...s]));
  } catch {
    /* 存不下就只管这一次 */
  }
}

/** 多久没机器人去过算「没巡过」(设计 §11 第 4 条;第二期挪到「布防时间」页可改)。 */
export const STALE_AFTER_MS = 2 * 3_600_000;

export interface Cell { i: number; j: number; t: number }

export function staleCells(cells: Cell[], now: number, after: number): { i: number; j: number; age_ms: number }[] {
  return cells.filter((c) => now - c.t > after).map((c) => ({ i: c.i, j: c.j, age_ms: now - c.t }));
}

/** 今晚的事:给保安的、从 ``since`` 起出的,新的在前。 */
export function tonightEvents(alerts: Alert[], since: number): Alert[] {
  return alerts.filter((a) => a.first_ms >= since && forGuard(a)).sort((a, b) => b.first_ms - a.first_ms);
}

/** 本班从几点算(交班时间,当地时间,默认 7:00、19:00;设计 §4.5、§11 第 4 条)。 */
export function shiftStart(now: number, hours: [number, number] = [7, 19]): number {
  const d = new Date(now);
  const [a, b] = [...hours].sort((x, y) => x - y);
  const at = (day: number, h: number) => new Date(d.getFullYear(), d.getMonth(), day, h).getTime();
  if (now >= at(d.getDate(), b)) return at(d.getDate(), b);
  if (now >= at(d.getDate(), a)) return at(d.getDate(), a);
  return at(d.getDate() - 1, b);
}

/** 沿机器人报的规划路径(终点就是下一个巡检点)量米数 ÷ 速度,回秒;没有路径回 null。 */
export function etaNext(path: Pt[] | null | undefined, at: Pt | null | undefined, mps = 0.5): number | null {
  if (!path || path.length === 0 || !at) return null;
  let m = Math.hypot(path[0][0] - at[0], path[0][1] - at[1]);
  for (let i = 1; i < path.length; i++) m += Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]);
  return Math.round(m / mps);
}

export interface ModeView {
  mode: "armed" | "home" | "visitor";
  until_ms?: number | null;
  zones?: { zone: string; armed: boolean }[];
}

export interface AuthzRow { name: string; zones: string[]; end: string; active_now?: boolean }

/** 防区图层上的标签和颜色:布防 / 撤防 / 访客模式到几点,加上此刻生效的授权。 */
export function zoneDecor(zone: string, mode: ModeView | null, authz: AuthzRow[]): { label: string; tone: "armed" | "off" } {
  const z = mode?.zones?.find((x) => x.zone === zone);
  const armed = z ? z.armed : true;                 // 没配过的防区一律布防(站点 modes 同一条规矩)
  const parts = [zone];
  if (armed) parts.push(t("modeArmed"));
  else if (mode?.mode === "visitor") parts.push(t("armVisitor", { t: mode.until_ms ? hm(mode.until_ms) : "—" }));
  else parts.push(t("zoneDisarmed"));
  for (const a of authz) {
    if (a.active_now && (a.zones.length === 0 || a.zones.includes(zone))) parts.push(t("authzUntil", { who: a.name, t: a.end }));
  }
  return { label: parts.join(" · "), tone: armed ? "armed" : "off" };
}
