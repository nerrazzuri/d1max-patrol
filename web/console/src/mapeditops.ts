// 地图编辑器的操作栈(W44 第四期,设计 §6):橡皮擦、画障碍(栅格那一半,存成新的一版),区域名、特殊地面、摆正、底图
// (显示那一半,跟着版本存)。撤销、重做;另存之前估一下擦了多少、画了多少。不碰 DOM,好测。
import type { Pt } from "./mapview";

export type GridOp = { op: "erase" | "obstacle"; shape: "stroke" | "line" | "rect"; points: Pt[]; width_m: number };
export type TerrainKind = "stairs" | "step" | "slope" | "grass" | "gravel";
export interface Terrain { id: string; kind: TerrainKind; polygon: Pt[]; slow_mps: number | null }
export interface Basemap { file: string; x: number; y: number; scale: number; rotate_deg: number; opacity: number }
export interface Display { rotate_deg: number | null; basemap: Basemap | null; regions: { name: string; polygon: Pt[] }[]; terrain: Terrain[] }
export interface EditState { ops: GridOp[]; display: Display }

/** 楼梯、台阶、斜坡默认放慢 0.3 m/s,草地、碎石路默认不放慢(设计 §11 第 16 条)。 */
export const SLOW_DEFAULT: Record<TerrainKind, number | null> = { stairs: 0.3, step: 0.3, slope: 0.3, grass: null, gravel: null };

export interface History { past: EditState[]; now: EditState; future: EditState[] }

export function start(display: Display): History {
  return { past: [], now: { ops: [], display }, future: [] };
}

export function apply(h: History, f: (s: EditState) => EditState): History {
  return { past: [...h.past, h.now].slice(-200), now: f(h.now), future: [] };
}

export function undo(h: History): History {
  if (!h.past.length) return h;
  return { past: h.past.slice(0, -1), now: h.past[h.past.length - 1], future: [h.now, ...h.future] };
}

export function redo(h: History): History {
  if (!h.future.length) return h;
  return { past: [...h.past, h.now], now: h.future[0], future: h.future.slice(1) };
}

/** 一个操作大概盖了多少平方米(给人确认用,不是精确值;站点回的才准)。 */
export function areaOf(o: GridOp): number {
  if (o.shape === "rect") {
    const [[x0, y0], [x1, y1]] = o.points;
    return Math.abs(x1 - x0) * Math.abs(y1 - y0);
  }
  let len = 0;
  for (let i = 1; i < o.points.length; i++) len += Math.hypot(o.points[i][0] - o.points[i - 1][0], o.points[i][1] - o.points[i - 1][1]);
  return len * o.width_m + Math.PI * (o.width_m / 2) ** 2;
}

export function summary(s: EditState): { erased: number; obstacle: number } {
  const sum = (k: GridOp["op"]) => Math.round(s.ops.filter((o) => o.op === k).reduce((a, o) => a + areaOf(o), 0) * 10) / 10;
  return { erased: sum("erase"), obstacle: sum("obstacle") };
}

/** 新的特殊地面:id 不重,按种类给默认的放慢。 */
export function newTerrain(have: Terrain[], kind: TerrainKind, polygon: Pt[]): Terrain {
  let n = have.length + 1;
  while (have.some((t) => t.id === `t${n}`)) n++;
  return { id: `t${n}`, kind, polygon, slow_mps: SLOW_DEFAULT[kind] };
}
