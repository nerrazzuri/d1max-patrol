// @vitest-environment happy-dom
// 录像页、报表页的页面交互(C-B3 外审):在途的回包、换了选择之后,页面不许串。真的把组件渲染出来点。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { dayRange } from "./recordings";
import type { Recording } from "./recordings";
import { RecordingsPage } from "./recpage";
import { ReportsPage } from "./reportpage";
import type { Daily } from "./reports";
import { MapsZonesPage } from "./zonesadmin";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** 一个扣在手里的回包。 */
function held() {
  let go!: (r: Response) => void;
  const p = new Promise<Response>((res) => (go = res));
  return { p, go };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  lang.value = "en";
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("录像页:「留着」的回包只动问的那一段(外审 I1)", () => {
  const day = dayRange("2026-10-11")!;
  const noon = day.since + 12 * 3_600_000;
  const rec = (id: number, robot: string, camera: string, start = noon): Recording => ({ id, robot_id: robot, camera, start_ms: start, bytes: 100, keep: 0 });
  // 每台狗每一路每一天各有自己的段:A 前 = 1、2(挨着的两段);A 后 = 3;B 前 = 4;A 前一天 = 5
  const LIST: Record<string, Recording[]> = {
    "A/front/2026-10-11": [rec(2, "A", "front", noon + 60_000), rec(1, "A", "front")],
    "A/back/2026-10-11": [rec(3, "A", "back")],
    "B/front/2026-10-11": [rec(4, "B", "front")],
    "A/front/2026-10-10": [rec(5, "A", "front", noon - 86_400_000)],
  };
  let keeps: { id: number; h: ReturnType<typeof held> }[];
  let say: ReturnType<typeof vi.fn>;

  function mount() {
    keeps = [];
    say = vi.fn();
    setFetch(((url: string, init?: RequestInit) => {
      const u = new URL(url, "https://site");
      if (init?.method === "POST") {
        const id = Number(/recordings\/(\d+)\/keep/.exec(u.pathname)![1]);
        const h = held();
        keeps.push({ id, h });
        return h.p;
      }
      if (u.pathname === "/api/alerts") return Promise.resolve(json({ alerts: [] }));
      const since = Number(u.searchParams.get("since"));
      const date = since === day.since ? "2026-10-11" : "2026-10-10";
      return Promise.resolve(json({ segment_s: 60, recordings: LIST[`${u.searchParams.get("robot")}/${u.searchParams.get("camera")}/${date}`] ?? [] }));
    }) as never);
    return render(<RecordingsPage robots={["A", "B"]} role="guard" now={noon + 3_600_000} robot="A" at={noon + 5000} say={say as never} />);
  }
  const src = (c: Element) => c.querySelector("video")?.getAttribute("src") ?? null;
  const osd = (c: Element) => c.querySelector(".well.rec .osd.top span")?.textContent ?? null;
  const playing = async (c: Element, id: number) => waitFor(() => expect(src(c)).toBe(`/api/recordings/${id}/video`));
  const idle = async (c: Element) => waitFor(() => expect(c.textContent).toContain("Click the timeline to play."));
  const kept = (id: number, robot: string, camera: string, start = noon) => json({ recording: { ...rec(id, robot, camera, start), keep: 1 } });

  it("没换选择:回来了按钮变成「不留了」,汇总里留着 1 段,画面还是这一段", async () => {
    const { container: c, getByText } = mount();
    await playing(c, 1);
    expect(osd(c)).toBe("A Front");
    fireEvent.click(getByText("Keep this segment"));
    keeps[0].h.go(kept(1, "A", "front"));
    await waitFor(() => expect(c.textContent).toContain("Stop keeping"));
    expect(src(c)).toBe("/api/recordings/1/video");
    expect(c.textContent).toContain("1 kept");
    expect(say).not.toHaveBeenCalled();
  });

  // 换了之后页面该是什么样:换狗、换一天 = 没在放;换前后 = 接着同一刻放另一路(那一路自己的段 3)
  const switches: [string, (c: Element, q: (t: string) => HTMLElement) => void, number | null, string | null][] = [
    // W44:换机器人接着同一刻放(时间轴共用一根播放线),B 那一刻有自己的段 4
    ["换成 B 机器人", (c) => fireEvent.click(Array.from(c.querySelectorAll(".recrobots button")).find((b) => b.textContent === "B")!), 4, "B Front"],
    ["换成后摄像头", (_c, q) => fireEvent.click(q("Back")), 3, "A Back"],
    ["换一天", (c) => fireEvent.change(c.querySelector('input[type="date"]')!, { target: { value: "2026-10-10" } }), null, null],
  ];
  for (const [name, change, nowPlaying, label] of switches) {
    for (const outcome of ["成功", "失败"] as const) {
      it(`${name}之后 A 前那一段的回包才到(${outcome}):不放回那一段、不标错、不弹提示`, async () => {
        const { container: c, getByText } = mount();
        await playing(c, 1);
        fireEvent.click(getByText("Keep this segment"));
        change(c, (t) => getByText(t));
        if (nowPlaying == null) await idle(c);
        else await playing(c, nowPlaying);
        const want = nowPlaying == null ? null : `/api/recordings/${nowPlaying}/video`;
        expect(src(c)).toBe(want);
        keeps[0].h.go(outcome === "成功" ? kept(1, "A", "front") : json({ error: "boom" }, 500));
        await tick();
        await tick();
        expect(src(c), "旧回包不许把那一段放回来").toBe(want);
        expect(osd(c)).toBe(label);
        expect(c.textContent).not.toContain("Stop keeping");
        expect(c.textContent).toContain("0 kept");
        expect(say).not.toHaveBeenCalled();
      });
    }
  }

  it("等回包时跳到下一段:回来了只改列表里那一行,正在放的还是下一段、按钮还是「留着」", async () => {
    const { container: c, getByText } = mount();
    await playing(c, 1);
    fireEvent.click(getByText("Keep this segment"));
    fireEvent.click(getByText("Next segment"));
    await playing(c, 2);
    keeps[0].h.go(kept(1, "A", "front"));
    await waitFor(() => expect(c.textContent).toContain("1 kept"));
    expect(src(c)).toBe("/api/recordings/2/video");
    expect(c.textContent).toContain("Keep this segment");
    expect(c.textContent).not.toContain("Stop keeping");
    // 回到那一段:看得见它已经留着了
    fireEvent.click(getByText("Previous segment"));
    await playing(c, 1);
    expect(c.textContent).toContain("Stop keeping");
  });

  it("站点回的不是问的那一段:不动", async () => {
    const { container: c, getByText } = mount();
    await playing(c, 1);
    fireEvent.click(getByText("Keep this segment"));
    keeps[0].h.go(kept(2, "A", "front", noon + 60_000));
    await tick();
    await tick();
    expect(src(c)).toBe("/api/recordings/1/video");
    expect(c.textContent).toContain("0 kept");
    expect(c.textContent).toContain("Keep this segment");
  });

  it("没换选择时失败了:说出来", async () => {
    const { container: c, getByText } = mount();
    await playing(c, 1);
    fireEvent.click(getByText("Keep this segment"));
    keeps[0].h.go(json({ error: "boom" }, 500));
    await waitFor(() => expect(say).toHaveBeenCalledTimes(1));
    expect(say.mock.calls[0][0]).toContain("boom");
  });
});

describe("报表页:导出、打印的只能是现在选的这一段的那一份(外审 I2)", () => {
  const empty = (since: number, until: number, key: string): Daily => ({
    since_ms: since, until_ms: until, rows_max: 1000,
    alarms: {
      total: 1, by_level: { P1: 1, P2: 0, P3: 0 }, acked: 0, resolved: 0, waiting: 1, closed_unacked: 0,
      ack: { P1: { n: 0, median_s: null, max_s: null }, P2: { n: 0, median_s: null, max_s: null } }, handlers: [], by_kind: [],
      rows: [{ key, level: "P1", kind: "intrusion", robot: "A", title: key, first_ms: since + 1000, count: 1, acked_by: "", acked_ms: null, resolved_by: "", resolved_ms: null, zone: null }],
      truncated: false,
    },
    incidents: { total: 1, by_outcome: { dispatched: 1 }, truncated: false,
      rows: [{ id: 1, received_at: since + 1000, source: "nvr", type: "intrusion", zone: key, intercept: null, outcome: "dispatched", robot_id: "A", result: null, note: "" }] },
    patrols: { total: 1, by_outcome: { started: 1 }, started: 1, by_result: { done: 1 }, truncated: false,
      rows: [{ id: 1, entry_id: key, scheduled_ms: since + 1000, outcome: "started", robot_id: "A", result: "done", note: "" }] },
    runs: { total: 0, finished: 0, photos: 0, by_robot: {} },
  });
  const d10 = dayRange("2026-10-10")!;
  const d11 = dayRange("2026-10-11")!;
  const me = { name: "gina", role: "guard", display_name: "", site_name: "Estate" };
  let pending: { since: number; until: number; h: ReturnType<typeof held> }[];
  let saved: { name: string; text: string }[];
  let blobs: Blob[];

  beforeEach(() => {
    pending = [];
    saved = [];
    blobs = [];
    setFetch(((url: string) => {
      const u = new URL(url, "https://site");
      const h = held();
      pending.push({ since: Number(u.searchParams.get("since")), until: Number(u.searchParams.get("until")), h });
      return h.p;
    }) as never);
    URL.createObjectURL = ((b: Blob) => `blob:${blobs.push(b)}`) as never;
    URL.revokeObjectURL = (() => {}) as never;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ name: this.download, text: "" });
    });
    window.print = vi.fn();
  });

  const BUTTONS = ["Print", "Alarms CSV", "Incidents CSV", "Patrols CSV"];
  const disabled = (q: (t: string) => HTMLElement) => BUTTONS.map((b) => (q(b) as HTMLButtonElement).disabled);
  const answer = (i: number, key: string) => pending[i].h.go(json(empty(pending[i].since, pending[i].until, key)));

  async function loaded10() {
    const r = render(<ReportsPage me={me} now={d10.since + 12 * 3_600_000} />);
    await waitFor(() => expect(pending.length).toBe(1));
    expect(disabled(r.getByText), "还没读到:都按不了").toEqual([true, true, true, true]);
    answer(0, "day10");
    await waitFor(() => expect(r.container.querySelector(".sheet")).not.toBeNull());
    expect(disabled(r.getByText)).toEqual([false, false, false, false]);
    return r;
  }
  const lastCsv = async () => (await blobs[blobs.length - 1].text());

  it("读到了:三张 CSV 的文件名按读到的那一份的时间段,内容是那一份的", async () => {
    const { getByText } = await loaded10();
    for (const b of BUTTONS.slice(1)) fireEvent.click(getByText(b));
    expect(saved.map((s) => s.name)).toEqual(["alarms_2026-10-10_0000-0000.csv", "incidents_2026-10-10_0000-0000.csv", "patrols_2026-10-10_0000-0000.csv"]);
    expect(await lastCsv()).toContain("day10");
    fireEvent.click(getByText("Print"));
    expect(window.print).toHaveBeenCalledTimes(1);
  });

  const changes: [string, (c: Element) => void, string][] = [
    ["换日期", (c) => fireEvent.change(c.querySelector('input[type="date"]')!, { target: { value: "2026-10-11" } }), "2026-10-11_0000-0000"],
    ["换起止钟点", (c) => fireEvent.input(c.querySelectorAll("input.hhmm")[1], { target: { value: "08:00" } }), "2026-10-10_0000-0800"],
    ["换成跨夜的班", (c) => { fireEvent.input(c.querySelectorAll("input.hhmm")[0], { target: { value: "20:00" } }); fireEvent.input(c.querySelectorAll("input.hhmm")[1], { target: { value: "08:00" } }); }, "2026-10-10_2000-0800"],
  ];
  for (const [name, change, tag] of changes) {
    it(`${name}、新的还没读回来:旧的那一份不显示,打印和三张 CSV 都按不了;读回来了才按得动,名字和内容都是新的`, async () => {
      const { container: c, getByText } = await loaded10();
      change(c);
      // 换了之后的第一帧就不许再拿旧的(不等 effect)
      expect(disabled(getByText)).toEqual([true, true, true, true]);
      expect(c.querySelector(".sheet")).toBeNull();
      expect(c.textContent).toContain("Loading");
      for (const b of BUTTONS) fireEvent.click(getByText(b));
      expect(saved).toEqual([]);
      expect(window.print).not.toHaveBeenCalled();
      await waitFor(() => expect(pending.length).toBeGreaterThan(1));
      await tick();
      expect(disabled(getByText)).toEqual([true, true, true, true]);
      answer(pending.length - 1, "fresh");
      await waitFor(() => expect(c.querySelector(".sheet")).not.toBeNull());
      expect(disabled(getByText)).toEqual([false, false, false, false]);
      fireEvent.click(getByText("Patrols CSV"));
      expect(saved).toEqual([{ name: `patrols_${tag}.csv`, text: "" }]);
      const text = await lastCsv();
      expect(text).toContain("fresh");
      expect(text).not.toContain("day10");
    });
  }

  it("新的没读到(失败):不拿旧的顶上,都按不了,说没读到", async () => {
    const { container: c, getByText } = await loaded10();
    fireEvent.change(c.querySelector('input[type="date"]')!, { target: { value: "2026-10-11" } });
    await waitFor(() => expect(pending.length).toBe(2));
    pending[1].h.go(json({ error: "x" }, 502));
    await waitFor(() => expect(c.textContent).toContain("Could not load the report."));
    expect(c.querySelector(".sheet")).toBeNull();
    expect(disabled(getByText)).toEqual([true, true, true, true]);
  });

  it("旧的一份回来得比新的晚:不显示、不能导出成新的", async () => {
    const r = render(<ReportsPage me={me} now={d10.since + 12 * 3_600_000} />);
    await waitFor(() => expect(pending.length).toBe(1));
    fireEvent.change(r.container.querySelector('input[type="date"]')!, { target: { value: "2026-10-11" } });
    await waitFor(() => expect(pending.length).toBe(2));
    expect([pending[1].since, pending[1].until]).toEqual([d11.since, d11.until]);
    answer(0, "day10"); // 第一天那一份现在才到
    await tick();
    await tick();
    expect(r.container.querySelector(".sheet")).toBeNull();
    expect(disabled(r.getByText)).toEqual([true, true, true, true]);
    answer(1, "day11");
    await waitFor(() => expect(r.container.querySelector(".sheet")).not.toBeNull());
    expect(r.container.textContent).toContain("day11");
    expect(r.container.textContent).not.toContain("day10");
  });

  it("证据那一行不叫「巡逻记录」(外审 M1)", async () => {
    const { container: c } = await loaded10();
    expect(c.textContent).toContain("Evidence received");
    expect(c.textContent).toContain("0 evidence records");
    expect(c.textContent).not.toMatch(/Patrol records/);
  });
});

describe("地图与防区页:硬禁区(决策 65)看得出来,只读", () => {
  it("硬禁区单独一个样子、悬停说遥控也开不进去、带到期时间;普通禁行区照旧;图例说明", async () => {
    const until = new Date(2026, 9, 14, 18, 30).getTime();
    setFetch(async (url: RequestInfo | URL) => {
      const u = String(url);
      if (u.endsWith("/api/maps")) return json({ maps: [{ map_id: "estate", version: "8", note: "", created_ms: 1, source: "x" }] });
      if (u.endsWith("/preview")) {
        return json({
          width: 100, height: 50, m_per_px: 0.1, left_x: 0, top_y: 5, zones: [
            { id: "pool", kind: "nogo", label: "Pool", hard: true, until_ms: until, polygon: [[1, 1], [2, 1], [2, 2]] },
            { id: "bed", kind: "nogo", label: "Bed", polygon: [[3, 1], [4, 1], [4, 2]] },
            { id: "lawn", kind: "slow", label: "Lawn", max_speed_mps: 0.3, polygon: [[5, 1], [6, 1], [6, 2]] },
          ],
        });
      }
      if (u.endsWith("/areas")) return json({ areas: [] });
      return json({});
    });
    const r = render(<MapsZonesPage say={() => {}} />);
    await waitFor(() => expect(r.container.querySelector("polygon.nogo-area")).toBeTruthy());
    const polys = [...r.container.querySelectorAll("polygon.nogo-area")];
    expect(polys.length).toBe(2);
    const hard = polys.find((p) => p.classList.contains("hard"))!;
    expect(hard).toBeTruthy();
    expect(hard.querySelector("title")!.textContent).toContain("Pool");
    expect(hard.querySelector("title")!.textContent).toContain("remote control");
    expect(hard.querySelector("title")!.textContent).toContain("18:30");
    const plain = polys.find((p) => !p.classList.contains("hard"))!;
    expect(plain.querySelector("title")!.textContent).toContain("Bed");
    expect(r.container.textContent).toContain("Hard no-go");
  });
});
