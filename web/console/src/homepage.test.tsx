// @vitest-environment happy-dom
// W44 第二期「现场概况」(设计 §5.0):要你处理的事(没有就整块不显示)、今天的安排(布防时间、今天的授权、临时撤防到几点、切模式)、
// 机器人小卡片、这周比上周。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { HomePage } from "./homepage";
import { lang } from "./i18n";
import type { Alert, Mode } from "./model";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const NOW = new Date(2026, 9, 28, 15, 0).getTime();

const alert = (o: Partial<Alert>): Alert => ({
  key: "k", kind: "cert_expiring", level: "P2", robot: "site", title: "证书快到期", detail: "12 天后", first_ms: NOW - 60_000, last_ms: NOW - 60_000,
  count: 1, acked_by: "", acked_ms: null, resolved_ms: null, audience: "owner", ...o,
});

let week: Record<string, number>;
let lastWeek: Record<string, number>;
let schedule: Record<string, unknown>;

beforeEach(() => {
  lang.value = "zh";
  location.hash = "";
  week = { patrols: 20, incidents: 3, false_alarms: 1, robot_problems: 0 };
  lastWeek = { patrols: 18, incidents: 1, false_alarms: 1, robot_problems: 2 };
  schedule = { enabled: true, slots: [], exceptions: [], today: [{ zone: "", windows: [["00:00", "07:00"], ["22:00", "24:00"]] },
    { zone: "北区", windows: [["20:00", "24:00"]] }] };
  setFetch(((url: string) => {
    if (url.startsWith("/api/arming/schedule")) return Promise.resolve(json(schedule));
    if (url.startsWith("/api/lists/authorizations")) {
      return Promise.resolve(json({ authorizations: [
        { id: 1, name: "维修工", zones: ["北区"], days: 127, start: "09:00", end: "18:00", until_ms: null, note: "", created_by: "o", active_now: true, on_today: true },
        { id: 2, name: "保洁", zones: [], days: 96, start: "09:00", end: "12:00", until_ms: null, note: "", created_by: "o", active_now: false, on_today: false },
      ] }));
    }
    if (url.startsWith("/api/robots/overview")) {
      return Promise.resolve(json({ robots: [{ robot_id: "A", name: "南区", revoked: false, expires_at: 0, online: true, battery_pct: 80, patrols_today: 4,
        problems: [], recent_problems: [], agent: "1", compat: "ok", manual_only: false }] }));
    }
    if (url.startsWith("/api/reports/summary")) {
      const since = Number(new URLSearchParams(url.split("?")[1]).get("since"));
      const isThis = since > NOW - 7 * 86_400_000;
      return Promise.resolve(json({ since_ms: since, until_ms: Number(new URLSearchParams(url.split("?")[1]).get("until")),
        totals: isThis ? week : lastWeek, days: [], events: [], truncated: false }));
    }
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const mode: Mode = { mode: "armed", until_ms: null, label: "按时间表:布防", source: "schedule", next_change_ms: new Date(2026, 9, 28, 22).getTime() } as Mode;
type Fn = (x: never) => void;
const page = (alerts: Alert[] = [], m: Mode | null = mode, on: Partial<Record<"ack" | "done" | "mode", Fn>> = {}) =>
  render(<HomePage alerts={alerts} mode={m} now={NOW} onAck={(on.ack ?? (() => {})) as (a: Alert) => void}
    onDone={(on.done ?? (() => {})) as (a: Alert) => void} onMode={(on.mode ?? (() => {})) as (m: Mode["mode"]) => void} />);
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("现场概况", () => {
  it("没有要处理的事,整块不显示", async () => {
    const { container: c } = page([alert({ kind: "stuck", audience: "help" })]);
    await waitFor(() => expect(c.querySelector(".home-today")).not.toBeNull());
    expect(c.querySelector(".home-todo")).toBeNull();
  });

  it("证书提醒写人话和该做什么,能处理", async () => {
    const done = vi.fn();
    const { container: c } = page([alert({ key: "c1" })], mode, { done });
    const todo = c.querySelector(".home-todo")!;
    expect(todo.textContent).toContain("证书");
    expect(todo.textContent).toContain("工程师");
    fireEvent.click(btn(todo, "处理好了"));
    expect(done).toHaveBeenCalledWith(expect.objectContaining({ key: "c1" }));
  });

  it("备份提醒带去系统页的按钮", () => {
    const { container: c } = page([alert({ key: "b1", kind: "backup_stale", title: "备份太久" })]);
    const a = c.querySelector('.home-todo a[href="#/admin/system"]');
    expect(a).not.toBeNull();
  });

  it("今天的安排:按防区的时间、今天的授权、谁说了算", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".home-today")!.textContent).toContain("维修工"));
    const s = c.querySelector(".home-today")!.textContent!;
    expect(s).toContain("00:00–07:00");
    expect(s).toContain("22:00–24:00");
    expect(s).toContain("北区");
    expect(s).toContain("09:00–18:00");
    expect(s).not.toContain("保洁");
    expect(s).toContain("22:00");
  });

  it("临时撤防开着写到几点", async () => {
    const { container: c } = page([], { mode: "visitor", until_ms: new Date(2026, 9, 28, 18).getTime(), visitor_zones: ["前门"] } as Mode);
    await waitFor(() => expect(c.querySelector(".home-today")!.textContent).toContain("18:00"));
  });

  it("切模式交出去(确认在外面)", async () => {
    const m = vi.fn();
    const { container: c } = page([], mode, { mode: m });
    fireEvent.click(btn(c.querySelector(".home-today")!, "留守"));
    expect(m).toHaveBeenCalledWith("home");
  });

  it("机器人小卡片点了去机器人页", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".home-robots a")).not.toBeNull());
    const a = c.querySelector(".home-robots a")!;
    expect(a.getAttribute("href")).toBe("#/admin/robots");
    expect(a.textContent).toContain("南区");
    expect(a.textContent).toContain("80%");
  });

  it("这周比上周", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".home-week")!.textContent).toContain("比上周多 2 次"));
    expect(c.querySelector(".home-week")!.textContent).toContain("跟上周一样");
  });
});
