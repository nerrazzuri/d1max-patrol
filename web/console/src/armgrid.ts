// 布防时间页的时间段运算(W44 第二期,设计 §5.1「布防时间」)。不碰 DOM,好测。分钟为单位,0–1440;zone '' = 全部防区一样。
export interface Slot { zone: string; day: number; start: number; end: number }
export interface WireSlot { zone: string; day: number; start: string; end: string }

/** 拖的时候吸到 30 分钟。 */
export const STEP = 30;

export function snap(m: number): number {
  return Math.max(0, Math.min(1440, Math.round(m / STEP) * STEP));
}

const same = (a: Slot, zone: string, day: number) => a.zone === zone && a.day === day;
const order = (a: Slot, b: Slot) => a.zone.localeCompare(b.zone) || a.day - b.day || a.start - b.start;

/** 加一段(倒着拖也行);跟同一防区同一天相交、相接的合并;短于 30 分钟的不加。 */
export function addBlock(slots: Slot[], zone: string, day: number, a: number, b: number): Slot[] {
  let start = snap(Math.min(a, b));
  let end = snap(Math.max(a, b));
  if (end - start < STEP) return slots;
  const keep: Slot[] = [];
  for (const x of slots) {
    if (same(x, zone, day) && x.start <= end && x.end >= start) {
      start = Math.min(start, x.start);
      end = Math.max(end, x.end);
    } else keep.push(x);
  }
  return [...keep, { zone, day, start, end }].sort(order);
}

/** 删掉含 ``at`` 那一刻的那一段。 */
export function removeBlock(slots: Slot[], zone: string, day: number, at: number): Slot[] {
  return slots.filter((x) => !(same(x, zone, day) && x.start <= at && at < x.end));
}

/** 拖一段的头或尾到 ``to``:吸到 30 分钟,不越过邻段,不短于 30 分钟。 */
export function moveEdge(slots: Slot[], zone: string, day: number, start: number, edge: "start" | "end", to: number): Slot[] {
  const me = slots.find((x) => same(x, zone, day) && x.start === start);
  if (!me) return slots;
  const others = slots.filter((x) => same(x, zone, day) && x !== me);
  const v = snap(to);
  let next: Slot;
  if (edge === "start") {
    const floor = Math.max(0, ...others.filter((x) => x.end <= me.start).map((x) => x.end));
    next = { ...me, start: Math.min(Math.max(v, floor), me.end - STEP) };
  } else {
    const ceil = Math.min(1440, ...others.filter((x) => x.start >= me.end).map((x) => x.start));
    next = { ...me, end: Math.max(Math.min(v, ceil), me.start + STEP) };
  }
  return slots.map((x) => (x === me ? next : x)).sort(order);
}

/** 把 ``from`` 那天的段复制到 ``toDays``(盖掉那几天原来的)。 */
export function copyDay(slots: Slot[], zone: string, from: number, toDays: number[]): Slot[] {
  const src = slots.filter((x) => same(x, zone, from));
  const targets = toDays.filter((d) => d !== from);
  const keep = slots.filter((x) => !(x.zone === zone && targets.includes(x.day)));
  return [...keep, ...targets.flatMap((d) => src.map((x) => ({ ...x, day: d })))].sort(order);
}

const hm = (m: number) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
const mins = (s: string) => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };

export function fromWire(w: WireSlot[]): Slot[] {
  return w.map((x) => ({ zone: x.zone, day: x.day, start: mins(x.start), end: mins(x.end) })).sort(order);
}

export function toWire(s: Slot[]): WireSlot[] {
  return s.map((x) => ({ zone: x.zone, day: x.day, start: hm(x.start), end: hm(x.end) }));
}

export { hm as minutesText };
