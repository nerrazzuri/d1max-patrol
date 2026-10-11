// 值班日报的算术(C-B3b):时间段、CSV、几个数怎么写。不碰 DOM,好测。数是站点数的(/api/reports/daily)。
import { dayRange } from "./recordings";

export interface AlarmRow {
  key: string;
  level: string;
  kind: string;
  robot: string;
  title: string;
  first_ms: number;
  count: number;
  acked_by: string;
  acked_ms: number | null;
  resolved_by: string;
  resolved_ms: number | null;
  zone: string | null;
}

export interface IncidentRow {
  id: number;
  received_at: number;
  source: string;
  type: string;
  zone: string;
  intercept: string | null;
  outcome: string;
  robot_id: string | null;
  result: string | null;
  note: string;
}

export interface PatrolRow {
  id: number;
  entry_id: string;
  scheduled_ms: number;
  outcome: string;
  robot_id: string | null;
  result: string | null;
  note: string;
}

export interface AckStat {
  n: number;
  median_s: number | null;
  max_s: number | null;
}

export interface Daily {
  since_ms: number;
  until_ms: number;
  rows_max: number;
  alarms: {
    total: number;
    by_level: { P1: number; P2: number; P3: number };
    acked: number;
    resolved: number;
    waiting: number;
    closed_unacked: number;
    ack: { P1: AckStat; P2: AckStat };
    handlers: { name: string; acked: number; resolved: number }[];
    by_kind: { kind: string; level: string; n: number }[];
    rows: AlarmRow[];
    truncated: boolean;
  };
  incidents: { total: number; by_outcome: Record<string, number>; rows: IncidentRow[]; truncated: boolean };
  patrols: { total: number; by_outcome: Record<string, number>; started: number; by_result: Record<string, number>; rows: PatrolRow[]; truncated: boolean };
  runs: { total: number; finished: number; photos: number; by_robot: Record<string, number> };
}

/** 一天里的 `HH:MM` → 分钟;`24:00` 算一天的尽头。看不懂回 null。 */
export function minutes(hhmm: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm);
  if (!m) return null;
  const h = Number(m[1]), mi = Number(m[2]);
  if (mi > 59 || h > 24 || (h === 24 && mi > 0)) return null;
  return h * 60 + mi;
}

/** 报表盖的时间段:那一天的 from 到 to。to 不比 from 晚 = 跨夜的班(到第二天的 to)。 */
export function period(date: string, from: string, to: string): { since: number; until: number } | null {
  const day = dayRange(date);
  const a = minutes(from), b = minutes(to);
  if (!day || a == null || b == null) return null;
  const at = (min: number, plusDays: number) => {
    const d = new Date(day.since);
    d.setDate(d.getDate() + plusDays);
    d.setHours(0, min, 0, 0); // 按当地的钟点摆,夏令时那天也对
    return d.getTime();
  };
  const since = at(a, 0);
  const until = b > a ? at(b, 0) : at(b, 1);
  return until > since ? { since, until } : null;
}

/** 多少秒写成人看的:`42 s`、`1 min 50 s`、`2 h 05 min`;没有就是「—」。 */
export function secondsText(s: number | null): string {
  if (s == null) return "—";
  const n = Math.round(s);
  if (n < 60) return `${n} s`;
  if (n < 3600) return `${Math.floor(n / 60)} min ${String(n % 60).padStart(2, "0")} s`;
  return `${Math.floor(n / 3600)} h ${String(Math.floor((n % 3600) / 60)).padStart(2, "0")} min`;
}

/** 一格 CSV:有逗号、引号、换行的加引号;以 = + - @ 开头的前面垫一个撇号(表格软件不当公式算)。 */
export function csvCell(v: unknown): string {
  let s = v == null ? "" : String(v);
  if (typeof v === "string" && /^[=+\-@\t\r]/.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** 整张 CSV:开头带 BOM(Excel 认得出 UTF-8,中文不乱),行尾 CRLF。 */
export function csv(head: string[], rows: unknown[][]): string {
  return "﻿" + [head, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

/** 当地时刻 `YYYY-MM-DD HH:MM:SS`(CSV、打印用;界面上跟别处一样用 hms)。 */
export function stamp(ms: number | null): string {
  if (ms == null) return "";
  const d = new Date(ms);
  const two = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

export function alarmsCsv(rows: AlarmRow[], title: (r: AlarmRow) => string): string {
  return csv(
    ["first_raised", "priority", "kind", "event", "robot", "zone", "times_raised", "acknowledged_by", "acknowledged_at", "seconds_to_acknowledge", "closed_by", "closed_at"],
    rows.map((r) => [stamp(r.first_ms), r.level, r.kind, title(r), r.robot, r.zone ?? "", r.count, r.acked_by, stamp(r.acked_ms),
      r.acked_ms == null ? "" : Math.max(0, Math.round((r.acked_ms - r.first_ms) / 1000)), r.resolved_by, stamp(r.resolved_ms)]),
  );
}

export function incidentsCsv(rows: IncidentRow[]): string {
  return csv(["received", "source", "type", "zone", "intercept", "outcome", "robot", "result", "note"],
    rows.map((r) => [stamp(r.received_at), r.source, r.type, r.zone, r.intercept ?? "", r.outcome, r.robot_id ?? "", r.result ?? "", r.note]));
}

export function patrolsCsv(rows: PatrolRow[]): string {
  return csv(["scheduled", "schedule", "outcome", "robot", "result", "note"],
    rows.map((r) => [stamp(r.scheduled_ms), r.entry_id, r.outcome, r.robot_id ?? "", r.result ?? "", r.note]));
}

/** 文件名里的时间段:`2026-10-11_0000-2400`。 */
export function fileTag(date: string, from: string, to: string): string {
  return `${date}_${from.replace(":", "")}-${to.replace(":", "")}`;
}
