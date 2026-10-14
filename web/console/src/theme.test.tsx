// @vitest-environment happy-dom
// 值班台的外观跟手机同一套(C-B6,决策 64):主题跟着系统的浅色 / 深色走、当场换;地图、时间轴的颜色取
// 当前这一套;状态的颜色按 mobile.json 的 usage(红 P1 / 离线,橙 P2,绿正常,别的中性)。
import { cleanup, render } from "@testing-library/preact";
import { afterEach, describe, expect, it } from "vitest";
import mobile from "../../../design/mobile.json";
import { lang } from "./i18n";
import { StateText } from "./live";
import { applyTheme, followSystem, mapRasterFilter, palette, scheme, themeColor, tone, toneColor } from "./theme";

/** 假的 matchMedia("(prefers-color-scheme: dark)"):能改 matches、能发 change。 */
function fakeQuery(dark: boolean) {
  const subs = new Set<(e: { matches: boolean }) => void>();
  return {
    matches: dark,
    addEventListener: (_: string, f: (e: { matches: boolean }) => void) => void subs.add(f),
    removeEventListener: (_: string, f: (e: { matches: boolean }) => void) => void subs.delete(f),
    flip(to: boolean) {
      this.matches = to;
      for (const f of subs) f({ matches: to });
    },
    get listeners() {
      return subs.size;
    },
  };
}

const css = (root: HTMLElement, name: string) => root.style.getPropertyValue(name);

afterEach(() => applyTheme("light"));

describe("主题跟着系统走(prefers-color-scheme)", () => {
  it("系统是深色就挂深色那一套,浅色就浅色", () => {
    for (const dark of [true, false]) {
      const root = document.createElement("div");
      const stop = followSystem(root, fakeQuery(dark) as unknown as MediaQueryList);
      const s = dark ? "dark" : "light";
      expect(root.dataset.theme).toBe(s);
      expect(scheme.value).toBe(s);
      expect(css(root, "--c-bg")).toBe(mobile.color[s].bg);
      expect(css(root, "--c-text")).toBe(mobile.color[s].text);
      expect(css(root, "color-scheme")).toBe(s);
      stop();
    }
  });

  it("系统一换当场换(不用刷新);取消跟随以后不再换", () => {
    const root = document.createElement("div");
    const q = fakeQuery(false);
    const stop = followSystem(root, q as unknown as MediaQueryList);
    expect(css(root, "--c-surface")).toBe(mobile.color.light.surface);
    q.flip(true);
    expect(root.dataset.theme).toBe("dark");
    expect(css(root, "--c-surface")).toBe(mobile.color.dark.surface);
    expect(css(root, "--c-error")).toBe(mobile.color.dark.error);
    q.flip(false);
    expect(css(root, "--c-surface")).toBe(mobile.color.light.surface);
    stop();
    expect(q.listeners).toBe(0);
    q.flip(true);
    expect(root.dataset.theme).toBe("light");
  });

  it("两套的每个颜色都挂上了(键跟 mobile.json 一样),字体是手机那三种", () => {
    const root = document.createElement("div");
    applyTheme("dark", root);
    for (const [k, v] of Object.entries(mobile.color.dark)) expect(css(root, `--c-${k}`)).toBe(v);
    expect(css(root, "--font-display")).toContain("Space Grotesk");
    expect(css(root, "--font-sans")).toContain("Inter");
    expect(css(root, "--font-mono")).toContain("JetBrains Mono");
  });
});

describe("地图、时间轴、画面的颜色取当前这一套", () => {
  it("地图的底、狗的颜色(正常绿、P1 红)跟着主题换", () => {
    applyTheme("light");
    expect(themeColor("mapBg")).toBe(mobile.color.light.surfaceVariant);
    expect(toneColor("idle")).toBe(mobile.color.light.success);
    expect(toneColor("deterring")).toBe(mobile.color.light.error);
    applyTheme("dark");
    expect(themeColor("mapBg")).toBe(mobile.color.dark.surfaceVariant);
    expect(toneColor("idle")).toBe(mobile.color.dark.success);
    expect(toneColor("deterring")).toBe(mobile.color.dark.error);
  });

  it("栅格底图:浅色原样;深色反亮度(手机 ColorFilter 的意思:空地变暗、墙变亮),滤镜挂在 --map-raster 上", () => {
    const root = document.createElement("div");
    applyTheme("light", root);
    expect(css(root, "--map-raster")).toBe("none");
    applyTheme("dark", root);
    expect(css(root, "--map-raster")).toBe(mapRasterFilter("dark"));
    // invert(a): y = a + (1 − 2a)·x;对一对手机的 y = 235 − 0.82·x(差不超过 5 级)
    const a = Number(/invert\(([\d.]+)\)/.exec(mapRasterFilter("dark"))![1]);
    for (const [x, want] of [[254, 27], [205, 67], [0, 235]]) {
      expect(Math.abs(255 * (a + (1 - 2 * a) * (x / 255)) - want)).toBeLessThanOrEqual(5);
    }
  });

  it("视频底两套都是黑的", () => {
    expect(palette("light").well).toBe("#000000");
    expect(palette("dark").well).toBe("#000000");
  });

  it("样式表里用到的每个颜色、圆角变量两套主题都有(写错名字会变成透明)", async () => {
    const { readFileSync } = await import("node:fs");
    const sheet = readFileSync(`${process.cwd()}/src/styles.css`, "utf-8"); // vitest 在 web/console 里跑
    const used = new Set([...sheet.matchAll(/var\(--(c|r)-([A-Za-z0-9]+)/g)].map((m) => `${m[1]}-${m[2]}`));
    expect(used.size).toBeGreaterThan(10);
    for (const s of ["light", "dark"] as const) {
      const root = document.createElement("div");
      applyTheme(s, root);
      for (const v of used) expect(css(root, `--${v}`), `${s} --${v}`).not.toBe("");
    }
  });

  it("没有的颜色名直接报错(不悄悄画成黑的)", () => {
    expect(() => themeColor("nope")).toThrow();
  });
});

describe("状态的颜色(mobile.json usage;决策 64)", () => {
  it("在线、正常 → 绿;P1、急停、离线 → 红;P2、低电量 → 橙;说不清 → 中性", () => {
    for (const s of ["online", "ok", "connected", "idle", "patrolling", "returning", "charging"]) expect(tone(s)).toBe("success");
    for (const s of ["P1", "estop", "offline", "deterring", "fell"]) expect(tone(s)).toBe("error");
    for (const s of ["P2", "lowBattery"]) expect(tone(s)).toBe("warning");
    for (const s of ["unknown", "P3", "", "whatever"]) expect(tone(s)).toBe("neutral");
  });

  it("狗的状态字带上对应的颜色类(样式表按它上色):正常绿、离线红、急停红、说不清不上色", () => {
    lang.value = "en";
    const cls = (el: Element) => el.querySelector(".state")!.className;
    expect(cls(render(<StateText s="idle" />).container)).toContain("tone-success");
    cleanup();
    expect(cls(render(<StateText s="offline" />).container)).toContain("tone-error");
    cleanup();
    expect(cls(render(<StateText s="estop" />).container)).toContain("tone-error");
    cleanup();
    const unknown = cls(render(<StateText s="idle" stale />).container);
    expect(unknown).not.toContain("tone-");
    cleanup();
  });

  it("颜色值就是当前这一套的 success / error / warning;中性用正文色", () => {
    applyTheme("dark");
    expect(toneColor("online")).toBe(mobile.color.dark.success);
    expect(toneColor("P1")).toBe(mobile.color.dark.error);
    expect(toneColor("P2")).toBe(mobile.color.dark.warning);
    expect(toneColor("offline")).toBe(mobile.color.dark.error);
    expect(toneColor("unknown")).toBe(mobile.color.dark.text);
    applyTheme("light");
    expect(toneColor("online")).toBe(mobile.color.light.success);
    expect(toneColor("unknown")).toBe(mobile.color.light.text);
  });
});
