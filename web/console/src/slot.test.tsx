// @vitest-environment happy-dom
// W44 第一期:画面位(设计 §4.1):前摄像头大、后摄像头小窗,点小窗互换;有热像才有热像按钮;断流停在最后一帧写已断几秒。
import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { lang } from "./i18n";
import type { UnitInfo } from "./live";
import type { Robot } from "./model";
import { VideoSlot } from "./slot";

const robot = (cams?: string[]): Robot => ({
  robot_id: "A", active: true, fresh: true, revoked: false, held: null,
  status: { online: true, last_seen: 0 },
  capabilities: cams ? { tasks: { video: { cameras: cams } } } : null,
});
const unit = (cams?: string[]): UnitInfo => ({ robot: robot(cams), state: "patrolling", battery: 82, task: "" });

const mainSrc = (c: Element) => (c.querySelector(".slot-main img.feed, .slot-main img.scene") as HTMLImageElement | null)?.getAttribute("src") ?? "";
const pipSrc = (c: Element) => (c.querySelector(".slot-pip img.feed") as HTMLImageElement | null)?.getAttribute("src") ?? "";

beforeEach(() => {
  lang.value = "zh";
  vi.stubGlobal("IntersectionObserver", undefined);    // 没有可见检测 = 当看得见,直接拉流
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("画面位", () => {
  it("点小窗两路互换", () => {
    const { container: c } = render(<VideoSlot u={unit()} now={0} pinned={false} onPin={() => {}} onZoom={() => {}} />);
    expect(mainSrc(c)).toContain("/video/front");
    expect(pipSrc(c)).toContain("/video/back");
    fireEvent.click(c.querySelector(".slot-pip")!);
    expect(mainSrc(c)).toContain("/video/back");
    expect(pipSrc(c)).toContain("/video/front");
  });

  it("没有热像不显示热像按钮", () => {
    const { container: c } = render(<VideoSlot u={unit()} now={0} pinned={false} onPin={() => {}} onZoom={() => {}} />);
    expect(c.querySelector(".slot-thermal")).toBeNull();
  });

  it("有热像,点了大画面换热像、前摄像头进小窗,再点换回", () => {
    const { container: c } = render(<VideoSlot u={unit(["front", "back", "thermal"])} now={0} pinned={false} onPin={() => {}} onZoom={() => {}} />);
    const b = c.querySelector(".slot-thermal") as HTMLButtonElement;
    fireEvent.click(b);
    expect(mainSrc(c)).toContain("/video/thermal");
    expect(pipSrc(c)).toContain("/video/front");
    expect(b.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(b);
    expect(mainSrc(c)).toContain("/video/front");
  });

  it("断流写最后画面和已断几秒", () => {
    const t0 = new Date(2026, 9, 27, 23, 10, 0).getTime();
    vi.spyOn(Date, "now").mockReturnValue(t0);
    const r = render(<VideoSlot u={unit()} now={t0} pinned={false} onPin={() => {}} onZoom={() => {}} />);
    fireEvent.error(r.container.querySelector(".slot-main img.feed")!);
    r.rerender(<VideoSlot u={unit()} now={t0 + 5000} pinned={false} onPin={() => {}} onZoom={() => {}} />);
    const lost = r.container.querySelector(".slot-main .lost")!;
    expect(lost.textContent).toContain("画面断了");
    expect(lost.textContent).toContain("23:10:00");
    expect(lost.textContent).toContain("已断 5 秒");
  });

  it("报警现场:大画面放现场,小窗放这台机器人的前摄像头", () => {
    const { container: c } = render(<VideoSlot u={unit()} now={0} pinned={false} onPin={() => {}} onZoom={() => {}}
      scene={{ label: "3 号 后花园", src: "/api/cameras/cam3/live" }} />);
    expect(mainSrc(c)).toBe("/api/cameras/cam3/live");
    expect(pipSrc(c)).toContain("/video/front");
  });

  it("钉住按钮跟着状态", () => {
    const pin = vi.fn();
    const { container: c } = render(<VideoSlot u={unit()} now={0} pinned onPin={pin} onZoom={() => {}} />);
    const b = c.querySelector(".slot-pin") as HTMLButtonElement;
    expect(b.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(b);
    expect(pin).toHaveBeenCalledOnce();
  });

  it("离线的不拉流,写离线", () => {
    const u: UnitInfo = { ...unit(), state: "offline" };
    const { container: c } = render(<VideoSlot u={u} now={0} pinned={false} onPin={() => {}} onZoom={() => {}} />);
    expect(c.querySelector("img.feed")).toBeNull();
    expect(c.querySelector(".slot-off")!.textContent).toContain("离线");
  });
});
