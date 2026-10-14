// @vitest-environment happy-dom
// 形状是给色盲看的(颜色之外的第二道编码):离线现在是红的,不能再借 P2 的三角,有自己的形状
// (空心圆加一道斜杠);P1 菱形、P2 三角照旧。
import { cleanup, render } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { lang } from "./i18n";
import { Shape } from "./icons";
import { JumpStrip, OfflineStrip, StateText, type UnitInfo } from "./live";
import type { Alert, Robot } from "./model";
import { AlarmQueue } from "./queue";

beforeEach(() => {
  lang.value = "en";
});
afterEach(cleanup);

const shapes = (el: Element) => [...el.querySelectorAll("svg.shape")].map((s) => s.getAttribute("class"));

const robot = (id: string): Robot => ({ robot_id: id, active: true, fresh: false, revoked: false, held: null, status: { online: false, last_seen: 0 } });
const unit = (id: string, state: UnitInfo["state"]): UnitInfo => ({ robot: robot(id), state, battery: null, task: "" });

const alert = (key: string, level: Alert["level"]): Alert => ({
  key, kind: "k", level, robot: "A", title: "t", detail: "", first_ms: 0, last_ms: 0, count: 1,
  acked_by: "", acked_ms: null, resolved_ms: null,
});

describe("离线的形状", () => {
  it("离线是空心圆加斜杠:不填色、跟 P1 菱形 / P2 三角 / P3 空心圆都不一样", () => {
    const { container } = render(<Shape kind="offline" />);
    const svg = container.querySelector("svg.shape.offline")!;
    expect(svg).toBeTruthy();
    expect(svg.querySelector("circle")).toBeTruthy();
    expect(svg.querySelector("path")!.getAttribute("d")).toBe("M2.8 9.2l6.4-6.4");
    expect(svg.querySelector("g")!.getAttribute("fill")).toBe("none");
  });

  it("值守页狗的状态:离线出离线形状,不是三角;急停还是菱形;正常没有形状", () => {
    expect(shapes(render(<StateText s="offline" />).container)).toEqual(["shape offline"]);
    cleanup();
    expect(shapes(render(<StateText s="estop" />).container)).toEqual(["shape P1"]);
    cleanup();
    expect(shapes(render(<StateText s="idle" />).container)).toEqual([]);
  });

  it("离线那一条、跳转条上的离线狗:离线形状", () => {
    const strip = render(<OfflineStrip u={unit("B", "offline")} onMap={() => {}} />).container;
    expect(shapes(strip)).toEqual(["shape offline"]);
    expect(strip.textContent).toContain("Offline");
    cleanup();
    const jump = render(<JumpStrip units={[unit("B", "offline"), unit("C", "fell")]} p1={new Set()} watching={0} total={2} />).container;
    expect(shapes(jump)).toEqual(["shape offline", "shape P1"]);
  });

  it("报警队列的 P2 还是三角、P1 还是菱形", () => {
    const { container } = render(
      <AlarmQueue active={[alert("a", "P1"), alert("b", "P2")]} shelved={[]} now={0} open={true}
        onToggle={() => {}} onAck={() => {}} onShelve={() => {}} onUnshelve={() => {}} onGo={() => {}} />,
    );
    const got = shapes(container);
    expect(got).toContain("shape P1");
    expect(got).toContain("shape P2");
    expect(got).not.toContain("shape offline");
    expect(container.querySelector("svg.shape.P2 path")!.getAttribute("d")).toBe("M6 1l6 10H0z");
  });
});
