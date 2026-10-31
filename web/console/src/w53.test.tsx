// @vitest-environment happy-dom
// W53 网页:今天的巡逻、账号申请审批(W52)、开站清单、使用教程(第一次自动开、关了记下)、拒绝原因按语言说、
// 只看角色、经理能进布防时间页。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { HelpDialog, SetupChecklist, WEB_TOUR } from "./guide";
import { lang, refusalText } from "./i18n";
import { canPatrol, canSchedule } from "./model";
import { pageAllowed, roleLabel } from "./shell";
import { siteHm, TodayPatrols } from "./todaypatrols";
import { Requests } from "./userspage";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let posted: { url: string; body: Record<string, unknown> }[];
let reqs: Record<string, unknown>[];
let guide: Record<string, unknown>;

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  reqs = [{ id: "r1", name: "wangxm", display: "王小明", created_ms: Date.now() }];
  guide = { tour: 1, web_tour: 0, setup_hidden: false, skip: [] };
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      if (url.startsWith("/api/account-requests/")) reqs = [];
      if (url === "/api/me/guide") { guide = { ...guide, ...body }; return Promise.resolve(json(guide)); }
      return Promise.resolve(json({ ok: true }));
    }
    if (url === "/api/patrols/today") {
      return Promise.resolve(json({ items: [
        { at_ms: Date.now(), kind: "schedule", label: "外围一圈", robot: "A", by: "", state: "done", reason: "", end_ms: null, adhoc_id: "" },
        { at_ms: Date.now(), kind: "adhoc", label: "临时路线 3 个点", robot: "B", by: "gina", state: "queued", reason: "", end_ms: null, adhoc_id: "q" },
      ] }));
    }
    if (url === "/api/account-requests") return Promise.resolve(json({ requests: reqs }));
    if (url === "/api/setup") {
      return Promise.resolve(json({ items: [{ id: "robots", done: true, skipped: false }, { id: "arming", done: false, skipped: false }], done: 1, total: 2 }));
    }
    if (url === "/api/me") return Promise.resolve(json({ name: "alice", role: "admin", guide }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("W53 网页", () => {
  it("今天的巡逻:一行一趟,临时的写谁派的", async () => {
    const { container: c } = render(<TodayPatrols />);
    await waitFor(() => expect(c.querySelectorAll(".today-row").length).toBe(2));
    expect(c.textContent).toContain("完成");
    expect(c.textContent).toContain("临时 · 临时路线 3 个点 · B · gina");
    expect(c.textContent).toContain("排队中");
  });

  it("账号申请:权限默认只看,选保安、再输密码批准", async () => {
    const { container: c } = render(<Requests say={() => {}} onDone={() => {}} />);
    await waitFor(() => expect(c.querySelector('tr[data-req="r1"]')).not.toBeNull());
    const sel = c.querySelector("select") as HTMLSelectElement;
    expect(sel.value).toBe("viewer");
    fireEvent.change(sel, { target: { value: "guard" } });
    fireEvent.click(btn(c, "批准"));
    const pw = await waitFor(() => document.querySelector("dialog input[type=password]") as HTMLInputElement);
    fireEvent.input(pw, { target: { value: "my-password" } });
    fireEvent.submit(pw.closest("form")!);
    await waitFor(() => expect(posted.some((p) => p.url === "/api/account-requests/r1/approve")).toBe(true));
    const p = posted.find((x) => x.url === "/api/account-requests/r1/approve")!;
    expect(p.body).toEqual({ role: "guard", confirm_password: "my-password" });
  });

  it("没有申请就不占地方", async () => {
    reqs = [];
    const { container: c } = render(<Requests say={() => {}} onDone={() => {}} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(c.textContent).toBe("");
  });

  it("开站清单:做完的打勾,没做的能「不用了」、能「去做」,能收起", async () => {
    const { container: c } = render(<SetupChecklist />);
    await waitFor(() => expect(c.querySelector('li[data-item="arming"]')).not.toBeNull());
    expect(c.textContent).toContain("开站清单 1/2");
    fireEvent.click(btn(c.querySelector('li[data-item="arming"]')!, "跳过"));
    await waitFor(() => expect(posted.some((p) => p.url === "/api/me/guide" && Array.isArray(p.body.skip))).toBe(true));
    fireEvent.click(btn(c, "暂不处理"));
    await waitFor(() => expect(posted.some((p) => p.body.setup_hidden === true)).toBe(true));
  });

  it("使用教程:按角色几步,点到最后「好了」记下看过了;常见问题按角色挑", async () => {
    let closed = false;
    const { container: c } = render(<HelpDialog role="guard" onClose={() => { closed = true; }} />, { container: document.body });
    expect(c.textContent).toContain("1 / 5");
    for (let i = 0; i < 4; i++) fireEvent.click(btn(c, "下一步"));
    fireEvent.click(btn(c, "完成"));
    expect(closed).toBe(true);
    await waitFor(() => expect(posted.some((p) => p.url === "/api/me/guide" && p.body.web_tour === WEB_TOUR)).toBe(true));
    cleanup();
    const { container: d } = render(<HelpDialog role="guard" onClose={() => {}} />, { container: document.body });
    fireEvent.click(btn(d, "常见问题"));
    expect(d.textContent).toContain("巡逻为何排队");
    expect(d.textContent).not.toContain("如何添加账号");
  });

  it("时刻按站点的钟(外审 #12)", () => {
    const ms = Date.UTC(2027, 0, 15, 8, 0);
    expect(siteHm(ms, 480)).toBe("16:00");
    expect(siteHm(ms, 0)).toBe("08:00");
  });

  it("拒绝原因:英文界面用 reason_en,中文照原话", () => {
    const data = { reason_code: "offline", reason_en: "Offline." };
    lang.value = "en";
    expect(refusalText("A 不在线", data)).toBe("Offline.");
    expect(refusalText("别的", undefined)).toBe("别的");
    lang.value = "zh";
    expect(refusalText("A 不在线", data)).toBe("A 不在线");
  });

  it("角色:只看的有名字、什么都不能派;经理能派巡逻、排日程、进布防时间页,别的管理页进不去", () => {
    expect(roleLabel("viewer")).toBe("roleViewer");
    expect(canPatrol("viewer") || canSchedule("viewer")).toBe(false);
    expect(canPatrol("owner") && canSchedule("owner")).toBe(true);
    expect(pageAllowed("admin/arming", "owner")).toBe(true);
    expect(pageAllowed("admin/users", "owner")).toBe(false);
    expect(pageAllowed("admin/layout", "owner")).toBe(false);
  });
});
