// @vitest-environment happy-dom
// POC 第 5 项:路线页「派一趟」—— 先估再派;撞日程说排队;不在线的不列。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { DispatchOnce } from "./routedispatch";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let posted: { url: string; body: unknown }[];
let plan: Record<string, unknown>;
const said: string[] = [];

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  said.length = 0;
  plan = { estimate_s: 600, start_ms: 0, queued: false, because: "" };
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posted.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
      return Promise.resolve(json({ ack: { result: "accepted" } }));
    }
    if (url === "/api/robots") return Promise.resolve(json({ robots: [{ robot_id: "A", fresh: true }, { robot_id: "B", fresh: false }] }));
    if (url.startsWith("/api/robots/A/patrol-plan?mission_id=loop")) return Promise.resolve(json(plan));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);
const btn = (root: Element | Document, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("派一趟", () => {
  it("选了机器人先看预计多久、现在就走;确认了才派", async () => {
    const { container: c } = render(<DispatchOnce route="loop" say={(s) => said.push(s)} />);
    fireEvent.click(btn(c, "派一趟"));
    const sel = await waitFor(() => { const s = document.querySelector<HTMLSelectElement>('select[name="robot"]'); expect(s?.options.length).toBe(2); return s!; });
    expect(Array.from(sel.options).map((o) => o.value)).toEqual(["", "A"]);
    expect(btn(document, "派出").disabled).toBe(true);
    fireEvent.change(sel, { target: { value: "A" } });
    await waitFor(() => expect(document.querySelector("[data-plan]")?.textContent).toContain("约 10 分钟"));
    expect(document.querySelector("[data-plan]")!.textContent).toContain("现在就走");
    expect(posted).toEqual([]);
    fireEvent.click(btn(document, "派出"));
    await waitFor(() => expect(posted).toEqual([{ url: "/api/robots/A/patrol", body: { mission_id: "loop" } }]));
    await waitFor(() => expect(said.join()).toContain("loop"));
  });

  it("撞日程:说几点之后开始,按钮是排队派出", async () => {
    plan = { estimate_s: 300, start_ms: Date.UTC(2026, 10, 5, 14, 0), queued: true, because: "夜巡", utc_offset_min: 480 };
    const { container: c } = render(<DispatchOnce route="loop" say={() => {}} />);
    fireEvent.click(btn(c, "派一趟"));
    const sel = await waitFor(() => { const s = document.querySelector<HTMLSelectElement>('select[name="robot"]'); expect(s?.options.length).toBe(2); return s!; });
    fireEvent.change(sel, { target: { value: "A" } });
    await waitFor(() => expect(document.querySelector("[data-plan]")?.textContent).toContain("22:00 之后开始(夜巡)".replace("(", "（").replace(")", "）")));
    expect(btn(document, "排队派出")).toBeTruthy();
  });
});
