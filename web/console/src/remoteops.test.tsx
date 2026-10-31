// @vitest-environment happy-dom
// W55 远程运维(网页):机器人诊断快照、日志、重启整机再输密码;老机器人不报运维能力时说清楚;站点重启再输密码;
// App 安装包连签名清单一起传;工程师账号可同时发 VPN 配置;网页错误攒着、定时传。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setFetch } from "./api";
import { flush, note } from "./errlog";
import { lang } from "./i18n";
import { AppPackages, RobotOps, SiteMetrics, SiteOps } from "./remoteops";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let posted: { url: string; body: unknown; headers: Record<string, string> }[];
let got: string[];

beforeEach(() => {
  lang.value = "zh";
  posted = [];
  got = [];
  setFetch(((url: string, init?: RequestInit) => {
    const h = (init?.headers ?? {}) as Record<string, string>;
    if (init?.method === "POST") {
      const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
      posted.push({ url, body, headers: h });
      if (url === "/api/robots/A/ops/reboot") {
        return Promise.resolve((body as { confirm_password?: string }).confirm_password === "pw" ? json({ ok: true }) : json({ error: "密码不对" }, 403));
      }
      return Promise.resolve(json({ ok: true, sent: 1, errors: [] }));
    }
    got.push(url);
    if (url === "/api/robots/A/ops/diag") {
      return Promise.resolve(json({ at_ms: 0, services: [{ unit: "d1max-agent.service", active: "active", sub: "running", enabled: "enabled", restarts: 2, since: "" }],
        release: { current: "v1.5.0" }, memory: { MemTotal: 8 * 1048576 * 1024, MemAvailable: 1048576 * 1024 }, env: { D1MAX_PIN: "***" } }));
    }
    if (url.startsWith("/api/robots/A/ops/journal")) return Promise.resolve(json({ unit: "d1max-agent", text: "line1\nline2", truncated: true }));
    if (url === "/api/system/vpn") {
      return Promise.resolve(json({ ready: true, peers: [{ name: "eng-1", kind: "engineer", address: "10.77.0.200", public: "K", expires_ms: 1 }],
        status: { up: true, peers: [{ public_key: "K", latest_handshake: 0 }] } }));
    }
    if (url === "/api/system/backups") return Promise.resolve(json({ dest: "/mnt/bak", last_ok_ms: 1 }));
    if (url.startsWith("/api/app-update?platform=android")) return Promise.resolve(json({ available: true, version: "1.2.0+12" }));
    if (url.startsWith("/api/app-update")) return Promise.resolve(json({ available: false }));
    if (url.startsWith("/api/metrics")) {
      return Promise.resolve(json({ metrics: [{ ts: 1, key: "site_disk_free", value: 0.31 }, { ts: 2, key: "site_disk_free", value: 0.2 },
        { ts: 1, key: "robots_online", value: 2 }] }));
    }
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);
const btn = (root: Element, text: string) => Array.from(root.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

const ALL = ["ops_diag", "ops_journal", "ops_restart", "ops_reboot", "ops_debug", "ops_timesync", "ops_cleanup", "cert_csr"];

describe("机器人远程运维", () => {
  it("老机器人没报运维能力:说要先升级,不给按钮", () => {
    const { container: c } = render(<RobotOps robotId="A" tasks={["patrol"]} say={() => {}} />);
    expect(c.textContent).toContain("暂不支持远程运维");
    expect(c.querySelector("button")).toBeNull();
  });
  it("诊断快照列服务、版本,密码类已打码", async () => {
    const { container: c } = render(<RobotOps robotId="A" tasks={ALL} say={() => {}} />);
    fireEvent.click(btn(c, "诊断快照"));
    await waitFor(() => expect(c.querySelector(".ops-svc")!.textContent).toContain("d1max-agent.service"));
    expect(c.textContent).toContain("v1.5.0");
    expect(c.textContent).toContain("***");
  });
  it("日志按服务和时间段取,能下载,截断了要说", async () => {
    const { container: c } = render(<RobotOps robotId="A" tasks={ALL} say={() => {}} />);
    fireEvent.click(btn(c, "查看日志"));
    await waitFor(() => expect(c.querySelector(".ops-logtext")!.textContent).toContain("line2"));
    expect(got.at(-1)).toBe("/api/robots/A/ops/journal?unit=d1max-agent&hours=6");
    expect(c.textContent).toContain("仅显示最近一部分");
    expect(c.querySelector('a[download="A-d1max-agent.log"]')).not.toBeNull();
  });
  it("重启整机要再输密码,错了对话框不关", async () => {
    const said: string[] = [];
    const { container: c } = render(<RobotOps robotId="A" tasks={ALL} say={(s) => said.push(s)} />);
    fireEvent.click(btn(c, "重启机器人"));
    const pw = () => document.querySelector('form.confirm-pw input[type="password"]') as HTMLInputElement;
    fireEvent.input(pw(), { target: { value: "bad" } });
    fireEvent.submit(document.querySelector("form.confirm-pw")!);
    await waitFor(() => expect(document.querySelector("form.confirm-pw .error")).not.toBeNull());
    fireEvent.input(pw(), { target: { value: "pw" } });
    fireEvent.submit(document.querySelector("form.confirm-pw")!);
    await waitFor(() => expect(said).toContain("机器人正在重启，约两分钟后重新上线。"));
    expect(document.querySelector("form.confirm-pw")).toBeNull();
  });
  it("重启服务带上选的服务名", async () => {
    const { container: c } = render(<RobotOps robotId="A" tasks={ALL} say={() => {}} />);
    fireEvent.change(c.querySelector('select[aria-label="服务"]')!, { target: { value: "d1max-localizer" } });
    fireEvent.click(btn(c, "重启服务"));
    await waitFor(() => expect(posted[0]).toMatchObject({ url: "/api/robots/A/ops/restart", body: { unit: "d1max-localizer" } }));
  });
});

describe("站点运维", () => {
  it("VPN 列出连接,重启站点要再输密码", async () => {
    const { container: c } = render(<SiteOps say={() => {}} />);
    await waitFor(() => expect(c.querySelector('[data-peer="eng-1"]')!.textContent).toContain("工程师"));
    expect(c.textContent).toContain("VPN 运行中");
    fireEvent.click(btn(c, "重启站点服务"));
    fireEvent.input(document.querySelector('form.confirm-pw input[type="password"]')!, { target: { value: "pw" } });
    fireEvent.submit(document.querySelector("form.confirm-pw")!);
    await waitFor(() => expect(posted[0]).toMatchObject({ url: "/api/system/restart", body: { confirm_password: "pw" } }));
  });
  it("App 安装包连签名清单一起传", async () => {
    const { container: c } = render(<AppPackages say={() => {}} />);
    await waitFor(() => expect(c.querySelector('[data-platform="android"]')!.textContent).toContain("1.2.0+12"));
    const [pkg, man] = Array.from(c.querySelectorAll('input[type="file"]'));
    fireEvent.change(pkg, { target: { files: [new File([new Uint8Array([1])], "nw.apk")] } });
    fireEvent.change(man, { target: { files: [new File(['{"name":"nw.apk","version":"1.3.0+13"}'], "nw.apk.manifest.json")] } });
    fireEvent.click(btn(c, "上传"));
    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].url).toBe("/api/app-update/upload?platform=android");
    expect(JSON.parse(atob(posted[0].headers["X-App-Manifest"]))).toEqual({ name: "nw.apk", version: "1.3.0+13" });
  });
});

describe("运行指标", () => {
  it("站点磁盘剩余是比例:显示百分比", async () => {
    const { container: c } = render(<SiteMetrics />);
    await waitFor(() => expect(c.querySelector('[data-metric="site_disk_free"] b')!.textContent).toBe("20%"));
  });
});

describe("网页错误记录", () => {
  it("攒着,flush 时一次传给站点", async () => {
    note("error", "boom", "x.js:1");
    await flush();
    const up = posted.find((p) => p.url === "/api/client-errors")!;
    expect(up.body).toMatchObject({ client: "web", items: [{ kind: "error", message: "boom" }] });
    await flush();
    expect(posted.filter((p) => p.url === "/api/client-errors").length).toBe(1);
  });
});
