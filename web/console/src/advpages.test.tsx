// @vitest-environment happy-dom
// W44 第四期 高级 → 诊断、软件升级、系统:检查单说人话、打包日志是下载、上传要再输密码、在线下载灰着、备份没成功写红、VPN 还没装。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { DiagPage, deviceText } from "./advdiag";
import { SystemPage2 } from "./advsystem";
import { UpgradePage } from "./advupgrade";
import { setFetch } from "./api";
import { lang } from "./i18n";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let health: unknown;
let uploads: { url: string; headers: Record<string, string> }[];
let posted: { url: string; body: unknown }[];

beforeEach(() => {
  lang.value = "zh";
  uploads = [];
  posted = [];
  health = { ok: false, version: "1.4.0", checks: { db: { ok: true }, disk: { ok: true, detail: "剩 120 GB" }, backup: { ok: false, detail: "已经 9 天没有备份成功" } } };
  setFetch(((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const h = (init.headers ?? {}) as Record<string, string>;
      if (url === "/api/releases/upload") {
        uploads.push({ url, headers: h });
        return Promise.resolve(h["X-D1Max-Confirm"] === "my-own-password" ? json({ release: { name: "v1.5.0" } }) : json({ error: "密码不对" }, 403));
      }
      posted.push({ url, body: JSON.parse(String(init.body ?? "{}")) });
      return Promise.resolve(json({}));
    }
    if (url === "/api/health") return Promise.resolve(json(health));
    if (url === "/api/robots") return Promise.resolve(json({ robots: [{ robot_id: "A", fresh: true, revoked: false, devices: { items: [{ id: "camera:front", state: "online" }, { id: "rtk", state: "degraded", detail: "浮点解" }] } }] }));
    if (url === "/api/releases") return Promise.resolve(json({ releases: [{ name: "v1.4.0", note: "" }], robots: { A: "v1.4.0" } }));
    if (url === "/api/versions") return Promise.resolve(json({ site: { version: "1.4.0" }, robots: [], clients: [] }));
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("诊断", () => {
  it("检查单说人话,机器人的设备也列", async () => {
    const { container: c } = render(<DiagPage say={() => {}} />);
    await waitFor(() => expect(c.querySelector('[data-check="db"]')!.textContent).toContain("数据库能读能写"));
    expect(c.querySelector('[data-check="backup"]')!.className).toContain("bad");
    await waitFor(() => expect(c.querySelector('[data-robot="A"]')!.textContent).toContain("前摄像头画面正常"));
    expect(c.querySelector('[data-robot="A"]')!.textContent).toContain("浮点解");
    expect(deviceText("link:router")).toContain("router");
    expect(deviceText("relay")).toBe("relay");
    expect(deviceText("board")).toBe("主控板温度");
    expect(deviceText("fan:stir")).toBe("风扇 stir");
    expect(deviceText("amp")).toBe("功放");
    expect(deviceText("lens:thermal")).toBe("热像镜头");
    expect(deviceText("nav2")).toBe("自主导航（Nav2）");
  });
  it("打包日志是下载", () => {
    const { container: c } = render(<DiagPage say={() => {}} />);
    expect(c.querySelector('a[href="/api/support-bundle?hours=24"]')!.hasAttribute("download")).toBe(true);
  });
});

describe("系统", () => {
  it("备份没成功写红,VPN 还没装", async () => {
    const { container: c } = render(<SystemPage2 me={{ name: "a", role: "admin", display_name: "", site_name: "翠湖庄园" }} now={0} say={() => {}} />);
    await waitFor(() => expect(c.querySelector(".sys-backup .sys-row")!.className).toContain("bad"));
    expect(c.querySelector(".sys-backup")!.textContent).toContain("9 天");
    expect(c.querySelector(".sys-vpn")!.textContent).toContain("未安装");
    expect(c.textContent).toContain("翠湖庄园");
  });
});

describe("软件升级", () => {
  it("上传要再输密码,在线下载灰着", async () => {
    const { container: c } = render(<UpgradePage say={() => {}} />);
    expect(btn(c, "在线下载").disabled).toBe(true);
    const f = new File([new Uint8Array([1, 2, 3])], "v1.5.0.tar.gz");
    fireEvent.change(c.querySelector('input[name="pkg"]')!, { target: { files: [f] } });
    fireEvent.click(btn(c, "上传升级包"));
    fireEvent.input(document.querySelector('form.confirm-pw input[type="password"]')!, { target: { value: "my-own-password" } });
    fireEvent.submit(document.querySelector("form.confirm-pw")!);
    await waitFor(() => expect(uploads.length).toBe(1));
    expect(uploads[0].headers["X-D1Max-Confirm"]).toBe("my-own-password");
  });
  it("充电且空闲时切换", async () => {
    const { container: c } = render(<UpgradePage say={() => {}} />);
    await waitFor(() => expect(c.querySelector('[data-release="v1.4.0"]')).not.toBeNull());
    fireEvent.click(btn(c.querySelector('[data-release="v1.4.0"]')!, "充电且空闲时切换"));
    await waitFor(() => expect(posted[0]).toEqual({ url: "/api/robots/A/release", body: { action: "activate", name: "v1.4.0", when: "charging" } }));
  });
});
