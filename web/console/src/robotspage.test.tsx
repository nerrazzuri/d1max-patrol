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
let pending: Record<string, unknown>[];
let said: string[];
let task: string | null;

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  said = [];
  pending = [];
  task = null;
  robotNames.value = {};
  robots = [card({}), card({ robot_id: "B", online: false, battery_pct: null, patrols_today: 0,
    problems: [{ key: "B/stuck#1", kind: "stuck", first_ms: NOW - 60_000 }, { device: "front_camera", state: "offline" }],
    recent_problems: [{ key: "B/stuck#1", kind: "stuck", first_ms: NOW - 60_000, resolved_ms: null }] })];
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      if (/\/(patrol|standby\/return|abort|relocalize)$/.test(url)) return Promise.resolve(json({ command_id: "c1", ack: { command_id: "c1", result: "accepted" } }));
      if (url.endsWith("/name")) robots = robots.map((r) => (url.includes(`/${r.robot_id}/`) ? { ...r, name: body.name } : r));
      if (url.endsWith("/revoke")) return Promise.resolve(json({ robot_id: "A", summary: "CRL", robot_wipe: "offline", broker_restart: "systemctl restart x" }));
      if (url.startsWith("/api/admin/bind/")) {
        if (url.endsWith("/approve") && body.confirm_password !== "pw") return Promise.resolve(json({ error: "密码不对" }, 403));
        pending = [];
        return Promise.resolve(json({ state: "ok" }));
      }
      return Promise.resolve(json({ robots }));
    }
    if (url.startsWith("/api/robots/overview")) return Promise.resolve(json({ robots }));
    if (url === "/api/schedule") return Promise.resolve(json({ missions: ["east-loop", "west-loop"] }));
    if (url === "/api/robots/A") return Promise.resolve(json({ robot_id: "A", status: { online: true, task: task ? { task_id: task, kind: "patrol", state: "running" } : null } }));
    if (url === "/api/admin/bind") return Promise.resolve(json({ requests: pending }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(() => {
  cleanup();
  robotNames.value = {};
});

const page = () => render(<RobotsPage say={(s) => said.push(s)} now={NOW} />);
const cardOf = (c: Element, id: string) => c.querySelector(`.robot-card[data-robot="${id}"]`)!;
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("机器人页", () => {
  it("卡片:名字、电量、今天几趟、毛病", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "B")).not.toBeNull());
    const b = cardOf(c, "B").textContent!;
    expect(b).toContain("离线");
    expect(b).toContain("2 个故障");
    expect(cardOf(c, "A").textContent).toContain("76%");
    expect(cardOf(c, "A").textContent).toContain("3 趟");
  });

  it("W60 没毛病就安静:不写正常、没有提示条;有毛病多一条提示、卡片标需要关注", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    expect(cardOf(c, "A").textContent).not.toContain("正常");
    expect(cardOf(c, "A").querySelector(".fleet-warn")).toBeNull();
    expect(cardOf(c, "B").querySelector(".fleet-warn")).not.toBeNull();
  });

  it("改名字", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    fireEvent.click(cardOf(c, "A"));
    const d = c.querySelector(".robot-detail")!;
    fireEvent.input(d.querySelector('input[name="name"]')!, { target: { value: "南区" } });
    fireEvent.click(btn(d, "保存名字"));
    await waitFor(() => expect(posted.find((p) => p.url === "/api/admin/robots/A/name")?.body).toEqual({ name: "南区" }));
    await waitFor(() => expect(cardOf(c, "A").textContent).toContain("南区"));
  });

  it("详情里有最近的毛病", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "B")).not.toBeNull());
    fireEvent.click(cardOf(c, "B"));
    expect(c.querySelector(".robot-detail")!.textContent).toContain("未到达点位");
  });

  it("W72 详情里的告警分三组放在最下面:要处理(P1)、待跟进、记录(自动恢复的标出来、收起来)", async () => {
    robots = [card({ robot_id: "B", recent_problems: [
      { key: "B/robot_offline#2", kind: "robot_offline", level: "P1", first_ms: NOW - 50_000, resolved_ms: NOW - 40_000, resolved_by: "site:back_soon" },
      { key: "B/loc_degraded#1", kind: "loc_degraded", level: "P2", first_ms: NOW - 60_000, resolved_ms: null, resolved_by: "" },
      { key: "B/stuck#1", kind: "stuck", level: "P1", first_ms: NOW - 70_000, resolved_ms: null, resolved_by: "" },
      { key: "B/fallen#1", kind: "fallen", level: "P1", first_ms: NOW - 80_000, resolved_ms: NOW - 1000, resolved_by: "gina" },
    ] })];
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "B")).not.toBeNull());
    fireEvent.click(cardOf(c, "B"));
    const d = c.querySelector(".robot-detail")!;
    const sec = d.querySelector(".robot-alerts")!;
    expect(sec).not.toBeNull();
    expect(d.lastElementChild).toBe(sec);
    const groups = Array.from(sec.querySelectorAll("[data-group]")).map((g) => g.getAttribute("data-group"));
    expect(groups).toEqual(["act", "later", "log"]);
    expect(sec.querySelector('[data-group="act"]')!.textContent).toContain("未到达点位");
    expect(sec.querySelector('[data-group="later"]')!.textContent).not.toContain("未到达点位");
    const log = sec.querySelector('[data-group="log"]') as HTMLDetailsElement;
    expect(log.tagName).toBe("DETAILS");
    expect(log.open).toBe(false);
    expect(log.textContent).toContain("自动恢复");
    expect(log.textContent).toContain("已处理");
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
    await waitFor(() => expect(said.join()).toContain("下次连接站点时将自动清除数据"));
  });

  it("解绑确认写明恢复出厂、数据删掉", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    fireEvent.click(cardOf(c, "A"));
    fireEvent.click(btn(c.querySelector(".robot-detail")!, "解绑"));
    expect(document.querySelector(".modal")!.textContent).toContain("恢复出厂");
  });

  it("W42 等待批准加入:核对码、再输密码批准、能起名字;拒绝要确认", async () => {
    pending = [{ id: "ab12", serial: "D1M-7", check: "042917", model: "D1 Max", version: "1", source: "192.168.1.20", created_ms: NOW, expires_ms: NOW + 1 }];
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('.bind-req[data-serial="D1M-7"]')).not.toBeNull());
    const row = c.querySelector('.bind-req[data-serial="D1M-7"]')!;
    expect(row.textContent).toContain("042917");
    fireEvent.click(btn(row, "批准"));
    const form = document.querySelector("form.bind-approve")!;
    expect(form.textContent).toContain("042917");
    fireEvent.input(form.querySelector('input:not([type="password"])')!, { target: { value: "西区" } });
    fireEvent.input(form.querySelector('input[type="password"]')!, { target: { value: "wrong" } });
    fireEvent.submit(form);
    await waitFor(() => expect(document.querySelector("form.bind-approve .error")?.textContent).toContain("密码错误"));
    fireEvent.input(form.querySelector('input[type="password"]')!, { target: { value: "pw" } });
    fireEvent.submit(form);
    await waitFor(() => expect(said.join()).toContain("D1M-7 已加入站点"));
    expect(posted.at(-1)).toEqual({ url: "/api/admin/bind/ab12/approve", body: { confirm_password: "pw", name: "西区" } });
    await waitFor(() => expect(c.querySelector(".bind-pending")).toBeNull());
  });

  it("W42 拒绝申请", async () => {
    pending = [{ id: "cd34", serial: "D1M-8", check: "111111", model: "", version: "", source: "x", created_ms: NOW, expires_ms: NOW + 1 }];
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('.bind-req[data-serial="D1M-8"]')).not.toBeNull());
    fireEvent.click(btn(c.querySelector(".bind-req")!, "拒绝"));
    fireEvent.click(btn(document.querySelector(".modal")!, "拒绝"));
    await waitFor(() => expect(posted.at(-1)?.url).toBe("/api/admin/bind/cd34/reject"));
  });

  it("W42 机器人上提了换 WiFi:卡片标出来,详情里确认", async () => {
    robots = [card({ wifi: { ssid: "Estate", link: "4g", change: { id: "0a1b2c3d4e5f", ssid: "New", mode: "trial", expires_ms: NOW } } })];
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    expect(cardOf(c, "A").textContent).toContain("WiFi 变更待确认");
    fireEvent.click(cardOf(c, "A"));
    const d = c.querySelector(".robot-detail")!;
    expect(d.textContent).toContain("Estate · 4G 连接");
    expect(d.querySelector(".wifi-change")!.textContent).toContain("正在尝试 WiFi「New」");
    fireEvent.click(btn(d, "确认换 WiFi"));
    await waitFor(() => expect(posted.at(-1)).toEqual({ url: "/api/robots/A/wifi", body: { id: "0a1b2c3d4e5f", approve: true } }));
  });

  it("W42 添加:说明用手机扫贴纸,工程师的开通码收在按钮后面", async () => {
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    fireEvent.click(btn(c, "添加四足机器人"));
    const m = document.querySelector(".modal")!;
    expect(m.textContent).toContain("扫描机器人贴纸");
    expect(m.querySelector("form")).toBeNull();
    fireEvent.click(btn(m, "工程师：用开通码手动添加"));
    expect(document.querySelector(".modal form")!.textContent).toContain("provision --code");
  });

  it("保安页用起的名字", () => {
    robotNames.value = { A: "南区" };
    const u: UnitInfo = { robot: { robot_id: "A", active: true, fresh: true, revoked: false, held: null, status: { online: true, last_seen: 0 }, capabilities: null },
      state: "idle", battery: 50, task: "" };
    const { container: c } = render(<LiveWall units={[u]} alerts={[]} now={0} onZoom={() => {}} />);
    expect(c.querySelector(".lw-card .slot-name")!.textContent).toBe("南区");
  });

  it("W60 经理只看:没有添加、批准、改名、解绑(W72:跟手机一样能派巡逻、停止当前任务)", async () => {
    const c = render(<RobotsPage say={() => {}} now={0} readOnly role="owner" />).container;
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    expect(btn(c, "添加机器人")).toBeUndefined();
    fireEvent.click(cardOf(c, "A"));
    const d = c.querySelector(".robot-detail")!;
    expect(d.querySelector("form")).toBeNull();
    expect(Array.from(d.querySelectorAll("button")).map((b) => b.textContent?.trim())).toEqual(["关闭", "开始巡逻", "停止当前任务"]);
  });
});

describe("W71 Nav2 没起来", () => {
  it("卡片提示条写 Nav2 不可用和原因", async () => {
    robots = [card({ problems: [{ nav2: "Nav2 转接节点未连接" }] })];
    const { container: c } = page();
    await waitFor(() => expect(cardOf(c, "A")?.querySelector(".fleet-warn")).toBeTruthy());
    expect(cardOf(c, "A").querySelector(".fleet-warn")!.textContent).toContain("Nav2 不可用(退回自带规划):Nav2 转接节点未连接");
  });

  it("W72 详情:现在、巡逻统计、地图、设备、登记、手动原因", async () => {
    robots[0] = card({ patrols_7d: 12, aborts_7d: 1, last_patrol_ms: NOW - 3_600_000, last_seen_ms: NOW - 3_000,
      storage: { used_ratio: 0.42, backlog_files: 3, oldest_backlog_s: 90 }, map: { id: "estate-1", version: "7", match: false },
      devices: [{ id: "lidar", state: "online" }, { id: "camera:thermal", state: "error" }], enrolled_at: NOW - 86_400_000 * 10,
      manual_only: true, manual_note: "先试跑", wifi: { ssid: "Estate", link: "wifi", change: null } });
    const { container: c } = render(<RobotsPage say={(s) => said.push(s)} now={NOW} />);
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    fireEvent.click(cardOf(c, "A"));
    const d = c.querySelector(".robot-detail")!.textContent!;
    for (const s of ["现在", "最后联系", "已用 42%", "待传 3 个", "近 7 天", "12 趟", "1 次", "estate-1 v7", "与站点不一致",
      "lidar", "camera:thermal", "故障", "先试跑", "Estate"]) expect(d).toContain(s);
  });

  it("W72 控制:选路线开始巡逻、回待命点、停止当前任务(现问站点在跑哪一趟)", async () => {
    task = "t9";
    const { container: c } = render(<RobotsPage say={(s) => said.push(s)} now={NOW} role="admin" />);
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    fireEvent.click(cardOf(c, "A"));
    const d = c.querySelector(".robot-detail")!;
    await waitFor(() => expect(d.querySelectorAll('select[name="route"] option').length).toBe(3));
    expect(btn(d, "开始巡逻").disabled).toBe(true);
    fireEvent.change(d.querySelector('select[name="route"]')!, { target: { value: "west-loop" } });
    fireEvent.click(btn(d, "开始巡逻"));
    await waitFor(() => expect(posted.find((p) => p.url === "/api/robots/A/patrol")?.body).toEqual({ mission_id: "west-loop" }));
    await waitFor(() => expect(said.join()).toContain("west-loop"));
    fireEvent.click(btn(d, "回待命点"));
    await waitFor(() => expect(posted.some((p) => p.url === "/api/robots/A/standby/return")).toBe(true));
    await waitFor(() => expect(btn(d, "停止当前任务").disabled).toBe(false));
    fireEvent.click(btn(d, "停止当前任务"));
    await waitFor(() => expect(posted.find((p) => p.url === "/api/robots/A/abort")?.body).toEqual({ task_id: "t9" }));
  });

  it("W72 控制:被叫停的给恢复;离线不能派;经理只能派巡逻和停", async () => {
    robots[0] = card({ held: { by: "bob", at_ms: NOW - 60_000 } });
    robots[1] = { ...robots[1], problems: [], recent_problems: [] };
    const { container: c } = render(<RobotsPage say={(s) => said.push(s)} now={NOW} role="admin" />);
    await waitFor(() => expect(cardOf(c, "A")).not.toBeNull());
    fireEvent.click(cardOf(c, "A"));
    let d = c.querySelector(".robot-detail")!;
    expect(d.textContent).toContain("bob");
    fireEvent.click(btn(d, "恢复"));
    await waitFor(() => expect(posted.some((p) => p.url === "/api/robots/A/resume")).toBe(true));
    fireEvent.click(cardOf(c, "B"));
    d = c.querySelector(".robot-detail")!;
    expect(btn(d, "回待命点").disabled).toBe(true);
    expect(d.textContent).toContain("离线");
    cleanup();
    const o = render(<RobotsPage say={(s) => said.push(s)} now={NOW} role="owner" readOnly />).container;
    await waitFor(() => expect(cardOf(o, "A")).not.toBeNull());
    fireEvent.click(cardOf(o, "A"));
    d = o.querySelector(".robot-detail")!;
    expect(btn(d, "开始巡逻")).toBeTruthy();
    expect(btn(d, "停止当前任务")).toBeTruthy();
    expect(btn(d, "回待命点")).toBeUndefined();
    expect(btn(d, "恢复")).toBeUndefined();
  });
});
