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
  it("「去这里」浮在图框里(绝对定位),不在图下面另占一块", () => {
    expect(decl(".mapwrap > .posebar")).toMatch(/position: ?absolute/);
    expect(decl(".mapbox > .posebar")).toBe("");
  });
});

describe("管理组各页铺满宽度", () => {
  it(".admin 不限最大宽度(宽屏右边不空一块)", () => {
    expect(decl(".admin")).not.toMatch(/max-width/);
  });
});

describe("巡检路线页整页一屏(跟防区页一样)", () => {
  const consoleSrc = readFileSync(`${process.cwd()}/src/console.tsx`, "utf-8");
  it("路线页算「带地图的页」", () => {
    expect(consoleSrc).toMatch(/fill = [^\n]*"admin\/routes"/);
  });
  it("路线页的地图在一屏模式里撑满(不写死高度)", () => {
    expect(flat).toMatch(/\.console\.fill \.routemap \.mapwrap \{ ?flex: ?1; ?height: ?auto;? ?\}/);
    expect(flat).toMatch(/\.console\.fill \.routesplit \{[^}]*flex: ?1/);
  });
});
