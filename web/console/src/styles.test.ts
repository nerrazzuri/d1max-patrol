// 真浏览器实测查出来的:「现场概况」页的外框用了 class="home",它的样式 `.home { display:flex; flex-direction:column }`
// 也套到了顶栏布防标签 `armstate home` 上(竖着排,字掉到底下)。布防标签用的状态类名不许在样式表里单独当选择器。
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

describe("样式表", () => {
  it("布防标签的状态类名(home、armed、visitor)只跟 .armstate 连着用", () => {
    const bad = [...css.matchAll(/(^|[\s,{(>~+])\.(home|armed|visitor)(?![-\w])/gm)].map((m) => m[0].trim());
    expect(bad).toEqual([]);
  });
});

describe("W62 地图上正在画的东西跟墙分得开", () => {
  // 墙在深色下是白、浅色下是黑 = 正文色;草稿点、草稿线、临时路线、路网要用品牌蓝(用户 2026-11-01:点位是黑的,看不见)
  const sheet = readFileSync(`${process.cwd()}/src/styles.css`, "utf-8").replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (sel: string) => {
    const i = sheet.indexOf(`${sel} {`);
    return i < 0 ? "" : sheet.slice(i, sheet.indexOf("}", i));
  };
  it.each([".draft-line", ".draft-pt", ".temp-route", ".temp-pt circle", ".rn-edge", ".me-op.obstacle"])("%s 用品牌蓝", (sel) => {
    const r = rule(sel);
    expect(r).toMatch(/var\(--c-brand\)/);
    expect(r).not.toMatch(/var\(--c-text\)|var\(--c-primary\)/);
  });
});
