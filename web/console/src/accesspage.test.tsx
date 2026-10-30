// @vitest-environment happy-dom
// W44 第二期 授权和临时撤防页(设计 §5.1):现在生效的在上面、能提前结束;加授权(每周重复 / 只这一天);全部授权;临时撤防单独一块。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AccessPage } from "./accesspage";
import { setFetch } from "./api";
import { lang } from "./i18n";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const at = (h: number, m = 0) => new Date(2026, 9, 27, h, m).getTime();

const row = (o: Record<string, unknown>) => ({ id: 3, name: "维修工", zones: ["北区"], days: 127, start: "09:00", end: "18:00", until_ms: null,
  note: "", created_by: "olga", active_now: true, on_today: true, ...o });
let authz: Record<string, unknown>[];
let mode: Record<string, unknown>;
let posted: { url: string; body: Record<string, unknown> }[];

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  authz = [row({}), row({ id: 4, name: "保洁", zones: [], days: 0b1100000, active_now: false, on_today: false })];
  mode = { mode: "armed", label: "布防", prev_mode: "", until_ms: null, source: "manual", zones: [{ zone: "北区" }, { zone: "前门" }] };
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      if (url.endsWith("/end")) authz = authz.map((a) => (a.id === 3 ? { ...a, active_now: false } : a));
      if (url === "/api/mode") return Promise.resolve(json({ ...mode, ...body }));
      return Promise.resolve(json({ authorizations: authz }));
    }
    if (url.startsWith("/api/lists/authorizations")) return Promise.resolve(json({ authorizations: authz }));
    if (url.startsWith("/api/mode")) return Promise.resolve(json(mode));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const page = () => render(<AccessPage say={() => {}} now={at(10)} />);
const btn = (c: Element, text: string) => Array.from(c.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("授权和临时撤防页", () => {
  it("现在生效的列在上面,能提前结束", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".acc-now")!.textContent).toContain("维修工"));
    expect(c.querySelector(".acc-now")!.textContent).toContain("18:00");
    expect(c.querySelector(".acc-now")!.textContent).not.toContain("保洁");
    fireEvent.click(btn(c.querySelector(".acc-now")!, "提前结束"));
    fireEvent.click(btn(document.body, "结束"));
    await waitFor(() => expect(posted.map((p) => p.url)).toContain("/api/lists/authorizations/3/end"));
    await waitFor(() => expect(c.querySelector(".acc-now")!.textContent).toContain("现在没有"));
  });

  it("全部授权都列出来", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".acc-all")!.textContent).toContain("保洁"));
    expect(c.querySelector(".acc-all")!.textContent).toContain("周末");
  });

  it("不重复的授权只占那一天", async () => {
    const { container: c } = page();
    await waitFor(() => expect(btn(c, "加一个授权")).toBeTruthy());
    fireEvent.click(btn(c, "加一个授权"));
    const f = document.querySelector("form.acc-add")!;
    fireEvent.input(f.querySelector('input[name="who"]')!, { target: { value: "客人" } });
    fireEvent.click(f.querySelector('input[name="repeat"]')!);          // 关掉每周重复
    fireEvent.input(f.querySelector('input[name="date"]')!, { target: { value: "2026-10-30" } });
    fireEvent.submit(f);
    await waitFor(() => expect(posted.some((p) => p.url === "/api/lists/authorizations")).toBe(true));
    const b = posted.find((p) => p.url === "/api/lists/authorizations")!.body;
    expect(b.name).toBe("客人");
    expect(b.days).toBe(0b0010000);                                    // 2026-10-30 是星期五
    expect(b.until_ms).toBe(new Date(2026, 9, 30, 23, 59, 59).getTime());
  });

  it("每周重复按选的星期", async () => {
    const { container: c } = page();
    await waitFor(() => expect(btn(c, "加一个授权")).toBeTruthy());
    fireEvent.click(btn(c, "加一个授权"));
    const f = document.querySelector("form.acc-add")!;
    fireEvent.input(f.querySelector('input[name="who"]')!, { target: { value: "维修工" } });
    fireEvent.click(f.querySelector('input[name="day1"]')!);            // 默认工作日;再勾上星期二以外的都不动
    fireEvent.submit(f);
    await waitFor(() => expect(posted.some((p) => p.url === "/api/lists/authorizations")).toBe(true));
    const b = posted.find((p) => p.url === "/api/lists/authorizations")!.body;
    expect(b.days).toBe(0b0011101);
    expect(b.until_ms).toBeNull();
  });

  it("开始临时撤防带防区和小时", async () => {
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".acc-visitor")!.textContent).toContain("北区"));
    fireEvent.click(c.querySelector('.acc-visitor input[value="北区"]')!);
    fireEvent.change(c.querySelector('.acc-visitor select[name="hours"]')!, { target: { value: "2" } });
    fireEvent.click(btn(c, "开始临时撤防"));
    await waitFor(() => expect(posted.find((p) => p.url === "/api/mode")?.body).toEqual({ mode: "visitor", zones: ["北区"], minutes: 120 }));
  });

  it("临时撤防开着写到几点、能现在结束", async () => {
    mode = { ...mode, mode: "visitor", label: "临时撤防", prev_mode: "home", until_ms: at(14), visitor_zones: ["北区"] };
    const { container: c } = page();
    await waitFor(() => expect(c.querySelector(".acc-visitor")!.textContent).toContain("14:00"));
    fireEvent.click(btn(c, "现在结束"));
    await waitFor(() => expect(posted.find((p) => p.url === "/api/mode")?.body).toEqual({ mode: "home" }));
  });
});
