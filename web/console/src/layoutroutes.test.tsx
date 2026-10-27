// @vitest-environment happy-dom
// W44 第三期 布置 → 巡逻路线:点图加点自动连线、外框的保存存下去;站点说两点之间走不通,那一段画红;不显示坐标;热像两项灰着。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ApiError, setFetch } from "./api";
import { lang } from "./i18n";
import type { LayerData, OwnLayer, TabProps } from "./layoutpage";
import { RoutesTab } from "./layoutroutes";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const META = { width: 100, height: 100, m_per_px: 0.1, left_x: 0, top_y: 10, zones: [] };
const ROUTE = { id: "night", map_id: "home", map_version: "3", revision: 4, bound: true, policy: {}, updated_by: "o", updated_ms: 1,
  waypoints: [{ name: "p1", x: 1, y: 1, yaw: 0 }, { name: "p2", x: 6, y: 1, yaw: 0 }] };

let posted: { url: string; body: Record<string, unknown> }[];
let save: (() => Promise<void>) | null;
let reply: "ok" | "noPath";
beforeEach(() => {
  lang.value = "zh";
  posted = [];
  save = null;
  reply = "ok";
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      if (reply === "noPath") return Promise.resolve(json({ error: "存不下", errors: [{ index: null, name: "", problem: "p1 到 p2 走不通", segment: [0, 1] }] }, 400));
      return Promise.resolve(json({ ...ROUTE, id: url.split("/").pop(), revision: 5, waypoints: body.waypoints }));
    }
    if (url.startsWith("/api/routes")) return Promise.resolve(json({ revision: 1, timezone: "Asia/Kuala_Lumpur", routes: [ROUTE], schedules: [] }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const FakeMap = (o: OwnLayer) => (
  <div class="fakemap">
    <button type="button" class="pick" onClick={() => o.onPick?.(30, 50)}>pick</button>
    <svg>{o.world}</svg>
  </div>
);
const props = (): TabProps => ({
  map: { map_id: "home", version: "3", note: "" }, base: "/api/maps/home/3", meta: META,
  data: { areas: [], nav: null, routes: [], standby: [], cameras: [], robots: [] } as LayerData, say: () => {},
  reload: () => Promise.resolve(), setDirty: () => {}, registerSave: (fn) => { save = fn; }, Map: FakeMap,
});
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("布置 → 巡逻路线", () => {
  it("点图加点自动连线,外框保存存下去", async () => {
    const { container: c } = render(<RoutesTab {...props()} />);
    await waitFor(() => expect(c.querySelector('[data-route-row="night"]')).not.toBeNull());
    fireEvent.click(btn(c, "+ 新建路线"));
    fireEvent.input(c.querySelector('input[aria-label="路线名"]')!, { target: { value: "evening" } });
    fireEvent.click(c.querySelector(".pick")!);
    fireEvent.click(c.querySelector(".pick")!);
    expect(c.querySelector(".fakemap polyline.route-line")).not.toBeNull();
    await waitFor(() => expect(save).not.toBeNull());
    await save!();
    const p = posted.find((x) => x.url === "/api/routes/evening")!.body;
    expect((p.waypoints as unknown[]).length).toBe(2);
    expect(p.map_version).toBe("3");
  });

  it("走不通那一段标红", async () => {
    reply = "noPath";
    const { container: c } = render(<RoutesTab {...props()} />);
    await waitFor(() => expect(c.querySelector('[data-route-row="night"]')).not.toBeNull());
    fireEvent.click(c.querySelector('[data-route-row="night"]')!);
    fireEvent.click(c.querySelector(".pick")!);
    await waitFor(() => expect(save).not.toBeNull());
    await expect(save!()).rejects.toBeInstanceOf(ApiError);
    await waitFor(() => expect(c.querySelector('[data-bad-seg="0"]')).not.toBeNull());
  });

  it("不显示坐标,热像两项灰着", async () => {
    const { container: c } = render(<RoutesTab {...props()} />);
    await waitFor(() => expect(c.querySelector('[data-route-row="night"]')).not.toBeNull());
    fireEvent.click(c.querySelector('[data-route-row="night"]')!);
    fireEvent.click(btn(c, "p1"));
    expect(c.textContent).not.toContain("x（米）");
    expect(c.querySelectorAll("[data-thermal] input[disabled]").length).toBe(2);
    expect(c.textContent).toContain("热像到货后开放");
    expect(c.querySelector(".panel-head")!.hasAttribute("hidden")).toBe(true);
  });
});
