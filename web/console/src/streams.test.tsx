// @vitest-environment happy-dom
// 演示站点实测:四台机器人 8 路画面长连占满浏览器对一个站点的 6 条连接(HTTP/1.1),页面自己的数据、报警进不来。
// 一页最多 MAX_STREAMS 路长连,超出的隔 2 秒取一张;报警现场、放大的先拿,其次大画面,小窗最后;有人让出来就升回长连。
import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { CameraWell } from "./camera";
import { lang } from "./i18n";
import { MAX_STREAMS, slotRank } from "./streams";

beforeEach(() => {
  lang.value = "zh";
  vi.stubGlobal("IntersectionObserver", undefined);
  setFetch((() => Promise.resolve(new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } }))) as typeof fetch);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const srcs = (c: Element) => Array.from(c.querySelectorAll("img.feed")).map((i) => i.getAttribute("src") ?? "");
const live = (c: Element) => srcs(c).filter((s) => !s.includes("still=1")).length;
const still = (c: Element) => srcs(c).filter((s) => s.includes("still=1")).length;

describe("一页的长连路数", () => {
  it("最多 3 路长连,留出连接给页面自己的数据和报警", () => {
    expect(MAX_STREAMS).toBe(3);
  });

  it("8 路:3 路长连,另外 5 路取一张、左下角写每 2 秒一张;2 秒后再取一张", async () => {
    const ids = ["A", "B", "C", "D"];
    const { container: c } = render(<div>{ids.flatMap((id) => [
      <CameraWell key={`${id}f`} robot={id} cam="front" now={0} variant="main" />,
      <CameraWell key={`${id}b`} robot={id} cam="back" now={0} variant="main" />])}</div>);
    expect(live(c)).toBe(3);
    expect(still(c)).toBe(5);
    expect(c.textContent).toContain("每 2 秒一张");
    const one = c.querySelector('img.feed[src*="still=1"]') as HTMLImageElement;
    const before = one.getAttribute("src");
    fireEvent.load(one);
    await vi.advanceTimersByTimeAsync(2100);
    expect(one.getAttribute("src")).not.toBe(before);
  });

  it("报警现场(一直拉)先拿长连;大画面先于小窗;有人走了,取一张的升回长连", async () => {
    expect(slotRank({ forceLive: true, variant: "main" })).toBeGreaterThan(slotRank({ variant: "main" }));
    expect(slotRank({ variant: "main" })).toBeGreaterThan(slotRank({ variant: "pip" }));
    const r = render(<div>
      <CameraWell key="p1" robot="A" cam="back" now={0} variant="pip" />
      <CameraWell key="p2" robot="B" cam="back" now={0} variant="pip" />
      <CameraWell key="m1" robot="C" cam="front" now={0} variant="main" />
      <CameraWell key="m2" robot="D" cam="front" now={0} variant="main" />
    </div>);
    const liveOf = () => srcs(r.container).filter((s) => !s.includes("still=1"));
    expect(liveOf().some((s) => s.includes("/C/"))).toBe(true);
    expect(liveOf().some((s) => s.includes("/D/"))).toBe(true);
    expect(liveOf().length).toBe(3);
    r.rerender(<div>
      <CameraWell key="p1" robot="A" cam="back" now={0} variant="pip" />
      <CameraWell key="p2" robot="B" cam="back" now={0} variant="pip" />
      <CameraWell key="m1" robot="C" cam="front" now={0} variant="main" />
      <CameraWell key="m2" robot="D" cam="front" now={0} variant="main" />
      <CameraWell key="z" robot="A" cam="front" now={0} forceLive />
    </div>);
    expect(liveOf().some((s) => s.includes("/A/video/front"))).toBe(true);   // 放大的抢到长连
    expect(liveOf().length).toBe(3);
    r.rerender(<div>
      <CameraWell key="p1" robot="A" cam="back" now={0} variant="pip" />
      <CameraWell key="p2" robot="B" cam="back" now={0} variant="pip" />
    </div>);
    expect(liveOf().length).toBe(2);
  });
});
