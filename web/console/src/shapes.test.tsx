// @vitest-environment happy-dom
// 形状是给色盲看的(颜色之外的第二道编码):离线现在是红的,不能再借 P2 的三角,有自己的形状
// (空心圆加一道斜杠);P1 菱形、P2 三角照旧。
import { cleanup, render } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { lang } from "./i18n";
import { Shape } from "./icons";
import { StateText } from "./live";

beforeEach(() => {
  lang.value = "en";
});
afterEach(cleanup);

const shapes = (el: Element) => [...el.querySelectorAll("svg.shape")].map((s) => s.getAttribute("class"));

describe("离线的形状", () => {
  it("离线是空心圆加斜杠:不填色、跟 P1 菱形 / P2 三角 / P3 空心圆都不一样", () => {
    const { container } = render(<Shape kind="offline" />);
    const svg = container.querySelector("svg.shape.offline")!;
    expect(svg).toBeTruthy();
    expect(svg.querySelector("circle")).toBeTruthy();
    expect(svg.querySelector("path")!.getAttribute("d")).toBe("M2.8 9.2l6.4-6.4");
    expect(svg.querySelector("g")!.getAttribute("fill")).toBe("none");
  });

  it("值守页机器人的状态:离线出离线形状,不是三角;急停还是菱形;正常没有形状", () => {
    expect(shapes(render(<StateText s="offline" />).container)).toEqual(["shape offline"]);
    cleanup();
    expect(shapes(render(<StateText s="estop" />).container)).toEqual(["shape P1"]);
    cleanup();
    expect(shapes(render(<StateText s="idle" />).container)).toEqual([]);
  });

});
