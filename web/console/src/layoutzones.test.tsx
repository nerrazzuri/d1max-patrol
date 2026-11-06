// @vitest-environment happy-dom
// W44 第三期 布置 → 防区(设计 §5.2):圈一块、起名;选中一个,勾哪些摄像头归它管;删要确认;都在「保存」时一起存。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import type { LayerData, OwnLayer, TabProps } from "./layoutpage";
import { ZonesTab } from "./layoutzones";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const META = { width: 100, height: 100, m_per_px: 0.1, left_x: 0, top_y: 10, zones: [] };

let posted: { url: string; body: Record<string, unknown> }[];
let save: (() => Promise<void>) | null;
let dirty: boolean;
beforeEach(() => {
  lang.value = "zh";
  posted = [];
  save = null;
  dirty = false;
  setFetch(((url: string, init?: RequestInit) => {
    posted.push({ url, body: JSON.parse(String(init?.body ?? "{}")) });
    return Promise.resolve(json({ areas: [] }));
  }) as typeof fetch);
});
afterEach(cleanup);

const data: LayerData = {
  areas: [{ zone: "北区", points: [[1, 1], [5, 1], [5, 5]] }], nav: null, routes: [], guardRoutes: [], posts: [], standby: [], robots: [],
  cameras: [{ name: "cam1", zone: "前门", onvif_url: "http://10.0.0.5/onvif", rtsp_url: "", username: "admin", has_password: true, motion: false },
    { name: "cam2", zone: "北区", onvif_url: "http://10.0.0.6/onvif", rtsp_url: "", username: "", has_password: false, motion: true }],
};
/** 假地图:一个按钮当点图(预览像素 10,20 → 地图 1,8)。 */
const FakeMap = (o: OwnLayer) => <div class="fakemap"><button type="button" class="pick" onClick={() => o.onPick?.(10, 20)}>pick</button></div>;
const props = (): TabProps => ({
  map: { map_id: "home", version: "3", note: "" }, base: "/api/maps/home/3", meta: META, data, say: () => {},
  reload: () => Promise.resolve(), setDirty: (b) => { dirty = b; }, registerSave: (fn) => { save = fn; }, Map: FakeMap,
});
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("布置 → 防区", () => {
  it("画一个防区存下", async () => {
    const { container: c } = render(<ZonesTab {...props()} />);
    fireEvent.click(btn(c, "绘制防区"));
    for (let i = 0; i < 3; i++) fireEvent.click(c.querySelector(".pick")!);
    fireEvent.click(btn(c, "完成绘制"));
    fireEvent.input(document.querySelector('.modal input[name="zone"]')!, { target: { value: "A区" } });
    fireEvent.submit(document.querySelector(".modal form")!);
    expect(dirty).toBe(true);
    expect(c.querySelector(".ly-side")!.textContent).toContain("A区");
    await save!();
    expect(posted.find((p) => p.url === "/api/maps/home/3/areas")?.body).toEqual({ zone: "A区", points: [[1, 8], [1, 8], [1, 8]] });
  });

  it("选中改摄像头归属", async () => {
    const { container: c } = render(<ZonesTab {...props()} />);
    fireEvent.click(btn(c, "北区"));
    const box = c.querySelector('.zone-cams input[value="cam1"]') as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect((c.querySelector('.zone-cams input[value="cam2"]') as HTMLInputElement).checked).toBe(true);
    fireEvent.click(box);
    expect(dirty).toBe(true);
    await save!();
    const p = posted.find((x) => x.url === "/api/admin/cameras")!.body;
    expect(p).toMatchObject({ name: "cam1", zone: "北区", edit: true, onvif_url: "http://10.0.0.5/onvif", username: "admin" });
    expect(p).not.toHaveProperty("password");
  });

  it("删防区要确认", async () => {
    const { container: c } = render(<ZonesTab {...props()} />);
    fireEvent.click(btn(c, "北区"));
    fireEvent.click(btn(c.querySelector(".ly-side")!, "删除"));
    fireEvent.click(btn(document.querySelector(".modal")!, "删除"));
    expect(c.querySelector(".ly-side")!.textContent).not.toContain("北区");
    await save!();
    await waitFor(() => expect(posted.find((p) => p.url === "/api/maps/home/3/areas")?.body).toEqual({ zone: "北区", remove: true }));
  });
});

describe("布置 → 防区:出口线、易藏点(W74 三期)", () => {
  let k = 0;
  /** 每点一下往右挪 1 m(预览像素 10 → 地图 1 m)。 */
  const Walk = (o: OwnLayer) => <div class="fakemap"><button type="button" class="pick" onClick={() => { k += 1; o.onPick?.(10 * k, 20); }}>pick</button></div>;
  const withMarks = (marks: LayerData["marks"]): TabProps => ({ ...props(), Map: Walk, data: { ...data, marks } });
  beforeEach(() => { k = 0; });

  it("画出口线:点两头、起名,存的时候发给站点", async () => {
    const { container: c } = render(<ZonesTab {...withMarks([])} />);
    expect(c.querySelector(".ly-marks")!.textContent).toContain("还没有出口线、易藏点");
    fireEvent.click(btn(c, "画出口线"));
    fireEvent.click(c.querySelector(".pick")!);
    fireEvent.click(c.querySelector(".pick")!);
    fireEvent.input(document.querySelector('.modal input[name="mark"]')!, { target: { value: "东门" } });
    fireEvent.submit(document.querySelector(".modal form")!);
    expect(dirty).toBe(true);
    expect(c.querySelector(".ly-marks")!.textContent).toContain("东门");
    await save!();
    expect(posted.find((p) => p.url === "/api/maps/home/3/areas" && p.body.mark)?.body)
      .toEqual({ mark: { kind: "exit", name: "东门", points: [[1, 8], [2, 8]], yaw: 0 } });
  });

  it("标易藏点:先点站的位置、再点朝哪看", async () => {
    const { container: c } = render(<ZonesTab {...withMarks([])} />);
    fireEvent.click(btn(c, "标易藏点"));
    fireEvent.click(c.querySelector(".pick")!);
    expect(c.querySelector(".ly-marks")!.textContent).toContain("再点它朝哪看");
    fireEvent.click(c.querySelector(".pick")!);
    fireEvent.input(document.querySelector('.modal input[name="mark"]')!, { target: { value: "楼梯间" } });
    fireEvent.submit(document.querySelector(".modal form")!);
    await save!();
    expect(posted.find((p) => p.body.mark)?.body).toEqual({ mark: { kind: "hide", name: "楼梯间", points: [[1, 8]], yaw: 0 } });
  });

  it("删一个已有的", async () => {
    const { container: c } = render(<ZonesTab {...withMarks([{ id: "m1", kind: "hide", name: "仓库", points: [[3, 3]], yaw: 1 }])} />);
    const row = Array.from(c.querySelectorAll(".ly-marks li")).find((li) => li.textContent?.includes("仓库"))!;
    fireEvent.click(btn(row, "删除"));
    expect(c.querySelector(".ly-marks")!.textContent).not.toContain("仓库");
    await save!();
    expect(posted.find((p) => p.body.mark_id)?.body).toEqual({ mark_id: "m1", remove: true });
  });
});
