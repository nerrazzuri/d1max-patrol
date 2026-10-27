// @vitest-environment happy-dom
// W44 值班页(设计 §4.1):报警横幅三个按钮、结论发对、别人处理掉了横幅消失、技术类不出横幅、迷路了能帮它找位置。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { DutyPage } from "./duty";
import { lang } from "./i18n";
import type { UnitInfo } from "./live";
import type { Alert, RobotState } from "./model";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const unit = (id: string, state: RobotState, o: Partial<UnitInfo> = {}): UnitInfo => ({
  robot: { robot_id: id, active: true, fresh: true, revoked: false, held: null, status: { online: state !== "offline", last_seen: 0 } },
  state, battery: 80, task: "", ...o,
});
const alert = (o: Partial<Alert>): Alert => ({
  key: "k1", kind: "intrusion", level: "P1", robot: "A", title: "后花园有人闯入", detail: "", first_ms: 1, last_ms: 1, count: 1,
  acked_by: "", acked_ms: null, resolved_ms: null, audience: "intrusion", context: { zone: "后花园" }, ...o,
});

let posts: { url: string; body: unknown }[] = [];
let resolveStatus = 200;

beforeEach(() => {
  lang.value = "zh";
  posts = [];
  resolveStatus = 200;
  vi.stubGlobal("IntersectionObserver", undefined);
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
      return Promise.resolve(url.endsWith("/resolve") && resolveStatus !== 200 ? json({ error: "没有这条告警" }, resolveStatus) : json({}));
    }
    if (url.startsWith("/api/cameras")) return Promise.resolve(json({ cameras: [{ name: "cam3", zone: "后花园", rtsp: true }] }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function page(alerts: Alert[], units = [unit("A", "patrolling"), unit("B", "idle")], say = vi.fn(), refresh = vi.fn(async () => {})) {
  const r = render(<DutyPage units={units} alerts={alerts} now={1000} role="guard" say={say} refresh={refresh} onZoom={() => {}} />);
  return { ...r, say, refresh };
}
const btn = (c: Element, text: string) => Array.from(c.querySelectorAll("button")).find((b) => b.textContent?.includes(text)) as HTMLButtonElement | undefined;

describe("值班页的报警横幅", () => {
  it("确认有人发 intruder", async () => {
    const { container: c } = page([alert({})]);
    fireEvent.click(btn(c.querySelector(".banner")!, "确认有人")!);
    await waitFor(() => expect(posts).toContainEqual({ url: "/api/alerts/k1/resolve", body: { verdict: "intruder" } }));
  });

  it("我来处理 = 确认收到", async () => {
    const { container: c } = page([alert({})]);
    fireEvent.click(btn(c.querySelector(".banner")!, "我来处理")!);
    await waitFor(() => expect(posts).toContainEqual({ url: "/api/alerts/k1/ack", body: {} }));
  });

  it("误报要先确认", async () => {
    const { container: c } = page([alert({})]);
    fireEvent.click(btn(c.querySelector(".banner")!, "误报")!);
    expect(posts).toEqual([]);
    fireEvent.click(btn(c.querySelector(".modal")!, "误报")!);
    await waitFor(() => expect(posts).toContainEqual({ url: "/api/alerts/k1/resolve", body: { verdict: "false_alarm" } }));
  });

  it("机器人需要帮忙的,第二个按钮是处理好了", async () => {
    const { container: c } = page([alert({ kind: "stuck", audience: "help", title: "A 被挡住了" })]);
    expect(btn(c.querySelector(".banner")!, "确认有人")).toBeUndefined();
    fireEvent.click(btn(c.querySelector(".banner")!, "处理好了")!);
    await waitFor(() => expect(posts).toContainEqual({ url: "/api/alerts/k1/resolve", body: { verdict: "handled" } }));
  });

  it("别人处理掉了_横幅消失", async () => {
    resolveStatus = 404;
    const r = page([alert({})]);
    fireEvent.click(btn(r.container.querySelector(".banner")!, "确认有人")!);
    await waitFor(() => expect(r.say).toHaveBeenCalledWith("已经有人处理了"));
    expect(r.say).toHaveBeenCalledTimes(1);
    expect(r.refresh).toHaveBeenCalled();
    r.rerender(<DutyPage units={[unit("A", "patrolling")]} alerts={[]} now={2000} role="guard" say={r.say} refresh={r.refresh} onZoom={() => {}} />);
    expect(r.container.querySelector(".banner")).toBeNull();
  });

  it("技术类不出横幅", () => {
    const { container: c } = page([alert({ kind: "cert_expiring", audience: "owner", title: "证书快到期" })]);
    expect(c.querySelector(".banner")).toBeNull();
  });

  it("两件报警写还有几件", () => {
    const { container: c } = page([alert({}), alert({ key: "k2", robot: "B", kind: "stuck", audience: "help", last_ms: 0 })]);
    expect(c.querySelector(".banner")!.textContent).toContain("还有 1 件");
  });

  it("迷路了有帮它找位置,点了地图上提示怎么做", async () => {
    const { container: c } = page([alert({ kind: "loc_lost_paused", audience: "help", robot: "B", title: "B 迷路了" })]);
    fireEvent.click(btn(c.querySelector(".banner")!, "帮它找位置")!);
    await waitFor(() => expect(c.querySelector(".duty-map")!.textContent).toContain("在地图上点 B 实际站的地方"));
  });

  it("有人闯入的现场放那个防区的摄像头", async () => {
    const { container: c } = page([alert({})]);
    await waitFor(() => expect(c.querySelector(".slot img.scene")?.getAttribute("src")).toBe("/api/cameras/cam3/live"));
  });
});

describe("值班页的地图", () => {
  const META = { width: 100, height: 100, m_per_px: 0.05, left_x: 0, top_y: 5 };
  const onMap = (state: RobotState = "patrolling") => unit("A", state, {
    robot: { robot_id: "A", active: true, fresh: true, revoked: false, held: null, status: { online: true, last_seen: 0 },
      loc: { pose: { x: 1, y: 1, yaw: 0 } }, capabilities: { tasks: { patrol: { map_id: "m", map_version: "1" } } } },
  });
  beforeEach(() => {
    setFetch(((url: string, init?: RequestInit) => {
      if (init?.method === "POST") return Promise.resolve(json({}));
      if (url.endsWith("/preview")) return Promise.resolve(json(META));
      if (url.endsWith("/areas")) return Promise.resolve(json({ areas: [] }));
      return Promise.resolve(json({}, 404));
    }) as typeof fetch);
  });

  it("平时只看,不进点图选位置", async () => {
    const { container: c } = page([], [onMap()]);
    await waitFor(() => expect(c.querySelector(".duty-map svg.map")).not.toBeNull());
    expect(c.querySelector(".duty-map svg.map")!.classList.contains("drawing")).toBe(false);
    expect(c.querySelector(".duty-map")!.textContent).not.toContain("点地图选一个位置");
  });

  it("点了帮它找位置才进点图选位置", async () => {
    const { container: c } = page([alert({ kind: "loc_lost_paused", audience: "help", robot: "A", title: "A 迷路了" })], [onMap()]);
    await waitFor(() => expect(c.querySelector(".duty-map svg.map")).not.toBeNull());
    fireEvent.click(btn(c.querySelector(".banner")!, "帮它找位置")!);
    await waitFor(() => expect(c.querySelector(".duty-map svg.map")!.classList.contains("drawing")).toBe(true));
  });
});

describe("值班页的状态条", () => {
  it("数据过期_状态写未知", () => {
    const { container: c } = page([], [unit("A", "patrolling", { stale: true })]);
    expect(c.querySelector(".duty-list")!.textContent).toContain("未知");
    expect(c.querySelector(".duty-list")!.textContent).not.toContain("巡逻中");
  });

  it("每台一条,写名字、状态、电量", () => {
    const { container: c } = page([]);
    const rows = c.querySelectorAll(".duty-list .duty-row");
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("A");
    expect(rows[0].textContent).toContain("80%");
  });
});
