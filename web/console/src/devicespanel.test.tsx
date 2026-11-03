// @vitest-environment happy-dom
// W73 第五期 外部设备按位置登记面板。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { DevicesPanel } from "./devicespanel";
import { lang } from "./i18n";

const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
let devices: Record<string, unknown>[];
let posted: Record<string, unknown>[];

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  devices = [];
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const b = JSON.parse(String(init.body));
      posted.push(b);
      if (b.remove) { devices = []; return Promise.resolve(json({ ok: true })); }
      devices = [{ id: b.id, kind: b.kind, map_id: b.map_id, map_version: b.map_version, x: b.x, y: b.y, yaw: 0, view_x: b.x, view_y: b.y, zone: "东区", note: "", source: `dev-${b.id}` }];
      return Promise.resolve(json({ secret: "ab".repeat(32), source: `dev-${b.id}`, device: devices[0] }));
    }
    if (url === "/api/devices") return Promise.resolve(json({ devices }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

describe("外部设备面板", () => {
  it("登记门磁:站点算出防区,密钥只显示一次;删除", async () => {
    const { container: c } = render(<DevicesPanel canEdit say={() => {}} />);
    await waitFor(() => expect(c.textContent).toContain("还没有登记设备"));
    for (const [n, v] of [["dev-id", "door-1"], ["dev-map", "shop"], ["dev-ver", "1"], ["dev-x", "3"], ["dev-y", "1"]])
      fireEvent.input(c.querySelector(`input[name=${n}]`)!, { target: { value: v } });
    fireEvent.click(Array.from(c.querySelectorAll("button")).find((b) => b.textContent === "登记 / 更新")!);
    await waitFor(() => expect(c.textContent).toContain("防区 东区"));
    expect(posted[0]).toEqual({ id: "door-1", kind: "contact", map_id: "shop", map_version: "1", x: 3, y: 1 });
    expect(c.querySelector(".device-secret")?.textContent).toContain("dev-door-1");
    fireEvent.click(Array.from(c.querySelectorAll("button")).find((b) => b.textContent === "删除")!);
    await waitFor(() => expect(c.textContent).toContain("还没有登记设备"));
  });

  it("没开这一项的站点不显示;没权限的看不到表单", async () => {
    setFetch((() => Promise.resolve(json({}, 404))) as typeof fetch);
    const { container: c } = render(<DevicesPanel canEdit say={() => {}} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(c.querySelector(".devices-panel")).toBeNull();
  });
});
