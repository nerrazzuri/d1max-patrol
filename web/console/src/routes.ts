// 巡检路线(2026-10-14):站点存的路线是唯一真理源。这里是类型、点的增删挪调序(纯函数,测试直接比坐标),
// 以及「屏上一点 → 地图坐标」「拖朝向」的换算。网络、画图在 routepage.tsx。
import type { Screen } from "./mapcanvas";
import { heading, toPx, type PreviewMeta, type Pt } from "./mapview";

export type RouteAction =
  | { type: "dwell"; seconds: number }
  | { type: "photo"; camera: "front" | "back" }
  | { type: "light"; on: boolean };

export interface RoutePoint {
  name: string;
  x: number;
  y: number;
  /** 弧度,地图系,逆时针。 */
  yaw: number;
  check?: string;
  actions?: RouteAction[];
}

export interface Route {
  id: string;
  map_id: string;
  map_version: string;
  revision: number;
  bound: boolean;
  waypoints: RoutePoint[];
  policy: Record<string, unknown>;
  updated_by: string;
  updated_ms: number;
}

export interface RouteSchedule {
  id: string;
  mission: string;
  at: string;
  days: string[];
  window_min: number;
  on_missed: "skip" | "run_late" | "alarm";
  priority?: number;
  robot?: string;
  every_min?: number;
  until?: string;
  revision: number;
}

export interface RouteView {
  revision: number;
  timezone: string | null;
  routes: Route[];
  schedules: RouteSchedule[];
}

/** 站点逐点校验没过时回的(400 的 errors)。index 为 null = 整条的问题。 */
export interface PointProblem { index: number | null; name: string; problem: string; segment?: [number, number] }

/** 编辑中的一条:isNew 的还没存过(base = 0)。 */
export interface Draft {
  id: string;
  isNew: boolean;
  base: number;
  points: RoutePoint[];
  policy: Record<string, unknown>;
  /** 在哪张图上编的(``map_id\u0000version``;外审 R2):存的时候跟当前选的图核对,对不上不发。 */
  map?: string;
}

const round = (v: number, d: number) => Math.round(v * 10 ** d) / 10 ** d;

/** 预览图上的像素 → 地图坐标(米,两位小数)。 */
export function pxToMap(meta: PreviewMeta, px: number, py: number): Pt {
  return [round(meta.left_x + px * meta.m_per_px, 2), round(meta.top_y - py * meta.m_per_px, 2)];
}

/** 从 (fx, fy) 朝 (tx, ty) 看的朝向(弧度,地图系)。两点重合 → null(朝向没法定)。 */
export function yawToward(fx: number, fy: number, tx: number, ty: number): number | null {
  if (Math.hypot(tx - fx, ty - fy) < 1e-6) return null;
  return round(Math.atan2(ty - fy, tx - fx), 4);
}

export const degOf = (yaw: number) => round((yaw * 180) / Math.PI, 1);
export const radOf = (deg: number) => round((deg * Math.PI) / 180, 4);

/** 新点的名字:P1、P2…,不跟已有的重名(照片按点位名归档,重名会互相覆盖)。 */
export function nextName(points: RoutePoint[]): string {
  const have = new Set(points.map((p) => p.name));
  for (let i = points.length + 1; ; i++) if (!have.has(`P${i}`)) return `P${i}`;
}

/** 在末尾加一个点;朝向 = 从上一个点走过来的方向(第一个点朝 +x)。 */
export function addPoint(points: RoutePoint[], x: number, y: number): RoutePoint[] {
  const last = points[points.length - 1];
  const yaw = (last && yawToward(last.x, last.y, x, y)) ?? last?.yaw ?? 0;
  return [...points, { name: nextName(points), x, y, yaw }];
}

export function movePoint(points: RoutePoint[], i: number, x: number, y: number): RoutePoint[] {
  return points.map((p, k) => (k === i ? { ...p, x, y } : p));
}

export function patchPoint(points: RoutePoint[], i: number, patch: Partial<RoutePoint>): RoutePoint[] {
  return points.map((p, k) => (k === i ? { ...p, ...patch } : p));
}

export function deletePoint(points: RoutePoint[], i: number): RoutePoint[] {
  return points.filter((_, k) => k !== i);
}

/** 把第 from 个挪到第 to 个的位置。 */
export function reorder(points: RoutePoint[], from: number, to: number): RoutePoint[] {
  if (from === to || from < 0 || to < 0 || from >= points.length || to >= points.length) return points;
  const out = points.slice();
  const [p] = out.splice(from, 1);
  out.splice(to, 0, p);
  return out;
}

/** 点上的动作有没有某种(拍照按相机分)。 */
export function hasAction(p: RoutePoint, type: RouteAction["type"], camera?: string): boolean {
  return (p.actions ?? []).some((a) => a.type === type && (a.type !== "photo" || a.camera === camera));
}

export function toggleAction(p: RoutePoint, action: RouteAction): RoutePoint {
  const cur = p.actions ?? [];
  const key = (a: RouteAction) => (a.type === "photo" ? `photo:${a.camera}` : a.type);
  const same = (a: RouteAction) => key(a) === key(action);
  const next = cur.some(same) ? cur.filter((a) => !same(a)) : [...cur, action];
  const { actions: _drop, ...rest } = p;
  return next.length ? { ...rest, actions: next } : rest;
}

export function setDwell(p: RoutePoint, seconds: number): RoutePoint {
  const others = (p.actions ?? []).filter((a) => a.type !== "dwell");
  const actions = seconds > 0 ? [{ type: "dwell", seconds } as RouteAction, ...others] : others;
  const { actions: _drop, ...rest } = p;
  return actions.length ? { ...rest, actions } : rest;
}

export function dwellOf(p: RoutePoint): number {
  const a = (p.actions ?? []).find((x) => x.type === "dwell");
  return a && a.type === "dwell" ? a.seconds : 0;
}

/** 屏上(客户端)一点 → 预览像素:世界层的 getScreenCTM().inverse()(转正、缩放、平移都倒回去)。 */
export function clientToPx(root: Element | null, cx: number, cy: number): Pt | null {
  const g = root?.querySelector("g.map-world") as SVGGraphicsElement | null;
  const m = g?.getScreenCTM?.();
  if (!m) return null;
  const i = m.inverse();
  return [i.a * cx + i.c * cy + i.e, i.b * cx + i.d * cy + i.f];
}

/** 屏上一点 → 地图坐标。 */
export function clientToMap(root: Element | null, meta: PreviewMeta, cx: number, cy: number): Pt | null {
  const p = clientToPx(root, cx, cy);
  return p ? pxToMap(meta, p[0], p[1]) : null;
}

/** 预览像素 → 地图坐标的反过来:地图坐标 → 预览像素(画用)。 */
export function mapToPx(meta: PreviewMeta, x: number, y: number): Pt {
  const p = toPx(meta, x, y);
  return [p.px, p.py];
}

/** 朝向把手在屏上的位置:点 (x, y) 屏上位置 + 沿 yaw 的定长。方向在预览上走一小段、跟着图一起变换,所以转正、缩放都对。 */
export function handleAt(meta: PreviewMeta, s: Screen, x: number, y: number, yaw: number, len: number): { x: number; y: number; hx: number; hy: number } {
  const q = toPx(meta, x, y);
  const [sx, sy] = s.px(q.px, q.py);
  const e = heading(q.px, q.py, yaw, 10);
  const [ex, ey] = s.px(e.hx, e.hy);
  const n = Math.hypot(ex - sx, ey - sy) || 1;
  return { x: sx, y: sy, hx: sx + ((ex - sx) / n) * len, hy: sy + ((ey - sy) / n) * len };
}
