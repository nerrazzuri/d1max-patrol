// 值班页的排序和两个画面位怎么分(W44,设计 §4.1)。不碰 DOM,好测。
import type { UnitInfo } from "./live";
import { forGuard, type Alert } from "./model";

/** 没解决的、给保安的报警,最急的在前:P1 在前,没确认的在前,新的在前。 */
export function guardAlarms(all: Alert[]): Alert[] {
  const rank = (a: Alert) => (a.level === "P1" ? 0 : a.level === "P2" ? 2 : 4) + (a.acked_ms == null ? 0 : 1);
  return all.filter((a) => a.resolved_ms == null && a.level !== "P3" && forGuard(a))
    .sort((a, b) => rank(a) - rank(b) || b.last_ms - a.last_ms);
}

const byId = (a: UnitInfo, b: UnitInfo) => a.robot.robot_id.localeCompare(b.robot.robot_id, "en", { numeric: true });

/** 0 派去处理报警的 → 1 需要帮忙的 → 2 巡逻中的 → 3 待命、充电的 → 4 离线;同一档按编号。 */
export function rankUnits(units: UnitInfo[], alerts: Alert[]): UnitInfo[] {
  const open = alerts.filter((a) => a.resolved_ms == null && forGuard(a));
  const sent = new Set(open.filter((a) => a.audience === "intrusion").map((a) => a.robot));
  const help = new Set(open.filter((a) => a.audience !== "intrusion").map((a) => a.robot));
  const tier = (u: UnitInfo): number => {
    const id = u.robot.robot_id;
    if (u.state === "offline") return 4;
    if (sent.has(id) || u.state === "deterring") return 0;
    if (help.has(id) || u.state === "fell" || u.state === "estop") return 1;
    if (u.state === "patrolling" || u.state === "returning" || u.state === "manual") return 2;
    return 3;
  };
  return [...units].sort((a, b) => tier(a) - tier(b) || byId(a, b));
}

export interface SlotPick {
  robot: string;
  /** 这个位是给哪件报警的现场。 */
  alert?: Alert;
}

/**
 * 两个画面位。``pins``:每个位钉住的机器人(没钉 = null)。``alarms``:{@link guardAlarms} 排好的。
 * 报警优先:头两件报警各占一个位(钉住的也让出来);剩下的位先照钉住的,再按排序补。
 */
export function pickSlots(ranked: UnitInfo[], pins: [string | null, string | null], alarms: Alert[]): [SlotPick | null, SlotPick | null] {
  const known = new Set(ranked.map((u) => u.robot.robot_id));
  const out: (SlotPick | null)[] = [null, null];
  const used = new Set<string>();
  const firstFree = () => ranked.find((u) => !used.has(u.robot.robot_id))?.robot.robot_id;
  // 报警占位:记在机器人名下的放那台;记在站点名下的(没机器人去)放排第一的还没用的
  let slot = 0;
  for (const a of alarms) {
    if (slot > 1) break;
    const id = known.has(a.robot) && !used.has(a.robot) ? a.robot : firstFree();
    if (!id) break;
    out[slot] = { robot: id, alert: a };
    used.add(id);
    slot += 1;
  }
  // 没被报警占的位:钉住的(还在、没被用)优先
  for (const i of [0, 1]) {
    if (out[i]) continue;
    const p = pins[i];
    if (p && known.has(p) && !used.has(p)) {
      out[i] = { robot: p };
      used.add(p);
    }
  }
  for (const i of [0, 1]) {
    if (out[i]) continue;
    const id = firstFree();
    if (id) {
      out[i] = { robot: id };
      used.add(id);
    }
  }
  return [out[0], out[1]];
}
