// @vitest-environment happy-dom
// POC 第 8 项:告警测试模式条幅 —— 管理员开、结束并清掉(先确认);保安只看得到条幅。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AlertTestBar } from "./alerttest";
import { setFetch } from "./api";
import { lang } from "./i18n";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let mode: Record<string, unknown>;
let posted: unknown[];

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  mode = { active: false, until_ms: null, by: "" };
  setFetch(((url: string, init?: RequestInit) => {
    if (url === "/api/alert-test" && init?.method === "POST") {
      const b = JSON.parse(String(init.body));
      posted.push(b);
      mode = b.action === "start" ? { active: true, until_ms: new Date(2026, 10, 5, 22, 30).getTime(), by: "alice" }
        : { active: false, until_ms: null, by: "", cleared: b.clear ? 3 : 0 };
      return Promise.resolve(json(mode));
    }
    if (url === "/api/alert-test") return Promise.resolve(json(mode));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);
const btn = (root: Element | Document, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("告警测试模式", () => {
  it("管理员开 30 分钟 → 条幅写到几点;结束并清掉先确认,说删了几条", async () => {
    let cleared = 0;
    const { container: c } = render(<AlertTestBar role="admin" onCleared={() => cleared++} />);
    await waitFor(() => expect(btn(c, "开始告警测试")).toBeTruthy());
    fireEvent.click(btn(c, "开始告警测试"));
    await waitFor(() => expect(c.querySelector('[data-alert-test="on"]')?.textContent).toContain("到 22:30 结束"));
    expect(posted).toEqual([{ action: "start", minutes: 30 }]);
    fireEvent.click(btn(c, "结束测试并清掉"));
    expect(posted.length).toBe(1);
    const dlg = await waitFor(() => { const x = document.querySelector("dialog"); expect(x).toBeTruthy(); return x!; });
    fireEvent.click(btn(dlg, "结束测试并清掉"));
    await waitFor(() => expect(c.textContent).toContain("删掉 3 条测试告警"));
    expect(posted[1]).toEqual({ action: "end", clear: true });
    expect(cleared).toBe(1);
  });

  it("保安:没开什么都不显示;开着只看条幅,没有按钮", async () => {
    const { container: c } = render(<AlertTestBar role="guard" onCleared={() => {}} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(c.querySelector("[data-alert-test]")).toBeNull();
    cleanup();
    mode = { active: true, until_ms: Date.now() + 60_000, by: "alice" };
    const { container: c2 } = render(<AlertTestBar role="guard" onCleared={() => {}} />);
    await waitFor(() => expect(c2.querySelector('[data-alert-test="on"]')).toBeTruthy());
    expect(c2.querySelectorAll("button").length).toBe(0);
  });
});
