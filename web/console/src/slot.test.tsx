// @vitest-environment happy-dom
// W44 第一期:画面位(设计 §4.1):前摄像头大、后摄像头小窗,点小窗互换;有热像才有热像按钮;断流停在最后一帧写已断几秒。
import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
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
      scene={{ label: "3 号 后花园", src: "/api/cameras/cam3/live", health: "/api/cameras/cam3/health" }} />);
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

// ---- 外审 R6:报警现场的固定摄像头跟机器人画面一样:断了写最后画面、已断几秒,自己重连;
// 静悄悄停帧按站点报的「最后一帧多久了」判(不拿浏览器重载图片的时刻当来了新帧)。

const SCENE = { label: "3 号 后花园", src: "/api/cameras/cam3/live", health: "/api/cameras/cam3/health" };
let health: Record<string, { online: boolean; last_frame_age_ms: number | null }>;
let asked: string[];

describe("外审R6 报警现场断流", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(new Date(2026, 9, 27, 23, 10, 0));
    health = {};
    asked = [];
    setFetch(((url: string) => {
      asked.push(url);
      const h = health[url];
      return Promise.resolve(h ? new Response(JSON.stringify(h), { headers: { "Content-Type": "application/json" } })
        : new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } }));
    }) as typeof fetch);
  });
  afterEach(() => vi.useRealTimers());

  const slot = (now: number, scene = SCENE) => <VideoSlot u={unit()} now={now} pinned={false} onPin={() => {}} onZoom={() => {}} scene={scene} />;
  const sceneImg = (c: Element) => c.querySelector(".slot-main img.scene") as HTMLImageElement | null;

  it("出过画面以后断了:写最后画面和已断几秒,3 秒后换个地址重连", async () => {
    const t0 = Date.now();
    const r = render(slot(t0));
    fireEvent.load(sceneImg(r.container)!);
    fireEvent.error(sceneImg(r.container)!);
    r.rerender(slot(t0 + 15000));
    const lost = r.container.querySelector(".slot-main .lost")!;
    expect(lost.textContent).toContain("画面断了");
    expect(lost.textContent).toContain("23:10:00");
    expect(lost.textContent).toContain("已断 15 秒");
    await vi.advanceTimersByTimeAsync(3100);
    expect(sceneImg(r.container)!.getAttribute("src")).toMatch(/^\/api\/cameras\/cam3\/live\?v=1$/);
  });

  it("一帧都没出就失败,也写断了、重连", async () => {
    const r = render(slot(Date.now()));
    fireEvent.error(sceneImg(r.container)!);
    expect(r.container.querySelector(".slot-main .lost")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(3100);
    expect(sceneImg(r.container)!.getAttribute("src")).toContain("?v=1");
  });

  it("静悄悄停帧:站点说最后一帧是 7 秒前,标断了(时刻按最后一帧算),重连", async () => {
    const t0 = Date.now();
    const r = render(slot(t0));
    fireEvent.load(sceneImg(r.container)!);
    health[SCENE.health] = { online: true, last_frame_age_ms: 100 };
    await vi.advanceTimersByTimeAsync(2100);
    expect(r.container.querySelector(".slot-main .lost")).toBeNull();
    health[SCENE.health] = { online: false, last_frame_age_ms: 7000 };
    await vi.advanceTimersByTimeAsync(2000);
    r.rerender(slot(Date.now()));
    const lost = r.container.querySelector(".slot-main .lost")!;
    expect(lost).not.toBeNull();
    expect(lost.textContent).toContain("23:09:57");             // 23:10:04 − 7 秒
    expect(asked).toContain(SCENE.health);
    await vi.advanceTimersByTimeAsync(3100);
    expect(sceneImg(r.container)!.getAttribute("src")).toContain("?v=1");
  });

  it("重连上了、站点也说有新帧了,才把断了的提示收掉", async () => {
    const r = render(slot(Date.now()));
    fireEvent.load(sceneImg(r.container)!);
    fireEvent.error(sceneImg(r.container)!);
    await vi.advanceTimersByTimeAsync(3100);
    health[SCENE.health] = { online: false, last_frame_age_ms: null };   // 重连了还没出画面:还在起
    await vi.advanceTimersByTimeAsync(2100);
    expect(r.container.querySelector(".slot-main .lost")).not.toBeNull();
    fireEvent.load(sceneImg(r.container)!);
    health[SCENE.health] = { online: true, last_frame_age_ms: 200 };
    await vi.advanceTimersByTimeAsync(2100);
    expect(r.container.querySelector(".slot-main .lost")).toBeNull();
  });

  it("换了现场:上一个现场晚到的健康回包不碰新的", async () => {
    let late!: (r: Response) => void;
    setFetch(((url: string) => (url === SCENE.health
      ? new Promise<Response>((res) => { late = res; })
      : Promise.resolve(new Response(JSON.stringify({ online: true, last_frame_age_ms: 100 }), { headers: { "Content-Type": "application/json" } })))) as typeof fetch);
    const r = render(slot(Date.now()));
    fireEvent.load(sceneImg(r.container)!);
    await vi.advanceTimersByTimeAsync(2100);                        // 问了旧现场,还没回
    const other = { label: "4 号 车库", src: "/api/cameras/cam4/live", health: "/api/cameras/cam4/health" };
    r.rerender(slot(Date.now(), other));
    fireEvent.load(sceneImg(r.container)!);
    late(new Response(JSON.stringify({ online: false, last_frame_age_ms: 9000 }), { headers: { "Content-Type": "application/json" } }));
    await vi.advanceTimersByTimeAsync(100);
    expect(sceneImg(r.container)!.getAttribute("src")).toBe("/api/cameras/cam4/live");
    expect(r.container.querySelector(".slot-main .lost")).toBeNull();
  });
});
