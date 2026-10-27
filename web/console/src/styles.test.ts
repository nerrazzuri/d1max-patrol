// 真浏览器实测查出来的:「家里的状况」页的外框用了 class="home",它的样式 `.home { display:flex; flex-direction:column }`
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
