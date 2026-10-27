// @vitest-environment happy-dom
// 地图给客户看(转正、图例 + 比例尺、区域名、全图、平面图):只改显示,站点、狗用的坐标一个不动。
// 屏上一点 → 地图坐标(在防区页上点着画多边形)要把转角、缩放、平移原样倒回去:画出来的和点出来的对得上。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import type { UnitInfo } from "./live";
import { SiteMap } from "./mappage";
import {
  applyMat,
  contentBox,
  fitView,
  fitWhole,
  invertMat,
  labelFits,
  placeLabel,
  pointInPolygon,
  rotationOf,
  scaleBar,
  toPx,
  visualCentre,
  worldMatrix,
  type Mat,
  type NavZone,
  type PreviewMeta,
} from "./mapview";
import type { Alert, Robot } from "./model";
import { MapsZonesPage } from "./zonesadmin";

const META: PreviewMeta = { width: 400, height: 300, m_per_px: 0.05, left_x: -10, top_y: 8 };
const D = Math.PI / 180;

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const tick = () => new Promise((r) => setTimeout(r, 0));

/** 滚轮(happy-dom 的 WheelEvent 不带指针位置;浏览器里 WheelEvent 是 MouseEvent,带 clientX/Y)。 */
function wheel(el: Element, deltaY: number, clientX: number, clientY: number) {
  const e = new MouseEvent("wheel", { bubbles: true, cancelable: true, clientX, clientY });
  Object.defineProperty(e, "deltaY", { value: deltaY });
  Object.defineProperty(e, "deltaMode", { value: 0 });
  el.dispatchEvent(e);
  expect(e.defaultPrevented, "滚轮不滚页面").toBe(true);
}

beforeEach(() => {
  lang.value = "en";
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// ------------------------------------------------------------------ 纯算

describe("转正:按 rotation_deg 顺时针绕图中心转(跟 CSS rotate(θdeg) 同向)", () => {
  it("图中心落在视图给的屏上位置;图上中心往右 10 像素那一点转到右下(屏上 y 朝下 = 顺时针)", () => {
    const m = worldMatrix({ ...META, rotation_deg: 30 }, { k: 2, cx: 500, cy: 300 });
    const [x0, y0] = applyMat(m, 200, 150);
    expect(x0).toBeCloseTo(500, 9);
    expect(y0).toBeCloseTo(300, 9);
    const [x, y] = applyMat(m, 210, 150);
    expect(x).toBeCloseTo(500 + 20 * Math.cos(30 * D), 9);
    expect(y).toBeCloseTo(300 + 20 * Math.sin(30 * D), 9);
    // 图上中心往上 10 像素那一点转到右上
    const [ux, uy] = applyMat(m, 200, 140);
    expect(ux).toBeCloseTo(500 + 20 * Math.sin(30 * D), 9);
    expect(uy).toBeCloseTo(300 - 20 * Math.cos(30 * D), 9);
  });

  it("负的角逆时针;线性部分就是 k·[cos −sin; sin cos](SVG matrix(a b c d) = rotate(θ) 再放大 k)", () => {
    for (const deg of [-31, -45, 0, 23, 44.9]) {
      const m = worldMatrix({ ...META, rotation_deg: deg }, { k: 1.5, cx: 0, cy: 0 });
      expect(m.a).toBeCloseTo(1.5 * Math.cos(deg * D), 12);
      expect(m.b).toBeCloseTo(1.5 * Math.sin(deg * D), 12);
      expect(m.c).toBeCloseTo(-1.5 * Math.sin(deg * D), 12);
      expect(m.d).toBeCloseTo(1.5 * Math.cos(deg * D), 12);
    }
  });

  it("没给、不是数、不是有限数 → 0(老站点照旧)", () => {
    expect(rotationOf(META)).toBe(0);
    expect(rotationOf({ ...META, rotation_deg: Number.NaN })).toBe(0);
    expect(rotationOf({ ...META, rotation_deg: "12" as never })).toBe(0);
    expect(rotationOf({ ...META, rotation_deg: Infinity })).toBe(0);
    expect(rotationOf({ ...META, rotation_deg: -31 })).toBe(-31);
  });

  it("倒回去回到原处", () => {
    const m = worldMatrix({ ...META, rotation_deg: -31 }, { k: 2.7, cx: 333, cy: 222 });
    const inv = invertMat(m);
    for (const [px, py] of [[0, 0], [400, 300], [123.4, 56.7]]) {
      const [sx, sy] = applyMat(m, px, py);
      const [bx, by] = applyMat(inv, sx, sy);
      expect(bx).toBeCloseTo(px, 9);
      expect(by).toBeCloseTo(py, 9);
    }
  });
});

describe("全图:转过的整张图放进可用的那一块,四个角都在里面、有一边贴满", () => {
  const corners = (m: Mat) => [[0, 0], [400, 0], [0, 300], [400, 300]].map(([x, y]) => applyMat(m, x, y));
  for (const deg of [0, 23, -31, 44.9, -45]) {
    for (const box of [{ w: 900, h: 600 }, { w: 500, h: 900 }, { w: 1600, h: 400 }]) {
      it(`转 ${deg}°,框 ${box.w}×${box.h}`, () => {
        const meta = { ...META, rotation_deg: deg };
        const pad = 16;
        const m = worldMatrix(meta, fitView(meta, box, pad));
        const c = corners(m);
        const xs = c.map((p) => p[0]);
        const ys = c.map((p) => p[1]);
        for (const x of xs) expect(x).toBeGreaterThanOrEqual(pad - 1e-9), expect(x).toBeLessThanOrEqual(box.w - pad + 1e-9);
        for (const y of ys) expect(y).toBeGreaterThanOrEqual(pad - 1e-9), expect(y).toBeLessThanOrEqual(box.h - pad + 1e-9);
        const spanX = Math.max(...xs) - Math.min(...xs);
        const spanY = Math.max(...ys) - Math.min(...ys);
        expect(Math.max(spanX / (box.w - 2 * pad), spanY / (box.h - 2 * pad))).toBeCloseTo(1, 9);
        // 居中
        expect((Math.max(...xs) + Math.min(...xs)) / 2).toBeCloseTo(box.w / 2, 9);
        expect((Math.max(...ys) + Math.min(...ys)) / 2).toBeCloseTo(box.h / 2, 9);
      });
    }
  }
});

describe("比例尺:整数好读的长度(1-2-5 进位),长度 = 米数 / 每像素米数 × 缩放", () => {
  const NICE = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
  for (const k of [0.3, 0.5, 1, 1.7, 3, 8, 20, 60]) {
    it(`缩放 ${k}`, () => {
      const s = scaleBar(0.05, k, 120);
      expect(NICE).toContain(s.m);
      expect(s.px).toBeCloseTo((s.m / 0.05) * k, 9);
      expect(s.px).toBeLessThanOrEqual(120);
      expect(s.px).toBeGreaterThan(120 / 2.5);
    });
  }
});

describe("区域名放在多边形的视觉中心", () => {
  it("长方形:正中", () => {
    const [x, y] = visualCentre([[0, 0], [10, 0], [10, 4], [0, 4]]);
    expect(x).toBeCloseTo(5, 1);
    expect(y).toBeCloseTo(2, 1);
  });
  it("U 形:顶点平均落在缺口里(外面),视觉中心在多边形里面", () => {
    const u: [number, number][] = [[0, 0], [12, 0], [12, 10], [8, 10], [8, 3], [4, 3], [4, 10], [0, 10]];
    const avg: [number, number] = [u.reduce((s, p) => s + p[0], 0) / u.length, u.reduce((s, p) => s + p[1], 0) / u.length];
    expect(pointInPolygon(avg, u)).toBe(false);
    expect(pointInPolygon(visualCentre(u), u)).toBe(true);
  });
  it("名字会压在机器人、报警点上:挪到它上面或下面(还在多边形里);挪不开就放回中心", () => {
    const room: [number, number][] = [[0, 0], [300, 0], [300, 200], [0, 200]];
    expect(placeLabel(room, "Lobby", 13, [])).toEqual(visualCentre(room));
    const [cx, cy] = visualCentre(room);
    const bot = { x: cx, y: cy, r: 20 };
    const p = placeLabel(room, "Lobby", 13, [bot])!;
    expect(p[0]).toBeCloseTo(cx, 6);
    expect(Math.abs(p[1] - cy)).toBeGreaterThanOrEqual(20 + 13 / 2);
    expect(pointInPolygon(p, room)).toBe(true);
    // 窄长条,上下都没地方:还在中心
    const strip: [number, number][] = [[0, 0], [300, 0], [300, 24], [0, 24]];
    const s = visualCentre(strip);
    expect(placeLabel(strip, "Hall", 13, [{ x: s[0], y: s[1], r: 20 }])).toEqual(s);
    expect(placeLabel([[0, 0], [20, 0], [20, 8], [0, 8]], "Lobby", 13, [])).toBeNull();
  });
  it("屏上太小放不下字就不写", () => {
    const big: [number, number][] = [[0, 0], [200, 0], [200, 60], [0, 60]];
    const tiny: [number, number][] = [[0, 0], [20, 0], [20, 8], [0, 8]];
    expect(labelFits(big, "Lobby", 13)).toBe(true);
    expect(labelFits(tiny, "Lobby", 13)).toBe(false);
    expect(labelFits([[0, 0], [60, 0], [60, 60], [0, 60]], "很长很长很长的区域名字", 13)).toBe(false);
  });
});

// ------------------------------------------------------------------ 渲染出来看

const RECT = { left: 40, top: 100, width: 900, height: 600 };

/** happy-dom 的 getScreenCTM 只回单位阵:照浏览器的规矩自己合(祖先的 transform + 根 svg 的 viewBox 摆进它的框)。 */
function installScreenCTM() {
  const parse = (s: string | null): DOMMatrix => {
    if (!s) return new DOMMatrix();
    const m = /^matrix\(([^)]+)\)$/.exec(s.trim());
    if (!m) throw new Error(`没想到的 transform:${s}`);
    return new DOMMatrix(m[1].trim().split(/[\s,]+/).map(Number));
  };
  const proto = (window as unknown as { SVGGraphicsElement: { prototype: { getScreenCTM: () => DOMMatrix } } }).SVGGraphicsElement.prototype;
  vi.spyOn(proto, "getScreenCTM").mockImplementation(function (this: Element) {
    let m = new DOMMatrix();
    let el: Element | null = this;
    while (el && el.tagName.toLowerCase() !== "svg") {
      m = parse(el.getAttribute("transform")).multiply(m);
      el = el.parentElement;
    }
    const [vx, vy, vw, vh] = el!.getAttribute("viewBox")!.split(/[\s,]+/).map(Number);
    const s = Math.min(RECT.width / vw, RECT.height / vh);
    const ox = RECT.left + (RECT.width - vw * s) / 2 - vx * s;
    const oy = RECT.top + (RECT.height - vh * s) / 2 - vy * s;
    return new DOMMatrix([s, 0, 0, s, ox, oy]).multiply(m);
  });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    ...RECT, x: RECT.left, y: RECT.top, right: RECT.left + RECT.width, bottom: RECT.top + RECT.height, toJSON: () => ({}),
  } as DOMRect);
}

/** 画上去的世界层(图 + 跟着图转的东西)的 transform。 */
function worldOf(c: Element): Mat {
  const g = c.querySelector("g.map-world");
  expect(g, "有世界层").not.toBeNull();
  const m = /^matrix\(([^)]+)\)$/.exec(g!.getAttribute("transform") ?? "");
  expect(m, "世界层用 matrix(…)").not.toBeNull();
  const [a, b, cc, d, e, f] = m![1].trim().split(/[\s,]+/).map(Number);
  for (const v of [a, b, cc, d, e, f]) expect(Number.isFinite(v), m![1]).toBe(true);
  return { a, b, c: cc, d, e, f };
}
const kOf = (m: Mat) => Math.hypot(m.a, m.b);

const robot = (id: string, pose: { x: number; y: number; yaw: number } | null): Robot => ({
  robot_id: id, active: true, fresh: true, revoked: false, held: null,
  loc: pose ? { pose } : null,
  status: { online: true, last_seen: 0 },
  capabilities: { tasks: { patrol: { map_id: "m1", map_version: "1" } } },
});
const unit = (r: Robot): UnitInfo => ({ robot: r, state: "idle", battery: 80, task: "" });
const P1 = (x: number, y: number): Alert => ({
  key: "a1", kind: "dog_sees_person", level: "P1", robot: "A", title: "Person", detail: "", first_ms: 0, last_ms: 0, count: 1,
  acked_by: "", acked_ms: null, resolved_ms: null, context: { pose: { map_id: "m1", map_version: "1", x, y } },
});

const ROOMS = [
  { name: "Lobby", points: [{ x: -8, y: 6 }, { x: 0, y: 6 }, { x: 0, y: 0 }, { x: -8, y: 0 }] },
  { name: "", points: [{ x: 1, y: 1 }, { x: 2, y: 1 }, { x: 2, y: 2 }] },
  { name: "Closet", points: [{ x: 5, y: 5 }, { x: 5.2, y: 5 }, { x: 5.2, y: 5.2 }, { x: 5, y: 5.2 }] },
];
const NOGO: NavZone = { id: "pool", kind: "nogo", label: "Pool", polygon: [[2, -2], [6, -2], [6, -5], [2, -5]] };

function mapPage(meta: PreviewMeta, opts: { units?: UnitInfo[]; p1?: Alert[]; secAreas?: { zone: string; points: [number, number][] }[] } = {}) {
  setFetch((async (url: RequestInfo | URL) => {
    const u = String(url);
    if (u.endsWith("/api/maps/m1/1/preview")) return json(meta);
    if (u.endsWith("/api/maps/m1/1/areas")) return json({ areas: opts.secAreas ?? [] });
    return json({ error: "nope" }, 404);
  }) as never);
  const units = opts.units ?? [unit(robot("A", { x: 2, y: 3, yaw: 0 }))];
  return render(<SiteMap units={units} p1={opts.p1 ?? []} now={0} selected={null} onSelect={() => {}} />);
}
const ready = async (c: Element) => waitFor(() => expect(c.querySelector("g.map-world")).not.toBeNull());
const legend = (c: Element) => [...c.querySelectorAll("[data-legend]")].map((e) => e.getAttribute("data-legend"));

describe("地图页:整张图和上面的东西一起转,字是正的", () => {
  for (const deg of [23, -31]) {
    it(`转 ${deg}°:底图、禁行区、区域多边形在世界层里;机器人、报警点跟着转到对的屏上位置;字不转`, async () => {
      installScreenCTM();
      const meta = { ...META, rotation_deg: deg, zones: [NOGO], areas: ROOMS };
      const { container: c } = mapPage(meta, { p1: [P1(-4, 2)] });
      await ready(c);
      const m = worldOf(c);
      expect(Math.atan2(m.b, m.a) / D).toBeCloseTo(deg, 9);
      const world = c.querySelector("g.map-world")!;
      expect(world.querySelector("image")).not.toBeNull();
      expect(world.querySelector("polygon.nogo-area")).not.toBeNull();
      expect(world.querySelector("polygon.sec-area")).not.toBeNull();
      // 狗:屏上位置 = 世界层的变换作用在它的预览像素上
      const dot = c.querySelector(".bot .bot-dot")!;
      const p = toPx(meta, 2, 3);
      const [sx, sy] = applyMat(m, p.px, p.py);
      expect(Number(dot.getAttribute("cx"))).toBeCloseTo(sx, 6);
      expect(Number(dot.getAttribute("cy"))).toBeCloseTo(sy, 6);
      // 朝向:yaw = 0 是地图 +x,在屏上跟着图转 deg
      const head = c.querySelector(".bot .bot-head")!;
      const ang = Math.atan2(Number(head.getAttribute("y2")) - sy, Number(head.getAttribute("x2")) - sx) / D;
      expect(ang).toBeCloseTo(deg, 6);
      // 报警点
      const ring = c.querySelector(".alarm-pin .ring")!;
      const q = toPx(meta, -4, 2);
      const [ax, ay] = applyMat(m, q.px, q.py);
      expect(Number(ring.getAttribute("cx"))).toBeCloseTo(ax, 6);
      expect(Number(ring.getAttribute("cy"))).toBeCloseTo(ay, 6);
      // 字:狗编号、区域名都不在世界层里,自己和祖先都没有 transform(正的、大小不跟着缩放变)
      const texts = [...c.querySelectorAll("svg text")];
      expect(texts.map((t) => t.textContent)).toEqual(expect.arrayContaining(["A", "Lobby"]));
      for (const t of texts) {
        expect(t.closest("g.map-world"), t.textContent ?? "").toBeNull();
        for (let el: Element | null = t; el && el.tagName.toLowerCase() !== "svg"; el = el.parentElement) {
          expect(el.getAttribute("transform"), t.textContent ?? "").toBeNull();
        }
      }
    });
  }

  it("区域名:写在视觉中心(屏上),空名字不画,屏上太小的不写字", async () => {
    installScreenCTM();
    const meta = { ...META, rotation_deg: 23, areas: ROOMS };
    const { container: c } = mapPage(meta);
    await ready(c);
    const m = worldOf(c);
    const labels = [...c.querySelectorAll("text.area-label")];
    expect(labels.map((l) => l.textContent)).toEqual(["Lobby"]);
    // 空名字的那块不画;Closet 太小:多边形画、字不写
    expect(c.querySelectorAll("g.map-world polygon.sec-area").length).toBe(2);
    const lobby = ROOMS[0].points.map(({ x, y }) => { const p = toPx(meta, x, y); return applyMat(m, p.px, p.py); });
    const [lx, ly] = visualCentre(lobby);
    expect(Number(labels[0].getAttribute("x"))).toBeCloseTo(lx, 6);
    expect(Number(labels[0].getAttribute("y"))).toBeCloseTo(ly, 6);
  });

  it("机器人站在区域名的位置上:名字让开,不压在机器人身上", async () => {
    installScreenCTM();
    const meta = { ...META, rotation_deg: 23, areas: [ROOMS[0]] };
    // 先不放狗,看名字本来在哪儿
    const first = mapPage(meta, { units: [unit(robot("B", null))] });
    await ready(first.container);
    const m = worldOf(first.container);
    const at = first.container.querySelector("text.area-label")!;
    const [lx, ly] = [Number(at.getAttribute("x")), Number(at.getAttribute("y"))];
    cleanup();
    // 狗就站在那一点(屏 → 预览 → 地图)
    const [px, py] = applyMat(invertMat(m), lx, ly);
    const pose = { x: meta.left_x + px * meta.m_per_px, y: meta.top_y - py * meta.m_per_px, yaw: 0 };
    const { container: c } = mapPage(meta, { units: [unit(robot("A", pose))] });
    await ready(c);
    const dot = c.querySelector(".bot .bot-dot")!;
    expect(Number(dot.getAttribute("cx"))).toBeCloseTo(lx, 6);
    expect(Number(dot.getAttribute("cy"))).toBeCloseTo(ly, 6);
    const moved = c.querySelector("text.area-label")!;
    expect(Number(moved.getAttribute("x"))).toBeCloseTo(lx, 6);
    expect(Math.abs(Number(moved.getAttribute("y")) - ly)).toBeGreaterThan(20);
  });

  it("站点没给 areas(老站点):用防区接口的那几块;两边重名的只画一次", async () => {
    installScreenCTM();
    const sec = [{ zone: "Back garden", points: [[-9, 7], [-1, 7], [-1, 1], [-9, 1]] as [number, number][] }];
    const { container: c } = mapPage(META, { secAreas: sec });
    await ready(c);
    await waitFor(() => expect([...c.querySelectorAll("text.area-label")].map((l) => l.textContent)).toEqual(["Back garden"]));
    cleanup();
    const both = mapPage({ ...META, areas: [{ name: "Back garden", points: sec[0].points.map(([x, y]) => ({ x, y })) }] }, { secAreas: sec });
    await ready(both.container);
    await tick();
    expect([...both.container.querySelectorAll("text.area-label")].map((l) => l.textContent)).toEqual(["Back garden"]);
  });

  it("新字段都没有:不转(matrix 的 b、c 为 0)、用栅格、照样画机器人", async () => {
    installScreenCTM();
    const { container: c } = mapPage(META);
    await ready(c);
    const m = worldOf(c);
    expect(m.b).toBe(0);
    expect(m.c).toBe(0);
    expect(c.querySelector("g.map-world image")!.getAttribute("href")).toBe("/api/maps/m1/1/preview.png");
    expect(c.querySelector(".bot")).not.toBeNull();
  });
});

describe("平面图:有 plan 就画它(拉到预览的宽高坐标里),没有用栅格", () => {
  it("有 plan", async () => {
    installScreenCTM();
    const { container: c } = mapPage({ ...META, plan: { url: "/api/maps/m1/1/plan.png", scale: 4 } });
    await ready(c);
    const img = c.querySelector("g.map-world image")!;
    expect(img.getAttribute("href")).toBe("/api/maps/m1/1/plan.png");
    expect(img.getAttribute("width")).toBe("400");
    expect(img.getAttribute("height")).toBe("300");
    expect(img.getAttribute("preserveAspectRatio")).toBe("none");
    expect(img.getAttribute("class")).toContain("mapimg");
    expect(c.querySelectorAll("g.map-world image").length).toBe(1);
  });
  it("plan 读不出来:退回栅格", async () => {
    installScreenCTM();
    const { container: c } = mapPage({ ...META, plan: { url: "/api/maps/m1/1/plan.png", scale: 4 } });
    await ready(c);
    fireEvent.error(c.querySelector("g.map-world image")!);
    await waitFor(() => expect(c.querySelector("g.map-world image")!.getAttribute("href")).toBe("/api/maps/m1/1/preview.png"));
  });
});

describe("图例只列图上真有的东西", () => {
  it("只有底图:地面、墙、没建到的", async () => {
    installScreenCTM();
    const { container: c } = mapPage(META, { units: [unit(robot("A", null))] });
    await ready(c);
    expect(legend(c)).toEqual(["floor", "wall", "unknown"]);
  });
  it("禁行区、区域、机器人、报警点都有:都列;没有限速区就不列限速区", async () => {
    installScreenCTM();
    const { container: c } = mapPage({ ...META, zones: [NOGO], areas: ROOMS }, { p1: [P1(-4, 2)] });
    await ready(c);
    expect(legend(c)).toEqual(["floor", "wall", "unknown", "nogo", "area", "robot", "alarm"]);
  });
  it("限速区", async () => {
    installScreenCTM();
    const { container: c } = mapPage({ ...META, zones: [{ ...NOGO, id: "s", kind: "slow" }] }, { units: [unit(robot("A", null))] });
    await ready(c);
    expect(legend(c)).toEqual(["floor", "wall", "unknown", "slow"]);
  });
  it("中文", async () => {
    installScreenCTM();
    lang.value = "zh";
    const { container: c } = mapPage({ ...META, zones: [NOGO] }, { units: [unit(robot("A", null))] });
    await ready(c);
    const text = c.querySelector(".maplegend")!.textContent!;
    for (const w of ["地面", "墙", "没建图", "禁行区"]) expect(text).toContain(w);
  });
});

describe("比例尺、缩放、全图按钮", () => {
  const bar = (c: Element) => {
    const el = c.querySelector("svg.scalebar")!;
    return { px: Number(el.getAttribute("data-px")), label: c.querySelector(".scale-label")!.textContent! };
  };
  it("比例尺跟着缩放对:屏上长度 = 米数 / m_per_px × 现在的 k;滚轮放大以后还对;「全图」回到开始的样子", async () => {
    installScreenCTM();
    const meta = { ...META, rotation_deg: -31 };
    const { container: c, getByText } = mapPage(meta);
    await ready(c);
    const fit = worldOf(c);
    const svg = c.querySelector("svg.map")!;
    const check = () => {
      const m = worldOf(c);
      const b = bar(c);
      const want = scaleBar(meta.m_per_px, kOf(m));
      expect(b.px).toBeCloseTo(want.px, 6);
      expect(b.label).toBe(`${want.m} m`);
      return m;
    };
    check();
    for (const dy of [-300, -300, 500]) {
      wheel(svg, dy, RECT.left + 300, RECT.top + 200);
      await tick();
      check();
    }
    const zoomed = worldOf(c);
    expect(kOf(zoomed)).not.toBeCloseTo(kOf(fit), 3);
    // 放大时滚轮下面那一点不动
    wheel(svg, -200, RECT.left + 300, RECT.top + 200);
    await tick();
    const after = worldOf(c);
    const fixed = applyMat(invertMat(zoomed), 300, 200);
    const [fx, fy] = applyMat(after, fixed[0], fixed[1]);
    expect(fx).toBeCloseTo(300, 6);
    expect(fy).toBeCloseTo(200, 6);
    fireEvent.click(getByText("Whole map"));
    await tick();
    expect(worldOf(c)).toEqual(fit);
  });

  it("拖动平移;拖完松手不算点了机器人", async () => {
    installScreenCTM();
    const onSelect = vi.fn();
    setFetch((async (url: RequestInfo | URL) => {
      const u = String(url);
      if (u.endsWith("/preview")) return json(META);
      if (u.endsWith("/areas")) return json({ areas: [] });
      return json({}, 404);
    }) as never);
    const { container: c } = render(<SiteMap units={[unit(robot("A", { x: 0, y: 0, yaw: 0 }))]} p1={[]} now={0} selected={null} onSelect={onSelect} />);
    await ready(c);
    const before = worldOf(c);
    const svg = c.querySelector("svg.map")!;
    const dot = c.querySelector(".bot")!;
    fireEvent.pointerDown(dot, { pointerId: 1, button: 0, clientX: 500, clientY: 400 });
    fireEvent.pointerMove(svg, { pointerId: 1, clientX: 530, clientY: 380 });
    fireEvent.pointerMove(svg, { pointerId: 1, clientX: 560, clientY: 360 });
    fireEvent.pointerUp(svg, { pointerId: 1, clientX: 560, clientY: 360 });
    fireEvent.click(dot, { clientX: 560, clientY: 360 });
    await tick();
    const after = worldOf(c);
    expect(after.e - before.e).toBeCloseTo(60, 6);
    expect(after.f - before.f).toBeCloseTo(-40, 6);
    expect(after.a).toBeCloseTo(before.a, 12);
    expect(onSelect).not.toHaveBeenCalled();
    // 不拖、直接点:算点了狗
    fireEvent.pointerDown(dot, { pointerId: 2, button: 0, clientX: 500, clientY: 400 });
    fireEvent.pointerUp(dot, { pointerId: 2, clientX: 501, clientY: 401 });
    fireEvent.click(dot, { clientX: 501, clientY: 401 });
    expect(onSelect).toHaveBeenCalledWith("A");
  });
});

// ------------------------------------------------------------------ 防区页:点着画,倒回地图坐标

describe("防区页:在转过的图上点着画多边形,存下去的点 = 想点的地图点(屏 → 图的逆变换)", () => {
  const WANT: [number, number][] = [[1.23, -2.5], [4.56, -2.5], [4.56, 1.11], [-3.07, 6.4]];

  async function draw(deg: number | undefined, zoom: boolean) {
    installScreenCTM();
    const meta: PreviewMeta = deg === undefined ? META : { ...META, rotation_deg: deg };
    const posted: unknown[] = [];
    setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.endsWith("/api/maps")) return json({ maps: [{ map_id: "m1", version: "1", note: "", created_ms: 0, source: "" }] });
      if (u.endsWith("/api/maps/m1/1/preview")) return json(meta);
      if (u.endsWith("/api/maps/m1/1/areas")) {
        if (init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          posted.push(body);
          return json({ areas: [{ zone: body.zone, points: body.points }] });
        }
        return json({ areas: [] });
      }
      return json({}, 404);
    }) as never);
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    const svg = r.container.querySelector("svg.map")!;
    if (zoom) {
      wheel(svg, -400, RECT.left + 250, RECT.top + 350);
      await tick();
    }
    fireEvent.click(r.getByText("Draw zone"));
    await tick();
    const m = worldOf(r.container);
    for (const [x, y] of WANT) {
      // 照画出来的样子算这一点在屏上哪儿,再加上 svg 在页面上的位置
      const p = toPx(meta, x, y);
      const [sx, sy] = applyMat(m, p.px, p.py);
      fireEvent.click(svg, { clientX: RECT.left + sx, clientY: RECT.top + sy });
      await tick();
    }
    expect(r.container.textContent).toContain(`(${WANT.length} so far)`);
    fireEvent.click(r.getByText("Finish zone"));
    await tick();
    fireEvent.input(r.container.ownerDocument.querySelector("dialog input")!, { target: { value: "Yard" } });
    fireEvent.submit(r.container.ownerDocument.querySelector("dialog form")!);
    await waitFor(() => expect(posted.length).toBe(1));
    const got = (posted[0] as { zone: string; points: [number, number][] });
    expect(got.zone).toBe("Yard");
    expect(got.points.length).toBe(WANT.length);
    got.points.forEach(([x, y], i) => {
      expect(Math.abs(x - WANT[i][0]), `x${i}`).toBeLessThan(1e-6);
      expect(Math.abs(y - WANT[i][1]), `y${i}`).toBeLessThan(1e-6);
    });
    return r;
  }

  for (const deg of [undefined, 0, 30, -30]) {
    it(`转角 ${deg ?? "没给"}`, async () => {
      await draw(deg, false);
    });
  }
  it("转 30° 又放大过", async () => {
    await draw(30, true);
  });

  it("防区页的字(防区名)也是正的,防区跟着图转", async () => {
    installScreenCTM();
    setFetch((async (url: RequestInfo | URL) => {
      const u = String(url);
      if (u.endsWith("/api/maps")) return json({ maps: [{ map_id: "m1", version: "1", note: "", created_ms: 0, source: "" }] });
      if (u.endsWith("/preview")) return json({ ...META, rotation_deg: -30, zones: [NOGO] });
      if (u.endsWith("/areas")) return json({ areas: [{ zone: "Back garden", points: [[-9, 7], [-1, 7], [-1, 1], [-9, 1]] }] });
      return json({}, 404);
    }) as never);
    const { container: c } = render(<MapsZonesPage say={() => {}} />);
    await ready(c);
    await waitFor(() => expect(c.querySelector("text.area-label")?.textContent).toBe("Back garden"));
    const label = c.querySelector("text.area-label")!;
    expect(label.closest("g.map-world")).toBeNull();
    expect(c.querySelector("g.map-world polygon.sec-area")).not.toBeNull();
    expect(c.querySelector("g.map-world polygon.nogo-area")).not.toBeNull();
    expect(legend(c)).toEqual(["floor", "wall", "unknown", "nogo", "zone"]);
  });
});

// ------------------------------------------------------------------ 地图台账 M7、M9、44(2026-10-14)

describe("全图按有内容的范围(content_bbox,预览像素):转过的范围放进框里、居中;没给按整张图", () => {
  const BB: [number, number, number, number] = [40, 30, 220, 160];
  const corners = (m: Mat, b: number[]) => [[b[0], b[1]], [b[2], b[1]], [b[0], b[3]], [b[2], b[3]]].map(([x, y]) => applyMat(m, x, y));
  for (const deg of [0, 23, -31, 44.9]) {
    for (const box of [{ w: 900, h: 600 }, { w: 500, h: 900 }]) {
      it(`转 ${deg}°,框 ${box.w}×${box.h}`, () => {
        const meta = { ...META, rotation_deg: deg, content_bbox: BB };
        const pad = 16;
        const m = worldMatrix(meta, fitView(meta, box, pad));
        const c = corners(m, BB);
        const xs = c.map((p) => p[0]);
        const ys = c.map((p) => p[1]);
        for (const x of xs) expect(x).toBeGreaterThanOrEqual(pad - 1e-9), expect(x).toBeLessThanOrEqual(box.w - pad + 1e-9);
        for (const y of ys) expect(y).toBeGreaterThanOrEqual(pad - 1e-9), expect(y).toBeLessThanOrEqual(box.h - pad + 1e-9);
        const spanX = Math.max(...xs) - Math.min(...xs);
        const spanY = Math.max(...ys) - Math.min(...ys);
        expect(Math.max(spanX / (box.w - 2 * pad), spanY / (box.h - 2 * pad))).toBeCloseTo(1, 9);
        expect((Math.max(...xs) + Math.min(...xs)) / 2).toBeCloseTo(box.w / 2, 9);
        expect((Math.max(...ys) + Math.min(...ys)) / 2).toBeCloseTo(box.h / 2, 9);
        // 比整张图放得大(空白不算进去)
        expect(fitView(meta, box, pad).k).toBeGreaterThan(fitWhole(meta, box, pad).k);
      });
    }
  }
  it("没给、坏的、没有大小的:按整张图;超出图的夹到图上", () => {
    const box = { w: 900, h: 600 };
    const whole = fitWhole(META, box);
    expect(fitView(META, box)).toEqual(whole);
    for (const bad of [[1, 2, 3], [0, 0, 0, 0], [5, 5, 5, 50], [Number.NaN, 0, 10, 10], "x", [0, 0, "9", 9]]) {
      expect(fitView({ ...META, content_bbox: bad as never }, box), JSON.stringify(bad)).toEqual(whole);
    }
    expect(contentBox({ ...META, content_bbox: [-50, -5, 900, 120] })).toEqual([0, 0, 400, 120]);
    expect(contentBox({ ...META, content_bbox: [220, 160, 40, 30] })).toEqual([40, 30, 220, 160]);
  });
});

describe("看到过、机器人没走过 + 抹掉区:图例只在有的时候列;抹掉区只描边(底图站点已经抹了)", () => {
  const ERASE: NavZone = { id: "e1", kind: "erase", label: "Junk", polygon: [[-9, 7], [-7, 7], [-7, 5], [-9, 5]] };
  it("站点说有看到过、图上有抹掉区:都列;抹掉区画成细虚线,悬停说机器人不去", async () => {
    installScreenCTM();
    const { container: c } = mapPage({ ...META, seen: true, zones: [NOGO, ERASE] }, { units: [unit(robot("A", null))] });
    await ready(c);
    expect(legend(c)).toEqual(["floor", "wall", "seen", "unknown", "nogo", "erase"]);
    const e = c.querySelector("g.map-world polygon.erase-area")!;
    expect(e).not.toBeNull();
    expect(e.querySelector("title")!.textContent).toBe("Erased region Junk: robots never go there");
    expect(c.querySelectorAll("g.map-world polygon.nogo-area").length).toBe(1);
  });
  it("中文图例", async () => {
    installScreenCTM();
    lang.value = "zh";
    const { container: c } = mapPage({ ...META, seen: true, zones: [ERASE] }, { units: [unit(robot("A", null))] });
    await ready(c);
    const text = c.querySelector(".maplegend")!.textContent!;
    for (const w of ["看到过、机器人没走过", "已抹掉（机器人不去）"]) expect(text).toContain(w);
  });
  it("刚打开就按有内容的范围放;「全图」也回到它", async () => {
    installScreenCTM();
    const meta = { ...META, rotation_deg: -31, content_bbox: [100, 50, 300, 250] };
    const { container: c, getByText } = mapPage(meta);
    await ready(c);
    const first = worldOf(c);
    // 范围的中心落在框的中心
    const [cx, cy] = applyMat(first, 200, 150);
    expect(cx).toBeCloseTo(RECT.width / 2, 6);
    expect(cy).toBeCloseTo(RECT.height / 2, 6);
    wheel(c.querySelector("svg.map")!, -300, RECT.left + 100, RECT.top + 100);
    await tick();
    expect(worldOf(c)).not.toEqual(first);
    fireEvent.click(getByText("Whole map"));
    await tick();
    expect(worldOf(c)).toEqual(first);
  });
});

describe("防区页:抹掉一块(导航区域 erase),点出来的地图点原样存;删掉要确认", () => {
  const WANT: [number, number][] = [[1.23, -2.5], [4.56, -2.5], [4.56, 1.11], [-3.07, 6.4]];
  const OLD: NavZone = { id: "pond", kind: "nogo", label: "Pool", polygon: [[2, -2], [6, -2], [6, -5], [2, -5]], hard: true };

  function site(meta: PreviewMeta, zones: NavZone[], rev: number) {
    const posted: { zones: NavZone[]; base_revision: number }[] = [];
    let cur = { revision: rev, zones };
    let previews = 0;
    setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.endsWith("/api/maps")) return json({ maps: [{ map_id: "m1", version: "1", note: "", created_ms: 0, source: "" }] });
      if (u.endsWith("/api/maps/m1/1/preview")) { previews += 1; return json(meta); }
      if (u.endsWith("/api/maps/m1/1/areas")) return json({ areas: [] });
      if (u.endsWith("/api/maps/m1/1/zones")) {
        if (init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          posted.push(body);
          cur = { revision: cur.revision + 1, zones: body.zones };
          return json({ ...cur, auto_confirmed: true });
        }
        return json(cur);
      }
      return json({}, 404);
    }) as never);
    return { posted, previews: () => previews };
  }

  for (const deg of [undefined, 30, -30]) {
    it(`转角 ${deg ?? "没给"}:抹掉的多边形跟想点的地图点差不到 1e-6,别的区域原样带着,修订号对`, async () => {
      installScreenCTM();
      const meta: PreviewMeta = deg === undefined ? META : { ...META, rotation_deg: deg };
      const s = site(meta, [OLD], 4);
      const r = render(<MapsZonesPage say={() => {}} />);
      await ready(r.container);
      await waitFor(() => expect((r.getByText("Erase a region") as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(r.getByText("Erase a region"));
      await tick();
      const m = worldOf(r.container);
      const svg = r.container.querySelector("svg.map")!;
      for (const [x, y] of WANT) {
        const p = toPx(meta, x, y);
        const [sx, sy] = applyMat(m, p.px, p.py);
        fireEvent.click(svg, { clientX: RECT.left + sx, clientY: RECT.top + sy });
        await tick();
      }
      expect(r.container.textContent).toContain(`(${WANT.length} so far)`);
      expect(r.container.querySelector("polyline.draft-line.erase")).not.toBeNull();
      const before = s.previews();
      fireEvent.click(r.getByText("Erase this region"));
      await tick();
      const dialog = r.container.ownerDocument.querySelector("dialog")!;
      expect(dialog.textContent).toContain("Robots will never go into it");
      fireEvent.click([...dialog.querySelectorAll("button")].find((b) => b.textContent === "Erase this region")!);
      await waitFor(() => expect(s.posted.length).toBe(1));
      const got = s.posted[0];
      expect(got.base_revision).toBe(4);
      expect(got.zones[0]).toEqual(OLD);
      const e = got.zones[1];
      expect(e.kind).toBe("erase");
      expect(e.id).toBe("e5");
      expect(e.polygon.length).toBe(WANT.length);
      e.polygon.forEach(([x, y], i) => {
        expect(Math.abs(x - WANT[i][0]), `x${i}`).toBeLessThan(1e-6);
        expect(Math.abs(y - WANT[i][1]), `y${i}`).toBeLessThan(1e-6);
      });
      await waitFor(() => expect(s.previews()).toBeGreaterThan(before));   // 重新取预览:平面图地址变了
      await waitFor(() => expect(r.container.querySelector(".erased-list")!.textContent).toContain("Erased"));
    });
  }

  it("删掉一块抹掉区:先说清楚是放宽(要在手机上重新确认),确认了才存", async () => {
    installScreenCTM();
    const ERASED: NavZone = { id: "e2", kind: "erase", label: "Junk", polygon: [[0, 0], [1, 0], [1, 1]] };
    const s = site(META, [OLD, ERASED], 7);
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.container.querySelector(".erased-list li")).not.toBeNull());
    fireEvent.click(r.container.querySelector(".erased-list li button")!);
    await tick();
    const dialog = r.container.ownerDocument.querySelector("dialog")!;
    expect(dialog.textContent).toContain("relaxes the robots' zones");
    fireEvent.click([...dialog.querySelectorAll("button")].find((b) => b.textContent === "Cancel")!);
    await tick();
    expect(s.posted.length).toBe(0);
    fireEvent.click(r.container.querySelector(".erased-list li button")!);
    await tick();
    const again = r.container.ownerDocument.querySelector("dialog")!;
    fireEvent.click([...again.querySelectorAll("button")].find((b) => b.textContent === "Remove")!);
    await waitFor(() => expect(s.posted.length).toBe(1));
    expect(s.posted[0]).toEqual({ zones: [OLD], base_revision: 7 });
  });
});

// ------------------------------------------------------------------ 虚拟墙、网页画禁行区限速区(2026-10-15)

describe("地图与防区:网页画禁行区、限速区、虚拟墙;列出机器人守的区域、删、确认", () => {
  const WALL: NavZone = { id: "w3", kind: "nogo", label: "Fence", hard: true, wall: [[-5, 2], [0, 2], [0, -2]],
    polygon: [[-5.15, 2.15], [0.15, 2.15], [0.15, -2.15], [-0.15, -2.15], [-0.15, 1.85], [-5.15, 1.85]] };

  function site(state: { revision: number; zones: NavZone[]; confirmed?: unknown; held?: NavZone[]; proposal?: unknown }) {
    const posted: { url: string; body: any }[] = [];
    setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.endsWith("/api/maps")) return json({ maps: [{ map_id: "m1", version: "1", note: "", created_ms: 0, source: "" }] });
      if (u.endsWith("/preview")) return json({ ...META, zones: state.zones });
      if (u.endsWith("/areas")) return json({ areas: [] });
      if (u.endsWith("/zones/confirm") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posted.push({ url: u, body });
        return json({ ...state, confirm_text: "WATER", confirmed: { revision: state.revision, by: "alice", at_ms: 0, current: true, how: "confirm" } });
      }
      if (u.endsWith("/zones") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posted.push({ url: u, body });
        return json({ revision: state.revision + 1, zones: body.zones, auto_confirmed: true, confirm_text: "WATER", confirmed: null });
      }
      if (u.endsWith("/zones")) return json({ confirm_text: "WATER", confirmed: null, ...state });
      return json({}, 404);
    }) as never);
    return posted;
  }

  async function clickAt(r: ReturnType<typeof render>, pts: [number, number][]) {
    const svg = r.container.querySelector("svg.map")!;
    const m = worldOf(r.container);
    for (const [x, y] of pts) {
      const p = toPx(META, x, y);
      const [sx, sy] = applyMat(m, p.px, p.py);
      fireEvent.click(svg, { clientX: RECT.left + sx, clientY: RECT.top + sy });
      await tick();
    }
  }

  it("画虚拟墙:两个点就能画完;存的是墙线(多边形站点算)、硬、带改之前的修订号", async () => {
    installScreenCTM();
    const posted = site({ revision: 4, zones: [] });
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    fireEvent.click(r.getByText("Draw virtual wall"));
    await tick();
    await clickAt(r, [[-3, 1]]);
    expect((r.getByText("Finish") as HTMLButtonElement).disabled).toBe(true);
    await clickAt(r, [[2, 1], [2, -3]]);
    expect(r.container.querySelector("[data-draw-hint]")!.textContent).toContain("3 points so far");
    expect(r.container.querySelector("polyline.draft-line.vwall")).not.toBeNull();
    fireEvent.click(r.getByText("Finish"));
    await tick();
    const dlg = r.container.ownerDocument.querySelector("dialog")!;
    fireEvent.click(dlg.querySelector('input[type="checkbox"]')!);      // 硬
    fireEvent.submit(dlg.querySelector("form")!);
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].body.base_revision).toBe(4);
    const [w] = posted[0].body.zones;
    expect(w).toMatchObject({ id: "w5", kind: "nogo", label: "Virtual wall", hard: true });
    expect(w.polygon).toBeUndefined();
    expect(w.wall.length).toBe(3);
    w.wall.forEach(([x, y]: [number, number], i: number) => {
      const want = [[-3, 1], [2, 1], [2, -3]][i];
      expect(Math.abs(x - want[0])).toBeLessThan(1e-6);
      expect(Math.abs(y - want[1])).toBeLessThan(1e-6);
    });
  });

  it("画限速区:限速、临时几小时(站点按自己的钟换成到期)", async () => {
    installScreenCTM();
    const posted = site({ revision: 1, zones: [NOGO] });
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    fireEvent.click(r.getByText("Draw slow zone"));
    await tick();
    await clickAt(r, [[-8, 6], [-4, 6], [-4, 3]]);
    fireEvent.click(r.getByText("Finish"));
    await tick();
    const dlg = r.container.ownerDocument.querySelector("dialog")!;
    fireEvent.input(dlg.querySelector('input[type="number"]')!, { target: { value: "0.5" } });
    fireEvent.click(dlg.querySelector('input[type="checkbox"]')!);      // 临时
    await tick();
    fireEvent.input(dlg.querySelectorAll('input[type="number"]')[1]!, { target: { value: "3" } });
    fireEvent.submit(dlg.querySelector("form")!);
    await waitFor(() => expect(posted.length).toBe(1));
    const zones = posted[0].body.zones;
    expect(zones[0]).toEqual(NOGO);                                    // 原有的原样带回去
    expect(zones[1]).toMatchObject({ kind: "slow", max_speed_mps: 0.5, for_s: 10800 });
    expect(zones[1].polygon.length).toBe(3);
    expect(zones[1].hard).toBeUndefined();
  });

  it("W41 画人员忽略区:没有硬禁区、限速,种类 ignore;图上虚线框、图例、列表里写种类", async () => {
    installScreenCTM();
    const posted = site({ revision: 1, zones: [NOGO] });
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    fireEvent.click(r.getByText("Draw person-ignore zone"));
    await tick();
    await clickAt(r, [[-8, 6], [-4, 6], [-4, 3]]);
    fireEvent.click(r.getByText("Finish"));
    await tick();
    const dlg = r.container.ownerDocument.querySelector("dialog")!;
    expect(dlg.textContent).toContain("not reported");
    expect(dlg.querySelectorAll('input[type="checkbox"]').length).toBe(1);   // 只有「临时」,没有硬禁区
    fireEvent.submit(dlg.querySelector("form")!);
    await waitFor(() => expect(posted.length).toBe(1));
    const z = posted[0].body.zones[1];
    expect(z).toMatchObject({ kind: "ignore", label: "Person-detection ignore zone" });
    expect(z.id.startsWith("i")).toBe(true);
    expect(z.hard).toBeUndefined();
    expect(z.max_speed_mps).toBeUndefined();
    cleanup();
    const IGN = { id: "i2", kind: "ignore", label: "展品", polygon: [[-8, 6], [-4, 6], [-4, 3]] } as unknown as NavZone;
    site({ revision: 2, zones: [NOGO, IGN] });
    const r2 = render(<MapsZonesPage say={() => {}} />);
    await ready(r2.container);
    await waitFor(() => expect(r2.container.querySelector("g.map-world polygon.ignore-area")).not.toBeNull());
    expect(legend(r2.container)).toContain("ignore");
    expect(r2.container.querySelector('[data-navzone="i2"]')?.textContent).toContain("Person-detection ignore zone");
  });

  it("虚拟墙画成一条粗线(画的那条),硬的实线;图例有虚拟墙;列表里写种类", async () => {
    installScreenCTM();
    site({ revision: 2, zones: [NOGO, WALL], confirmed: { revision: 2, by: "alice", at_ms: 0, current: true, how: "tighten" } });
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.container.querySelector("[data-navzone]")).not.toBeNull());
    const line = r.container.querySelector('g.map-world polyline.vwall-line[data-vwall="w3"]')!;
    expect(line).not.toBeNull();
    expect(line.classList.contains("hard")).toBe(true);
    expect(line.getAttribute("points")!.split(" ").length).toBe(3);
    expect(r.container.querySelectorAll("g.map-world polygon.nogo-area").length).toBe(1);   // 墙的带子不另画
    expect(legend(r.container)).toContain("vwall");
    expect(r.container.querySelector('[data-navzone="w3"]')!.textContent).toContain("Virtual wall · hard");
    expect(r.container.querySelector('[data-navzone="pool"]')!.textContent).toContain("No-go zone");
    expect(r.container.querySelector('[data-nav-status="ok"]')!.textContent).toContain("alice");
    expect(r.queryByText("Confirm this revision")).toBeNull();
  });

  it("没确认:写明机器人照旧守着哪些;确认这一版带修订号;删一个是整份存回去少它", async () => {
    installScreenCTM();
    const posted = site({ revision: 7, zones: [WALL], confirmed: { revision: 6, by: "bob", at_ms: 0, current: false, how: "confirm" }, held: [NOGO] });
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.container.querySelector('[data-nav-status="unconfirmed"]')).not.toBeNull());
    expect(r.container.querySelector("[data-nav-held]")!.textContent).toContain("Pool");
    fireEvent.click(r.getByText("Confirm this revision"));
    await tick();
    expect(r.container.ownerDocument.querySelector("dialog")!.textContent).toContain("WATER");
    fireEvent.click([...r.container.ownerDocument.querySelectorAll("dialog button")].find((b) => b.textContent === "Confirm this revision")!);
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].url).toContain("/zones/confirm");
    expect(posted[0].body).toEqual({ revision: 7, confirm: true });
    fireEvent.click(r.container.querySelector('[data-navzone="w3"] button')!);
    await tick();
    fireEvent.click(r.container.ownerDocument.querySelector("dialog .btn.stop")!);
    await waitFor(() => expect(posted.length).toBe(2));
    expect(posted[1].body.zones).toEqual([]);
  });

  it("一个区域都没有:确认要明说 empty_ok;有从上一版带过来的建议:不在网页确认", async () => {
    installScreenCTM();
    const posted = site({ revision: 0, zones: [], confirmed: null });
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.queryByText("Confirm this revision")).not.toBeNull());
    fireEvent.click(r.getByText("Confirm this revision"));
    await tick();
    fireEvent.click([...r.container.ownerDocument.querySelectorAll("dialog button")].find((b) => b.textContent === "Confirm this revision")!);
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].body).toEqual({ revision: 0, confirm: true, empty_ok: true });
    cleanup();
    site({ revision: 0, zones: [], confirmed: null, proposal: { zones: [] } });
    const r2 = render(<MapsZonesPage say={() => {}} />);
    await ready(r2.container);
    await waitFor(() => expect(r2.container.querySelector("[data-navzones]")).not.toBeNull());
    expect(r2.queryByText("Confirm this revision")).toBeNull();
    expect(r2.container.textContent).toContain("Check them in the phone app first");
  });
});

// ------------------------------------------------------------------ 外审 R3(2026-10-15 下午)

describe("地图与防区:换图后才回来的旧回包不进这张图;确认的就是人刚看过的那一份", () => {
  const MAPS_AB = { maps: [{ map_id: "B", version: "1", note: "", created_ms: 0, source: "" }, { map_id: "A", version: "1", note: "", created_ms: 0, source: "" }] };
  const zone = (id: string): NavZone => ({ id, kind: "nogo", hard: true, label: id, polygon: [[0, 0], [2, 0], [2, 2], [0, 2]] });
  const B7 = { revision: 7, zones: [zone("B-flower")], held: [zone("B-pool")], confirmed: null, confirm_text: "B safety check" };

  function abSite(o: { holdA?: boolean; bAfter?: () => unknown; holdPost?: boolean }) {
    const held: { a?: (r: Response) => void; post?: (r: Response) => void } = {};
    const posted: [string, any][] = [];
    let bGets = 0;
    setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (init?.method === "POST") {
        posted.push([u, JSON.parse(String(init.body))]);
        if (o.holdPost) return new Promise<Response>((r) => { held.post = r; });
        return json({ ...B7, revision: 8, confirmed: { revision: 8, by: "admin", at_ms: 0, current: true, how: "confirm" } });
      }
      if (u === "/api/maps") return json(MAPS_AB);
      if (u.endsWith("/preview")) return json(META);
      if (u.endsWith("/areas")) return json({ areas: [] });
      if (u === "/api/maps/A/1/zones" && o.holdA) return new Promise<Response>((r) => { held.a = r; });
      if (u === "/api/maps/A/1/zones") return json({ revision: 7, zones: [zone("A-pool")], confirmed: null, confirm_text: "A safety check" });
      if (u === "/api/maps/B/1/zones") { bGets += 1; return json(bGets > 1 && o.bAfter ? o.bAfter() : B7); }
      return json({}, 404);
    }) as never);
    return { held, posted };
  }
  const panel = (c: Element) => c.querySelector("[data-navzones]")?.textContent ?? "";
  const confirmYes = (c: Element) => [...c.ownerDocument.querySelectorAll("dialog button")].find((b) => b.textContent === "Confirm this revision")!;

  it("A 的区域晚到:列表、确认框都还是 B 的;确认发到 B、带 B 看过的修订", async () => {
    installScreenCTM();
    const { held, posted } = abSite({ holdA: true });
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    fireEvent.change(r.getByLabelText("Map"), { target: { value: "A\u00001" } });
    await waitFor(() => expect(held.a).toBeTruthy());
    fireEvent.change(r.getByLabelText("Map"), { target: { value: "B\u00001" } });
    await waitFor(() => expect(panel(r.container)).toContain("B-flower"));
    held.a!(json({ revision: 7, zones: [zone("A-pool")], confirmed: null, confirm_text: "A safety check" }));
    await new Promise((res) => setTimeout(res, 20));
    expect(panel(r.container)).toContain("B-flower");
    expect(panel(r.container)).not.toContain("A-pool");
    fireEvent.click(r.getByText("Confirm this revision"));
    await tick();
    expect(r.container.ownerDocument.querySelector("dialog")!.textContent).toContain("B safety check");
    fireEvent.click(confirmYes(r.container));
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0]).toEqual(["/api/maps/B/1/zones/confirm", { revision: 7, confirm: true }]);
  });

  it("弹框以后区域被人改了(同一个修订号、内容不一样也算):不确认,让人重看", async () => {
    installScreenCTM();
    const said: string[] = [];
    const { posted } = abSite({ bAfter: () => ({ ...B7, zones: [] }) });   // 有人把花坛删了
    const r = render(<MapsZonesPage say={(s) => said.push(s)} />);
    await ready(r.container);
    fireEvent.change(r.getByLabelText("Map"), { target: { value: "B\u00001" } });   // 一开始选的是 A(列表最后一张)
    await waitFor(() => expect(panel(r.container)).toContain("B-flower"));
    fireEvent.click(r.getByText("Confirm this revision"));
    await tick();
    fireEvent.click(confirmYes(r.container));
    await waitFor(() => expect(said.some((s) => s.includes("look again"))).toBe(true));
    expect(posted).toEqual([]);
    await waitFor(() => expect(panel(r.container)).not.toContain("B-flower"));
  });

  it("确认框开着换了图:框关掉,不发确认", async () => {
    installScreenCTM();
    const { posted } = abSite({});
    const r = render(<MapsZonesPage say={() => {}} />);
    await ready(r.container);
    fireEvent.change(r.getByLabelText("Map"), { target: { value: "B\u00001" } });   // 一开始选的是 A(列表最后一张)
    await waitFor(() => expect(panel(r.container)).toContain("B-flower"));
    fireEvent.click(r.getByText("Confirm this revision"));
    await tick();
    expect(r.container.ownerDocument.querySelector("dialog")).not.toBeNull();
    fireEvent.change(r.getByLabelText("Map"), { target: { value: "A\u00001" } });
    await tick();
    expect(r.container.ownerDocument.querySelector("dialog")).toBeNull();
    expect(posted).toEqual([]);
  });

  it("换图以后才回来的保存结果:不在 A 上说「存好了」、不拿 B 的结果盖 A 的列表", async () => {
    installScreenCTM();
    const said: string[] = [];
    const { held, posted } = abSite({ holdPost: true });
    const r = render(<MapsZonesPage say={(s) => said.push(s)} />);
    await ready(r.container);
    fireEvent.change(r.getByLabelText("Map"), { target: { value: "B\u00001" } });   // 一开始选的是 A(列表最后一张)
    await waitFor(() => expect(panel(r.container)).toContain("B-flower"));
    fireEvent.click(r.container.querySelector('[data-navzone="B-flower"] button')!);
    await tick();
    fireEvent.click(r.container.ownerDocument.querySelector("dialog .btn.stop")!);
    await waitFor(() => expect(posted.length).toBe(1));
    fireEvent.change(r.getByLabelText("Map"), { target: { value: "A\u00001" } });
    await waitFor(() => expect(panel(r.container)).toContain("A-pool"));
    held.post!(json({ ...B7, revision: 8, zones: [] }));
    await new Promise((res) => setTimeout(res, 20));
    expect(panel(r.container)).toContain("A-pool");
    expect(said.filter((s) => s.includes("revision 8"))).toEqual([]);
  });
});
