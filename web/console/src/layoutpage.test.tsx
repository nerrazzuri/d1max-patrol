// @vitest-environment happy-dom
// W44 第三期「布置」外框(设计 §5.2):五个标签、地址带 tab、别的层淡着画、有改动保存才亮、切标签先问没存的、
// 保存回 409 说有人改过、历史抽屉能退回、旧地址跳过来。标签用假的,只测外框。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { useEffect } from "preact/hooks";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ApiError, setFetch } from "./api";
import { lang } from "./i18n";
import { LayoutPage, tabFromHash, type TabProps } from "./layoutpage";
import { readRoute } from "./shell";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const META = { width: 100, height: 100, m_per_px: 0.1, left_x: 0, top_y: 10, zones: [] };

let posted: { url: string; body: unknown }[];
let saveResult: "ok" | "conflict";

beforeEach(() => {
  lang.value = "zh";
  location.hash = "#/admin/layout";
  posted = [];
  saveResult = "ok";
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posted.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
      return Promise.resolve(json({}));
    }
    if (url === "/api/maps") return Promise.resolve(json({ maps: [{ map_id: "home", version: "3", note: "", active: true }] }));
    if (url.endsWith("/preview")) return Promise.resolve(json(META));
    if (url.endsWith("/areas")) return Promise.resolve(json({ areas: [{ zone: "后花园", points: [[1, 1], [5, 1], [5, 5]] }] }));
    if (url.endsWith("/zones")) return Promise.resolve(json({ revision: 2, zones: [] }));
    if (url.startsWith("/api/routes")) return Promise.resolve(json({ routes: [{ id: "night", revision: 1, waypoints: [{ name: "a", x: 1, y: 1, yaw: 0 }, { name: "b", x: 4, y: 4, yaw: 0 }] }] }));
    if (url.startsWith("/api/admin/cameras")) return Promise.resolve(json({ cameras: [] }));
    if (url.includes("/standby")) return Promise.resolve(json({ points: [{ name: "dock", x: 2, y: 2, yaw: 0, map_id: "home", map_version: "3" }] }));
    if (url.startsWith("/api/layout/history")) {
      return Promise.resolve(json({ history: [{ id: 9, kind: "zones", item: "", by: "alice", at_ms: 2, note: "" }, { id: 7, kind: "zones", item: "", by: "olga", at_ms: 1, note: "" }] }));
    }
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

/** 假标签:点「改」就有没存的改动,注册一个保存。 */
function Fake(p: TabProps) {
  useEffect(() => {
    p.registerSave(async () => {
      posted.push({ url: "fake-save", body: {} });
      if (saveResult === "conflict") throw new ApiError(409, "有人改过");
    });
  }, []);
  return <div class="fake"><button type="button" onClick={() => p.setDirty(true)}>改</button><p.Map /></div>;
}
const TABS = { zones: Fake, nogo: Fake, routes: Fake, standby: Fake, cameras: Fake };
const page = () => render(<LayoutPage say={() => {}} robots={["A"]} tabs={TABS} />);
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;
const tabBtn = (c: Element, text: string) => Array.from(c.querySelectorAll(".ly-tabs button")).find((b) => b.textContent === text) as HTMLButtonElement;

describe("布置外框", () => {
  it("五个标签、地址带tab", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".fake")).not.toBeNull());
    expect(Array.from(c.querySelectorAll(".ly-tabs button")).map((b) => b.textContent)).toEqual(["防区", "禁行区", "巡逻路线", "待命点", "摄像头"]);
    expect(tabFromHash("#/admin/layout?tab=routes")).toBe("routes");
    expect(tabFromHash("#/admin/layout?tab=nope")).toBe("zones");
  });

  it("别的层淡着画", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".layout-map .dim .ly-route")).not.toBeNull());
    await waitFor(() => expect(c.querySelector(".layout-map .dim .ly-standby")).not.toBeNull());
    fireEvent.click(tabBtn(c, "巡逻路线"));
    await waitFor(() => expect(c.querySelector(".layout-map.tab-routes")).not.toBeNull());
    expect(c.querySelector(".layout-map .dim .ly-route")).toBeNull();
  });

  it("有改动保存才亮,保存调标签的", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".fake")).not.toBeNull());
    const save = c.querySelector(".ly-save") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(btn(c, "改"));
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(posted.map((p) => p.url)).toContain("fake-save"));
    await waitFor(() => expect((c.querySelector(".ly-save") as HTMLButtonElement).disabled).toBe(true));
    // 存完标签按新数据重开:再改一次照样能存
    fireEvent.click(btn(c, "改"));
    expect((c.querySelector(".ly-save") as HTMLButtonElement).disabled).toBe(false);
  });

  it("切标签先问没存的", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".fake")).not.toBeNull());
    fireEvent.click(btn(c, "改"));
    fireEvent.click(tabBtn(c, "禁行区"));
    expect(document.body.textContent).toContain("还没保存");
    fireEvent.click(btn(document.querySelector(".modal")!, "丢掉"));
    await waitFor(() => expect(tabBtn(c, "禁行区").getAttribute("aria-selected")).toBe("true"));
  });

  it("409说有人改过,看最新的", async () => {
    saveResult = "conflict";
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".fake")).not.toBeNull());
    fireEvent.click(btn(c, "改"));
    fireEvent.click(c.querySelector(".ly-save")!);
    await waitFor(() => expect(c.querySelector(".ly-conflict")!.textContent).toContain("刚才有人改过"));
    fireEvent.click(btn(c.querySelector(".ly-conflict")!, "看最新的"));
    expect(c.querySelector(".ly-conflict")).toBeNull();
  });

  it("历史抽屉能退回", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".fake")).not.toBeNull());
    fireEvent.click(btn(c, "历史版本"));
    await waitFor(() => expect(c.querySelector(".ly-history")!.textContent).toContain("olga"));
    fireEvent.click(btn(c.querySelector(".ly-history")!, "退回到这一版"));
    fireEvent.click(btn(document.querySelector(".modal")!, "退回到这一版"));
    await waitFor(() => expect(posted.map((p) => p.url)).toContain("/api/layout/history/7/restore"));
  });

  it("外审R3:退回带上是哪一层、哪张图", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".fake")).not.toBeNull());
    fireEvent.click(btn(c, "历史版本"));
    await waitFor(() => expect(c.querySelector(".ly-history")!.textContent).toContain("olga"));
    fireEvent.click(btn(c.querySelector(".ly-history")!, "退回到这一版"));
    fireEvent.click(btn(document.querySelector(".modal")!, "退回到这一版"));
    await waitFor(() => expect(posted.find((p) => p.url.endsWith("/7/restore"))).toBeTruthy());
    expect(posted.find((p) => p.url.endsWith("/7/restore"))!.body).toEqual({ expect: { kind: "zones", map_id: "home", version: "3" } });
  });

  it("外审R3:换了图,旧图的历史和确认框立刻撤掉,晚到的旧回包不认", async () => {
    const pendingB: ((r: Response) => void)[] = [];
    const lateA: ((r: Response) => void)[] = [];
    let aCalls = 0;
    setFetch(((url: string, init?: RequestInit) => {
      if (init?.method === "POST") { posted.push({ url, body: JSON.parse(String(init.body ?? "{}")) }); return Promise.resolve(json({})); }
      if (url === "/api/maps") return Promise.resolve(json({ maps: [{ map_id: "A", version: "1", active: true }, { map_id: "B", version: "1", active: false }] }));
      if (url.endsWith("/preview")) return Promise.resolve(json(META));
      if (url.endsWith("/areas")) return Promise.resolve(json({ areas: [] }));
      if (url.endsWith("/zones")) return Promise.resolve(json({ revision: 0, zones: [] }));
      if (url.startsWith("/api/routes")) return Promise.resolve(json({ routes: [] }));
      if (url.startsWith("/api/admin/cameras")) return Promise.resolve(json({ cameras: [] }));
      if (url.startsWith("/api/layout/history?")) {
        if (url.includes("map_id=B")) return new Promise<Response>((r) => pendingB.push(r));
        aCalls += 1;
        const rows = json({ history: [{ id: 12, kind: "zones", by: "A-now", at_ms: 2, note: "", item: "" }, { id: 11, kind: "zones", by: "A-old", at_ms: 1, note: "", item: "" }] });
        if (aCalls === 2) return new Promise<Response>((r) => lateA.push(() => r(rows)));
        return Promise.resolve(rows);
      }
      return Promise.resolve(json({}, 404));
    }) as typeof fetch);
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".fake")).not.toBeNull());
    fireEvent.click(btn(c, "历史版本"));
    await waitFor(() => expect(c.querySelector(".ly-history")!.textContent).toContain("A-old"));
    fireEvent.click(btn(c.querySelector(".ly-history")!, "退回到这一版"));
    expect(document.querySelector(".modal")).not.toBeNull();
    const sel = c.querySelector("select") as HTMLSelectElement;
    fireEvent.change(sel, { target: { value: "B\u00001" } });
    await waitFor(() => expect(pendingB.length).toBe(1));
    expect(document.querySelector(".modal")).toBeNull();
    expect(c.querySelector(".ly-history")!.textContent).not.toContain("A-old");
    expect(btn(c.querySelector(".ly-history")!, "退回到这一版")).toBeUndefined();
    // A → B → A:第二次 A 的回包晚到之前,B 的回包也晚到 —— 不能把 B 的那份当成 A 的
    fireEvent.change(sel, { target: { value: "A\u00001" } });
    await waitFor(() => expect(lateA.length).toBe(1));
    pendingB[0](json({ history: [{ id: 30, kind: "zones", by: "B-now", at_ms: 2, note: "", item: "" }, { id: 29, kind: "zones", by: "B-old", at_ms: 1, note: "", item: "" }] }));
    await new Promise((r) => setTimeout(r, 20));
    expect(c.querySelector(".ly-history")!.textContent).not.toContain("B-old");
    lateA[0](json({}));
    await waitFor(() => expect(c.querySelector(".ly-history")!.textContent).toContain("A-old"));
    expect(posted.filter((p) => p.url.includes("/restore"))).toEqual([]);
  });

  it("旧地址跳过来", () => {
    expect(readRoute("#/admin/zones").page).toBe("admin/layout");
    expect(readRoute("#/admin/routes").page).toBe("admin/layout");
    expect(readRoute("#/admin/layout?tab=routes").page).toBe("admin/layout");
  });

  it("实测查出:地址里的 tab 变了(浏览器后退、前进)跟着换标签;有没存的先问,不丢就把地址改回来", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".fake")).not.toBeNull());
    const on = () => c.querySelector('.ly-tabs button[aria-selected="true"], .ly-tabs button.on')?.textContent;
    location.hash = "#/admin/layout?tab=nogo";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await waitFor(() => expect(on()).toBe("禁行区"));
    fireEvent.click(btn(c, "改"));
    location.hash = "#/admin/layout?tab=routes";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await waitFor(() => expect(document.querySelector(".modal")).not.toBeNull());
    fireEvent.click(btn(document.querySelector(".modal")!, "取消"));
    expect(on()).toBe("禁行区");
    expect(location.hash).toBe("#/admin/layout?tab=nogo");
  });
});
