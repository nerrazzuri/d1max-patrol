// @vitest-environment happy-dom
// W44 第二期 用户页(设计 §5.1「用户」):角色写界面名字、自己那一行不能删停、工程师临时账号(再输密码、口令只显示一次、
// 还剩多久、只能提前关和删)、看谁做过什么、删人要确认。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { lastLoginText, UsersPage } from "./userspage";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const NOW = new Date(2026, 9, 27, 12, 0).getTime();

const acct = (o: Record<string, unknown>) => ({ name: "x", role: "guard", disabled: false, display_name: "", expires_ms: null, last_login_ms: null, expired: false, ...o });
let accounts: Record<string, unknown>[];
let posted: { url: string; body: Record<string, unknown> }[];
let pwOk: boolean;

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  pwOk = true;
  accounts = [
    acct({ name: "alice", role: "admin", display_name: "陈太", last_login_ms: NOW - 3_600_000 }),
    acct({ name: "olga", role: "owner", display_name: "陈先生" }),
    acct({ name: "gina", role: "guard", display_name: "王保安", last_login_ms: NOW - 3 * 86_400_000 }),
    acct({ name: "eng-20261027-0900", role: "engineer", display_name: "李工", expires_ms: NOW + 2 * 3_600_000 + 10 * 60_000 }),
  ];
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      posted.push({ url, body });
      if (url === "/api/accounts/engineer") {
        if (!pwOk) return Promise.resolve(json({ error: "密码不对" }, 403));
        return Promise.resolve(json({ name: "eng-20261027-1200", password: "s3cret-once-only", expires_ms: NOW + 4 * 3_600_000, accounts }));
      }
      if (url.endsWith("/remove")) accounts = accounts.filter((a) => !url.includes(`/${a.name}/`));
      return Promise.resolve(json({ accounts }));
    }
    if (url.startsWith("/api/accounts")) return Promise.resolve(json({ accounts }));
    if (url.startsWith("/api/audit")) {
      return Promise.resolve(json({ audit: [{ id: 1, at: NOW - 60_000, actor: "gina", actor_role: "guard", action: "POST /api/mode", target: "armed", status: 200 }] }));
    }
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

const page = () => render(<UsersPage me="alice" say={() => {}} now={NOW} />);
const tr = (c: Element, name: string) => c.querySelector(`tr[data-name="${name}"]`)!;
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("用户页", () => {
  it("最后登录说人话", () => {
    expect(lastLoginText(null, NOW)).toBe("没登录过");
    expect(lastLoginText(NOW - 3_600_000, NOW)).toBe("今天 11:00");
    expect(lastLoginText(NOW - 3 * 86_400_000, NOW)).toBe("3 天前");
  });

  it("角色写界面名字", async () => {
    const { container: c } = page();
    await waitFor(() => expect(tr(c, "alice")).not.toBeNull());
    expect(tr(c, "alice").textContent).toContain("业主（最高权限）");
    expect(tr(c, "olga").textContent).toContain("家人 / 只看");
    expect(tr(c, "eng-20261027-0900").textContent).toContain("工程师（临时）");
    expect((tr(c, "gina").querySelector("select") as HTMLSelectElement).selectedOptions[0].textContent).toBe("保安");
  });

  it("自己那一行不能删、不能停用", async () => {
    const { container: c } = page();
    await waitFor(() => expect(tr(c, "alice")).not.toBeNull());
    expect(btn(tr(c, "alice"), "删除")).toBeUndefined();
    expect(btn(tr(c, "alice"), "停用")).toBeUndefined();
    expect(btn(tr(c, "gina"), "删除")).toBeTruthy();
  });

  it("开工程师账号要再输密码,口令只显示一次", async () => {
    const { container: c } = page();
    await waitFor(() => expect(tr(c, "alice")).not.toBeNull());
    fireEvent.click(btn(c, "给工程师开临时账号"));
    fireEvent.input(document.querySelector('form.eng-form input[name="display"]')!, { target: { value: "王工" } });
    fireEvent.click(document.querySelector('form.eng-form input[value="24"]')!);
    fireEvent.submit(document.querySelector("form.eng-form")!);
    await waitFor(() => expect(document.querySelector("form.confirm-pw")).not.toBeNull());
    fireEvent.input(document.querySelector('form.confirm-pw input[type="password"]')!, { target: { value: "my-own-password" } });
    fireEvent.submit(document.querySelector("form.confirm-pw")!);
    await waitFor(() => expect(document.body.textContent).toContain("s3cret-once-only"));
    expect(posted.find((p) => p.url === "/api/accounts/engineer")!.body).toEqual({ display_name: "王工", hours: 24, confirm_password: "my-own-password" });
    expect(document.body.textContent).toContain("eng-20261027-1200");
    expect(document.body.textContent).toContain("关掉以后就看不到了");
    fireEvent.click(btn(document.body, "关闭"));
    expect(document.body.textContent).not.toContain("s3cret-once-only");
  });

  it("密码输错不关对话框", async () => {
    pwOk = false;
    const { container: c } = page();
    await waitFor(() => expect(tr(c, "alice")).not.toBeNull());
    fireEvent.click(btn(c, "给工程师开临时账号"));
    fireEvent.input(document.querySelector('form.eng-form input[name="display"]')!, { target: { value: "王工" } });
    fireEvent.submit(document.querySelector("form.eng-form")!);
    await waitFor(() => expect(document.querySelector("form.confirm-pw")).not.toBeNull());
    fireEvent.input(document.querySelector('form.confirm-pw input[type="password"]')!, { target: { value: "wrong-password" } });
    fireEvent.submit(document.querySelector("form.confirm-pw")!);
    await waitFor(() => expect(document.querySelector("form.confirm-pw")!.textContent).toContain("密码不对"));
  });

  it("工程师那一行写还剩多久、只能提前关和删", async () => {
    const { container: c } = page();
    await waitFor(() => expect(tr(c, "eng-20261027-0900")).not.toBeNull());
    const r = tr(c, "eng-20261027-0900");
    expect(r.textContent).toContain("还剩 2 小时 10 分");
    expect(r.querySelector("select")).toBeNull();
    expect(btn(r, "重设密码")).toBeUndefined();
    fireEvent.click(btn(r, "提前关"));
    await waitFor(() => expect(posted.find((p) => p.url === "/api/accounts/eng-20261027-0900")?.body).toEqual({ disabled: true }));
  });

  it("点一个人看他做过什么", async () => {
    const { container: c } = page();
    await waitFor(() => expect(tr(c, "gina")).not.toBeNull());
    fireEvent.click(tr(c, "gina").querySelector(".user-name")!);
    await waitFor(() => expect(c.querySelector(".user-audit")!.textContent).toContain("切布防模式"));
  });

  it("删人要确认", async () => {
    const { container: c } = page();
    await waitFor(() => expect(tr(c, "gina")).not.toBeNull());
    fireEvent.click(btn(tr(c, "gina"), "删除"));
    expect(document.body.textContent).toContain("做过的事还留在记录里");
    fireEvent.click(btn(document.querySelector(".modal, [role=dialog]")!, "删除"));
    await waitFor(() => expect(posted.map((p) => p.url)).toContain("/api/accounts/gina/remove"));
    await waitFor(() => expect(c.querySelector('tr[data-name="gina"]')).toBeNull());
  });
});
