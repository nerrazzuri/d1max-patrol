// @vitest-environment happy-dom
// W60:画面底下那条 —— 机器人画面写在做什么、电量、定位(最多三项);固定摄像头的现场写地点,跟机器人画面分得开。
import { render } from "@testing-library/preact";
import { describe, expect, it } from "vitest";
import { setLang } from "./i18n";
import type { UnitInfo } from "./live";
import { SlotFoot } from "./slot";

const unit = (o: Partial<UnitInfo> = {}, loc: object | null = { pose: { x: 0, y: 0, yaw: 0 }, anchored: true }): UnitInfo => ({
  robot: { robot_id: "A", active: true, fresh: true, revoked: false, held: null, loc } as UnitInfo["robot"],
  state: "patrolling", battery: 82.4, task: "巡逻中", ...o,
});

describe("画面底下那条", () => {
  setLang("zh");
  it("机器人画面:在做什么 · 电量 · 定位,最多三项", () => {
    const c = render(<SlotFoot u={unit()} />).container;
    expect(Array.from(c.querySelectorAll(".slot-foot > span")).map((x) => x.textContent)).toEqual(["巡逻中", "82%", "定位良好"]);
  });
  it("定位变差写出来;离线、数据过期不写", () => {
    expect(render(<SlotFoot u={unit({}, { pose: null, degraded: "icp" })} />).container.textContent).toContain("定位变差");
    expect(render(<SlotFoot u={unit({ state: "offline" })} />).container.querySelector(".slot-foot")).toBeNull();
    expect(render(<SlotFoot u={unit({ stale: true })} />).container.querySelector(".slot-foot")).toBeNull();
  });
  it("固定摄像头的现场:写「固定摄像头 · 哪里」,不写机器人的电量", () => {
    const c = render(<SlotFoot u={unit()} scene={{ label: "北门 1 号", src: "/x", health: "/y" }} />).container;
    expect(c.textContent).toBe("固定摄像头 · 北门 1 号");
    expect(c.querySelector(".slot-foot.scene")).not.toBeNull();
  });
});
