// @vitest-environment happy-dom
// W44 报警记录页(设计 §4.5):本班统计、上一班留的话、筛选(本班 / 今天 / 日期 × 类型 × 只看没处理的)、
// 每行抓拍 + 时间 + 什么事 + 结论和谁 + 看录像 / 去处理;截了写明;技术类不列。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { AlarmLogPage } from "./alarmlog";
import { alertTitle, lang } from "./i18n";
import type { Alert } from "./model";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const at = (h: number, m = 0) => new Date(2026, 9, 27, h, m).getTime();
const NOW = at(23, 10);

const alert = (o: Partial<Alert>): Alert => ({
  key: "k", kind: "intrusion", level: "P1", robot: "A", title: "北区有人闯入", detail: "3 号摄像头", first_ms: at(21, 46), last_ms: at(21, 46),
  count: 1, acked_by: "", acked_ms: null, resolved_ms: null, audience: "intrusion", ...o,
});

let alerts: Alert[] = [];
let truncated = false;
let note: unknown = null;
let posted: unknown[] = [];
let duty: unknown = null;

beforeEach(() => {
  lang.value = "zh";
  alerts = [];
  truncated = false;
  note = null;
  posted = [];
  duty = null;
  setFetch(((url: string, init?: RequestInit) => {
    if (url.startsWith("/api/shift-notes")) {
      if (init?.method === "POST") {
        const b = JSON.parse(String(init.body));
        posted.push(b);
        note = { id: 9, at_ms: NOW, author: "wang", text: b.text };
        return Promise.resolve(json({ note }));
      }
      return Promise.resolve(json({ latest: note }));
    }
    if (url.startsWith("/api/duty-settings")) return Promise.resolve(duty ? json(duty) : json({}, 404));
    if (url.startsWith("/api/alerts")) return Promise.resolve(json({ alerts, limit: 500, truncated }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const page = (role = "guard") => render(<AlarmLogPage now={NOW} role={role} />);
const rows = (c: Element) => Array.from(c.querySelectorAll(".it"));
const chip = (c: Element, text: string) => Array.from(c.querySelectorAll(".chip")).find((b) => b.textContent === text) as HTMLButtonElement;

describe("报警记录页", () => {
  it("实测查出:站点给的「在哪」写在那一行", async () => {
    duty = null;
    alerts = [alert({ key: "w", kind: "robot_offline_idle", audience: "offline", robot: "A", title: "机器人掉线了", detail: "", context: { where: "A区" } as never, first_ms: at(21, 50) })];
    const { container: c } = page();
    await waitFor(() => expect(rows(c).length).toBe(1));
    expect(rows(c)[0].textContent).toContain("在A区");
  });

  it("交班时间按站点设置", async () => {
    duty = { shift_hours: [8, 20], stale_hours: 2 };
    alerts = [alert({ key: "a", first_ms: at(19, 30) }), alert({ key: "b", first_ms: at(20, 30) })];
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".sum .box span")!.textContent).toContain("20:00"));
    await waitFor(() => expect(rows(c).length).toBe(1));
  });

  it("本班按19点算", async () => {
    alerts = [alert({ key: "a", first_ms: at(19, 30) }), alert({ key: "b", first_ms: at(18, 0), resolved_ms: at(18, 5) })];
    const { container: c } = page();
    await waitFor(() => expect(rows(c).length).toBe(1));
    expect(c.querySelector(".sum .box b")!.textContent).toBe("1");
    fireEvent.click(chip(c, "今天"));
    expect(rows(c).length).toBe(2);
  });

  it("只看没处理的", async () => {
    alerts = [alert({ key: "a" }), alert({ key: "b", first_ms: at(20, 0), resolved_ms: at(20, 5), verdict: "handled" })];
    const { container: c } = page();
    await waitFor(() => expect(rows(c).length).toBe(2));
    fireEvent.click(chip(c, "只看没处理的"));
    expect(rows(c).length).toBe(1);
    expect(rows(c)[0].classList.contains("open")).toBe(true);
  });

  it("按类型筛", async () => {
    alerts = [alert({ key: "a" }), alert({ key: "b", kind: "stuck", audience: "help", title: "C 被挡住了", first_ms: at(22, 58) }),
      alert({ key: "c", kind: "robot_offline", audience: "offline", title: "B 掉线", first_ms: at(19, 31) })];
    const { container: c } = page();
    await waitFor(() => expect(rows(c).length).toBe(3));
    const titles = () => rows(c).map((r) => r.querySelector("h5")!.textContent);
    fireEvent.click(chip(c, "机器人需要帮忙"));
    expect(titles()).toEqual([alertTitle("zh", "stuck", "C 被挡住了")]);
    expect(rows(c)[0].querySelector("h5")!.classList.contains("o")).toBe(true);
    fireEvent.click(chip(c, "全部"));
    expect(rows(c).length).toBe(3);
    expect(titles()[0]).toBe(alertTitle("zh", "stuck", "C 被挡住了"));
  });

  it("误报写误报和谁", async () => {
    alerts = [alert({ resolved_ms: at(21, 49), resolved_by: "wang", verdict: "false_alarm" } as Partial<Alert>)];
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".it .res")!.textContent).toContain("误报"));
    expect(c.querySelector(".it .res")!.textContent).toContain("wang");
    expect(c.querySelector(".it .res")!.textContent).toContain("21:49");
  });

  it("没处理的写还没处理、按钮去处理;处理了的看录像", async () => {
    alerts = [alert({ key: "a" }), alert({ key: "b", first_ms: at(20, 0), resolved_ms: at(20, 1), verdict: "intruder" })];
    const { container: c } = page();
    await waitFor(() => expect(rows(c).length).toBe(2));
    const [open, done] = rows(c);
    expect(open.querySelector(".res")!.textContent).toContain("还没处理");
    expect(open.querySelector("a.btn")!.getAttribute("href")).toBe("#/duty");
    expect(done.querySelector("a.btn")!.getAttribute("href")).toContain("#/recordings?robot=A&t=");
  });

  it("抓拍用录像那一刻的一帧", async () => {
    alerts = [alert({})];
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".it img")).not.toBeNull());
    expect(c.querySelector(".it img")!.getAttribute("src")).toBe(`/api/recordings/frame?robot=A&camera=front&t=${at(21, 46)}`);
  });

  it("技术类不列", async () => {
    alerts = [alert({ key: "a" }), alert({ key: "t", kind: "cert_expiring", audience: "owner", title: "证书快到期" })];
    const { container: c } = page();
    await waitFor(() => expect(rows(c).length).toBe(1));
    expect(c.textContent).not.toContain("证书");
  });

  it("截了写明", async () => {
    alerts = [alert({})];
    truncated = true;
    const { container: c } = page();
    await waitFor(() => expect(c.textContent).toContain("只显示最近 500 条"));
  });

  it("留话写进去显示成上一班的", async () => {
    note = { id: 1, at_ms: at(19, 2), author: "li", text: "北区东边的灯坏了" };
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".note")!.textContent).toContain("北区东边的灯坏了"));
    expect(c.querySelector(".note")!.textContent).toContain("li");
    expect(c.querySelector(".note")!.textContent).toContain("19:02");
    fireEvent.input(c.querySelector(".note textarea")!, { target: { value: "3 号摄像头误报多" } });
    fireEvent.click(Array.from(c.querySelectorAll(".note button")).find((b) => b.textContent === "留话")!);
    await waitFor(() => expect(c.querySelector(".note")!.textContent).toContain("3 号摄像头误报多"));
    expect(posted).toEqual([{ text: "3 号摄像头误报多" }]);
  });

  it("经理账号不能留话", async () => {
    const { container: c } = render(<AlarmLogPage now={NOW} role="viewer" />);
    await waitFor(() => expect(c.querySelector(".note")).not.toBeNull());
    expect(c.querySelector(".note textarea")).toBeNull();
  });
});
