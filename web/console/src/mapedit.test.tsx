// @vitest-environment happy-dom
// W44 第四期 地图编辑器:橡皮擦一笔发出去的点和粗细、另存前列出面积要确认、特殊地面楼梯默认放慢、摆正只改显示、
// 栅格换了说有人改过、只改显示的不另存新版本。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import type { OwnLayer } from "./layoutpage";
import { editTarget, MapEditPage } from "./mapedit";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const META = { width: 100, height: 100, m_per_px: 0.1, left_x: 0, top_y: 10, zones: [], rotation_deg: 5 };

let posted: { url: string; body: Record<string, unknown> }[];
let editReply: number;
beforeEach(() => {
  lang.value = "zh";
  posted = [];
  editReply = 200;
  location.hash = "#/admin/mapedit?map=home&version=3";
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      if (url.endsWith("/edit")) return Promise.resolve(editReply === 200 ? json({ version: "4", erased_m2: 1 }) : json({ error: "换了" }, editReply));
      return Promise.resolve(json(body));
    }
    if (url.endsWith("/preview")) return Promise.resolve(json(META));
    if (url.endsWith("/noise")) return Promise.resolve(json({ digest: "abc", specks: [] }));
    if (url.endsWith("/display")) return Promise.resolve(json({ rotate_deg: null, basemap: null, regions: [], terrain: [] }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const Fake = (o: OwnLayer) => <div class="fakemap"><button type="button" class="pick" onClick={() => o.onPick?.(40, 30)}>pick</button><svg>{o.world}</svg></div>;
const page = () => render(<MapEditPage say={() => {}} target={{ map_id: "home", version: "3" }} Canvas={Fake} />);
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("地图编辑器", () => {
  it("地址里拿图和版本", () => {
    expect(editTarget("#/admin/mapedit?map=home&version=3")).toEqual({ map_id: "home", version: "3" });
    expect(editTarget("#/admin/mapedit")).toBeNull();
  });

  it("橡皮擦一笔,另存前列出面积要确认,发出去的点和粗细对", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('.mapedit[data-ready]')).not.toBeNull());
    fireEvent.click(c.querySelector(".pick")!);
    fireEvent.click(c.querySelector(".pick")!);
    fireEvent.click(btn(c, "画好了"));
    expect(c.querySelector("polyline.me-op.erase")).not.toBeNull();
    fireEvent.click(c.querySelector(".me-save")!);
    expect(document.body.textContent).toContain("平方米");
    fireEvent.click(btn(document.querySelector(".modal")!, "另存成新的一版"));
    await waitFor(() => expect(posted.find((p) => p.url.endsWith("/edit"))).toBeTruthy());
    const b = posted.find((p) => p.url.endsWith("/edit"))!.body;
    expect(b.digest).toBe("abc");
    expect(b.ops).toEqual([{ op: "erase", shape: "stroke", points: [[4, 7], [4, 7]], width_m: 0.3 }]);
    await waitFor(() => expect(posted.some((p) => p.url === "/api/admin/maps/home/4/display")).toBe(true));
  });

  it("方框两个对角就画完", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('.mapedit[data-ready]')).not.toBeNull());
    fireEvent.click(c.querySelector('[data-tool="obstacle"]')!);
    fireEvent.click(btn(c, "方框"));
    fireEvent.click(c.querySelector(".pick")!);
    fireEvent.click(c.querySelector(".pick")!);
    expect(c.querySelector("polygon.me-op.obstacle")).not.toBeNull();
  });

  it("特殊地面:楼梯默认放慢 0.3", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('.mapedit[data-ready]')).not.toBeNull());
    fireEvent.click(c.querySelector('[data-tool="terrain"]')!);
    for (let i = 0; i < 3; i++) fireEvent.click(c.querySelector(".pick")!);
    fireEvent.click(btn(c, "画好了"));
    expect((c.querySelector('[data-terrain] input') as HTMLInputElement).value).toBe("0.3");
  });

  it("摆正只改显示:不另存新版本,只存显示设置", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('.mapedit[data-ready]')).not.toBeNull());
    fireEvent.click(c.querySelector('[data-tool="rotate"]')!);
    fireEvent.change(c.querySelector('input[name="rotate"]')!, { target: { value: "12" } });
    fireEvent.click(c.querySelector(".me-save")!);
    fireEvent.click(btn(document.querySelector(".modal")!, "保存"));
    await waitFor(() => expect(posted.find((p) => p.url === "/api/admin/maps/home/3/display")?.body.rotate_deg).toBe(12));
    expect(posted.some((p) => p.url.endsWith("/edit"))).toBe(false);
  });

  it("栅格换了说有人改过", async () => {
    editReply = 409;
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('.mapedit[data-ready]')).not.toBeNull());
    fireEvent.click(c.querySelector(".pick")!);
    fireEvent.click(btn(c, "画好了"));
    fireEvent.click(c.querySelector(".me-save")!);
    fireEvent.click(btn(document.querySelector(".modal")!, "另存成新的一版"));
    await waitFor(() => expect(c.querySelector(".ly-conflict")!.textContent).toContain("换了"));
  });

  it("外审R5:换了图,旧图的草稿不带过去;三样都到了才能存", async () => {
    let resolveB!: (r: Response) => void;
    setFetch(((url: string, init?: RequestInit) => {
      if (init?.method === "POST") { posted.push({ url, body: JSON.parse(String(init.body ?? "{}")) }); return Promise.resolve(json({ version: "2" })); }
      if (url.endsWith("/preview")) return Promise.resolve(json(META));
      if (url.endsWith("/noise")) return Promise.resolve(json({ digest: url.includes("/B/") ? "digest-B" : "digest-A" }));
      if (url.includes("/B/") && url.endsWith("/display")) return new Promise<Response>((r) => { resolveB = r; });
      return Promise.resolve(json({ rotate_deg: null, basemap: null, regions: [], terrain: [] }));
    }) as typeof fetch);
    const ui = render(<MapEditPage say={() => {}} target={{ map_id: "A", version: "1" }} Canvas={Fake} />);
    await waitFor(() => expect(ui.container.querySelector(".mapedit[data-ready]")).not.toBeNull());
    fireEvent.click(ui.container.querySelector(".pick")!);
    fireEvent.click(btn(ui.container, "画好了"));
    expect((ui.container.querySelector(".me-save") as HTMLButtonElement).disabled).toBe(false);
    ui.rerender(<MapEditPage say={() => {}} target={{ map_id: "B", version: "1" }} Canvas={Fake} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(ui.container.textContent).toContain("B:1");
    expect(ui.container.querySelector(".mapedit[data-ready]")).toBeNull();
    expect((ui.container.querySelector(".me-save") as HTMLButtonElement).disabled).toBe(true);
    expect(ui.container.querySelector(".me-op")).toBeNull();
    resolveB(json({ rotate_deg: null, basemap: null, regions: [], terrain: [] }));
    await waitFor(() => expect(ui.container.querySelector(".mapedit[data-ready]")).not.toBeNull());
    expect((ui.container.querySelector(".me-save") as HTMLButtonElement).disabled).toBe(true);   // B 上什么都没画
    expect(posted).toEqual([]);
  });

  it("外审R5:显示设置读不出来不当成空的,不能存(存了会把区域名全抹掉)", async () => {
    setFetch(((url: string, init?: RequestInit) => {
      if (init?.method === "POST") { posted.push({ url, body: {} }); return Promise.resolve(json({})); }
      if (url.endsWith("/preview")) return Promise.resolve(json(META));
      if (url.endsWith("/noise")) return Promise.resolve(json({ digest: "abc" }));
      return Promise.resolve(json({ error: "坏了" }, 500));
    }) as typeof fetch);
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".me-loaderr")).not.toBeNull());
    expect(c.querySelector(".mapedit[data-ready]")).toBeNull();
    fireEvent.click(c.querySelector(".pick")!);
    expect(c.querySelector(".me-save")!.hasAttribute("disabled")).toBe(true);
  });

  it("外审R5:另存还没回来就换了图,回来以后不跳、不改新图", async () => {
    let finishEdit!: (r: Response) => void;
    setFetch(((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posted.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
        if (url.endsWith("/edit")) return new Promise<Response>((r) => { finishEdit = r; });
        return Promise.resolve(json({}));
      }
      if (url.endsWith("/preview")) return Promise.resolve(json(META));
      if (url.endsWith("/noise")) return Promise.resolve(json({ digest: "abc" }));
      return Promise.resolve(json({ rotate_deg: null, basemap: null, regions: [], terrain: [] }));
    }) as typeof fetch);
    const ui = render(<MapEditPage say={() => {}} target={{ map_id: "A", version: "1" }} Canvas={Fake} />);
    await waitFor(() => expect(ui.container.querySelector(".mapedit[data-ready]")).not.toBeNull());
    fireEvent.click(ui.container.querySelector(".pick")!);
    fireEvent.click(btn(ui.container, "画好了"));
    fireEvent.click(ui.container.querySelector(".me-save")!);
    fireEvent.click(btn(document.querySelector(".modal")!, "另存成新的一版"));
    await waitFor(() => expect(posted.some((p) => p.url.endsWith("/A/1/edit"))).toBe(true));
    location.hash = "#/admin/mapedit?map=B&version=1";
    ui.rerender(<MapEditPage say={() => {}} target={{ map_id: "B", version: "1" }} Canvas={Fake} />);
    finishEdit(json({ version: "2" }));
    await new Promise((r) => setTimeout(r, 20));
    expect(location.hash).toBe("#/admin/mapedit?map=B&version=1");
    // 确认过的那次照样存完(A 的新版本 2 的显示设置),B 那边一个请求都没有
    await waitFor(() => expect(posted.map((p) => p.url)).toEqual(["/api/admin/maps/A/1/edit", "/api/admin/maps/A/2/display"]));
    expect(location.hash).toBe("#/admin/mapedit?map=B&version=1");
  });
});
