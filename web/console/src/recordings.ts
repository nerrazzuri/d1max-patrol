// 录像页的算术(C-B3):一天的范围、哪一段盖住某一刻、有录像的时间段、下一段。不碰 DOM,好测。
// 站点的录像是一段一段的(每段 segment_s 秒,按开头时刻登记);时间轴按**当地的一天**画。

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

/** 从一条告警去录像页:那台狗、告警前 10 秒起。 */
export function recordingLink(a: { robot: string; first_ms: number }): string {
  return `#/recordings?robot=${encodeURIComponent(a.robot)}&t=${Math.max(1, a.first_ms - 10_000)}`;
}
