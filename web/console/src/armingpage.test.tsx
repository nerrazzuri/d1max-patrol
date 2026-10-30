// @vitest-environment happy-dom
// W44 第二期 布防时间页(设计 §5.1):一周七行色块、全部一样 / 每个防区单独设、例外、交班时间、保存、没存提醒、谁说了算。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { ArmingPage } from "./armingpage";
import { lang } from "./i18n";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const at = (h: number, m = 0) => new Date(2026, 9, 26, h, m).getTime();

let schedule: Record<string, unknown>;
let mode: Record<string, unknown>;
let posted: { url: string; body: Record<string, unknown> }[];

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  schedule = { enabled: true, slots: [{ zone: "", day: 0, start: "22:00", end: "24:00" }], exceptions: [], today: [] };
  mode = { mode: "armed", label: "按时间表:布防", source: "schedule", state: "armed", next_change_ms: at(22), manual_until_ms: null,
    zones: [{ zone: "北区", armed: true, home_armed: true }] };
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      if (url === "/api/arming/exceptions") {
        schedule = { ...schedule, exceptions: [{ id: 1, ...body }] };
        return Promise.resolve(json(schedule));
      }
      if (url === "/api/arming/schedule") return Promise.resolve(json({ ...schedule, ...body }));
      return Promise.resolve(json(body));
    }
    if (url.startsWith("/api/arming/schedule")) return Promise.resolve(json(schedule));
    if (url.startsWith("/api/duty-settings")) return Promise.resolve(json({ shift_hours: [7, 19], stale_hours: 2 }));
    if (url.startsWith("/api/mode")) return Promise.resolve(json(mode));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const page = () => render(<ArmingPage say={() => {}} now={at(12)} />);
const row = (c: Element, day: number) => c.querySelector(`.armrow[data-day="${day}"]`)!;
const btn = (c: Element, text: string) => Array.from(c.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;
function wide(track: Element) {
  (track as HTMLElement).getBoundingClientRect = () => ({ left: 0, width: 1440, top: 0, height: 20, right: 1440, bottom: 20, x: 0, y: 0, toJSON() {} }) as DOMRect;
}
function drag(track: Element, from: number, to: number) {
  wide(track);
  fireEvent.pointerDown(track, { clientX: from, pointerId: 1 });
  fireEvent.pointerMove(track, { clientX: to, pointerId: 1 });
  fireEvent.pointerUp(track, { clientX: to, pointerId: 1 });
}

describe("布防时间页", () => {
  it("读出来画色块", async () => {
    const { container: c } = page();
    await waitFor(() => expect(row(c, 0).querySelectorAll(".armblock").length).toBe(1));
    const b = row(c, 0).querySelector(".armblock") as HTMLElement;
    expect(parseFloat(b.style.left)).toBeCloseTo(91.667, 2);
    expect(parseFloat(b.style.width)).toBeCloseTo(8.333, 2);
    expect(row(c, 1).querySelectorAll(".armblock").length).toBe(0);
  });

  it("拖出一段再保存,发整份", async () => {
    const { container: c } = page();
    await waitFor(() => expect(row(c, 0).querySelector(".armblock")).not.toBeNull());
    drag(row(c, 1).querySelector(".armtrack")!, 600, 720);
    expect(row(c, 1).querySelectorAll(".armblock").length).toBe(1);
    fireEvent.click(c.querySelector(".arm-save")!);
    await waitFor(() => expect(posted.some((p) => p.url === "/api/arming/schedule")).toBe(true));
    const body = posted.find((p) => p.url === "/api/arming/schedule")!.body;
    expect(body.enabled).toBe(true);
    expect(body.slots).toContainEqual({ zone: "", day: 1, start: "10:00", end: "12:00" });
    expect(body.slots).toContainEqual({ zone: "", day: 0, start: "22:00", end: "24:00" });
  });

  it("点色块能删", async () => {
    const { container: c } = page();
    await waitFor(() => expect(row(c, 0).querySelector(".armblock")).not.toBeNull());
    fireEvent.click(row(c, 0).querySelector(".armblock")!);
    fireEvent.click(btn(c, "删掉这一段"));
    expect(row(c, 0).querySelectorAll(".armblock").length).toBe(0);
  });

  it("复制到别的天", async () => {
    const { container: c } = page();
    await waitFor(() => expect(row(c, 0).querySelector(".armblock")).not.toBeNull());
    fireEvent.click(row(c, 0).querySelector(".arm-copy")!);
    fireEvent.click(c.querySelector('.arm-copyto input[value="1"]')!);
    fireEvent.click(c.querySelector('.arm-copyto input[value="2"]')!);
    fireEvent.click(btn(c, "复制"));
    expect(row(c, 1).querySelectorAll(".armblock").length).toBe(1);
    expect(row(c, 2).querySelectorAll(".armblock").length).toBe(1);
  });

  it("单独设防区只改那个防区", async () => {
    const { container: c } = page();
    await waitFor(() => expect(row(c, 0).querySelector(".armblock")).not.toBeNull());
    fireEvent.click(btn(c, "每个防区单独设"));
    fireEvent.click(btn(c, "北区"));
    expect(c.textContent).toContain("跟全部防区一样");
    fireEvent.click(btn(c, "单独设这个防区"));
    drag(row(c, 2).querySelector(".armtrack")!, 480, 600);
    fireEvent.click(c.querySelector(".arm-save")!);
    await waitFor(() => expect(posted.some((p) => p.url === "/api/arming/schedule")).toBe(true));
    const slots = posted.find((p) => p.url === "/api/arming/schedule")!.body.slots as { zone: string; day: number }[];
    expect(slots).toContainEqual({ zone: "北区", day: 0, start: "22:00", end: "24:00" });
    expect(slots).toContainEqual({ zone: "北区", day: 2, start: "08:00", end: "10:00" });
    expect(slots.filter((x) => x.zone === "")).toEqual([{ zone: "", day: 0, start: "22:00", end: "24:00" }]);
  });

  it("加例外", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".arm-ex")).not.toBeNull());
    fireEvent.input(c.querySelector('.arm-ex input[name="first"]')!, { target: { value: "2026-10-30" } });
    fireEvent.input(c.querySelector('.arm-ex input[name="last"]')!, { target: { value: "2026-11-02" } });
    fireEvent.input(c.querySelector('.arm-ex input[name="note"]')!, { target: { value: "出远门" } });
    fireEvent.click(btn(c, "加一个例外"));
    await waitFor(() => expect(posted.find((p) => p.url === "/api/arming/exceptions")?.body).toEqual(
      { first_day: "2026-10-30", last_day: "2026-11-02", state: "armed", note: "出远门" }));
    await waitFor(() => expect(c.querySelector(".arm-ex")!.textContent).toContain("出远门"));
  });

  it("改交班时间", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector('select[name="shiftA"]')).not.toBeNull());
    await waitFor(() => expect((c.querySelector('select[name="shiftA"]') as HTMLSelectElement).value).toBe("7"));
    fireEvent.change(c.querySelector('select[name="shiftA"]')!, { target: { value: "8" } });
    fireEvent.change(c.querySelector('select[name="shiftB"]')!, { target: { value: "20" } });
    fireEvent.click(c.querySelector(".arm-save")!);
    await waitFor(() => expect(posted.find((p) => p.url === "/api/duty-settings")?.body).toEqual({ shift_hours: [8, 20], stale_hours: 2 }));
  });

  it("没存离开有提示", async () => {
    const { container: c } = page();
    await waitFor(() => expect(row(c, 0).querySelector(".armblock")).not.toBeNull());
    expect(c.querySelector(".arm-dirty")).toBeNull();
    drag(row(c, 3).querySelector(".armtrack")!, 60, 120);
    expect(c.querySelector(".arm-dirty")).not.toBeNull();
    const ev = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(ev);
    expect(ev.defaultPrevented).toBe(true);
  });

  it("写明现在谁说了算", async () => {
    mode = { ...mode, source: "manual", label: "留守", mode: "home", manual_until_ms: at(22) };
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".arm-who")!.textContent).toContain("22:00"));
    expect(c.querySelector(".arm-who")!.textContent).toContain("留守");
    expect(c.querySelector(".arm-who")!.textContent).toContain("回时间表");
  });
});
