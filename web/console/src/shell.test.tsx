// @vitest-environment happy-dom
// W44 第一期:新外壳(顶栏、路由、主题、管理员的临时提醒页)。真的把 Console 渲染出来看。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import type { Me } from "./app";
import { Console } from "./console";
import { lang } from "./i18n";
import type { Alert } from "./model";
import { pageAllowed, pageScheme, readRoute, roleLabel, schemeFor } from "./shell";
import { applyTheme } from "./theme";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const alert = (o: Partial<Alert>): Alert => ({
  key: "k", kind: "stuck", level: "P1", robot: "A", title: "t", detail: "", first_ms: 1, last_ms: 1, count: 1,
  acked_by: "", acked_ms: null, resolved_ms: null, ...o,
});

let alerts: Alert[] = [];
let posted: { url: string; body: unknown }[] = [];

function fakeSite() {
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posted.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
      return Promise.resolve(json({}));
    }
    if (url.startsWith("/api/robots")) return Promise.resolve(json({ robots: [] }));
    if (url.startsWith("/api/alerts")) return Promise.resolve(json({ alerts }));
    if (url.startsWith("/api/mode")) return Promise.resolve(json({ mode: "armed", until_ms: null }));
    if (url.startsWith("/api/deterrence")) return Promise.resolve(json({ sessions: [] }));
    if (url.startsWith("/api/watch/summary")) return Promise.resolve(json({ robots: [] }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
}

const me = (role: string): Me => ({ name: "wang", role, display_name: "王", site_name: "翠湖庄园" });

beforeEach(() => {
  lang.value = "zh";
  alerts = [];
  posted = [];
  location.hash = "";
  fakeSite();
});
afterEach(() => {
  cleanup();
  applyTheme("light");
});

describe("外壳的纯函数", () => {
  it("实测查出:没权限的页面写清楚,不给空白", () => {
    expect(pageAllowed("admin/users", "admin")).toBe(true);
    expect(pageAllowed("admin/users", "engineer")).toBe(false);
    expect(pageAllowed("admin/users", "owner")).toBe(false);
    expect(pageAllowed("admin/home", "engineer")).toBe(true);
    expect(pageAllowed("admin/home", "guard")).toBe(false);
    expect(pageAllowed("duty", "guard")).toBe(true);
  });

  it("角色的界面名字", () => {
    expect(roleLabel("admin")).toBe("roleOwner");
    expect(roleLabel("guard")).toBe("roleGuard");
    expect(roleLabel("owner")).toBe("roleFamily");
    expect(roleLabel("engineer")).toBe("roleEngineer");
  });
  it("保安页深色,管理页浅色", () => {
    for (const p of ["duty", "live", "map", "recordings", "alarms"] as const) expect(pageScheme(p)).toBe("dark");
    expect(pageScheme("admin/users")).toBe("light");
  });
  it("个人选了就按个人的,选「跟随系统」就跟系统", () => {
    expect(schemeFor("duty", "auto", false)).toBe("dark");
    expect(schemeFor("admin/users", "auto", true)).toBe("light");
    expect(schemeFor("duty", "light", true)).toBe("light");
    expect(schemeFor("admin/users", "dark", false)).toBe("dark");
    expect(schemeFor("duty", "system", false)).toBe("light");
    expect(schemeFor("admin/users", "system", true)).toBe("dark");
  });
  it("老地址跳新地址", () => {
    expect(readRoute("#/reports").page).toBe("admin/reports");
    expect(readRoute("#/localization").page).toBe("admin/localization");
    expect(readRoute("#/admin/notices").page).toBe("admin/home");
    expect(readRoute("#/admin/lists").page).toBe("admin/access");
    expect(readRoute("").page).toBe("duty");
    expect(readRoute("#/nonsense").page).toBe("duty");
    expect(readRoute("#/recordings?robot=A&t=5")).toMatchObject({ page: "recordings", robot: "A", at: 5 });
  });
});

describe("新外壳", () => {
  it("默认进值班页", async () => {
    const c = render(<Console me={me("guard")} onOut={() => {}} />);
    await waitFor(() => expect(c.container.querySelector('main[data-page="duty"]')).not.toBeNull());
  });

  it("保安看不到管理,管理员看得到", () => {
    const g = render(<Console me={me("guard")} onOut={() => {}} />);
    expect(g.container.querySelector(".tabsnav")!.textContent).not.toContain("管理");
    cleanup();
    const a = render(<Console me={me("admin")} onOut={() => {}} />);
    expect(a.container.querySelector(".tabsnav")!.textContent).toContain("管理");
  });

  it("管理入口指现场概况", () => {
    const a = render(<Console me={me("admin")} onOut={() => {}} />);
    const link = Array.from(a.container.querySelectorAll(".tabsnav a")).find((x) => x.textContent === "管理")!;
    expect(link.getAttribute("href")).toBe("#/admin/home");
  });

  it("管理导航分三组,高级默认收着", async () => {
    location.hash = "#/admin/home";
    const a = render(<Console me={me("admin")} onOut={() => {}} />);
    const nav = a.container.querySelector(".adminnav")!;
    expect(Array.from(nav.querySelectorAll(".navgroup-title")).map((x) => x.textContent)).toEqual(["常用", "布置", "高级"]);
    const hrefs = () => Array.from(nav.querySelectorAll("a")).map((x) => x.getAttribute("href"));
    expect(hrefs()).toEqual(expect.arrayContaining(["#/admin/home", "#/admin/arming", "#/admin/access", "#/admin/users", "#/admin/robots", "#/admin/reports", "#/admin/layout"]));
    expect(hrefs()).not.toContain("#/admin/system");
    fireEvent.click(Array.from(nav.querySelectorAll(".navgroup-title")).find((x) => x.textContent === "高级")!);
    expect(hrefs()).toEqual(expect.arrayContaining(["#/admin/maps", "#/admin/localization", "#/admin/system"]));
  });

  it("在高级的页里,高级是展开的", () => {
    location.hash = "#/admin/system";
    const a = render(<Console me={me("admin")} onOut={() => {}} />);
    expect(a.container.querySelector('.adminnav a[href="#/admin/system"]')).not.toBeNull();
  });

  it("工程师看得到管理、看不到用户", () => {
    location.hash = "#/admin/home";
    const e = render(<Console me={me("engineer")} onOut={() => {}} />);
    expect(e.container.querySelector(".tabsnav")!.textContent).toContain("管理");
    expect(e.container.querySelector('.adminnav a[href="#/admin/users"]')).toBeNull();
    expect(e.container.querySelector('.adminnav a[href="#/admin/robots"]')).not.toBeNull();
  });

  it("经理看不到管理", () => {
    location.hash = "#/admin/home";
    const f = render(<Console me={me("owner")} onOut={() => {}} />);
    expect(f.container.querySelector(".tabsnav")!.textContent).not.toContain("管理");
    expect(f.container.querySelector(".adminnav")).toBeNull();
  });

  it("顶栏没有语言按钮,在用户菜单里", () => {
    const c = render(<Console me={me("guard")} onOut={() => {}} />);
    const bar = c.container.querySelector(".topbar")!;
    expect(Array.from(bar.querySelectorAll("button")).some((b) => b.textContent === "EN")).toBe(false);
    fireEvent.click(bar.querySelector(".usermenu > button")!);
    expect(Array.from(bar.querySelectorAll("button")).some((b) => b.textContent === "EN")).toBe(true);
  });

  it("W58 用户菜单里能打开第三方软件声明(新窗口)", () => {
    const c = render(<Console me={me("guard")} onOut={() => {}} />);
    const bar = c.container.querySelector(".topbar")!;
    fireEvent.click(bar.querySelector(".usermenu > button")!);
    const a = Array.from(bar.querySelectorAll("a")).find((x) => x.textContent === "第三方软件声明")!;
    expect(a.getAttribute("href")).toBe("/assets/third-party-notices.txt");
    expect(a.getAttribute("target")).toBe("_blank");
    expect(a.getAttribute("rel")).toContain("noopener");
  });

  it("布防状态只显示,不能点", async () => {
    const c = render(<Console me={me("admin")} onOut={() => {}} />);
    await waitFor(() => expect(c.container.querySelector(".armstate")!.textContent).toContain("布防"));
    const s = c.container.querySelector(".armstate")!;
    expect(s.tagName).not.toBe("BUTTON");
    expect(s.querySelector("button")).toBeNull();
  });

  it("报警数只数给保安的", async () => {
    alerts = [alert({ key: "a", kind: "stuck", audience: "help" }), alert({ key: "b", kind: "cert_expiring", level: "P2", audience: "owner" })];
    const c = render(<Console me={me("guard")} onOut={() => {}} />);
    await waitFor(() => expect(c.container.querySelector(".alarmcount")!.textContent).toContain("1"));
  });

  it("保安页深色,管理页浅色(真的挂上)", async () => {
    location.hash = "#/map";
    const c = render(<Console me={me("admin")} onOut={() => {}} />);
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));
    location.hash = "#/admin/users";
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    c.unmount();
  });

  it("现场概况只列技术类的要你处理的事,保安进不去", async () => {
    alerts = [alert({ key: "a", kind: "cert_expiring", level: "P2", title: "证书快到期", audience: "owner" }),
      alert({ key: "b", kind: "stuck", title: "卡住了", audience: "help" })];
    location.hash = "#/admin/notices";
    const a = render(<Console me={me("admin")} onOut={() => {}} />);
    await waitFor(() => expect(a.container.querySelector(".home-todo")!.textContent).toContain("证书"));
    expect(a.container.querySelector(".home-todo")!.textContent).not.toContain("卡住了");
    fireEvent.click(Array.from(a.container.querySelectorAll(".home-todo button")).find((b) => b.textContent === "已处理")!);
    await waitFor(() => expect(posted).toContainEqual({ url: "/api/alerts/a/resolve", body: { verdict: "handled" } }));
    cleanup();
    const g = render(<Console me={me("guard")} onOut={() => {}} />);
    expect(g.container.querySelector(".home")).toBeNull();
  });
});
