// @vitest-environment happy-dom
// W44 第三期 布置 → 禁行区(设计 §5.2、§9):禁行区 / 虚拟墙 / 限速区,临时的选几天;收紧当场生效;删了写「等你确认」,
// 确认那一句是人话,放宽的确认要再输密码;机器人还没收到写「等机器人确认中」。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import type { LayerData, NavZones, OwnLayer, TabProps } from "./layoutpage";
import { NogoTab } from "./layoutnogo";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const META = { width: 100, height: 100, m_per_px: 0.1, left_x: 0, top_y: 10, zones: [] };
const POOL = { id: "n1", kind: "nogo", label: "泳池", polygon: [[1, 1], [3, 1], [3, 3]] as [number, number][] };

let posted: { url: string; body: Record<string, unknown> }[];
let save: (() => Promise<void>) | null;
let reloaded: string[];
let robots: unknown[];
beforeEach(() => {
  lang.value = "zh";
  posted = [];
  save = null;
  reloaded = [];
  robots = [];
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posted.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
      return Promise.resolve(json({ revision: 3, zones: [], auto_confirmed: true }));
    }
    if (url.startsWith("/api/robots")) return Promise.resolve(json({ robots }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const nav = (o: Partial<NavZones> = {}): NavZones => ({ revision: 2, zones: [POOL], confirmed: { revision: 2, by: "olga", at_ms: 1, current: true, how: "confirm" }, held: [], ...o });
const FakeMap = (o: OwnLayer) => <div class="fakemap"><button type="button" class="pick" onClick={() => o.onPick?.(10, 20)}>pick</button></div>;
const props = (n: NavZones): TabProps => ({
  map: { map_id: "home", version: "3", note: "" }, base: "/api/maps/home/3", meta: META,
  data: { areas: [], nav: n, routes: [], standby: [], cameras: [], robots: ["A"] } as LayerData, say: () => {},
  reload: (k) => { reloaded.push(String(k)); return Promise.resolve(); }, setDirty: () => {}, registerSave: (fn) => { save = fn; }, Map: FakeMap,
});
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;
function draw(c: Element, kind: string, n: number) {
  fireEvent.click(btn(c, kind));
  for (let i = 0; i < n; i++) fireEvent.click(c.querySelector(".pick")!);
  fireEvent.click(btn(c, "画好了"));
}

describe("布置 → 禁行区", () => {
  it("画禁行区_收紧当场生效", async () => {
    const { container: c } = render(<NogoTab {...props(nav())} />);
    draw(c, "画禁行区", 3);
    fireEvent.input(document.querySelector('.modal input[name="label"]')!, { target: { value: "花坛" } });
    fireEvent.submit(document.querySelector(".modal form")!);
    expect(c.querySelector(".ly-side")!.textContent).toContain("花坛");
    await save!();
    const b = posted.find((p) => p.url === "/api/maps/home/3/zones")!.body;
    expect(b.base_revision).toBe(2);
    expect((b.zones as { label: string }[]).map((z) => z.label)).toEqual(["泳池", "花坛"]);
    expect(reloaded).toContain("nav");
  });

  it("删了写等你确认,确认要那一句和密码", async () => {
    const n = nav({ zones: [], held: [POOL], confirmed: { revision: 2, by: "olga", at_ms: 1, current: false, how: "confirm" }, revision: 3 });
    const { container: c } = render(<NogoTab {...props(n)} />);
    expect(c.querySelector(".nogo-status")!.textContent).toContain("等你确认");
    expect(c.querySelector(".nogo-status")!.textContent).toContain("泳池");
    fireEvent.click(btn(c, "确认这一版"));
    const yes = btn(document.querySelector(".modal")!, "确认");
    expect(yes.disabled).toBe(true);
    fireEvent.click(document.querySelector('.modal input[name="sure"]')!);
    fireEvent.click(btn(document.querySelector(".modal")!, "确认"));
    await waitFor(() => expect(document.querySelector("form.confirm-pw")).not.toBeNull());
    fireEvent.input(document.querySelector('form.confirm-pw input[type="password"]')!, { target: { value: "my-own-password" } });
    fireEvent.submit(document.querySelector("form.confirm-pw")!);
    await waitFor(() => expect(posted.find((p) => p.url === "/api/maps/home/3/zones/confirm")?.body).toEqual(
      { revision: 3, confirm: true, empty_ok: true, confirm_password: "my-own-password" }));
  });

  it("临时的带天数", async () => {
    const { container: c } = render(<NogoTab {...props(nav())} />);
    draw(c, "画禁行区", 3);
    fireEvent.click(document.querySelector('.modal input[name="temp"]')!);
    fireEvent.change(document.querySelector('.modal select[name="days"]')!, { target: { value: "3" } });
    fireEvent.submit(document.querySelector(".modal form")!);
    await save!();
    const zs = posted.find((p) => p.url === "/api/maps/home/3/zones")!.body.zones as { for_s?: number }[];
    expect(zs[1].for_s).toBe(3 * 86400);
  });

  it("虚拟墙两个点就行", async () => {
    const { container: c } = render(<NogoTab {...props(nav())} />);
    draw(c, "画虚拟墙", 2);
    fireEvent.submit(document.querySelector(".modal form")!);
    await save!();
    const zs = posted.find((p) => p.url === "/api/maps/home/3/zones")!.body.zones as { wall?: unknown; kind: string }[];
    expect(zs[1].kind).toBe("nogo");
    expect(zs[1].wall).toEqual([[1, 8], [1, 8]]);
  });

  it("机器人还没收到写等机器人确认中", async () => {
    robots = [{ robot_id: "A", running: { map_id: "home", map_version: "3", zones: { match: false, dog_rev: 1, site_rev: 2 } } }];
    const { container: c } = render(<NogoTab {...props(nav())} />);
    await waitFor(() => expect(c.querySelector(".nogo-status")!.textContent).toContain("等机器人确认中"));
  });

  it("人员忽略区:没有硬禁区那一项,种类 ignore", async () => {
    const { container: c } = render(<NogoTab {...props(nav())} />);
    draw(c, "画人员忽略区", 3);
    expect(document.querySelector('.modal input[name="hard"]')).toBeNull();
    fireEvent.submit(document.querySelector(".modal form")!);
    await save!();
    const zs = posted.find((p) => p.url === "/api/maps/home/3/zones")!.body.zones as { kind: string; hard?: boolean }[];
    expect(zs[1].kind).toBe("ignore");
    expect(zs[1].hard).toBeUndefined();
  });

  it("从没确认过的写清楚,不说删了什么", () => {
    const { container: c } = render(<NogoTab {...props(nav({ confirmed: null, held: [], zones: [] }))} />);
    expect(c.querySelector(".nogo-status")!.textContent).toContain("还没人确认过");
    expect(c.querySelector(".nogo-status")!.textContent).not.toContain("删掉");
  });
});
