// @vitest-environment happy-dom
// W44 第三期 布置 → 摄像头:放到图上、改张角、存下(不带口令);从图上拿掉存成 null。
import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { CamerasTab } from "./layoutcams";
import type { LayerData, OwnLayer, TabProps } from "./layoutpage";

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
    return Promise.resolve(json({}));
  }) as typeof fetch);
});
afterEach(cleanup);

const cam = (o: Record<string, unknown> = {}) => ({ name: "cam1", zone: "前门", onvif_url: "http://10.0.0.5/onvif", rtsp_url: "", username: "admin", has_password: true, motion: false, ...o });
const props = (cams: ReturnType<typeof cam>[]): TabProps => ({
  map: { map_id: "home", version: "3", note: "" }, base: "/api/maps/home/3", meta: META,
  data: { areas: [], nav: null, routes: [], standby: [], robots: [], cameras: cams } as LayerData, say: () => {},
  reload: () => Promise.resolve(), setDirty: () => {}, registerSave: (fn) => { save = fn; },
  Map: (o: OwnLayer) => <div class="fakemap"><button type="button" class="pick" onClick={() => o.onPick?.(40, 30)}>pick</button><svg>{o.world}</svg></div>,
});
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("布置 → 摄像头", () => {
  it("放到图上、改张角、存下,不带口令", async () => {
    const { container: c } = render(<CamerasTab {...props([cam()])} />);
    fireEvent.click(btn(c, "cam1"));
    fireEvent.click(btn(c, "放到图上"));
    fireEvent.click(c.querySelector(".pick")!);
    expect(c.querySelector(".fakemap path.cam-fan")).not.toBeNull();
    fireEvent.input(c.querySelector('input[name="fov"]')!, { target: { value: "60" } });
    await save!();
    const b = posted.find((p) => p.url === "/api/admin/cameras")!.body;
    expect(b.placement).toEqual({ map_id: "home", map_version: "3", x: 4, y: 7, yaw: 0, fov_deg: 60 });
    expect(b).not.toHaveProperty("password");
    expect(b.edit).toBe(true);
  });

  it("从图上拿掉存成 null", async () => {
    const pl = { map_id: "home", map_version: "3", x: 1, y: 1, yaw: 0, fov_deg: 90 };
    const { container: c } = render(<CamerasTab {...props([cam({ placement: pl })])} />);
    fireEvent.click(btn(c, "cam1"));
    fireEvent.click(btn(c, "从图上拿掉"));
    await save!();
    expect(posted.find((p) => p.url === "/api/admin/cameras")!.body.placement).toBeNull();
  });

  it("没改不存", async () => {
    render(<CamerasTab {...props([cam()])} />);
    await save!();
    expect(posted).toEqual([]);
  });

  it("实测查出:站点回的警告(防区没映射、没画多边形)要说出来,列表里也标着", async () => {
    const said: string[] = [];
    setFetch(((_url: string, init?: RequestInit) => {
      if (init?.method === "POST") return Promise.resolve(json({ ok: true, camera: {}, warnings: [{ code: "zone_unmapped", text: "x" }, { code: "zone_no_polygon", text: "y" }] }));
      return Promise.resolve(json({}));
    }) as typeof fetch);
    const r = render(<CamerasTab {...props([cam({ warnings: [{ code: "zone_unmapped", text: "x" }] })])} say={(s: string) => said.push(s)} />);
    expect(r.container.querySelector('.ly-list [data-warning="zone_unmapped"]')).not.toBeNull();
    fireEvent.click(btn(r.container, "加一台摄像头"));
    const ins = Array.from(document.querySelectorAll(".modal input:not([type=checkbox]):not([type=password])")) as HTMLInputElement[];
    fireEvent.input(ins[0], { target: { value: "cam2" } });
    fireEvent.input(ins[1], { target: { value: "http://10.0.0.6" } });
    fireEvent.input(ins[3], { target: { value: "后院" } });
    fireEvent.submit(document.querySelector(".modal form")!);
    await new Promise((x) => setTimeout(x, 30));
    expect(said.join(" ")).toContain("还没映射到拦截点");
    expect(said.join(" ")).toContain("没在任何一张图上画多边形");
  });
});
