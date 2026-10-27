// 站点回来的数据(跟手机用的夹具 mobile/test/fixtures 同形)和值班台要的派生量。

export type Level = "P1" | "P2" | "P3";

export interface Alert {
  key: string;
  kind: string;
  level: Level;
  robot: string;
  title: string;
  detail: string;
  first_ms: number;
  last_ms: number;
  count: number;
  acked_by: string;
  acked_ms: number | null;
  resolved_by?: string;
  resolved_ms: number | null;
  shelved_by?: string;
  shelved_ms?: number | null;
  shelved_until_ms?: number | null;
  shelved_reason?: string;
  /** 给谁看(W44,站点 alert_audience):intrusion / fire / help / offline 给保安,owner 给业主。老站点没有。 */
  audience?: string;
  /** 结论(W44):intruder 确认有人 / false_alarm 误报 / handled 处理好了;空 = 没说。 */
  verdict?: string;
  context?: {
    zone?: string;
    persons?: { count?: number; nearest_m?: number };
    pose?: { map_id: string; map_version: string; x: number; y: number };
  };
}

export interface Robot {
  robot_id: string;
  /** 业主起的名字(W44 第二期);没起是空串、老站点没有。 */
  name?: string;
  active: boolean;
  fresh: boolean;
  revoked: boolean;
  held: unknown;
  loc?: {
    pose?: { x: number; y: number; yaw: number } | null;
    anchored?: boolean;
    // 运行可视化:来源、偏差、质量、变差、为什么不可信(狗遥测的 loc 块,老狗没有)
    source?: string;
    sigma_m?: number | null;
    quality?: number | null;
    reason?: string;
    degraded?: string;
    localizer?: string;
  } | null;
  /** 被挡 / 重规划的状态(站点 runviz;没被挡、没重规划就没有这个键)。 */
  nav?: {
    blocked?: { since_ms: number; for_s: number; reason: string; x: number | null; y: number | null; map_id: string; map_version: string };
    replan?: { why: string; n: number; at_ms: number };
  } | null;
  status?: {
    online: boolean;
    last_seen: number;
    ready?: { estop_clear?: boolean };
    task?: { kind: string; state: string } | null;
  } | null;
  capabilities?: {
    tasks?: Record<string, Record<string, unknown>>;
  } | null;
}

export interface DeterSession {
  robot_id: string;
  level: number;
  persons?: { count?: number; nearest_m?: number };
  /** 狗**接受了**「开」、还在保持时间里的几路(老站点不发)。 */
  active?: string[];
  /** 这台狗有哪几路;null / 不发 = 不知道。 */
  outputs?: string[] | null;
}

/** 这只狗有哪几路相机(W40:装了热像的多一路;老狗不报 video.cameras 就是前后两路)。 */
export function robotCams(r: Robot | null | undefined): ("front" | "back" | "thermal")[] {
  const v = r?.capabilities?.tasks?.video as { cameras?: unknown } | undefined;
  const c = Array.isArray(v?.cameras) ? (v.cameras as unknown[]) : null;
  if (!c) return ["front", "back"];
  return (["front", "back", "thermal"] as const).filter((x) => c.includes(x));
}

/** 警笛在第 3 级(站点 deterrence.RELAYS);第 2 级只有灯和语音警告(C-B2 外审 I1)。 */
export const SIREN_LEVEL = 3;

export type SirenState = "on" | "requested" | "off" | "notFitted";

/** 一场驱离里警笛算什么状态。"on" 只在狗接受了开警笛的命令时才算:级别到了只是要求,不等于响了。 */
export function sirenState(d: DeterSession): SirenState {
  if (Array.isArray(d.outputs) && !d.outputs.includes("siren")) return "notFitted";
  if (Array.isArray(d.active) && d.active.includes("siren")) return "on";
  return d.level >= SIREN_LEVEL ? "requested" : "off";
}

export interface Summary {
  robots: { robot_id: string; battery_pct: number | null; charging?: boolean }[];
}

export interface Mode {
  mode: "armed" | "home" | "visitor";
  /** 访客模式到几点(站点 modes.view)。 */
  until_ms?: number | null;
  /** 每个防区此刻布没布防(站点 modes.view)。 */
  zones?: { zone: string; armed: boolean }[];
}

/** 给保安看的四类(W44,设计 §7)。 */
export const GUARD_AUDIENCES = new Set(["intrusion", "fire", "help", "offline"]);

/** 这条告警给不给保安看。老站点不报 audience 的一律算给保安(宁可多看)。 */
export function forGuard(a: Alert): boolean {
  return a.audience == null || GUARD_AUDIENCES.has(a.audience);
}

export type RobotState =
  | "patrolling"
  | "deterring"
  | "fell"
  | "returning"
  | "charging"
  | "idle"
  | "manual"
  | "estop"
  | "offline";

/** 一只狗现在该显示成什么状态(B0 规范「狗的状态」)。顺序有讲究:离线、急停、翻倒压过别的。 */
export function robotState(r: Robot, deter: DeterSession | undefined, charging: boolean, fell = false): RobotState {
  const st = r.status;
  if (!st || !st.online || !r.fresh) return "offline";
  if (st.ready && st.ready.estop_clear === false) return "estop";
  if (fell) return "fell";
  if (deter) return "deterring";
  if (r.held) return "manual";
  const kind = st.task?.kind ?? "";
  if (kind === "patrol" || kind === "goto") return "patrolling";
  if (kind === "standby" || kind === "return") return "returning";
  if (charging) return "charging";
  return "idle";
}

/** 没解决的告警,P1 在前,同级新的在前。 */
export function openAlerts(all: Alert[]): Alert[] {
  const rank = { P1: 0, P2: 1, P3: 2 } as const;
  return all
    .filter((a) => a.resolved_ms == null)
    .sort((a, b) => rank[a.level] - rank[b.level] || b.last_ms - a.last_ms);
}

/** 这只狗巡的是哪张图(能力里 patrol 的 map_id / map_version)。 */
/** W41:这只狗能不能「自动找位置」(拿看到的墙在整张图上套;能力 ``relocalize.global``)。 */
export function canFindPose(r: Robot | null | undefined): boolean {
  const rc = r?.capabilities?.tasks?.relocalize as { global?: unknown } | undefined;
  return rc?.global === true;
}

export function robotMap(r: Robot): { map_id: string; version: string } | null {
  const p = r.capabilities?.tasks?.patrol as { map_id?: string; map_version?: string } | undefined;
  return p?.map_id && p.map_version ? { map_id: p.map_id, version: p.map_version } : null;
}

/** 业主或工程师(临时):权限一样,只是工程师不能管账号(W44 第二期,设计 §3)。 */
export function isOwnerLike(role: string): boolean {
  return role === "admin" || role === "engineer";
}

export function canDispatch(role: string): boolean {
  return isOwnerLike(role) || role === "guard";
}

export function canArm(role: string): boolean {
  return isOwnerLike(role) || role === "guard" || role === "owner";
}

/** 异常状态(着色、带形状);别的都是中性(ISA-101)。 */
export function abnormal(s: RobotState): "p1" | "p2" | null {
  if (s === "deterring" || s === "estop" || s === "fell") return "p1";
  if (s === "offline") return "p2";
  return null;
}

export const FELL_KINDS = new Set(["fallen", "force_flipped"]);

export type AlarmState = "unacked" | "new" | "acked" | "shelved" | "resolved";

/** ISA-18.2 的报警状态:P1 没确认叫 Unacknowledged,P2 没确认叫 New。 */
export function alarmState(a: Alert, now: number): AlarmState {
  if (a.resolved_ms != null) return "resolved";
  if (a.shelved_until_ms != null && now < a.shelved_until_ms) return "shelved";
  if (a.acked_ms != null) return "acked";
  return a.level === "P1" ? "unacked" : "new";
}

/** 报警队列(只有 P1、P2;P3 不进队列):未确认的在前,再按级别、再按时间新的在前。 */
export function queue(all: Alert[], now: number): { active: Alert[]; shelved: Alert[] } {
  const live = all.filter((a) => a.level !== "P3" && a.resolved_ms == null);
  const shelved = live.filter((a) => alarmState(a, now) === "shelved");
  const rank = (a: Alert) => (a.acked_ms == null ? 0 : 1) * 10 + (a.level === "P1" ? 0 : 1);
  const active = live
    .filter((a) => alarmState(a, now) !== "shelved")
    .sort((a, b) => rank(a) - rank(b) || b.last_ms - a.last_ms);
  return { active, shelved };
}

/** 视频墙的顺序:按编号固定(值班员靠位置记画面);有没解除 P1 的狗另外置顶。 */
export function wallOrder(robots: Robot[], p1Robots: Set<string>): { pinned: Robot[]; rest: Robot[] } {
  const byId = [...robots].sort((a, b) => a.robot_id.localeCompare(b.robot_id, "en", { numeric: true }));
  return { pinned: byId.filter((r) => p1Robots.has(r.robot_id)), rest: byId.filter((r) => !p1Robots.has(r.robot_id)) };
}

/** 「0:42」「12:05」「1:02:03」。 */
export function duration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
