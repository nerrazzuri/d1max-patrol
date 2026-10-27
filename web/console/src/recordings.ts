// 录像页的算术(C-B3):一天的范围、哪一段盖住某一刻、有录像的时间段、下一段。不碰 DOM,好测。
// 站点的录像是一段一段的(每段 segment_s 秒,按开头时刻登记);时间轴按**当地的一天**画。

import { forGuard, type Alert } from "./model";

export interface Recording {
  id: number;
  robot_id: string;
  camera: string;
  start_ms: number;
  bytes: number;
  keep: number;
}

/** 当地日期 `YYYY-MM-DD`(日期框用的)。 */
export function dateInput(ms: number): string {
  const d = new Date(ms);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
}

/** 当地那一天的头和尾(毫秒,尾不含)。看不懂的日期回 null。夏令时那两天不是 24 小时,照实算。 */
export function dayRange(date: string): { since: number; until: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!m) return null;
  const y = Number(m[1]), mo = Number(m[2]) - 1, d = Number(m[3]);
  const since = new Date(y, mo, d).getTime();
  const until = new Date(y, mo, d + 1).getTime();
  const got = new Date(since);
  if (Number.isNaN(since) || got.getDate() !== d || got.getMonth() !== mo) return null; // 2 月 30 日、13 月:不收
  return { since, until };
}

/** 24 小时制的 `HH:MM` 或 `HH:MM:SS` → 这一天的第几秒。看不懂、出界的回 null。 */
export function clockSeconds(text: string): number | null {
  const m = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(text.trim());
  if (!m) return null;
  const h = Number(m[1]), mi = Number(m[2]), s = Number(m[3] ?? "0");
  return h > 23 || mi > 59 || s > 59 ? null : (h * 60 + mi) * 60 + s;
}

/** 按开头时刻从早到晚(站点给的是新的在前)。 */
export function ascending(rows: Recording[]): Recording[] {
  return [...rows].sort((a, b) => a.start_ms - b.start_ms || a.id - b.id);
}

/** 有录像的时间段:挨着的(差不到 2 秒)并成一段。`rows` 要是从早到晚的。 */
export function spans(rows: Recording[], segS: number): { from: number; to: number }[] {
  const out: { from: number; to: number }[] = [];
  for (const r of rows) {
    const to = r.start_ms + segS * 1000;
    const last = out[out.length - 1];
    if (last && r.start_ms - last.to < 2000) last.to = Math.max(last.to, to);
    else out.push({ from: r.start_ms, to });
  }
  return out;
}

/** 盖住 `ms` 那一刻的那一段,和从它开头算起第几秒。没有盖住的:回 null(那一刻没录)。 */
export function segmentAt(rows: Recording[], ms: number, segS: number): { rec: Recording; offsetS: number } | null {
  let hit: Recording | null = null;
  for (const r of rows) {
    if (r.start_ms <= ms && ms < r.start_ms + segS * 1000) hit = r; // 重叠时取开头晚的那段
  }
  return hit ? { rec: hit, offsetS: (ms - hit.start_ms) / 1000 } : null;
}

/** `ms` 之后(含)最早开头的一段:点在空档上时往后找最近的。 */
export function firstAfter(rows: Recording[], ms: number): Recording | null {
  return rows.find((r) => r.start_ms >= ms) ?? null;
}

/** 这一段后面那一段(放完自动接着放)。 */
export function nextSegment(rows: Recording[], cur: Recording): Recording | null {
  const i = rows.findIndex((r) => r.id === cur.id);
  return i >= 0 && i + 1 < rows.length ? rows[i + 1] : null;
}

export function prevSegment(rows: Recording[], cur: Recording): Recording | null {
  const i = rows.findIndex((r) => r.id === cur.id);
  return i > 0 ? rows[i - 1] : null;
}

/** 时间轴上的横坐标:这一天过了多少分钟(0–1440,出界的夹住)。 */
export function minuteOf(ms: number, since: number): number {
  return Math.min(1440, Math.max(0, (ms - since) / 60_000));
}

/** 一共录了多久(毫秒),只算落在这一天里的部分。 */
export function covered(sp: { from: number; to: number }[], since: number, until: number): number {
  return sp.reduce((s, x) => s + Math.max(0, Math.min(x.to, until) - Math.max(x.from, since)), 0);
}

/** 「留着」的回包怎么用(C-B3 外审 I1):只改**问的那一段** —— 列表里那一行,和正在放的那一段(如果还是它)。
 * 回来的不是问的那一段、选择已经换了(`current` 是 false):什么都不动。绝不因为一个回包换掉正在放的段。 */
export function applyKeep<S extends { rows: Recording[] | null; cur: Recording | null }>(s: S, reqId: number, got: Recording, current: boolean): S {
  if (!current || got.id !== reqId) return s;
  return {
    ...s,
    rows: s.rows && s.rows.map((r) => (r.id === reqId ? got : r)),
    cur: s.cur && s.cur.id === reqId ? got : s.cur,
  };
}

/** 这一段是不是现在选的这台狗、这一路的:不是就不放、不往画面上写字(证据不许标错来源)。 */
export function belongs(rec: Recording | null, robot: string, cam: string): rec is Recording {
  return rec != null && rec.robot_id === robot && rec.camera === cam;
}

/** 从一条告警去录像页:那台狗、告警前 10 秒起。 */
export function recordingLink(a: { robot: string; first_ms: number }): string {
  return `#/recordings?robot=${encodeURIComponent(a.robot)}&t=${Math.max(1, a.first_ms - 10_000)}`;
}

/** 录像页地图上的一个轨迹点(站点 ``/api/robots/{id}/track``;时刻是机器人的钟,跟录像同一个钟)。 */
export interface TrackPt { t: number; map_id: string; map_version: string; x: number; y: number }

/** 跟上一个点差太久就算那一刻不知道在哪(站点没挪也每 10 秒记一个,两倍留余量)。 */
export const MAX_GAP_MS = 20_000;

/** ``t`` 那一刻机器人在哪:取 ≤ t 的最后一个点;离它超过 {@link MAX_GAP_MS} 就是不知道。 */
export function poseAt(points: TrackPt[], t: number): TrackPt | null {
  let hit: TrackPt | null = null;
  for (const p of points) {
    if (p.t > t) break;
    hit = p;
  }
  return hit && t - hit.t <= MAX_GAP_MS ? hit : null;
}

/** 点了轨迹:离点的那一处最近(3 米以内)、在同一张图上的那个点的时刻;没有回 null。 */
export function nearestTime(points: TrackPt[], x: number, y: number, map: { map_id: string; version: string }): number | null {
  let best: TrackPt | null = null;
  let d = 3;
  for (const p of points) {
    if (p.map_id !== map.map_id || p.map_version !== map.version) continue;
    const e = Math.hypot(p.x - x, p.y - y);
    if (e <= d) { d = e; best = p; }
  }
  return best ? best.t : null;
}

export interface Lane { robot: string; segs: [number, number][]; marks: { t: number; kind: "alarm" | "help" }[] }

/** 时间轴每台一行:有录像的段、报警(有人闯入、起火)和需要帮忙(帮忙、掉线)的记号;技术类不标。 */
export function lanes(robots: string[], recs: Recording[], alerts: Alert[], day: { since: number; until: number }, segS: number): Lane[] {
  return robots.map((robot) => {
    const sp = spans(ascending(recs.filter((r) => r.robot_id === robot)), segS);
    const marks = alerts.filter((a) => a.robot === robot && a.first_ms >= day.since && a.first_ms < day.until && forGuard(a))
      .map((a) => ({ t: a.first_ms, kind: (a.audience === "intrusion" || a.audience === "fire" ? "alarm" : "help") as "alarm" | "help" }))
      .sort((a, b) => a.t - b.t);
    return { robot, segs: sp.map((s): [number, number] => [s.from, s.to]), marks };
  });
}

/** 时间轴拉宽拉窄:新宽度 = 现在 × ``factor``(夹在 5 分钟到一整天),``at`` 那一刻留在原来的位置;不出这一天。 */
export function viewSpan(day: { since: number; until: number }, cur: { since: number; until: number }, at: number, factor: number): { since: number; until: number } {
  const full = day.until - day.since;
  const w = Math.min(full, Math.max(300_000, (cur.until - cur.since) * factor));
  const f = (at - cur.since) / (cur.until - cur.since);
  let since = at - f * w;
  since = Math.min(day.until - w, Math.max(day.since, since));
  return { since, until: since + w };
}
