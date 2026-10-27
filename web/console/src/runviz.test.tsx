// @vitest-environment happy-dom
// 运行可视化(网页):地图上的规划路径 / 轨迹 / 被挡 / 定位问题的点(转角下对得上)、每只狗下面的定位原因和
// 被挡说法、定位历史页的过滤、摄像头登记(校验警告、口令不回显)。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { CamerasPanel } from "./camadmin";
import { lang } from "./i18n";
import type { UnitInfo } from "./live";
import { LocHistoryPage } from "./locpage";
import { MapPage } from "./mappage";
import { applyMat, toPx, type Mat, type PreviewMeta } from "./mapview";
import type { Robot } from "./model";
import { RunInfo } from "./runinfo";
import { blockedText, episodesPath, locLine, locState, replanText, type Episode, type VizData } from "./runviz";

const META: PreviewMeta = { width: 400, height: 300, m_per_px: 0.05, left_x: -10, top_y: 8 };
const D = Math.PI / 180;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  lang.value = "en";
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function installScreenCTM() {
  // 跟 mapview.test.tsx 一样:happy-dom 没有 getScreenCTM,只要世界层存在、图是放在框里的就行
  const proto = SVGElement.prototype as unknown as { getScreenCTM?: () => unknown };
  proto.getScreenCTM = function () {
    return { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0, inverse() { return this; } };
  };
}

function worldOf(c: Element): Mat {
  const g = c.querySelector("g.map-world")!;
  const m = /^matrix\(([^)]+)\)$/.exec(g.getAttribute("transform") ?? "")!;
  const [a, b, cc, d, e, f] = m[1].trim().split(/[\s,]+/).map(Number);
  return { a, b, c: cc, d, e, f };
}

const robot = (id: string, over: Partial<Robot> = {}): Robot => ({
  robot_id: id, active: true, fresh: true, revoked: false, held: null,
  loc: { pose: { x: 2, y: 3, yaw: 0 }, anchored: true, source: "scan_match", sigma_m: 0.12, quality: 0.88, reason: "" },
  status: { online: true, last_seen: 0 },
  capabilities: { tasks: { patrol: { map_id: "m1", map_version: "1" } } },
  ...over,
});
const unit = (r: Robot): UnitInfo => ({ robot: r, state: "idle", battery: 80, task: "" });

const EPISODE: Episode = {
  id: 1, robot_id: "A", kind: "lost", start_ms: 1_000_000, end_ms: 1_060_000, ongoing: false, duration_s: 60, end_how: "recovered",
  cause: "定位器给的位置是 3.0 秒前的", source: "bridge", sigma_m: null, task_id: "patrol-3", map_id: "m1", map_version: "1", x: -3, y: 2,
};
const VIZ: VizData = {
  robots: {
    A: {
      map: { map_id: "m1", version: "1" },
      path: [[2, 3], [4, 3], [4, 6]],
      trail: [[0, 3], [1, 3], [2, 3]],
      blocked: { since_ms: 1, for_s: 12, reason: "前方 0.4 m 有挡", x: 4, y: 4.5, map_id: "m1", map_version: "1" },
      replan: { why: "detour", n: 1, at_ms: 1 },
    },
  },
};

function mapPage(meta: PreviewMeta, viz: VizData | null = VIZ, eps: Episode[] = [EPISODE]) {
  const urls: string[] = [];
  setFetch((async (url: RequestInfo | URL) => {
    const u = String(url);
    urls.push(u);
    if (u.endsWith("/api/maps/m1/1/preview")) return json(meta);
    if (u.endsWith("/api/maps/m1/1/areas")) return json({ areas: [] });
    if (u.startsWith("/api/runviz")) return json(viz ?? { robots: {} });
    if (u.startsWith("/api/loc-episodes")) return json({ episodes: eps });
    return json({ error: "nope" }, 404);
  }) as never);
  const r = render(<MapPage units={[unit(robot("A"))]} p1={[]} now={0} selected={null} onSelect={() => {}} onVideo={() => {}} />);
  return { ...r, urls };
}

describe("地图上的规划路径、轨迹、被挡、定位问题的点", () => {
  for (const deg of [0, 23, -31]) {
    it(`转 ${deg}°:路径和轨迹在世界层里(预览像素,跟底图一起转);被挡的三角、问题点在标记层、屏上位置对得上`, async () => {
      installScreenCTM();
      const meta = { ...META, rotation_deg: deg };
      const { container: c } = mapPage(meta);
      await waitFor(() => expect(c.querySelector("polyline.rv-path")).not.toBeNull());
      const m = worldOf(c);
      expect(Math.atan2(m.b, m.a) / D).toBeCloseTo(deg, 9);
      const world = c.querySelector("g.map-world")!;
      const path = world.querySelector("polyline.rv-path")!;
      const want = [[2, 3], [4, 3], [4, 6]].map(([x, y]) => { const p = toPx(meta, x, y); return `${p.px.toFixed(1)},${p.py.toFixed(1)}`; }).join(" ");
      expect(path.getAttribute("points")).toBe(want);
      expect(world.querySelector("polyline.rv-trail")).not.toBeNull();
      // 标记层:不在世界层里,位置 = 世界层变换作用在预览像素上
      await waitFor(() => expect(c.querySelector("circle.rv-ep")).not.toBeNull());
      const dot = c.querySelector("circle.rv-ep")!;
      expect(dot.closest("g.map-world")).toBeNull();
      const p = toPx(meta, -3, 2);
      const [sx, sy] = applyMat(m, p.px, p.py);
      expect(Number(dot.getAttribute("cx"))).toBeCloseTo(sx, 6);
      expect(Number(dot.getAttribute("cy"))).toBeCloseTo(sy, 6);
      const tri = c.querySelector("g.rv-blocked path")!;
      const q = toPx(meta, 4, 4.5);
      const [bx, by] = applyMat(m, q.px, q.py);
      const nums = tri.getAttribute("d")!.match(/-?\d+(\.\d+)?/g)!.map(Number);
      expect(nums[0]).toBeCloseTo(bx, 6);          // M x y-h:顶点在被挡的 x 上
      expect(nums[1]).toBeCloseTo(by - 9, 6);
      // 终点标记
      expect(c.querySelector("path.rv-goal")).not.toBeNull();
    });
  }

  // 2026-10-15 改版:运行图层的图例并进地图自己那一行图例(不另占一行),这里在那一行里找。
  it("图例只列图上有的:有路径、轨迹、被挡、问题点时四项都在(并在地图图例那一行里);什么都没有就一项都没有", async () => {
    installScreenCTM();
    const RUN = ["path", "trail", "blocked", "episode"];
    const runKeys = (root: Element) => [...root.querySelectorAll(".maplegend [data-legend]")]
      .map((e) => e.getAttribute("data-legend")).filter((k) => RUN.includes(k ?? ""));
    const { container: c } = mapPage(META);
    await waitFor(() => expect(c.querySelector('.maplegend [data-legend="episode"]')).not.toBeNull());
    expect(runKeys(c)).toEqual(RUN);
    expect(c.querySelector('[data-testid="run-legend"]')).toBeNull();
    cleanup();
    const empty = mapPage(META, { robots: {} }, []);
    await waitFor(() => expect(empty.container.querySelector("g.map-world")).not.toBeNull());
    expect(runKeys(empty.container)).toEqual([]);
    expect(empty.container.querySelector("polyline.rv-path")).toBeNull();
  });

  it("别的图上的路径不画;拿不到 runviz 也不影响地图", async () => {
    installScreenCTM();
    const other: VizData = { robots: { A: { ...VIZ.robots.A, map: { map_id: "m1", version: "2" } } } };
    const { container: c } = mapPage(META, other, []);
    await waitFor(() => expect(c.querySelector("g.map-world")).not.toBeNull());
    expect(c.querySelector("polyline.rv-path")).toBeNull();
    cleanup();
    setFetch((async (url: RequestInfo | URL) => {
      const u = String(url);
      if (u.endsWith("/preview")) return json(META);
      if (u.endsWith("/areas")) return json({ areas: [] });
      return json({ error: "boom" }, 500);
    }) as never);
    const r = render(<MapPage units={[unit(robot("A"))]} p1={[]} now={0} selected={null} onSelect={() => {}} onVideo={() => {}} />);
    await waitFor(() => expect(r.container.querySelector("g.map-world")).not.toBeNull());
  });

  it("侧栏:选中的机器人下面写定位和被挡;定位问题只查这张图近 7 天", async () => {
    installScreenCTM();
    const { container: c, urls } = mapPage(META);
    await waitFor(() => expect(c.querySelector('[data-testid="run-info"]')).not.toBeNull());
    expect(urls.some((u) => u.startsWith("/api/loc-episodes?") && u.includes("map_id=m1") && u.includes("map_version=1") && u.includes("since_ms="))).toBe(true);
  });
});

describe("每台机器人下面的定位和被挡", () => {
  it("定位正常:来源、偏差、质量;变差:带原因;丢了:原话;没设位置;老机器人没上报", () => {
    expect(locState(robot("A").loc)).toBe("ok");
    expect(locLine(robot("A").loc)).toBe("Localization: scan matching, error about 0.1 m");
    const deg = { ...robot("A").loc!, degraded: "定位偏差可能到 0.60 m" };
    expect(locState(deg)).toBe("degraded");
    expect(locLine(deg)).toContain("degraded: 定位偏差可能到 0.60 m");
    const lost = { ...robot("A").loc!, reason: "定位偏差可能到 4.0 m" };
    expect(locState(lost)).toBe("lost");
    expect(locLine(lost)).toBe("Localization unreliable: 定位偏差可能到 4.0 m");
    const unset = { anchored: false, reason: "" };
    expect(locState(unset)).toBe("unset");
    expect(locLine(unset)).toBe("Localization: position not set");
    expect(locState(null)).toBe("unknown");
    expect(locLine(null)).toBe("Localization: not reported");
    lang.value = "zh";
    expect(locLine(robot("A").loc)).toBe("定位:点云匹配,偏差约 0.1 m");
  });

  it("RunInfo:变差标橙、丢了标红带形状;被挡写「被挡 N 秒:原因」;重规划写次数和原因", () => {
    const r = robot("A", {
      loc: { ...robot("A").loc!, degraded: "在拿里程推算" },
      nav: { blocked: VIZ.robots.A.blocked, replan: VIZ.robots.A.replan },
    });
    const { container: c } = render(<RunInfo robot={r} />);
    const loc = c.querySelector("[data-loc]")!;
    expect(loc.getAttribute("data-loc")).toBe("degraded");
    expect(loc.className).toBe("warn");
    expect(loc.textContent).toContain("0.88");
    expect(c.querySelector("[data-nav=blocked]")!.textContent).toBe("Blocked 12 s: 前方 0.4 m 有挡");
    expect(c.querySelector("[data-nav=replan]")!.textContent).toBe("Re-planned 1× (detouring around an obstacle)");
    cleanup();
    const lost = render(<RunInfo robot={robot("A", { loc: { anchored: true, reason: "雷达没数据" } })} />);
    expect(lost.container.querySelector("[data-loc=lost]")!.className).toBe("lost");
    expect(lost.container.querySelector("[data-loc=lost] svg.shape")).not.toBeNull();
    cleanup();
    expect(render(<RunInfo robot={robot("A", { loc: undefined, nav: undefined })} />).container.innerHTML).toBe("");
  });

  it("说法:被挡没给原因、重规划各种原因、中文", () => {
    expect(blockedText({ ...VIZ.robots.A.blocked!, reason: "" })).toBe("Blocked 12 s: no reason given");
    expect(replanText({ why: "off_route", n: 2, at_ms: 0 })).toBe("Re-planned 2× (off the route)");
    expect(replanText({ why: "zones", n: 1, at_ms: 0 })).toContain("zones changed");
    expect(replanText({ why: "???", n: 1, at_ms: 0 })).toContain("off the route");
    lang.value = "zh";
    expect(blockedText(VIZ.robots.A.blocked!)).toBe("被挡 12 秒:前方 0.4 m 有挡");
    expect(replanText({ why: "near_nogo", n: 3, at_ms: 0 })).toBe("重规划 3 次(离禁行区太近)");
  });
});

describe("定位历史页", () => {
  const ROWS: Episode[] = [EPISODE, { ...EPISODE, id: 2, kind: "degraded", end_ms: null, ongoing: true, end_how: "", cause: "在拿里程推算", x: null, y: null }];

  function page() {
    const urls: string[] = [];
    setFetch((async (url: RequestInfo | URL) => {
      urls.push(String(url));
      return json({ episodes: ROWS });
    }) as never);
    const r = render(<LocHistoryPage robots={["A", "B"]} now={1_700_000_000_000} />);
    return { ...r, urls };
  }

  it("列出每一段:时长、种类、地点、机器人的原话、任务、怎么结束的;没结束的写还没恢复;没有地点的不给地图链接", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelectorAll("tbody tr").length).toBe(2));
    const rows = [...c.querySelectorAll("tbody tr")];
    expect(rows[0].textContent).toContain("1 min 0 s");
    expect(rows[0].textContent).toContain("Not localized");
    expect(rows[0].textContent).toContain("(-3.0, 2.0) m");
    expect(rows[0].textContent).toContain("定位器给的位置是 3.0 秒前的");
    expect(rows[0].textContent).toContain("patrol-3");
    expect(rows[0].textContent).toContain("recovered");
    expect(rows[0].querySelector("a")!.getAttribute("href")).toBe("#/map?robot=A");
    expect(rows[1].textContent).toContain("still ongoing");
    expect(rows[1].querySelector("a")).toBeNull();
  });

  it("过滤:选机器人、种类、时间段都进查询;默认最近 7 天", async () => {
    const { container: c, urls } = page();
    await waitFor(() => expect(urls.length).toBeGreaterThan(0));
    expect(urls[0]).toMatch(/^\/api\/loc-episodes\?since_ms=\d+&limit=500$/);
    const [robotSel, kindSel, periodSel] = [...c.querySelectorAll("select")] as HTMLSelectElement[];
    fireEvent.change(robotSel, { target: { value: "B" } });
    await waitFor(() => expect(urls.some((u) => u.includes("robot=B"))).toBe(true));
    fireEvent.change(kindSel, { target: { value: "degraded" } });
    await waitFor(() => expect(urls.some((u) => u.includes("robot=B") && u.includes("kind=degraded"))).toBe(true));
    fireEvent.change(periodSel, { target: { value: "all" } });
    await waitFor(() => expect(urls.at(-1)).toBe("/api/loc-episodes?robot=B&kind=degraded&limit=500"));
  });

  it("查询串:空的不带", () => {
    expect(episodesPath({})).toBe("/api/loc-episodes");
    expect(episodesPath({ robot: "", kind: "lost", since_ms: 5 })).toBe("/api/loc-episodes?kind=lost&since_ms=5");
  });

  it("拿不到:显示错误,不假装没有问题", async () => {
    setFetch((async () => json({ error: "站点忙" }, 500)) as never);
    const { container: c } = render(<LocHistoryPage robots={["A"]} now={1_700_000_000_000} />);
    await waitFor(() => expect(c.querySelector("[role=alert]")).not.toBeNull());
    expect(c.querySelector("[role=alert]")!.textContent).toContain("站点忙");
    expect(c.textContent).not.toContain("No localization problems");
  });
});

describe("摄像头登记", () => {
  const CAM = {
    name: "gate-cam", zone: "front-gate", onvif_url: "http://192.168.1.50", username: "admin", has_password: true,
    rtsp_set: true, rtsp_url: "rtsp://192.168.1.50/s", motion: false, zone_mapped: false, zone_polygon: true,
    warnings: [{ code: "zone_unmapped", text: "x" }],
  };

  function mount(rows = [CAM]) {
    const posts: { url: string; body: Record<string, unknown> }[] = [];
    setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posts.push({ url: u, body });
        return json({ ok: true, camera: CAM, warnings: [{ code: "zone_no_polygon", text: "y" }] });
      }
      if (u === "/api/admin/cameras") return json({ cameras: rows });
      if (u === "/api/cameras") return json({ cameras: [{ name: "gate-cam", connected: true, error: "" }] });
      return json({}, 404);
    }) as never);
    const say = vi.fn();
    const r = render(<CamerasPanel say={say} />);
    return { ...r, posts, say };
  }

  it("列表:防区警告随行显示;状态连上了;口令只说有没有", async () => {
    const { container: c } = mount();
    await waitFor(() => expect(c.querySelector("tbody tr")).not.toBeNull());
    const row = c.querySelector("tbody tr")!;
    expect(row.querySelector("[data-warning=zone_unmapped]")!.textContent).toContain("not mapped to an intercept point");
    expect(row.textContent).toContain("(password set)");
    expect(row.textContent).toContain("Connected");
  });

  it("添加:POST 带口令;存好后把站点的防区警告说出来", async () => {
    const { container: c, posts, say } = mount([]);
    await waitFor(() => expect(c.textContent).toContain("No cameras yet."));
    fireEvent.click([...c.querySelectorAll("button")].find((b) => b.textContent === "Add camera")!);
    const inputs = () => [...document.querySelectorAll(".camform input")] as HTMLInputElement[];
    const set = (i: number, v: string) => fireEvent.input(inputs()[i], { target: { value: v } });
    await waitFor(() => expect(inputs().length).toBeGreaterThan(5));
    set(0, "yard-cam"); set(1, "http://10.0.0.9"); set(2, "bob"); set(3, "pw-1"); set(4, "back-yard");
    fireEvent.submit(document.querySelector(".camform")!);
    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts[0].url).toBe("/api/admin/cameras");
    expect(posts[0].body).toMatchObject({ name: "yard-cam", onvif_url: "http://10.0.0.9", username: "bob", password: "pw-1", zone: "back-yard", edit: false });
    await waitFor(() => expect(say).toHaveBeenCalled());
    expect(say.mock.calls[0][0]).toContain("no polygon");
  });

  it("改:不勾「换口令」就不带 password 键(站点保留原来的);勾了才带;口令框不预填", async () => {
    const { container: c, posts } = mount();
    await waitFor(() => expect(c.querySelector("tbody tr")).not.toBeNull());
    fireEvent.click([...c.querySelectorAll("button")].find((b) => b.textContent === "Edit")!);
    await waitFor(() => expect(document.querySelector(".camform")).not.toBeNull());
    expect(document.querySelector<HTMLInputElement>("input[type=password]")).toBeNull();
    expect(document.querySelector<HTMLInputElement>(".camform input[readonly]")!.value).toBe("gate-cam");
    fireEvent.submit(document.querySelector(".camform")!);
    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts[0].body.edit).toBe(true);
    expect("password" in posts[0].body).toBe(false);
    // 再改一次,这回换口令
    cleanup();
    const again = mount();
    await waitFor(() => expect(again.container.querySelector("tbody tr")).not.toBeNull());
    fireEvent.click([...again.container.querySelectorAll("button")].find((b) => b.textContent === "Edit")!);
    await waitFor(() => expect(document.querySelector(".camform")).not.toBeNull());
    fireEvent.click(document.querySelector<HTMLInputElement>(".camform input[type=checkbox]")!);
    const pw = await waitFor(() => { const e = document.querySelector<HTMLInputElement>("input[type=password]"); expect(e).not.toBeNull(); return e!; });
    expect(pw.value).toBe("");
    fireEvent.input(pw, { target: { value: "new-pw" } });
    fireEvent.submit(document.querySelector(".camform")!);
    await waitFor(() => expect(again.posts.length).toBe(1));
    expect(again.posts[0].body.password).toBe("new-pw");
  });

  it("删:确认以后才发", async () => {
    const { container: c, posts } = mount();
    await waitFor(() => expect(c.querySelector("tbody tr")).not.toBeNull());
    fireEvent.click([...c.querySelectorAll("button")].find((b) => b.textContent === "Remove")!);
    expect(posts.length).toBe(0);
    fireEvent.click([...document.querySelectorAll("button")].find((b) => b.textContent === "Remove" && b.closest("[role=dialog], .modal, .dialog") !== null) ?? [...document.querySelectorAll("button")].filter((b) => b.textContent === "Remove").at(-1)!);
    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts[0].url).toBe("/api/admin/cameras/gate-cam/remove");
  });

  it("站点拒了(400):错误显示在表单里,不关表单", async () => {
    setFetch((async (url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") return json({ error: "地址里不要带账号口令" }, 400);
      if (String(url) === "/api/admin/cameras") return json({ cameras: [] });
      return json({ cameras: [] });
    }) as never);
    const { container: c } = render(<CamerasPanel say={() => {}} />);
    await waitFor(() => expect(c.textContent).toContain("No cameras yet."));
    fireEvent.click([...c.querySelectorAll("button")].find((b) => b.textContent === "Add camera")!);
    await waitFor(() => expect(document.querySelector(".camform")).not.toBeNull());
    const f = [...document.querySelectorAll(".camform input")] as HTMLInputElement[];
    fireEvent.input(f[0], { target: { value: "c1" } });
    fireEvent.input(f[1], { target: { value: "http://u:p@h" } });
    fireEvent.input(f[4], { target: { value: "z" } });
    fireEvent.submit(document.querySelector(".camform")!);
    await waitFor(() => expect(document.querySelector(".camform [role=alert]")).not.toBeNull());
    expect(document.querySelector(".camform [role=alert]")!.textContent).toContain("地址里不要带账号口令");
  });
});

describe("W41 轨迹分段:遥控器开的那段不连线", () => {
  it("按断点切段,一个点的段不画;老站点没有断点就是一整条", async () => {
    const { trailSegs } = await import("./runviz");
    const tr: [number, number][] = [[0, 0], [1, 0], [2, 0], [5, 5], [9, 9], [10, 9]];
    expect(trailSegs({ trail: tr, trail_cuts: [3, 4] })).toEqual([[[0, 0], [1, 0], [2, 0]], [[9, 9], [10, 9]]]);
    expect(trailSegs({ trail: tr })).toEqual([tr]);
    expect(trailSegs({ trail: tr, trail_cuts: [0, 99, -1, 1.5] })).toEqual([tr]);
    expect(trailSegs({ trail: [[0, 0]] })).toEqual([]);
  });
  it("地图上每段一条线", async () => {
    installScreenCTM();
    const viz: VizData = { robots: { A: { ...VIZ.robots.A, trail: [[0, 3], [1, 3], [6, 6], [7, 6]], trail_cuts: [2] } } };
    const { container: c } = mapPage(META, viz);
    await waitFor(() => expect(c.querySelectorAll("polyline.rv-trail").length).toBe(2));
  });
});

describe("W41 Live 页 P1 报警卡片", () => {
  it("报警卡片里也有定位那一行", async () => {
    const { AlarmTile } = await import("./live");
    const a = { key: "k", kind: "intrusion", title: "有人闯入", robot: "A", first_ms: 0, acked_by: "", acked_ms: null, context: {} };
    const { container: c } = render(
      <AlarmTile a={a as never} u={unit(robot("A"))} role="admin" now={1000} onAck={() => {}} onSiren={() => {}} onFalse={() => {}} onMap={() => {}} onOpen={() => {}} />,
    );
    const ri = c.querySelector('.alarm-tile [data-testid="run-info"]');
    expect(ri).not.toBeNull();
    expect(ri!.textContent).toContain("scan matching");
  });
});
