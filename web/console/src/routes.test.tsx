// @vitest-environment happy-dom
// 路线编辑(2026-10-14):点图加点、拖点、拖朝向把手、调序、删、存;地图页点图「去这里 / 设位置」。
// 点的是屏上哪儿,存下去 / 发出去的就是那一处的地图坐标 —— 转 0° / ±30° 都要对得上(屏 → 图的逆变换)。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import type { UnitInfo } from "./live";
import { MapPage } from "./mappage";
import { applyMat, toPx, type Mat, type PreviewMeta } from "./mapview";
import type { Robot } from "./model";
import { RoutesPage } from "./routepage";
import {
  addPoint,
  deletePoint,
  nextName,
  pxToMap,
  reorder,
  setDwell,
  toggleAction,
  yawToward,
  type RoutePoint,
} from "./routes";
import { RDICTS } from "./routes_i18n";

const META: PreviewMeta = { width: 400, height: 300, m_per_px: 0.05, left_x: -10, top_y: 8 };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const tick = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => { lang.value = "en"; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

// ------------------------------------------------------------------ 纯函数

const P = (name: string, x: number, y: number, yaw = 0): RoutePoint => ({ name, x, y, yaw });

describe("点的增删挪调序", () => {
  it("新点接在末尾,名字不重,朝向 = 从上一个点走过来的方向", () => {
    let ps = addPoint([], 1, 2);
    expect(ps).toEqual([{ name: "P1", x: 1, y: 2, yaw: 0 }]);
    ps = addPoint(ps, 1, 5);
    expect(ps[1].name).toBe("P2");
    expect(ps[1].yaw).toBeCloseTo(Math.PI / 2, 3);
    ps = addPoint(ps, 1, 5);                                   // 跟上一个点重合:朝向沿用,不是 NaN
    expect(ps[2].yaw).toBeCloseTo(Math.PI / 2, 3);
    expect(new Set(ps.map((p) => p.name)).size).toBe(3);
  });
  it("删掉中间的以后再加,名字不跟剩下的撞", () => {
    const ps = deletePoint([P("P1", 0, 0), P("P2", 1, 0), P("P3", 2, 0)], 0);
    expect(nextName(ps)).toBe("P4");
    expect(addPoint(ps, 3, 0).map((p) => p.name)).toEqual(["P2", "P3", "P4"]);
  });
  it("调序:挪前、挪后、越界不动", () => {
    const ps = [P("a", 0, 0), P("b", 1, 0), P("c", 2, 0)];
    expect(reorder(ps, 2, 0).map((p) => p.name)).toEqual(["c", "a", "b"]);
    expect(reorder(ps, 0, 1).map((p) => p.name)).toEqual(["b", "a", "c"]);
    expect(reorder(ps, 0, 5)).toBe(ps);
  });
  it("动作:拍照按相机分,开关;停留秒数 0 = 去掉;都没了就不带 actions", () => {
    let p = P("a", 0, 0);
    p = toggleAction(p, { type: "photo", camera: "front" });
    p = toggleAction(p, { type: "photo", camera: "back" });
    p = setDwell(p, 3);
    expect(p.actions).toEqual([{ type: "dwell", seconds: 3 }, { type: "photo", camera: "front" }, { type: "photo", camera: "back" }]);
    p = toggleAction(p, { type: "photo", camera: "front" });
    p = setDwell(p, 0);
    expect(p.actions).toEqual([{ type: "photo", camera: "back" }]);
    p = toggleAction(p, { type: "photo", camera: "back" });
    expect("actions" in p).toBe(false);
  });
  it("朝向:地图系逆时针;重合没有朝向", () => {
    expect(yawToward(0, 0, 1, 1)).toBeCloseTo(Math.PI / 4, 3);
    expect(yawToward(0, 0, -1, 0)).toBeCloseTo(Math.PI, 3);
    expect(yawToward(1, 1, 1, 1)).toBeNull();
  });
  it("像素 → 地图坐标跟 toPx 互逆(两位小数)", () => {
    for (const [x, y] of [[1.23, -2.5], [-9.99, 7.5], [0, 0]]) {
      const q = toPx(META, x, y);
      const [bx, by] = pxToMap(META, q.px, q.py);
      expect(Math.abs(bx - x)).toBeLessThan(0.006);
      expect(Math.abs(by - y)).toBeLessThan(0.006);
    }
  });
  it("中英词条一一对应", () => {
    expect(Object.keys(RDICTS.zh).sort()).toEqual(Object.keys(RDICTS.en).sort());
    for (const k of Object.keys(RDICTS.en) as (keyof typeof RDICTS.en)[]) expect(RDICTS.zh[k].length).toBeGreaterThan(0);
  });
});

// ------------------------------------------------------------------ 渲染出来点

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

function worldOf(c: Element): Mat {
  const g = c.querySelector("g.map-world")!;
  const m = /^matrix\(([^)]+)\)$/.exec(g.getAttribute("transform") ?? "")!;
  const [a, b, cc, d, e, f] = m[1].trim().split(/[\s,]+/).map(Number);
  return { a, b, c: cc, d, e, f };
}
/** 地图上 (x, y) 米在页面(客户端)上的位置。 */
function clientOf(c: Element, meta: PreviewMeta, x: number, y: number): { clientX: number; clientY: number } {
  const p = toPx(meta, x, y);
  const [sx, sy] = applyMat(worldOf(c), p.px, p.py);
  return { clientX: RECT.left + sx, clientY: RECT.top + sy };
}
const ready = async (c: Element) => waitFor(() => expect(c.querySelector("g.map-world")).not.toBeNull());
const near = (a: number, b: number, tol = 0.011) => expect(Math.abs(a - b), `${a} vs ${b}`).toBeLessThan(tol);

/** 拖:按在 el 上,在 window 上移到 to,再抬起,然后(浏览器会)补一个 click 到 svg 上。 */
async function drag(el: Element, from: { clientX: number; clientY: number }, to: { clientX: number; clientY: number }, svg?: Element) {
  fireEvent.pointerDown(el, { ...from, button: 0, pointerId: 1 });
  fireEvent.pointerMove(window, { ...from, clientX: (from.clientX + to.clientX) / 2, clientY: (from.clientY + to.clientY) / 2, pointerId: 1 });
  fireEvent.pointerMove(window, { ...to, pointerId: 1 });
  fireEvent.pointerUp(window, { ...to, pointerId: 1 });
  if (svg) fireEvent.click(svg, to);                     // 抬起落在地图上的那个 click
  await tick();
}

const MAPS = { maps: [{ map_id: "m1", version: "1", note: "", created_ms: 0, source: "" }] };

type Posted = { path: string; body: any };
function routesApi(opts: { meta: PreviewMeta; routes?: any[]; timezone?: string | null; schedules?: any[]; post?: (path: string, body: any) => Response | undefined }) {
  const posted: Posted[] = [];
  const view = () => ({ revision: 3, timezone: opts.timezone ?? null, routes: opts.routes ?? [], schedules: opts.schedules ?? [] });
  setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    if (u.endsWith("/api/maps")) return json(MAPS);
    if (u.endsWith("/preview")) return json(opts.meta);
    if (u.includes("/api/routes?")) return json(view());
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      posted.push({ path: u, body });
      const custom = opts.post?.(u, body);
      if (custom) return custom;
      if (u.includes("/api/routes/") && !u.endsWith("/delete")) {
        return json({ id: u.split("/").pop(), map_id: "m1", map_version: "1", revision: (body.base_revision ?? 0) + 1, bound: true,
          waypoints: body.waypoints, policy: {}, updated_by: "a", updated_ms: 0 });
      }
      return json({ ok: true });
    }
    return json({ error: "nope" }, 404);
  }) as never);
  return posted;
}

async function openNew(meta: PreviewMeta, name = "night", extra: Partial<Parameters<typeof routesApi>[0]> = {}) {
  installScreenCTM();
  const posted = routesApi({ meta, ...extra });
  const r = render(<RoutesPage say={() => {}} />);
  await ready(r.container);
  fireEvent.click(r.getByText("+ New route"));
  await tick();
  fireEvent.input(r.getByPlaceholderText("Route name"), { target: { value: name } });
  await tick();
  return { r, posted, svg: r.container.querySelector("svg.map")!, c: r.container };
}

describe("路线页:点图加点,存下去的是点的那一处的地图坐标", () => {
  const WANT: [number, number][] = [[1.23, -2.5], [4.56, -2.5], [4.56, 1.11], [-3.07, 6.4]];
  for (const deg of [undefined, 0, 30, -30]) {
    it(`转角 ${deg ?? "没给"}`, async () => {
      const meta: PreviewMeta = deg === undefined ? META : { ...META, rotation_deg: deg };
      const { r, posted, svg, c } = await openNew(meta);
      for (const [x, y] of WANT) {
        fireEvent.click(svg, clientOf(c, meta, x, y));
        await tick();
      }
      expect(c.querySelectorAll("[data-point]").length).toBe(WANT.length);
      fireEvent.click(r.getByText("Save route"));
      await waitFor(() => expect(posted.length).toBe(1));
      expect(posted[0].path).toContain("/api/routes/night");
      const b = posted[0].body;
      expect(b).toMatchObject({ map_id: "m1", map_version: "1", base_revision: 0 });
      expect(b.waypoints.length).toBe(WANT.length);
      b.waypoints.forEach((w: RoutePoint, i: number) => { near(w.x, WANT[i][0]); near(w.y, WANT[i][1]); });
      expect(b.waypoints.map((w: RoutePoint) => w.name)).toEqual(["P1", "P2", "P3", "P4"]);
    });
  }
  it("转 30° 又放大过", async () => {
    const meta = { ...META, rotation_deg: 30 };
    const { r, posted, svg, c } = await openNew(meta);
    const wheel = new MouseEvent("wheel", { bubbles: true, cancelable: true, clientX: RECT.left + 250, clientY: RECT.top + 350 });
    Object.defineProperty(wheel, "deltaY", { value: -400 });
    Object.defineProperty(wheel, "deltaMode", { value: 0 });
    svg.dispatchEvent(wheel);
    await tick();
    for (const [x, y] of WANT.slice(0, 2)) {
      fireEvent.click(svg, clientOf(c, meta, x, y));
      await tick();
    }
    fireEvent.click(r.getByText("Save route"));
    await waitFor(() => expect(posted.length).toBe(1));
    posted[0].body.waypoints.forEach((w: RoutePoint, i: number) => { near(w.x, WANT[i][0]); near(w.y, WANT[i][1]); });
  });
});

describe("路线页:拖点、拖朝向把手、调序、删、动作", () => {
  for (const deg of [0, 30, -30]) {
    it(`转 ${deg}°:拖点到哪儿存哪儿;拖把手朝向对;拖完不多出一个点`, async () => {
      const meta = { ...META, rotation_deg: deg };
      const { r, posted, svg, c } = await openNew(meta);
      for (const [x, y] of [[0, 0], [3, 0]]) {
        fireEvent.click(svg, clientOf(c, meta, x, y));
        await tick();
      }
      const pt0 = c.querySelector('[data-point="0"]')!;
      // 拖第 0 个点到 (-2, 3);抬起后 click 落在地图上也不许当成「加点」
      await drag(pt0, clientOf(c, meta, 0, 0), clientOf(c, meta, -2, 3), svg);
      expect(c.querySelectorAll("[data-point]").length).toBe(2);
      // 第 1 个点被选中:拖它的把手指向 (3, 4)(正上方)→ 朝向 +90°
      fireEvent.click(c.querySelector('[data-point="1"]')!);
      await tick();
      await drag(c.querySelector("[data-handle]")!, clientOf(c, meta, 3, 0), clientOf(c, meta, 3, 4), svg);
      expect(c.querySelectorAll("[data-point]").length).toBe(2);
      fireEvent.click(r.getByText("Save route"));
      await waitFor(() => expect(posted.length).toBe(1));
      const w = posted[0].body.waypoints as RoutePoint[];
      near(w[0].x, -2); near(w[0].y, 3);
      near(w[1].x, 3); near(w[1].y, 0);
      near(w[1].yaw, Math.PI / 2, 0.01);
    });
  }

  it("上移 / 下移 / 删除 / 改名 / 动作,存下去的顺序和内容就是屏上看到的", async () => {
    const { r, posted, svg, c } = await openNew(META);
    for (const [x, y] of [[0, 0], [1, 0], [2, 0]]) {
      fireEvent.click(svg, clientOf(c, META, x, y));
      await tick();
    }
    fireEvent.click(r.getByLabelText("Move up 3"));                    // P3 变成第 2 个
    fireEvent.click(r.getByLabelText("Remove point 1"));               // 删 P1
    await tick();
    expect([...c.querySelectorAll(".pointlist .link")].map((e) => e.textContent)).toEqual(["P3", "P2"]);
    fireEvent.click(c.querySelector('[data-point="0"]')!);
    await tick();
    fireEvent.input(r.getByLabelText("Point name"), { target: { value: "Gate" } });
    fireEvent.input(r.getByLabelText("Dwell (s)"), { target: { value: "5" } });
    fireEvent.click(r.getByLabelText("Photo (front)"));
    fireEvent.input(r.getByLabelText("What to check here"), { target: { value: "padlock" } });
    fireEvent.input(r.getByLabelText("Heading (°)"), { target: { value: "90" } });
    fireEvent.click(r.getByText("Save route"));
    await waitFor(() => expect(posted.length).toBe(1));
    const w = posted[0].body.waypoints as RoutePoint[];
    expect(w.map((p) => p.name)).toEqual(["Gate", "P2"]);
    expect(w[0].actions).toEqual([{ type: "dwell", seconds: 5 }, { type: "photo", camera: "front" }]);
    expect(w[0].check).toBe("padlock");
    near(w[0].yaw, Math.PI / 2, 0.001);
    near(w[0].x, 2); near(w[1].x, 1);
  });
});

describe("路线页:站点的回答", () => {
  const ROUTE = { id: "loop", map_id: "m1", map_version: "1", revision: 4, bound: true, updated_by: "a", updated_ms: 0, policy: {},
    waypoints: [P("a", 1, 1), P("b", 2, 2), P("c", 3, 3)] };

  it("逐点校验没过:错的点标红、说原因,存不下;改好重存按原来的修订号带过去", async () => {
    installScreenCTM();
    let fail = true;
    const posted = routesApi({ meta: META, routes: [ROUTE], post: () => fail
      ? json({ error: "2 处不能用", errors: [{ index: 1, name: "b", problem: "b 在图上不是可通行的地方" }, { index: 2, name: "c", problem: "c 离建图时走过的地方 9.0 m" }] }, 400)
      : undefined });
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.getByText("loop")).toBeTruthy());
    fireEvent.click(r.getByText("loop"));
    await tick();
    fireEvent.click(r.container.querySelector("svg.map")!, clientOf(r.container, META, 5, 5));   // 加一个点才算改过
    await tick();
    fireEvent.click(r.getByText("Save route"));
    await waitFor(() => expect(r.container.querySelectorAll("[data-point].bad").length).toBe(2));
    expect(r.container.textContent).toContain("b 在图上不是可通行的地方");
    expect(r.container.textContent).toContain("c 离建图时走过的地方");
    expect(posted[0].body.base_revision).toBe(4);
    fail = false;
    fireEvent.click(r.getByText("Save route"));
    await waitFor(() => expect(posted.length).toBe(2));
    await waitFor(() => expect(r.container.querySelectorAll("[data-point].bad").length).toBe(0));
  });

  it("别人改过(409):提示重新拉取,不覆盖;拉取后是站点上的样子", async () => {
    installScreenCTM();
    let first = true;
    setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.endsWith("/api/maps")) return json(MAPS);
      if (u.endsWith("/preview")) return json(META);
      if (u.includes("/api/routes?")) return json({ revision: 3, timezone: null, routes: [ROUTE], schedules: [] });
      if (u.endsWith("/api/routes/loop") && init?.method === "POST") return json({ error: "路线 loop 已经被改过(现在是第 5 版,你看到的是第 4 版)", errors: [] }, 409);
      if (u.endsWith("/api/routes/loop")) {
        const got = { ...ROUTE, revision: 5, waypoints: first ? [P("z", 0, 0)] : [] };
        first = false;
        return json(got);
      }
      return json({}, 404);
    }) as never);
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.getByText("loop")).toBeTruthy());
    fireEvent.click(r.getByText("loop"));
    await tick();
    fireEvent.click(r.container.querySelector("svg.map")!, clientOf(r.container, META, 5, 5));
    await tick();
    fireEvent.click(r.getByText("Save route"));
    await waitFor(() => expect(r.getByRole("alert").textContent).toContain("Someone else changed this route"));
    fireEvent.click(r.getByText("Reload"));
    await waitFor(() => expect(r.container.querySelectorAll("[data-point]").length).toBe(1));
  });

  it("列表里点哪条图上只画哪条;没选时只给提示;要对照再勾「同时显示其他路线」", async () => {
    installScreenCTM();
    const other = { ...ROUTE, id: "other", bound: false, waypoints: [P("x", 0, 0), P("y", 1, 0)] };
    const sch = { id: "n1", mission: "loop", at: "22:00", days: ["mon"], window_min: 30, on_missed: "skip", revision: 7 };
    routesApi({ meta: META, routes: [ROUTE, other], schedules: [sch] });
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.getByText("loop")).toBeTruthy());
    expect(r.container.querySelectorAll("polyline.route-line").length).toBe(0);
    expect(r.container.querySelectorAll(".route-pt").length).toBe(0);
    expect(r.container.querySelector("[data-route-hint]")).not.toBeNull();
    // 列表一行:点数、排程数、没绑地图版本的标出来
    expect(r.container.querySelector('[data-route-row="loop"]')?.textContent).toContain("1 schedule");
    expect(r.container.querySelector('[data-route-row="other"]')?.textContent).toContain("no map version");
    fireEvent.click(r.getByText("other"));
    await tick();
    expect([...r.container.querySelectorAll("polyline.route-line")].map((x) => x.getAttribute("data-route"))).toEqual(["other"]);
    expect(r.container.querySelectorAll(".route-pt.other").length).toBe(0);
    expect(r.container.querySelector("[data-route-hint]")).toBeNull();
    // 编辑页:列表不在了,「← 全部路线」回去
    expect(r.container.querySelector("[data-route-row]")).toBeNull();
    fireEvent.click(r.getByText("← All routes"));
    await tick();
    fireEvent.click(r.getByLabelText("Also show the other routes (dashed)"));
    fireEvent.click(r.getByText("loop"));
    await tick();
    expect(r.container.querySelector('polyline.route-line:not(.other)[data-route="loop"]')).not.toBeNull();
    expect(r.container.querySelector('polyline.route-line.other[data-route="other"]')).not.toBeNull();
  });

  it("右侧栏两页:列表页只有路线列表和「新建路线」;新建在编辑页顶上填名字,重名不能存;有没存的改动回列表先问", async () => {
    installScreenCTM();
    routesApi({ meta: META, routes: [ROUTE] });
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.getByText("loop")).toBeTruthy());
    expect(r.queryByPlaceholderText("Route name")).toBeNull();
    expect(r.queryByText("Save route")).toBeNull();
    fireEvent.click(r.getByText("+ New route"));
    await tick();
    expect(r.container.querySelector("[data-route-row]")).toBeNull();
    fireEvent.input(r.getByPlaceholderText("Route name"), { target: { value: "loop" } });
    await tick();
    expect(r.getByText("A route with this name already exists on the site.")).toBeTruthy();
    fireEvent.click(r.container.querySelector("svg.map")!, clientOf(r.container, META, 1, 1));
    await tick();
    expect((r.getByText("Save route") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.input(r.getByPlaceholderText("Route name"), { target: { value: "night" } });
    await tick();
    expect((r.getByText("Save route") as HTMLButtonElement).disabled).toBe(false);
    const asked: string[] = [];
    const orig = window.confirm;
    window.confirm = ((m: string) => { asked.push(m); return false; }) as typeof window.confirm;
    try {
      fireEvent.click(r.getByText("← All routes"));
      await tick();
      expect(asked.length).toBe(1);
      expect(r.getByPlaceholderText("Route name")).toBeTruthy();       // 不确认就留在编辑页
      window.confirm = (() => true) as typeof window.confirm;
      fireEvent.click(r.getByText("← All routes"));
      await tick();
      expect(r.container.querySelector('[data-route-row="loop"]')).not.toBeNull();
    } finally {
      window.confirm = orig;
    }
  });

  it("没改的不能存;删路线先确认,带当前修订号", async () => {
    installScreenCTM();
    const other = { ...ROUTE, id: "other", waypoints: [P("x", 0, 0), P("y", 1, 0)] };
    const posted = routesApi({ meta: META, routes: [ROUTE, other] });
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.getByText("loop")).toBeTruthy());
    fireEvent.click(r.getByText("loop"));
    await tick();
    expect((r.getByText("Save route") as HTMLButtonElement).disabled).toBe(true);
    expect(r.container.querySelector('polyline.route-line[data-route="loop"]')).not.toBeNull();
    fireEvent.click(r.getByText("Delete route"));
    await tick();
    fireEvent.click(r.container.ownerDocument.querySelector("dialog .btn.stop")!);
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].path).toContain("/api/routes/loop/delete");
    expect(posted[0].body).toEqual({ base_revision: 4 });
  });

  it("排程:没设时区先设;加排程带这条路线和基准 0;删排程带它的修订号", async () => {
    installScreenCTM();
    const sch = { id: "n1", mission: "loop", at: "22:00", days: ["mon"], window_min: 30, on_missed: "skip", revision: 7 };
    const posted = routesApi({ meta: META, routes: [ROUTE], timezone: null, schedules: [sch] });
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.getByText("loop")).toBeTruthy());
    fireEvent.click(r.getByText("loop"));
    await waitFor(() => expect(r.getByText("Set timezone")).toBeTruthy());
    fireEvent.input(r.getByPlaceholderText("Asia/Kuala_Lumpur"), { target: { value: "Asia/Kuala_Lumpur" } });
    fireEvent.click(r.getByText("Set timezone"));
    await waitFor(() => expect(posted.at(-1)!.path).toContain("/api/route-timezone"));
    expect(posted.at(-1)!.body).toEqual({ timezone: "Asia/Kuala_Lumpur" });
  });

  it("排程:有时区时列出这条路线的排程,加、删", async () => {
    installScreenCTM();
    const sch = { id: "n1", mission: "loop", at: "22:00", days: ["mon"], window_min: 30, on_missed: "skip", revision: 7 };
    const elsewhere = { ...sch, id: "n2", mission: "other" };
    const posted = routesApi({ meta: META, routes: [ROUTE], timezone: "UTC", schedules: [sch, elsewhere] });
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    await waitFor(() => expect(r.getByText("loop")).toBeTruthy());
    fireEvent.click(r.getByText("loop"));
    await waitFor(() => expect(r.getByText("n1")).toBeTruthy());
    expect(r.queryByText("n2")).toBeNull();
    fireEvent.input(r.container.querySelector('.form input[maxlength="64"]')!, { target: { value: "dawn" } });
    fireEvent.input(r.container.querySelector('input[type="time"]')!, { target: { value: "05:30" } });
    fireEvent.click(r.getByText("Add schedule"));
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].path).toContain("/api/route-schedules/dawn");
    expect(posted[0].body).toMatchObject({ mission: "loop", at: "05:30", window_min: 30, on_missed: "skip", base_revision: 0 });
    fireEvent.click(r.getByLabelText("Remove schedule n1"));
    await tick();
    fireEvent.click(r.container.ownerDocument.querySelector("dialog .btn.stop")!);
    await waitFor(() => expect(posted.length).toBe(2));
    expect(posted[1].path).toContain("/api/route-schedules/n1/delete");
    expect(posted[1].body).toEqual({ base_revision: 7 });
  });
});

// ------------------------------------------------------------------ 地图页:去这里 / 设位置

const robot = (id: string, pose: { x: number; y: number; yaw: number } | null): Robot => ({
  robot_id: id, active: true, fresh: true, revoked: false, held: null,
  loc: pose ? { pose } : null,
  status: { online: true, last_seen: 0 },
  capabilities: { tasks: { patrol: { map_id: "m1", map_version: "1" } } },
});
const unit = (r: Robot): UnitInfo => ({ robot: r, state: "idle", battery: 80, task: "" });

function mapPage(meta: PreviewMeta, role: string, post?: (path: string) => Response | undefined) {
  const posted: Posted[] = [];
  setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    if (u.endsWith("/api/maps/m1/1/preview")) return json(meta);
    if (u.endsWith("/api/maps/m1/1/areas")) return json({ areas: [] });
    if (init?.method === "POST") {
      posted.push({ path: u, body: JSON.parse(String(init.body)) });
      return post?.(u) ?? json({ ack: { result: "accepted" }, task_id: "t1" });
    }
    return json({ error: "nope" }, 404);
  }) as never);
  const r = render(<MapPage units={[unit(robot("A", { x: 0, y: 0, yaw: 0 }))]} p1={[]} now={0} role={role} selected={null}
    onSelect={() => {}} onVideo={() => {}} />);
  return { r, posted };
}

describe("地图页:点一点去这里 / 设位置,发出去的就是点的坐标和拖的朝向", () => {
  for (const deg of [0, 30, -30]) {
    it(`转 ${deg}°:去这里发 goto,目标 = 点的地图坐标、朝向 = 拖的方向`, async () => {
      installScreenCTM();
      const meta = { ...META, rotation_deg: deg };
      const { r, posted } = mapPage(meta, "guard");
      await ready(r.container);
      const svg = r.container.querySelector("svg.map")!;
      fireEvent.click(svg, clientOf(r.container, meta, 3.21, -1.5));
      await tick();
      const [hx, hy] = [3.21, 4.5];                                // 把手拖到正上方 → 朝向 90°
      await drag(r.container.querySelector('[data-handle="pose"]')!, clientOf(r.container, meta, 3.21, -1.5), clientOf(r.container, meta, hx, hy), svg);
      expect(r.container.querySelector("[data-pose-text]")!.textContent).toContain("x 3.21 m, y -1.5 m, heading 90°");
      fireEvent.click(r.getByText("Go here"));
      await waitFor(() => expect(posted.length).toBe(1));
      expect(posted[0].path).toContain("/api/robots/A/goto");
      const t = posted[0].body.target;
      expect(t).toMatchObject({ map_id: "m1", map_version: "1", frame_id: "map" });
      near(t.x, 3.21); near(t.y, -1.5); near(t.yaw, Math.PI / 2, 0.01);
      await waitFor(() => expect(r.container.querySelector("[data-pose-msg]")!.textContent).toContain("Go-to sent to A"));
    });
  }

  it("站点拒了:原因原样显示(比如监护模式要手机的租约)", async () => {
    installScreenCTM();
    const { r } = mapPage(META, "admin", () => json({ error: "A 要人现场监护:先在手机上打开监护" }, 409));
    await ready(r.container);
    fireEvent.click(r.container.querySelector("svg.map")!, clientOf(r.container, META, 1, 1));
    await tick();
    fireEvent.click(r.getByText("Go here"));
    await waitFor(() => expect(r.container.querySelector("[data-pose-msg]")!.textContent).toContain("A 要人现场监护:先在手机上打开监护"));
  });

  it("设位置:先弹确认(写着 x、y、朝向),确认后发 relocalize,x/y/yaw 就是点的;取消不发", async () => {
    installScreenCTM();
    const meta = { ...META, rotation_deg: 30 };
    const { r, posted } = mapPage(meta, "admin");
    await ready(r.container);
    fireEvent.click(r.container.querySelector("svg.map")!, clientOf(r.container, meta, -2.5, 2.25));
    await tick();
    fireEvent.click(r.getByText("Set position here"));
    await tick();
    const dlg = r.container.ownerDocument.querySelector("dialog")!;
    expect(dlg.textContent).toContain("x -2.5 m, y 2.25 m");
    fireEvent.click([...dlg.querySelectorAll("button")].find((b) => b.textContent === "Cancel")!);
    await tick();
    expect(posted.length).toBe(0);
    fireEvent.click(r.getByText("Set position here"));
    await tick();
    fireEvent.click([...r.container.ownerDocument.querySelectorAll("dialog button")].find((b) => b.textContent === "Set position here")!);
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].path).toContain("/api/robots/A/relocalize");
    near(posted[0].body.x, -2.5); near(posted[0].body.y, 2.25);
    // 外审 W39-I2:带上底图的图号、版本,站点核对
    expect(Object.keys(posted[0].body).sort()).toEqual(["map_id", "map_version", "x", "y", "yaw"]);
  });

  it("业主、没带角色的看不到这套工具,点图也不选位置", async () => {
    installScreenCTM();
    for (const role of ["owner", ""]) {
      const { r } = mapPage(META, role);
      await ready(r.container);
      fireEvent.click(r.container.querySelector("svg.map")!, clientOf(r.container, META, 1, 1));
      await tick();
      expect(r.queryByText("Go here")).toBeNull();
      expect(r.container.querySelector("[data-pose]")).toBeNull();
      cleanup();
    }
  });

  it("点机器人(选机器人)不会同时选出一个位置", async () => {
    installScreenCTM();
    const { r } = mapPage(META, "admin");
    await ready(r.container);
    await waitFor(() => expect(r.container.querySelector("g.bot")).not.toBeNull());
    fireEvent.click(r.container.querySelector("g.bot")!);
    await tick();
    expect(r.container.querySelector("[data-pose]")).toBeNull();
  });
});

// ------------------------------------------------------------------ 外审 R2(2026-10-15 下午):迟到的回包

describe("路线页:读、存、删的回包晚到,不进现在这一页(换了图、换了路线、回了列表)", () => {
  const MAPS2 = { maps: [{ map_id: "B", version: "1", note: "" }, { map_id: "A", version: "1", note: "" }] };
  const route = (map: string, id: string, rev = 1, pts = [P("P1", 1, 1)]) =>
    ({ id, map_id: map, map_version: "1", bound: true, revision: rev, waypoints: pts, policy: {}, schedules: [], updated_ms: 0, updated_by: "admin" });

  function slowSite(o: { holdSave?: boolean; holdList?: string; failSave?: boolean }) {
    const held: { save?: (r: Response) => void; list?: (r: Response) => void } = {};
    const posted: [string, any][] = [];
    setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posted.push([u, body]);
        if (posted.length === 1 && o.holdSave) return new Promise<Response>((r) => { held.save = r; });
        return json(route(body.map_id, u.split("/").pop()!, 3, body.waypoints));
      }
      if (u === "/api/maps") return json(MAPS2);
      if (u.endsWith("/preview")) return json(META);
      if (u.includes("/api/routes?")) {
        const m = u.includes("map_id=B") ? "B" : "A";
        const body = { revision: 1, timezone: null, routes: [route(m, m === "B" ? "br" : "ar")], schedules: [] };
        if (o.holdList === m && !held.list) return new Promise<Response>((r) => { held.list = r; });
        return json(body);
      }
      if (u.endsWith("/api/routes/ar")) return json(route("A", "ar", 5));
      return json({}, 404);
    }) as never);
    return { held, posted };
  }

  async function editAr(r: ReturnType<typeof render>) {
    fireEvent.change(r.getByLabelText("Map version"), { target: { value: "A\u00001" } });
    await waitFor(() => expect(r.container.querySelector('[data-route-row="ar"]')).not.toBeNull());
    fireEvent.click(r.container.querySelector('[data-route-row="ar"]')!);
    await tick();
    fireEvent.click(r.getByText("P1"));
    fireEvent.input(r.getByLabelText("x (m)"), { target: { value: "2" } });
  }

  it("存 A 的路线时回列表、换到 B 编 br:A 的回包晚到不把 ar 放进来;在 B 上存的是 br、B 图", async () => {
    installScreenCTM();
    (window as any).confirm = vi.fn(() => true);
    const { held, posted } = slowSite({ holdSave: true });
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    await editAr(r);
    fireEvent.click(r.getByText("Save route"));
    await waitFor(() => expect(posted.length).toBe(1));
    fireEvent.click(r.getByText("← All routes"));
    fireEvent.change(r.getByLabelText("Map version"), { target: { value: "B\u00001" } });
    await waitFor(() => expect(r.container.querySelector('[data-route-row="br"]')).not.toBeNull());
    fireEvent.click(r.container.querySelector('[data-route-row="br"]')!);
    await tick();
    held.save!(json(route("A", "ar", 2, posted[0][1].waypoints)));
    await new Promise((res) => setTimeout(res, 20));
    expect(r.container.querySelector(".routeside h2")?.textContent).toBe("br");
    expect((r.getByLabelText("Map version") as HTMLSelectElement).value).toBe("B\u00001");
    fireEvent.click(r.getByText("P1"));
    fireEvent.input(r.getByLabelText("x (m)"), { target: { value: "3" } });
    fireEvent.click(r.getByText("Save route"));
    await waitFor(() => expect(posted.length).toBe(2));
    expect(posted[1][0]).toBe("/api/routes/br");
    expect(posted[1][1]).toMatchObject({ map_id: "B", map_version: "1", base_revision: 1 });
  });

  it("A → B → A 又打开 ar:第一次存的回包晚到,不盖掉重新打开的那一份", async () => {
    installScreenCTM();
    (window as any).confirm = vi.fn(() => true);
    const { held, posted } = slowSite({ holdSave: true });
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    await editAr(r);
    fireEvent.click(r.getByText("Save route"));
    await waitFor(() => expect(posted.length).toBe(1));
    fireEvent.click(r.getByText("← All routes"));
    fireEvent.change(r.getByLabelText("Map version"), { target: { value: "B\u00001" } });
    await waitFor(() => expect(r.container.querySelector('[data-route-row="br"]')).not.toBeNull());
    await editAr(r);                                         // 又回到 A、又打开 ar、又改了
    held.save!(json(route("A", "ar", 2, [P("P1", 9, 9)])));
    await new Promise((res) => setTimeout(res, 20));
    expect(r.getByLabelText("x (m)")).toHaveProperty("value", "2");
    expect(r.queryByText("Route saved (revision 2)")).toBeNull();
  });

  it("A 的路线列表晚到:不写进 B 的列表", async () => {
    installScreenCTM();
    const { held } = slowSite({ holdList: "A" });
    const r = render(<RoutesPage say={() => {}} />);
    await ready(r.container);
    fireEvent.change(r.getByLabelText("Map version"), { target: { value: "A\u00001" } });
    await waitFor(() => expect(held.list).toBeTruthy());
    fireEvent.change(r.getByLabelText("Map version"), { target: { value: "B\u00001" } });
    await waitFor(() => expect(r.container.querySelector('[data-route-row="br"]')).not.toBeNull());
    held.list!(json({ revision: 1, timezone: null, routes: [route("A", "ar")], schedules: [] }));
    await new Promise((res) => setTimeout(res, 20));
    expect(r.container.querySelector('[data-route-row="ar"]')).toBeNull();
    expect(r.container.querySelector('[data-route-row="br"]')).not.toBeNull();
  });

  it("离开以后才回来的保存报错:不显示、不标点", async () => {
    installScreenCTM();
    (window as any).confirm = vi.fn(() => true);
    const said: string[] = [];
    let fail: (r: Response) => void = () => {};
    setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (init?.method === "POST") return new Promise<Response>((r) => { fail = r; });
      if (u === "/api/maps") return json(MAPS2);
      if (u.endsWith("/preview")) return json(META);
      if (u.includes("/api/routes?")) return json({ revision: 1, timezone: null, routes: [route("B", "br")], schedules: [] });
      return json({}, 404);
    }) as never);
    const r = render(<RoutesPage say={(s) => said.push(s)} />);
    await ready(r.container);
    await waitFor(() => expect(r.container.querySelector('[data-route-row="br"]')).not.toBeNull());
    fireEvent.click(r.container.querySelector('[data-route-row="br"]')!);
    await tick();
    fireEvent.click(r.getByText("P1"));
    fireEvent.input(r.getByLabelText("x (m)"), { target: { value: "2" } });
    fireEvent.click(r.getByText("Save route"));
    await tick();
    fireEvent.click(r.getByText("← All routes"));
    await tick();
    fail(json({ error: "P1 is not a drivable spot", errors: [{ index: 0, problem: "wall" }] }, 400));
    await new Promise((res) => setTimeout(res, 20));
    expect(said.filter((s) => s.includes("drivable"))).toEqual([]);
    expect(r.container.querySelector('[data-route-row="br"]')).not.toBeNull();
  });
});
