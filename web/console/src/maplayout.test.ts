// 地图页地图下面那两样(「去这里」工具条、运行图层图例)只占自己的高,地图占满剩下的(2026-10-15 用户截图:
// 运行图层图例借了 .maplegend 的 flex: 1,在竖着排的 .mapbox 里跟地图平分了高度,地图下面空出一大片灰)。
// happy-dom 量不了排版,这里看样式表那条规矩还在;真的高多少靠浏览器截图核对。
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const flat = readFileSync(`${process.cwd()}/src/styles.css`, "utf-8")
  .replace(/\/\*[\s\S]*?\*\//g, "").replace(/\s+/g, " ");

function decl(sel: string): string {
  const esc = sel.replace(/[.*+?^${}()|[\]\\>]/g, "\\$&");
  const m = flat.match(new RegExp(`(?:^|[}{;]) ?[^{}]*${esc}[^{}]* ?\\{([^}]*)\\}`));
  return m ? m[1] : "";
}

describe("地图页:地图下面的东西不跟地图抢高", () => {
  it("地图区竖着排,地图本身 flex: 1", () => {
    expect(decl(".mapbox")).toMatch(/flex-direction: ?column/);
    expect(decl(".mapcanvas")).toMatch(/flex: ?1\b/);
  });
  it("工具条、运行图层图例 flex: none(不能跟着 .maplegend 的 flex: 1 长高)", () => {
    expect(decl(".mapbox > .posebar")).toMatch(/flex: ?none/);
    expect(decl(".mapbox > .runlegend")).toMatch(/flex: ?none/);
  });
});
