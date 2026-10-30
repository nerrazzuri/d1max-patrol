// @vitest-environment happy-dom
// W44 录像页(设计 §4.4):机器人用按钮选;大画面 + 另一路小窗;±10 秒、截图、导出(只有能导出的人)、留作证据;
// 右边地图写那一刻机器人在哪;底下时间轴每台一行、带报警记号、能拉宽拉窄。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { dayRange, type Recording } from "./recordings";
import { RecordingsPage } from "./recpage";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const day = dayRange("2026-10-27")!;
const at = day.since + 21 * 3_600_000;
const rec = (id: number, robot: string, camera: string, start: number): Recording => ({ id, robot_id: robot, camera, start_ms: start, bytes: 1, keep: 0 });

beforeEach(() => {
  lang.value = "zh";
  setFetch(((url: string) => {
    const u = new URL(url, "https://site");
    if (u.pathname === "/api/alerts") {
      return Promise.resolve(json({ alerts: [
        { key: "a", kind: "intrusion", level: "P1", robot: "A", title: "北区有人闯入", detail: "", first_ms: at + 30_000, last_ms: at + 30_000,
          count: 1, acked_by: "", acked_ms: null, resolved_ms: null, audience: "intrusion" },
        { key: "b", kind: "stuck", level: "P2", robot: "B", title: "被挡", detail: "", first_ms: at + 600_000, last_ms: at + 600_000,
          count: 1, acked_by: "", acked_ms: null, resolved_ms: null, audience: "help" },
      ] }));
    }
    if (u.pathname === "/api/robots/A/track") {
      return Promise.resolve(json({ points: [{ t: at, map_id: "m", map_version: "1", x: 1, y: 2 }, { t: at + 5000, map_id: "m", map_version: "1", x: 2, y: 2 }], thinned: false }));
    }
    if (u.pathname === "/api/recordings") {
      const robot = u.searchParams.get("robot");
      const cam = u.searchParams.get("camera");
      const all = [rec(1, "A", "front", at), rec(2, "A", "back", at), rec(3, "B", "front", at + 600_000)];
      return Promise.resolve(json({ segment_s: 60, recordings: all.filter((r) => (!robot || r.robot_id === robot) && (!cam || r.camera === cam)) }));
    }
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function page(role = "guard") {
  return render(<RecordingsPage robots={["A", "B"]} role={role} now={at + 3_600_000} robot="A" at={at + 1000} say={() => {}} />);
}
const btn = (c: Element, text: string) => Array.from(c.querySelectorAll("button")).find((b) => b.textContent?.includes(text)) as HTMLButtonElement | undefined;

describe("录像页", () => {
  it("机器人用按钮选,选中的那台按下", async () => {
    const { container: c } = page();
    const chips = Array.from(c.querySelectorAll(".recrobots button"));
    expect(chips.map((b) => b.textContent)).toEqual(["A", "B"]);
    expect(chips[0].getAttribute("aria-pressed")).toBe("true");
  });

  it("大画面放选的那一路,小窗放另一路(同一刻)", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector("video.feed")?.getAttribute("src")).toBe("/api/recordings/1/video"));
    await waitFor(() => expect(c.querySelector(".recpip video")?.getAttribute("src")).toBe("/api/recordings/2/video"));
  });

  it("保安没有导出按钮,管理员有", async () => {
    const g = page("guard");
    await waitFor(() => expect(g.container.querySelector("video.feed")).not.toBeNull());
    expect(btn(g.container, "导出这一段")).toBeUndefined();
    expect(btn(g.container, "截图")).toBeDefined();
    cleanup();
    const a = page("admin");
    await waitFor(() => expect(btn(a.container, "导出这一段")).toBeDefined());
  });

  it("右边地图写那一刻机器人在哪", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".rmap-hint")!.textContent).toContain("A"));
    expect(c.querySelector(".rmap-hint")!.textContent).toContain("21:00:01");
  });

  it("时间轴每台一行,报警和需要帮忙分开标", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelectorAll(".lane .mk").length).toBe(2));
    const [a, b] = Array.from(c.querySelectorAll(".lane"));
    expect(a.querySelectorAll(".mk.alarm").length).toBe(1);
    expect(b.querySelectorAll(".mk.help").length).toBe(1);
    expect(a.classList.contains("on")).toBe(true);
  });

  it("点另一台那一行换成看它", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelectorAll(".lane").length).toBe(2));
    fireEvent.click(c.querySelectorAll(".lane b")[1]);
    expect(c.querySelectorAll(".recrobots button")[1].getAttribute("aria-pressed")).toBe("true");
  });

  it("滚轮拉宽拉窄时间轴", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelectorAll(".lane").length).toBe(2));
    const span = () => c.querySelector(".tl")!.getAttribute("data-span");
    expect(span()).toBe(String(day.until - day.since));
    fireEvent.wheel(c.querySelector(".tl-tracks")!, { deltaY: -100, clientX: 0 });
    expect(Number(span())).toBeLessThan(day.until - day.since);
  });

  it("±10 秒", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector("video.feed")).not.toBeNull());
    expect(btn(c, "10 秒 ⏩")).toBeDefined();
    expect(btn(c, "⏪ 10 秒")).toBeDefined();
  });
});
