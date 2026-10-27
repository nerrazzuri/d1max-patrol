// W44 值班页的排序和两个画面位怎么分(设计 §4.1):派去处理报警的 → 需要帮忙的 → 巡逻中的 → 待命、充电的;
// 钉住的位不被报警以外的东西换掉;报警来时钉住的位也让出来;两件报警两个位各给一件。
import { describe, expect, it } from "vitest";
import { guardAlarms, pickSlots, rankUnits } from "./dutyrank";
import type { UnitInfo } from "./live";
import type { Alert, RobotState } from "./model";

const unit = (id: string, state: RobotState): UnitInfo => ({
  robot: { robot_id: id, active: true, fresh: true, revoked: false, held: null, status: { online: state !== "offline", last_seen: 0 } },
  state, battery: 80, task: "",
});
const alert = (key: string, robot: string, o: Partial<Alert> = {}): Alert => ({
  key, kind: "stuck", level: "P1", robot, title: "", detail: "", first_ms: 1, last_ms: 1, count: 1,
  acked_by: "", acked_ms: null, resolved_ms: null, audience: "help", ...o,
});
const ids = (us: UnitInfo[]) => us.map((u) => u.robot.robot_id);

describe("排序", () => {
  const us = [unit("A", "idle"), unit("B", "patrolling"), unit("C", "charging"), unit("D", "patrolling"), unit("E", "offline")];

  it("派去处理报警的排第一", () => {
    const a = [alert("i", "D", { kind: "intrusion", audience: "intrusion" })];
    expect(ids(rankUnits(us, a))[0]).toBe("D");
  });

  it("需要帮忙的排在巡逻前,巡逻的排在待命前,离线最后", () => {
    const a = [alert("h", "C")];
    expect(ids(rankUnits(us, a))).toEqual(["C", "B", "D", "A", "E"]);
  });

  it("驱离中的算派去处理报警的", () => {
    expect(ids(rankUnits([...us, unit("F", "deterring")], []))[0]).toBe("F");
  });

  it("技术类告警不影响排序", () => {
    const a = [alert("t", "A", { kind: "cert_expiring", audience: "owner" })];
    expect(ids(rankUnits(us, a))[0]).toBe("B");
  });

  it("同一档按编号(数字按大小)", () => {
    expect(ids(rankUnits([unit("R10", "idle"), unit("R2", "idle")], []))).toEqual(["R2", "R10"]);
  });
});

describe("报警排序", () => {
  it("没解决的、给保安的;P1 在前,没确认的在前,新的在前", () => {
    const a = [
      alert("old", "A", { last_ms: 1 }),
      alert("p2", "A", { level: "P2", last_ms: 9 }),
      alert("acked", "B", { acked_ms: 5, last_ms: 8 }),
      alert("new", "C", { last_ms: 7 }),
      alert("done", "D", { resolved_ms: 3 }),
      alert("tech", "E", { audience: "owner" }),
    ];
    expect(guardAlarms(a).map((x) => x.key)).toEqual(["new", "old", "acked", "p2"]);
  });
});

describe("两个画面位", () => {
  const us = [unit("A", "patrolling"), unit("B", "patrolling"), unit("C", "idle")];

  it("没报警:按排序挑前两台", () => {
    const [s1, s2] = pickSlots(rankUnits(us, []), [null, null], []);
    expect([s1?.robot, s2?.robot]).toEqual(["A", "B"]);
  });

  it("钉住的不被巡逻的换掉", () => {
    const [s1, s2] = pickSlots(rankUnits(us, []), ["C", null], []);
    expect([s1?.robot, s2?.robot]).toEqual(["C", "A"]);
  });

  it("钉住的机器人不在了,位就空出来按排序补", () => {
    const [s1] = pickSlots(rankUnits(us, []), ["Z", null], []);
    expect(s1?.robot).toBe("A");
  });

  it("报警来时钉住的位让出来(报警优先),另一位照旧钉住", () => {
    const a = [alert("h", "B")];
    const [s1, s2] = pickSlots(rankUnits(us, a), ["C", "A"], guardAlarms(a));
    expect(s1).toMatchObject({ robot: "B", alert: { key: "h" } });
    expect(s2?.robot).toBe("A");
  });

  it("两件报警_两个画面位各给一件", () => {
    const a = [alert("h1", "B", { last_ms: 5 }), alert("h2", "C", { last_ms: 3 })];
    const [s1, s2] = pickSlots(rankUnits(us, a), [null, null], guardAlarms(a));
    expect(s1).toMatchObject({ robot: "B", alert: { key: "h1" } });
    expect(s2).toMatchObject({ robot: "C", alert: { key: "h2" } });
  });

  it("报警记在站点名下(没机器人去):位上放排第一的机器人,带着这件报警", () => {
    const a = [alert("u", "site", { kind: "intrusion_unanswered", audience: "intrusion" })];
    const [s1, s2] = pickSlots(rankUnits(us, a), [null, null], guardAlarms(a));
    expect(s1).toMatchObject({ robot: "A", alert: { key: "u" } });
    expect(s2?.robot).toBe("B");
  });

  it("只有一台机器人:第二个位空着", () => {
    const one = [unit("A", "idle")];
    expect(pickSlots(rankUnits(one, []), [null, null], [])[1]).toBeNull();
  });
});
