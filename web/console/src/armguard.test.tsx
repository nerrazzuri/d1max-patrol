// @vitest-environment happy-dom
// W73 第二期 值守方案面板:配哪条防区路线、哪台、每圈隔多久;预报几圈、每个防区多久看一次;此刻;值守记录。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { GuardPanel, runText } from "./armguard";
import { lang } from "./i18n";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const NOW = new Date(2026, 10, 4, 1, 30).getTime();

let plan: Record<string, unknown>;
let posted: { url: string; body: Record<string, unknown> }[];

const FC = {
  entries: [{ id: 1, route: "night-guard", robot: "", gap_min: 0, lap_s: 575, cycle_s: 575, mode: "schedule",
    windows: [{ day: 2, start: "01:00", end: "03:00", minutes: 120, laps: 12 }] }],
  zones: [{ zone: "东区", routes: ["night-guard"], interval_s: 575, limit_min: 15, limit_set: false },
    { zone: "西区", routes: ["night-guard"], interval_s: 575, limit_min: 15, limit_set: false }],
  warnings: ["中区 布防了但没有机器人去看(只靠摄像头)"],
};
const STATUS = {
  duty: { id: 3, started_ms: NOW - 30 * 60_000 },
  runs: [{ entry: 1, robot_id: "A", state: "post", lap: 3, task_id: "guard-watch-1-x", next_lap_ms: NOW + 60_000, watch_zone: "东区" }],
  waiting: {},
  zones: [{ zone: "东区", last_ms: NOW - 65_000, robot_id: "A", how: "post", limit_min: 15, armed: true, overdue: false },
    { zone: "西区", last_ms: NOW - 20 * 60_000, robot_id: "A", how: "point", limit_min: 15, armed: true, overdue: true }],
  now_ms: NOW,
};
const DUTIES = { duties: [{ id: 3, started_ms: NOW - 30 * 60_000, ended_ms: null, laps: 2, interruptions: 1, missed: 0, failed: 0,
  zones: [{ zone: "东区", looks: 5, max_gap_s: 610 }] }] };

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  plan = { revision: 4, entries: [{ id: 1, route: "night-guard", robot: "", gap_min: 0, shuffle: false }], limits: {}, forecast: FC };
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      plan = { ...plan, ...body, revision: 5 };
      return Promise.resolve(json(plan));
    }
    if (url === "/api/guard/plan") return Promise.resolve(json(plan));
    if (url === "/api/guard/status") return Promise.resolve(json(STATUS));
    if (url === "/api/guard/duties") return Promise.resolve(json(DUTIES));
    if (url.startsWith("/api/guard/duties/")) return Promise.resolve(json({ ...DUTIES.duties[0], log: [{ at_ms: NOW, robot_id: "A", kind: "point", zone: "东区", point: "A1", detail: "" }] }));
    if (url.startsWith("/api/routes?kind=guard")) return Promise.resolve(json({ routes: [{ id: "night-guard" }, { id: "west-guard" }] }));
    if (url === "/api/robots") return Promise.resolve(json({ robots: [{ robot_id: "A" }, { robot_id: "B" }] }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const panel = (canEdit = true) => render(<GuardPanel say={() => {}} now={NOW} canEdit={canEdit} />);
const btn = (c: Element, text: string) => Array.from(c.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("值守方案面板", () => {
  it("预报:一圈多久、这段约几圈、每个防区多久看一次、超时线、提示", async () => {
    const { container: c } = panel();
    await waitFor(() => expect(c.textContent).toContain("一圈约 9:35"));
    expect(c.textContent).toContain("周三 01:00–03:00：约 12 圈");
    expect(c.textContent).toContain("约每 9:35 看一次");
    expect(c.textContent).toContain("超过 15 分钟没看报警");
    expect(c.textContent).toContain("中区 布防了但没有机器人去看");
    expect(c.querySelector(".guard-clash")).toBeNull();
  });

  it("现在就撞着的巡逻排程列出来", async () => {
    plan = { ...plan, clashes: ["A 在周一 21:55–22:03有巡逻(排程 s1),跟周一 22:00 开始的布防值守(防区路线 night-guard)撞了"] };
    const { container: c } = panel();
    await waitFor(() => expect(c.querySelector(".guard-clash")?.textContent).toContain("排程 s1"));
  });

  it("改每圈间隔、加一条、设超时线 → 保存带 base_revision", async () => {
    const { container: c } = panel();
    await waitFor(() => expect(c.querySelector("select[name=gap]")).not.toBeNull());
    expect(btn(c, "保存值守方案").disabled).toBe(true);
    fireEvent.change(c.querySelector("select[name=gap]")!, { target: { value: "30" } });
    await waitFor(() => expect(c.textContent).toContain("值守方案有改动还没保存"));
    fireEvent.click(btn(c, "加一条路线"));
    const robots = c.querySelectorAll("select[name=robot]");
    await waitFor(() => expect(robots.length).toBe(2));
    fireEvent.change(robots[1], { target: { value: "B" } });
    fireEvent.input(c.querySelector("input[name='limit-东区']")!, { target: { value: "40" } });
    fireEvent.click(btn(c, "保存值守方案"));
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].url).toBe("/api/guard/plan");
    expect(posted[0].body).toEqual({
      entries: [{ route: "night-guard", robot: "", gap_min: 30, shuffle: false },
        { route: "night-guard", robot: "B", gap_min: 0, shuffle: false }],
      limits: { "东区": 40 }, base_revision: 4,
    });
    await waitFor(() => expect(c.textContent).not.toContain("值守方案有改动还没保存"));
  });

  it("没权限的只看:没有保存、选不了", async () => {
    const { container: c } = panel(false);
    await waitFor(() => expect(c.querySelector("select[name=route]")).not.toBeNull());
    expect(btn(c, "保存值守方案")).toBeUndefined();
    expect((c.querySelector("select[name=route]") as HTMLSelectElement).disabled).toBe(true);
    expect(c.querySelector("input[name='limit-东区']")).toBeNull();
  });

  it("此刻:谁在哪、每个防区多久前看过、超时标出来;最近一段值守记录", async () => {
    const { container: c } = panel();
    await waitFor(() => expect(c.textContent).toContain("在值守位上盯着东区"));
    expect(c.textContent).toContain("1:05 前看过");
    expect(c.querySelector(".guard-seen li.overdue")?.textContent).toContain("西区");
    expect(c.textContent).toContain("跑完 2 圈 · 被打断 1 次");
    expect(c.textContent).toContain("东区：看了 5 次，最长隔 10:10");
    fireEvent.click(btn(c, "看明细"));
    await waitFor(() => expect(c.querySelector(".guard-log")?.textContent).toContain("到了 东区 A1"));
  });

  it("英文也说得通", () => {
    lang.value = "en";
    expect(runText({ entry: 1, robot_id: "A", state: "lap", lap: 2, task_id: "", next_lap_ms: 0, watch_zone: "" })).toBe("running lap 2");
    expect(runText({ entry: 1, robot_id: "A", state: "paused", lap: 2, task_id: "", next_lap_ms: 0, watch_zone: "" })).toBe("stopped by a person, resting");
  });
});
