// @vitest-environment happy-dom
// W73 第一期网页:布置 → 防区路线(按防区生成、存成 guard、没有排程)、巡逻路线每点预计时间、防区的查看点 / 值守位。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import type { LayerData, OwnLayer, TabProps, ZonePost } from "./layoutpage";
import { GuardRoutesTab, RoutesTab } from "./layoutroutes";
import { ZonesTab } from "./layoutzones";
import { clock } from "./routes";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const META = { width: 100, height: 100, m_per_px: 0.1, left_x: 0, top_y: 10, zones: [] };
const ROUTE = { id: "night", map_id: "home", map_version: "3", revision: 4, bound: true, policy: {}, updated_by: "o", updated_ms: 1,
  waypoints: [{ name: "p1", x: 1, y: 1, yaw: 0 }, { name: "p2", x: 6, y: 1, yaw: 0 }] };
const GEN = {
  waypoints: [{ name: "东区-1", x: 2, y: 2, yaw: 0, actions: [{ type: "photo", camera: "front" }] }, { name: "东区-2", x: 4, y: 2, yaw: 0 }],
  problems: [], zones: [{ zone: "东区", points: 2, note: "" }, { zone: "西区", points: 0, note: "防区里没有走得到的地方" }],
  spacing_m: 6, start: null,
};

let posted: { url: string; body: Record<string, unknown> }[];
let gets: string[];
let save: (() => Promise<void>) | null;
beforeEach(() => {
  lang.value = "zh";
  posted = [];
  gets = [];
  save = null;
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      if (url === "/api/guard-routes/generate") return Promise.resolve(json(GEN));
      if (url.endsWith("/areas")) return Promise.resolve(json({ areas: [], posts: [] }));
      return Promise.resolve(json({ ...ROUTE, id: url.split("/").pop(), revision: 1, kind: body.kind, waypoints: body.waypoints }));
    }
    gets.push(url);
    if (url.endsWith("/eta")) {
      return Promise.resolve(json({ id: "night", total_s: 125, speed_mps: 0.5, basis: "planned", waypoints: [
        { name: "p1", travel_m: 0, planned_s: 0, actual_s: null, samples: 0, use_s: 0, arrive_s: 0 },
        { name: "p2", travel_m: 5, planned_s: 10, actual_s: 31, samples: 5, use_s: 31, arrive_s: 125 }] }));
    }
    if (url.includes("/areas")) return Promise.resolve(json({ areas: [{ zone: "东区", points: [] }, { zone: "西区", points: [] }], posts: [] }));
    if (url.startsWith("/api/routes")) {
      return Promise.resolve(json({ revision: 1, timezone: "Asia/Kuala_Lumpur", routes: url.includes("kind=guard") ? [] : [ROUTE], schedules: [] }));
    }
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const screen = { map: (x: number, y: number) => [x * 10, (10 - y) * 10] as [number, number], px: (a: number, b: number) => [a, b] as [number, number], k: 1 };
const FakeMap = (o: OwnLayer) => (
  <div class="fakemap">
    <button type="button" class="pick" onClick={() => o.onPick?.(10, 20)}>pick</button>
    <svg>{o.world}{o.marks?.(screen)}</svg>
  </div>
);
const base = (data: Partial<LayerData> = {}): TabProps => ({
  map: { map_id: "home", version: "3", note: "" }, base: "/api/maps/home/3", meta: META,
  data: { areas: [], nav: null, routes: [], guardRoutes: [], posts: [], standby: [], cameras: [], robots: [], ...data }, say: () => {},
  reload: () => Promise.resolve(), setDirty: () => {}, registerSave: (fn) => { save = fn; }, Map: FakeMap,
});
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("clock", () => {
  it("精确到秒", () => {
    expect(clock(0)).toBe("0:00");
    expect(clock(125.4)).toBe("2:05");
    expect(clock(3725)).toBe("1:02:05");
  });
});

describe("布置 → 防区路线", () => {
  it("只列防区路线;按防区生成放进编辑页,存的时候带 kind guard;没有排程", async () => {
    const { container: c } = render(<GuardRoutesTab {...base()} />);
    await waitFor(() => expect(gets.some((u) => u.startsWith("/api/routes?") && u.includes("kind=guard"))).toBe(true));
    expect(c.textContent).toContain("不能排进巡逻排程");
    fireEvent.click(btn(c, "按防区生成"));
    await waitFor(() => expect(c.querySelector('[data-gen-zone="东区"]')).not.toBeNull());
    fireEvent.input(c.querySelector('.guard-gen input[aria-label="路线名"]')!, { target: { value: "night-guard" } });
    fireEvent.input(c.querySelector('input[aria-label="东区 转几圈"]')!, { target: { value: "2" } });
    fireEvent.submit(c.querySelector(".guard-gen")!);
    await waitFor(() => expect(posted.some((p) => p.url === "/api/guard-routes/generate")).toBe(true));
    const gen = posted.find((p) => p.url === "/api/guard-routes/generate")!.body;
    expect(gen.zones).toEqual([{ zone: "东区", laps: 2, dwell_s: 0, photos: 1 }, { zone: "西区", laps: 1, dwell_s: 0, photos: 1 }]);
    await waitFor(() => expect(c.querySelectorAll(".pointlist li").length).toBe(2));
    expect(c.textContent).toContain("生成了 2 个点");
    expect(c.textContent).toContain("西区：没放点");
    await waitFor(() => expect(save).not.toBeNull());
    await save!();
    const body = posted.find((p) => p.url === "/api/routes/night-guard")!.body;
    expect(body.kind).toBe("guard");
    expect((body.waypoints as unknown[]).length).toBe(2);
    await waitFor(() => expect(c.querySelector("h2")?.textContent).toBe("night-guard"));
    expect(c.textContent).not.toContain("排程");
  });
});

describe("布置 → 巡逻路线:每点预计时间", () => {
  it("存好的路线显示每点 +分:秒 和整条用时;改了就说保存后重算", async () => {
    const { container: c } = render(<RoutesTab {...base()} />);
    await waitFor(() => expect(c.querySelector('[data-route-row="night"]')).not.toBeNull());
    expect(gets.find((u) => u.startsWith("/api/routes?"))).toContain("kind=patrol");
    fireEvent.click(c.querySelector('[data-route-row="night"]')!);
    await waitFor(() => expect(c.querySelector('[data-eta="1"]')).not.toBeNull());
    expect(c.querySelector('[data-eta="1"]')!.textContent).toBe("+2:05");
    expect(c.querySelector('[data-eta="1"]')!.getAttribute("title")).toContain("最近实际：0:31");
    expect(c.querySelector('[data-eta-total="1"]')!.textContent).toContain("整条约 2:05");
    fireEvent.click(c.querySelector(".pick")!);              // 加一个点 → 改了
    await waitFor(() => expect(c.querySelector('[data-eta="1"]')).toBeNull());
    expect(c.textContent).toContain("保存以后重新计算时间");
  });
});

describe("布置 → 防区:查看点和值守位", () => {
  const AREAS = [{ zone: "东区", points: [[0, 0], [5, 0], [5, 9], [0, 9]] as [number, number][] }];
  const POSTS: ZonePost[] = [{ zone: "东区", auto: true, note: "", view: { x: 2, y: 2 }, watch: { x: 3, y: 3, yaw: 0 } }];
  it("图上画出来;挪查看点、存的时候只送 posts", async () => {
    const { container: c } = render(<ZonesTab {...base({ areas: AREAS, posts: POSTS })} />);
    expect(c.querySelector('[data-view="东区"]')).not.toBeNull();
    expect(c.querySelector('[data-watch="东区"]')).not.toBeNull();
    fireEvent.click(btn(c, "东区"));
    expect(c.querySelector('[data-posts-state="1"]')!.textContent).toBe("站点自动选的");
    fireEvent.click(btn(c, "挪查看点"));
    fireEvent.click(c.querySelector(".pick")!);              // 预览像素 10,20 → 地图 1,8
    expect(c.querySelector('[data-view="东区"]')!.getAttribute("cx")).toBe("10");
    await waitFor(() => expect(save).not.toBeNull());
    await save!();
    const p = posted.filter((x) => x.url === "/api/maps/home/3/areas");
    expect(p).toEqual([{ url: "/api/maps/home/3/areas",
      body: { zone: "东区", posts: { view: { x: 1, y: 8 }, watch: { x: 3, y: 3, yaw: 0 } } } }]);
  });
  it("挪过的能恢复自动(送 posts: null)", async () => {
    const manual: ZonePost[] = [{ ...POSTS[0], auto: false, updated_by: "alice" }];
    const { container: c } = render(<ZonesTab {...base({ areas: AREAS, posts: manual })} />);
    fireEvent.click(btn(c, "东区"));
    expect(c.textContent).toContain("alice 挪过");
    fireEvent.click(btn(c, "恢复自动"));
    await waitFor(() => expect(save).not.toBeNull());
    await save!();
    expect(posted.filter((x) => x.url.endsWith("/areas")).map((x) => x.body)).toEqual([{ zone: "东区", posts: null }]);
  });
});
