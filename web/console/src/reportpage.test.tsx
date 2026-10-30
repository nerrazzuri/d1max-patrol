// @vitest-environment happy-dom
// W44 第二期 报表页(设计 §5.1「报表」):这周 / 上周 / 这个月 / 自己选;四个大数字;按天柱状图;事件点了看录像;
// 导出 PDF = 打印;超过 31 天写明不查;显示、导出的只能是现在选的那一段的那一份(外审 I2 同一条规矩)。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { ReportsPage } from "./reportpage";
import { reportRange } from "./reports";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const NOW = new Date(2026, 9, 28, 15, 0).getTime();            // 2026-10-28 星期三
const day0 = (y: number, m: number, d: number) => new Date(y, m - 1, d).getTime();

let asked: string[];
function summaryFor(url: string) {
  const q = new URLSearchParams(url.split("?")[1]);
  const since = Number(q.get("since"));
  const until = Number(q.get("until"));
  const days = [];
  for (let t = since; t < until; t += 86_400_000) {
    const d = new Date(t);
    days.push({ day: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`,
      patrols: 2, incidents: d.getDate() === 27 ? 2 : 0, false_alarms: d.getDate() === 27 ? 1 : 0, robot_problems: 0 });
  }
  return { since_ms: since, until_ms: until, totals: { patrols: 2 * days.length, incidents: 2, false_alarms: 1, robot_problems: 0 }, days,
    events: [{ key: "i1", kind: "intrusion", level: "P1", robot: "A", title: "北区有人闯入", first_ms: day0(2026, 10, 27) + 3_600_000,
      verdict: "false_alarm", resolved_by: "gina", audience: "intrusion" },
    { key: "s1", kind: "intrusion_unanswered", level: "P1", robot: "site", title: "前门有入侵", first_ms: day0(2026, 10, 27) + 7_200_000,
      verdict: "intruder", resolved_by: "g1", audience: "intrusion", zone: "前门" },
    { key: "s2", kind: "intrusion_unanswered", level: "P1", robot: "site", title: "有入侵", first_ms: day0(2026, 10, 27) + 7_300_000,
      verdict: "intruder", resolved_by: "g1", audience: "intrusion" }], truncated: false };
}

beforeEach(() => {
  lang.value = "zh";
  asked = [];
  location.hash = "";
  setFetch(((url: string) => {
    asked.push(url);
    if (url.startsWith("/api/reports/summary")) return Promise.resolve(json(summaryFor(url)));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const page = () => render(<ReportsPage now={NOW} />);
const btn = (c: Element, text: string) => Array.from(c.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("报表的时间段", () => {
  it("这周从星期一 0 点到下星期一 0 点", () => {
    expect(reportRange("week", NOW)).toEqual({ since: day0(2026, 10, 26), until: day0(2026, 11, 2) });
    expect(reportRange("lastweek", NOW)).toEqual({ since: day0(2026, 10, 19), until: day0(2026, 10, 26) });
    expect(reportRange("month", NOW)).toEqual({ since: day0(2026, 10, 1), until: day0(2026, 11, 1) });
  });
  it("自己选:两头都算,超过 31 天回 null", () => {
    expect(reportRange("custom", NOW, "2026-10-01", "2026-10-03")).toEqual({ since: day0(2026, 10, 1), until: day0(2026, 10, 4) });
    expect(reportRange("custom", NOW, "2026-10-01", "2026-11-01")).toBeNull();
    expect(reportRange("custom", NOW, "2026-10-05", "2026-10-01")).toBeNull();
  });
});

describe("报表页", () => {
  it("四个大数字", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelectorAll(".rep-stats .stat").length).toBe(4));
    const nums = Array.from(c.querySelectorAll(".rep-stats .stat-n")).map((x) => x.textContent);
    expect(nums).toEqual(["14", "2", "1", "0"]);
    expect(asked[0]).toBe(`/api/reports/summary?since=${day0(2026, 10, 26)}&until=${day0(2026, 11, 2)}`);
  });

  it("柱子按天、高度按比例", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelectorAll(".rep-day").length).toBe(7));
    const tall = (d: string) => parseFloat((c.querySelector(`.rep-day[data-day="${d}"] .rep-bar`) as HTMLElement).style.height);
    expect(tall("2026-10-27")).toBeCloseTo(100, 0);
    expect(tall("2026-10-26")).toBeCloseTo(50, 0);
  });

  it("实测查出:摄像头报的事「机器人」一栏不写 site,写哪个防区;点了不跳录像", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('.rep-events tr[data-key="s1"]')).not.toBeNull());
    const cells = (k: string) => Array.from(c.querySelectorAll(`.rep-events tr[data-key="${k}"] td`)).map((x) => x.textContent);
    expect(cells("s1")[1]).toBe("前门（摄像头）");
    expect(cells("s2")[1]).toBe("—");
    expect(c.querySelector(".rep-events")!.textContent).not.toContain("site");
    location.hash = "#/admin/reports";
    fireEvent.click(c.querySelector('.rep-events tr[data-key="s1"]')!);
    expect(location.hash).toBe("#/admin/reports");
  });

  it("点事件跳到录像那一刻", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('.rep-events tr[data-key="i1"]')).not.toBeNull());
    expect(c.querySelector('.rep-events tr[data-key="i1"]')!.textContent).toContain("误报");
    fireEvent.click(c.querySelector('.rep-events tr[data-key="i1"]')!);
    expect(location.hash).toBe(`#/recordings?robot=A&t=${day0(2026, 10, 27) + 3_600_000}`);
  });

  it("自己选超过31天写明不查", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".rep-stats")).not.toBeNull());
    fireEvent.click(btn(c, "自己选"));
    fireEvent.input(c.querySelector('input[name="from"]')!, { target: { value: "2026-09-01" } });
    fireEvent.input(c.querySelector('input[name="to"]')!, { target: { value: "2026-10-20" } });
    await waitFor(() => expect(c.textContent).toContain("最长 31 天"));
    expect(asked.some((u) => u.includes(`since=${day0(2026, 9, 1)}`))).toBe(false);
    expect(c.querySelector(".rep-stats")).toBeNull();
  });

  it("导出 PDF 调打印", async () => {
    const print = vi.fn();
    vi.stubGlobal("print", print);
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".rep-stats")).not.toBeNull());
    fireEvent.click(btn(c, "导出 PDF"));
    expect(print).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});
