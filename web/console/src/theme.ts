// 值班台的外观(C-B6,决策 64):跟手机 App 同一套设计系统。数值的唯一来源是 design/mobile.json
// (手机从它生成 nw_tokens.g.dart);浅色、深色两套,跟着系统(prefers-color-scheme)走,系统一换当场换,
// 不用刷新。颜色挂成 CSS 变量 --c-<名字>;用 CSSOM 设(不是内联 style 属性),站点的 CSP(style-src 'self')允许。
// 地图、时间轴都是 SVG,颜色走 CSS 变量,换主题时跟着变;地图底图(栅格)在深色下用 --map-raster 滤镜反亮度。
import { signal } from "@preact/signals";
import mobile from "../../../design/mobile.json";

export type Scheme = "light" | "dark";
export type Palette = Record<string, string>;

/** 品牌色(W60):只用在导航选中、焦点、主按钮、地图上选中的东西 —— 不表示健康(绿)、也不表示提示。 */
//  品牌蓝本身就是设计系统的 tertiary(design/mobile.json,手机网页同一个值);这里只补网页要的软底和字色。
const BRAND: Record<Scheme, { brandSoft: string; onBrand: string; brandText: string }> = {
  dark: { brandSoft: "#14263A", onBrand: "#04121F", brandText: "#E8F3FE" },
  light: { brandSoft: "#E3EFFB", onBrand: "#FFFFFF", brandText: "#0B3A66" },
};

/** 网页多出来的几样(手机没有对应的):视频底两套都是黑的;地图的底跟手机一样用 surfaceVariant;侧栏比底亮一点。 */
function webExtras(p: Palette, s: Scheme): Palette {
  const b = BRAND[s];
  return { well: "#000000", mapBg: p.surfaceVariant, brand: p.tertiary, focus: p.tertiary, ...b,
    sidebar: s === "dark" ? "#0F1218" : "#ECEEF1" };
}

/** 深色下底图反亮度的那个 a(CSS invert(a))。 */
const RASTER_INVERT = 0.91;

export function palette(s: Scheme): Palette {
  const base = (mobile.color as Record<Scheme, Palette>)[s];
  return { ...base, ...webExtras(base, s) };
}

/**
 * 深色下地图栅格的滤镜:跟手机的 ColorFilter(y = 235 − 0.82·x)一样的意思 —— 空地 254→约 24、未知 205→约 64、
 * 墙 0→约 232。CSS 的 invert(a) 是 y = a − (2a − 1)·x,a = 0.91 时斜率 −0.82、截距 232。浅色下原样。
 * 只用在底图(.mapimg:栅格或平面图)上,不用在上面画的狗、防区。
 */
export function mapRasterFilter(s: Scheme): string {
  return s === "dark" ? `invert(${RASTER_INVERT})` : "none";
}

/** 底图(栅格、平面图)上的一个灰度在这一套下显示成什么颜色(跟 --map-raster 滤镜一样算):图例色块用。 */
export function rasterShade(grey: number, s: Scheme): string {
  const a = RASTER_INVERT;
  const v = s === "dark" ? 255 * (a + (1 - 2 * a) * (grey / 255)) : grey;
  const h = Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0");
  return `#${h}${h}${h}`;
}

const QUERY = "(prefers-color-scheme: dark)";

function systemScheme(): Scheme {
  try {
    return typeof window !== "undefined" && window.matchMedia?.(QUERY).matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

/** 现在用的是哪一套。组件要按主题取颜色时读它(读了就跟着重画)。 */
export const scheme = signal<Scheme>(systemScheme());

/** 现在这一套里某个颜色的值(给画布、SVG 属性之类用;样式表里直接写 var(--c-名字))。 */
export function themeColor(name: string): string {
  const v = palette(scheme.value)[name];
  if (v === undefined) throw new Error(`没有这个颜色:${name}`);
  return v;
}

export function applyTheme(s: Scheme, root: HTMLElement = document.documentElement): void {
  for (const [k, v] of Object.entries(palette(s))) root.style.setProperty(`--c-${k}`, v);
  for (const [k, v] of Object.entries(mobile.radius)) root.style.setProperty(`--r-${k}`, `${v}px`);
  const fb = mobile.font.fallback.map((f) => `'${f}'`).join(", ");
  root.style.setProperty("--font-display", `'${mobile.font.display}', ${fb}, system-ui, sans-serif`);
  root.style.setProperty("--font-sans", `'${mobile.font.sans}', ${fb}, system-ui, sans-serif`);
  root.style.setProperty("--font-mono", `'${mobile.font.mono}', ${fb}, ui-monospace, monospace`);
  root.style.setProperty("--map-raster", mapRasterFilter(s));
  root.style.setProperty("color-scheme", s);
  root.dataset.theme = s;
  scheme.value = s;
}

type SchemeQuery = Pick<MediaQueryList, "matches" | "addEventListener" | "removeEventListener">;

/** 按系统的设置挂上一套,并且跟着系统换(当场)。回一个取消跟随的函数。 */
/** 外壳按页面定了用哪一套(W44:保安页深色、管理页浅色):定了就不跟系统换;``null`` = 跟系统。 */
let pinned: Scheme | null = null;
let systemDark = false;

/** 外壳调:定成某一套,或者放开(回到跟系统)。 */
export function pinScheme(s: Scheme | null, root: HTMLElement = document.documentElement): void {
  pinned = s;
  applyTheme(s ?? (systemDark ? "dark" : "light"), root);
}

/** 系统现在是不是深色(followSystem 记下的)。 */
export function systemIsDark(): boolean {
  return systemDark;
}

export function followSystem(
  root: HTMLElement = document.documentElement,
  mql: SchemeQuery | null = typeof window !== "undefined" && window.matchMedia ? window.matchMedia(QUERY) : null,
): () => void {
  systemDark = !!mql?.matches;
  applyTheme(pinned ?? (systemDark ? "dark" : "light"), root);
  if (!mql) return () => {};
  const on = (e: { matches: boolean }) => {
    systemDark = e.matches;
    if (pinned == null) applyTheme(e.matches ? "dark" : "light", root);
  };
  mql.addEventListener("change", on as (e: MediaQueryListEvent) => void);
  return () => mql.removeEventListener("change", on as (e: MediaQueryListEvent) => void);
}

/** 颜色的用法(design/mobile.json 的 usage):红只给 P1、急停、离线、破坏性操作;橙只给 P2、低电量;绿给正常。 */
export type Tone = "error" | "warning" | "success" | "info" | "neutral";

const USAGE = mobile.usage as Record<string, string>;
/** 值班台上的状态 → mobile.json usage 里的键(没列的就是中性)。 */
const ALIAS: Record<string, string> = {
  P1: "p1", P2: "p2", P3: "p3",
  deterring: "p1", fell: "p1", estop: "estop", offline: "offline",
  patrolling: "online", returning: "online", charging: "online", idle: "online", manual: "online",
  online: "online", connected: "connected", ok: "ok", lowBattery: "lowBattery", battery: "battery",
};

export function tone(state: string): Tone {
  const k = ALIAS[state];
  const v = k ? USAGE[k] : undefined;
  return v === "error" || v === "warning" || v === "success" || v === "info" ? v : "neutral";
}

/** 某个状态现在该用的颜色值;中性回正文色。 */
export function toneColor(state: string): string {
  const t = tone(state);
  return themeColor(t === "neutral" ? "text" : t);
}
