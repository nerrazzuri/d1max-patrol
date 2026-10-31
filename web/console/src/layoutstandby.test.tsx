// @vitest-environment happy-dom
// W44 第三期 布置 → 待命点:选中一个点图挪过去、存下;加一个待命点(选哪台、起名);设默认。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import type { LayerData, OwnLayer, TabProps } from "./layoutpage";
import { StandbyTab } from "./layoutstandby";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const META = { width: 100, height: 100, m_per_px: 0.1, left_x: 0, top_y: 10, zones: [] };

let posted: { url: string; body: Record<string, unknown> }[];
let save: (() => Promise<void>) | null;
beforeEach(() => {
  lang.value = "zh";
  posted = [];
  save = null;
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") posted.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
    if (url.startsWith("/api/chargers")) return Promise.resolve(json({ chargers: [] }));
    return Promise.resolve(json({ points: [] }));
  }) as typeof fetch);
});
afterEach(cleanup);

const data: LayerData = { areas: [], nav: null, routes: [], cameras: [], robots: ["A", "B"],
  standby: [{ robot_id: "A", name: "dock", x: 2, y: 2, yaw: 0, map_id: "home", map_version: "3", default: true }] };
const FakeMap = (o: OwnLayer) => <div class="fakemap"><button type="button" class="pick" onClick={() => o.onPick?.(40, 30)}>pick</button></div>;
const props = (): TabProps => ({
  map: { map_id: "home", version: "3", note: "" }, base: "/api/maps/home/3", meta: META, data, say: () => {},
  reload: () => Promise.resolve(), setDirty: () => {}, registerSave: (fn) => { save = fn; }, Map: FakeMap,
});
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("布置 → 待命点", () => {
  it("选中以后点图挪过去,存下", async () => {
    const { container: c } = render(<StandbyTab {...props()} />);
    fireEvent.click(btn(c, "A · dock"));
    fireEvent.click(c.querySelector(".pick")!);
    await save!();
    expect(posted.find((p) => p.url === "/api/robots/A/standby")?.body).toEqual(
      { name: "dock", map_id: "home", map_version: "3", x: 4, y: 7, yaw: 0, default: true });
  });

  it("添加待命点", async () => {
    const { container: c } = render(<StandbyTab {...props()} />);
    fireEvent.click(btn(c, "添加待命点"));
    fireEvent.click(c.querySelector(".pick")!);
    fireEvent.change(document.querySelector('.modal select[name="robot"]')!, { target: { value: "B" } });
    fireEvent.input(document.querySelector('.modal input[name="name"]')!, { target: { value: "porch" } });
    fireEvent.submit(document.querySelector(".modal form")!);
    expect(c.querySelector(".ly-side")!.textContent).toContain("B · porch");
    await save!();
    await waitFor(() => expect(posted.find((p) => p.url === "/api/robots/B/standby")?.body).toMatchObject({ name: "porch", x: 4, y: 7, default: true }));
    expect(posted.some((p) => p.url === "/api/robots/A/standby")).toBe(false);
  });

  it("删要确认", async () => {
    const { container: c } = render(<StandbyTab {...props()} />);
    fireEvent.click(btn(c, "A · dock"));
    fireEvent.click(btn(c.querySelector(".ly-detail")!, "删除"));
    fireEvent.click(btn(document.querySelector(".modal")!, "删除"));
    await save!();
    expect(posted.find((p) => p.url === "/api/robots/A/standby")?.body).toEqual({ name: "dock", remove: true });
  });
});
