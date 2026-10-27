// @vitest-environment happy-dom
// 录像页、报表页的页面交互(C-B3 外审):在途的回包、换了选择之后,页面不许串。真的把组件渲染出来点。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { dayRange } from "./recordings";
import type { Recording } from "./recordings";
import { RecordingsPage } from "./recpage";
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
