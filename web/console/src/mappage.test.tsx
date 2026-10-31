// @vitest-environment happy-dom
// W44 地图页(设计 §4.3):点图让最近的机器人过去看看(先问是谁、要多久,确认才派);保安的临时路线按顺序发点;
// 走不到的点说是第几个;禁行区图层默认关。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import type { UnitInfo } from "./live";
import { camMarks, camsOnMap, camWorld, GoLookCard, mapAreas, MapPage, TempRoutePanel } from "./mappage";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const MAP = { map_id: "m", version: "1" };

let posts: { url: string; body: unknown }[] = [];
let nearest: { status: number; body: unknown } = { status: 200, body: { robot_id: "A", path_m: 30, eta_s: 60 } };
const OK = { command_id: "c1", ack: { command_id: "c1", result: "accepted" } };
let patrolReply: { status: number; body: unknown } = { status: 200, body: OK };

beforeEach(() => {
  lang.value = "zh";
  posts = [];
  nearest = { status: 200, body: { robot_id: "A", path_m: 30, eta_s: 60 } };
  patrolReply = { status: 200, body: OK };
  vi.stubGlobal("IntersectionObserver", undefined);
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
      if (url.endsWith("/patrol")) return Promise.resolve(json(patrolReply.body, patrolReply.status));
      return Promise.resolve(json(OK));
    }
    if (url.startsWith("/api/robots/nearest")) return Promise.resolve(json(nearest.body, nearest.status));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const unitA: UnitInfo = {
  robot: { robot_id: "A", active: true, fresh: true, revoked: false, held: null, status: { online: true, last_seen: 0 },
    loc: { pose: { x: 0, y: 0, yaw: 0 } } },
  state: "patrolling", battery: 80, task: "",
};
const btn = (c: Element, text: string) => Array.from(c.querySelectorAll("button")).find((b) => b.textContent?.includes(text)) as HTMLButtonElement | undefined;

describe("让最近的机器人过去看看", () => {
  it("点图问最近的再确认才派", async () => {
    const { container: c } = render(<GoLookCard map={MAP} at={[3, 4]} units={[unitA]} onClose={() => {}} say={() => {}} />);
    await waitFor(() => expect(c.textContent).toContain("约 1 分钟"));
    expect(c.textContent).toContain("A");
    expect(posts).toEqual([]);
    fireEvent.click(btn(c, "派出")!);
    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts[0].url).toBe("/api/robots/A/goto");
    expect(posts[0].body).toMatchObject({ target: { map_id: "m", map_version: "1", frame_id: "map", x: 3, y: 4 } });
  });

  it("没有能派的说清楚", async () => {
    nearest = { status: 404, body: { error: "这张图上没有能派的机器人" } };
    const { container: c } = render(<GoLookCard map={MAP} at={[3, 4]} units={[unitA]} onClose={() => {}} say={() => {}} />);
    await waitFor(() => expect(c.textContent).toContain("此地图上没有可派的机器人"));
    expect(btn(c, "派出")).toBeUndefined();
  });
});

describe("保安的临时路线", () => {
  it("临时路线按顺序发点,派给选的那台", async () => {
    const sent = vi.fn();
    const { container: c } = render(<TempRoutePanel map={MAP} points={[[1, 2], [3, 4]]} robots={["A", "B"]}
      onUndo={() => {}} onCancel={() => {}} onSent={sent} say={() => {}} />);
    fireEvent.click(btn(c, "B")!);
    fireEvent.click(btn(c, "开始")!);
    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts[0]).toEqual({ url: "/api/robots/B/patrol", body: { points: [{ x: 1, y: 2 }, { x: 3, y: 4 }], map_id: "m", map_version: "1" } });
    await waitFor(() => expect(sent).toHaveBeenCalled());
  });

  it("走不到的点说是第几个", async () => {
    patrolReply = { status: 400, body: { error: "1 处不能用", errors: [{ index: 1, name: "P2", problem: "在障碍或禁行区里" }] } };
    const { container: c } = render(<TempRoutePanel map={MAP} points={[[1, 2], [3, 4]]} robots={["A"]}
      onUndo={() => {}} onCancel={() => {}} onSent={() => {}} say={() => {}} />);
    fireEvent.click(btn(c, "开始")!);
    await waitFor(() => expect(c.textContent).toContain("第 2 个点"));
    expect(c.textContent).toContain("在障碍或禁行区里");
  });

  it("没点点不能开始", () => {
    const { container: c } = render(<TempRoutePanel map={MAP} points={[]} robots={["A"]}
      onUndo={() => {}} onCancel={() => {}} onSent={() => {}} say={() => {}} />);
    expect(btn(c, "开始")!.disabled).toBe(true);
  });
});

describe("地图页的图层开关", () => {
  it("W60 基本图层默认开(含禁行区),待命点默认关;保安看不到工程图层、不显示定位那些", () => {
    try { localStorage.removeItem("d1max.maplayers"); } catch { /* */ }
    const { container: c } = render(<MapPage units={[]} p1={[]} now={0} selected={null} onSelect={() => {}} onVideo={() => {}} role="guard"
      mode={null} onArm={() => {}} />);
    const box = (name: string) => Array.from(c.querySelectorAll(".layers label")).find((l) => l.textContent?.includes(name))?.querySelector("input") as HTMLInputElement;
    expect(box("禁行区").checked).toBe(true);
    expect(box("防区和布防").checked).toBe(true);
    expect(box("巡逻路线").checked).toBe(true);
    expect(box("摄像头").checked).toBe(true);
    expect(c.textContent).not.toContain("定位");
    expect(c.querySelector(".eng-layers")).toBeNull();
  });

  it("W60 工程师多一组工程图层(默认关):覆盖、定位历史", () => {
    try { localStorage.removeItem("d1max.maplayers"); } catch { /* */ }
    const { container: c } = render(<MapPage units={[]} p1={[]} now={0} selected={null} onSelect={() => {}} onVideo={() => {}} role="engineer"
      mode={null} onArm={() => {}} />);
    const eng = c.querySelector(".eng-layers")!;
    expect(eng.querySelector("summary")!.textContent).toBe("工程图层");
    expect(Array.from(eng.querySelectorAll("label")).map((l) => l.textContent)).toEqual(["未巡逻时长", "定位历史"]);
    expect(Array.from(eng.querySelectorAll("input")).every((i) => !(i as HTMLInputElement).checked)).toBe(true);
  });
});

describe("地图页的摄像头图层(W44 第三期)", () => {
  const META = { width: 100, height: 100, m_per_px: 0.1, left_x: 0, top_y: 10, zones: [] };
  const cams = [
    { name: "cam1", live: true, placement: { map_id: "m", map_version: "1", x: 2, y: 2, yaw: 0, fov_deg: 90 } },
    { name: "cam2", live: true, placement: { map_id: "m", map_version: "2", x: 2, y: 2, yaw: 0, fov_deg: 90 } },
    { name: "cam3", live: true, placement: null },
  ];
  it("只画这张图这一版上的,画扇形", () => {
    const on = camsOnMap(cams, MAP);
    expect(on.map((c) => c.name)).toEqual(["cam1"]);
    const { container: c } = render(<svg>{camWorld(META, on)}</svg>);
    expect(c.querySelectorAll("path.cam-fan").length).toBe(1);
  });
  it("点摄像头看实时画面", () => {
    const open = vi.fn();
    const s = { map: (x: number, y: number) => [x * 10, 100 - y * 10] as [number, number], px: (a: number, b: number) => [a, b] as [number, number], k: 1 };
    const { container: c } = render(<svg>{camMarks(s, camsOnMap(cams, MAP), open)}</svg>);
    fireEvent.click(c.querySelector('[data-cam="cam1"]')!);
    expect(open).toHaveBeenCalledWith("cam1");
  });
});

describe("地图上画编辑器里起的区域名(W44 第四期)", () => {
  it("区域名跟防区一起画,同名的不重画", () => {
    const meta = { width: 100, height: 100, m_per_px: 0.1, left_x: 0, top_y: 10,
      display: { regions: [{ name: "A区", polygon: [[0, 0], [1, 0], [1, 1]] as [number, number][] },
        { name: "西区", polygon: [[0, 0], [1, 0], [1, 1]] as [number, number][] }] } };
    const got = mapAreas(meta, [{ zone: "西区", points: [[2, 2], [3, 2], [3, 3]] }]);
    expect(got.map((a) => a.name)).toEqual(["西区", "A区"]);
  });
});
