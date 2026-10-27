// @vitest-environment happy-dom
// W44 第二期 机器人页(设计 §5.1「机器人」):卡片(名字、在线、电量、今天几趟、毛病)、详情(改名、版本、最近的毛病、
// 手动 / 自动派、重出开通码、解绑要确认再输密码)、添加;起的名字保安页也用。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { robotNames } from "./data";
import { lang } from "./i18n";
import type { UnitInfo } from "./live";
import { LiveWall } from "./livewall";
import { RobotsPage } from "./robotspage";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const NOW = new Date(2026, 9, 27, 12).getTime();

const card = (o: Record<string, unknown>) => ({ robot_id: "A", name: "", revoked: false, expires_at: NOW + 300 * 86_400_000, online: true,
  battery_pct: 76, patrols_today: 3, problems: [], recent_problems: [], agent: "0.9.1", compat: "ok", manual_only: false, ...o });
let robots: Record<string, unknown>[];
let posted: { url: string; body: Record<string, unknown> }[];

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  robotNames.value = {};
  robots = [card({}), card({ robot_id: "B", online: false, battery_pct: null, patrols_today: 0,
    problems: [{ key: "B/stuck#1", kind: "stuck", first_ms: NOW - 60_000 }, { device: "front_camera", state: "offline" }],
    recent_problems: [{ key: "B/stuck#1", kind: "stuck", first_ms: NOW - 60_000, resolved_ms: null }] })];
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      if (url.endsWith("/name")) robots = robots.map((r) => (url.includes(`/${r.robot_id}/`) ? { ...r, name: body.name } : r));
      if (url.endsWith("/revoke")) return Promise.resolve(json({ robot_id: "A", summary: "CRL", broker_restart: "systemctl restart x" }));
      return Promise.resolve(json({ robots }));
    }
    if (url.startsWith("/api/robots/overview")) return Promise.resolve(json({ robots }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(() => {
  cleanup();
  robotNames.value = {};
});

const page = () => render(<RobotsPage say={() => {}} now={NOW} />);
const cardOf = (c: Element, id: string) => c.querySelector(`.robot-card[data-robot="${id}"]`)!;
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("机器人页", () => {
  it("卡片:名字、电量、今天几趟、毛病", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "B")).not.toBeNull());
    const b = cardOf(c, "B").textContent!;
    expect(b).toContain("离线");
    expect(b).toContain("2 个毛病");
    expect(cardOf(c, "A").textContent).toContain("76%");
    expect(cardOf(c, "A").textContent).toContain("今天巡了 3 趟");
  });

  it("没毛病写正常", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    expect(cardOf(c, "A").textContent).toContain("正常");
  });

  it("改名字", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    fireEvent.click(cardOf(c, "A"));
    const d = c.querySelector(".robot-detail")!;
    fireEvent.input(d.querySelector('input[name="name"]')!, { target: { value: "前院" } });
    fireEvent.click(btn(d, "保存名字"));
    await waitFor(() => expect(posted.find((p) => p.url === "/api/admin/robots/A/name")?.body).toEqual({ name: "前院" }));
    await waitFor(() => expect(cardOf(c, "A").textContent).toContain("前院"));
  });

  it("详情里有最近的毛病", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "B")).not.toBeNull());
    fireEvent.click(cardOf(c, "B"));
    expect(c.querySelector(".robot-detail")!.textContent).toContain("点位没到");
  });

  it("解绑要确认再输密码", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    fireEvent.click(cardOf(c, "A"));
    fireEvent.click(btn(c.querySelector(".robot-detail")!, "解绑"));
    fireEvent.click(btn(document.querySelector(".modal")!, "解绑"));
    await waitFor(() => expect(document.querySelector("form.confirm-pw")).not.toBeNull());
    fireEvent.input(document.querySelector('form.confirm-pw input[type="password"]')!, { target: { value: "my-own-password" } });
    fireEvent.submit(document.querySelector("form.confirm-pw")!);
    await waitFor(() => expect(posted.find((p) => p.url === "/api/admin/robots/A/revoke")?.body).toEqual({ confirm_password: "my-own-password" }));
  });

  it("保安页用起的名字", () => {
    robotNames.value = { A: "前院" };
    const u: UnitInfo = { robot: { robot_id: "A", active: true, fresh: true, revoked: false, held: null, status: { online: true, last_seen: 0 }, capabilities: null },
      state: "idle", battery: 50, task: "" };
    const { container: c } = render(<LiveWall units={[u]} alerts={[]} now={0} onZoom={() => {}} />);
    expect(c.querySelector(".lw-card .slot-name")!.textContent).toBe("前院");
  });
});
