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
  areas: [{ zone: "北区", points: [[1, 1], [5, 1], [5, 5]] }], nav: null, routes: [], standby: [], robots: [],
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
    fireEvent.click(btn(c, "画一个防区"));
    for (let i = 0; i < 3; i++) fireEvent.click(c.querySelector(".pick")!);
    fireEvent.click(btn(c, "画好了"));
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
