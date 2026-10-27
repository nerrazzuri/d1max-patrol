// @vitest-environment happy-dom
// W44 实况页(设计 §4.2、§2.4):所有机器人前后两路一直在播,2×2 按紧急程度排;每张卡片能把后摄像头换成热像,
// 「全部看热像」只换有热像的;离线的不拉流。
import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { lang } from "./i18n";
import type { UnitInfo } from "./live";
import { LiveWall } from "./livewall";
import type { Alert, RobotState } from "./model";

const unit = (id: string, state: RobotState, cams?: string[]): UnitInfo => ({
  robot: { robot_id: id, active: true, fresh: true, revoked: false, held: null, status: { online: state !== "offline", last_seen: 0 },
    capabilities: cams ? { tasks: { video: { cameras: cams } } } : null },
  state, battery: 50, task: "",
});
const help = (robot: string): Alert => ({
  key: `h-${robot}`, kind: "stuck", level: "P1", robot, title: "", detail: "", first_ms: 1, last_ms: 1, count: 1,
  acked_by: "", acked_ms: null, resolved_ms: null, audience: "help",
});
const cards = (c: Element) => Array.from(c.querySelectorAll(".lw-card")).map((x) => x.getAttribute("data-robot"));
const srcs = (card: Element) => Array.from(card.querySelectorAll("img.feed")).map((i) => i.getAttribute("src") ?? "");

beforeEach(() => {
  lang.value = "zh";
  vi.stubGlobal("IntersectionObserver", undefined);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("实况页", () => {
  it("按紧急程度排", () => {
    const us = [unit("A", "idle"), unit("B", "patrolling"), unit("C", "patrolling")];
    const { container: c } = render(<LiveWall units={us} alerts={[help("C")]} now={0} onZoom={() => {}} />);
    expect(cards(c)).toEqual(["C", "B", "A"]);
  });

  it("每张卡片前后两路并排都在播", () => {
    const { container: c } = render(<LiveWall units={[unit("A", "patrolling")]} alerts={[]} now={0} onZoom={() => {}} />);
    const s = srcs(c.querySelector(".lw-card")!);
    expect(s.some((x) => x.includes("/video/front"))).toBe(true);
    expect(s.some((x) => x.includes("/video/back"))).toBe(true);
  });

  it("卡片上的热像按钮把后摄像头那一半换成热像", () => {
    const { container: c } = render(<LiveWall units={[unit("A", "patrolling", ["front", "back", "thermal"])]} alerts={[]} now={0} onZoom={() => {}} />);
    fireEvent.click(c.querySelector(".lw-card .slot-thermal")!);
    const s = srcs(c.querySelector(".lw-card")!);
    expect(s.some((x) => x.includes("/video/thermal"))).toBe(true);
    expect(s.some((x) => x.includes("/video/back"))).toBe(false);
    expect(s.some((x) => x.includes("/video/front"))).toBe(true);
  });

  it("全部看热像只换有热像的", () => {
    const us = [unit("A", "patrolling", ["front", "back", "thermal"]), unit("B", "patrolling")];
    const { container: c } = render(<LiveWall units={us} alerts={[]} now={0} onZoom={() => {}} />);
    fireEvent.click(c.querySelector(".lw-allthermal")!);
    const [a, b] = Array.from(c.querySelectorAll(".lw-card"));
    expect(srcs(a).some((x) => x.includes("/video/thermal"))).toBe(true);
    expect(srcs(b).some((x) => x.includes("/video/back"))).toBe(true);
  });

  it("没有一台有热像就不出「全部看热像」", () => {
    const { container: c } = render(<LiveWall units={[unit("A", "patrolling")]} alerts={[]} now={0} onZoom={() => {}} />);
    expect(c.querySelector(".lw-allthermal")).toBeNull();
  });

  it("离线的卡片写离线,不拉流", () => {
    const { container: c } = render(<LiveWall units={[unit("A", "offline")]} alerts={[]} now={0} onZoom={() => {}} />);
    expect(c.querySelector(".lw-card img.feed")).toBeNull();
    expect(c.querySelector(".lw-card")!.textContent).toContain("离线");
  });

  it("放大把这一台交出去", () => {
    const zoom = vi.fn();
    const { container: c } = render(<LiveWall units={[unit("A", "patrolling")]} alerts={[]} now={0} onZoom={zoom} />);
    fireEvent.click(c.querySelector(".lw-card .slot-zoom")!);
    expect(zoom).toHaveBeenCalledWith("A");
  });
});
