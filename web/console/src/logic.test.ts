// 值班台的逻辑(商业化 B1):词条齐不齐、狗的状态、告警排序、坐标换算、请求头。
// 站点数据用手机那边的夹具(mobile/test/fixtures,站点测试真跑出来的),两边对同一份契约。
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { api, ApiError, onUnauthorized, setFetch } from "./api";
import { alertTitle, DICTS, initialLang, knownKinds, tr } from "./i18n";
import { heading, onMap, toPx } from "./mapview";
import { abnormal, alarmState, canDispatch, duration, openAlerts, queue, robotMap, robotState, wallOrder, type Alert, type Robot } from "./model";

const fx = (name: string) =>
  JSON.parse(readFileSync(new URL(`../../../mobile/test/fixtures/${name}.json`, import.meta.url), "utf-8"));

describe("i18n", () => {
  it("中文词条都是英文里有的 key", () => {
    for (const k of Object.keys(DICTS.zh)) expect(Object.keys(DICTS.en)).toContain(k);
  });
  it("每个词条中英文都有字,不显示 key;变量替换", () => {
    for (const k of Object.keys(DICTS.en) as (keyof typeof DICTS.en)[]) {
      expect(tr("zh", k as never).length).toBeGreaterThan(0);
      expect(tr("zh", k as never)).not.toBe(k);
    }
    expect(tr("en", "watching", { n: 2, total: 6 })).toBe("Watching 2 of 6 cameras");
    expect(tr("zh", "watching", { n: 2, total: 6 })).toBe("正在看 2 / 6 路画面");
  });
  it("第一次按系统语言,选过就记住", () => {
    expect(initialLang(null, ["zh-CN", "en"])).toBe("zh");
    expect(initialLang(null, ["en-GB"])).toBe("en");
    expect(initialLang(null, [])).toBe("en");
    expect(initialLang("en", ["zh-CN"])).toBe("en");
    expect(initialLang("bogus", ["zh-TW"])).toBe("zh");
  });
  it("告警标题按种类;不认识的用站点原文", () => {
    expect(alertTitle("en", "estop_pressed", "急停被按下")).toBe("E-stop pressed");
    expect(alertTitle("zh", "estop_pressed", "x")).toBe("急停被按下");
    expect(alertTitle("en", "brand_new_kind", "站点原文")).toBe("站点原文");
    expect(alertTitle("en", "$doc", "原文")).toBe("原文");
    expect(knownKinds().length).toBeGreaterThan(60);
  });
});

describe("model", () => {
  const robot: Robot = fx("site_robots").robots[0];
  it("夹具里的狗:在线、急停没解除 → 急停", () => {
    expect(robotState(robot, undefined, false)).toBe("estop");
  });
  it("状态优先级:离线 > 急停 > 驱离 > 手动 > 任务 > 充电 > 待命", () => {
    const ok = { ...robot, status: { ...robot.status!, ready: { estop_clear: true } } };
    expect(robotState({ ...ok, fresh: false }, undefined, false)).toBe("offline");
    expect(robotState(ok, { robot_id: "A", level: 1 }, false)).toBe("deterring");
    expect(robotState({ ...ok, held: { by: "x" } }, undefined, false)).toBe("manual");
    const task = (kind: string) => ({ ...ok, status: { ...ok.status!, task: { kind, state: "running" } } });
    expect(robotState(task("patrol"), undefined, false)).toBe("patrolling");
    expect(robotState(task("standby"), undefined, false)).toBe("returning");
    expect(robotState(ok, undefined, true)).toBe("charging");
    expect(robotState(ok, undefined, false)).toBe("idle");
  });
  it("巡的是哪张图", () => {
    expect(robotMap(robot)).toEqual({ map_id: "estate-1", version: "7" });
    expect(robotMap({ ...robot, capabilities: null })).toBeNull();
  });
  it("没解决的告警:P1 在前、同级新的在前、解决了的不要", () => {
    const base: Alert = fx("site_alerts").alerts[0];
    const a = (key: string, level: Alert["level"], last: number, resolved: number | null = null): Alert =>
      ({ ...base, key, level, last_ms: last, resolved_ms: resolved });
    const got = openAlerts([a("p2", "P2", 9), a("old", "P1", 1), a("new", "P1", 5), a("done", "P1", 7, 8)]);
    expect(got.map((x) => x.key)).toEqual(["new", "old", "p2"]);
  });
  it("角色", () => {
    expect(canDispatch("guard")).toBe(true);
    expect(canDispatch("owner")).toBe(false);
  });
});

describe("mapview", () => {
  const meta = { width: 200, height: 100, m_per_px: 0.5, left_x: -10, top_y: 20 };
  it("地图坐标 → 预览像素(y 朝下)", () => {
    expect(toPx(meta, -10, 20)).toEqual({ px: 0, py: 0 });
    expect(toPx(meta, 0, 0)).toEqual({ px: 20, py: 40 });
    expect(onMap(meta, 20, 40)).toBe(true);
    expect(onMap(meta, -5, 40)).toBe(false);
    expect(onMap(meta, -5, 40, 10)).toBe(true);
  });
  it("朝向:yaw=90° 朝图上方", () => {
    const { hx, hy } = heading(10, 10, Math.PI / 2, 5);
    expect(hx).toBeCloseTo(10);
    expect(hy).toBeCloseTo(5);
  });
});

describe("api", () => {
  it("改东西的请求带防伪造头、带 cookie;GET 不带", async () => {
    const calls: { path: string; init: RequestInit }[] = [];
    setFetch(async (path, init) => {
      calls.push({ path: String(path), init: init! });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    await api("GET", "/api/robots");
    await api("POST", "/api/mode", { mode: "armed" });
    const h0 = calls[0].init.headers as Record<string, string>;
    const h1 = calls[1].init.headers as Record<string, string>;
    expect(h0["X-D1Max-Web"]).toBeUndefined();
    expect(h1["X-D1Max-Web"]).toBe("1");
    expect(calls[1].init.credentials).toBe("same-origin");
    expect(calls[1].init.body).toBe(JSON.stringify({ mode: "armed" }));
  });
  it("401 回登录页(登录本身的 401 不算);错误带站点的原因", async () => {
    const cb = vi.fn();
    onUnauthorized.cb = cb;
    setFetch(async () => new Response(JSON.stringify({ error: "没登录" }), { status: 401 }));
    await expect(api("GET", "/api/robots")).rejects.toMatchObject({ status: 401, message: "没登录" });
    expect(cb).toHaveBeenCalledTimes(1);
    await expect(api("POST", "/api/login", {})).rejects.toBeInstanceOf(ApiError);
    await expect(api("GET", "/api/me")).rejects.toBeInstanceOf(ApiError);
    expect(cb).toHaveBeenCalledTimes(1);
    setFetch(async () => {
      throw new TypeError("offline");
    });
    await expect(api("GET", "/api/robots")).rejects.toMatchObject({ status: 0 });
  });
});

describe("报警状态与队列(ISA-18.2)", () => {
  const base: Alert = fx("site_alerts").alerts[0];
  const mk = (o: Partial<Alert>): Alert => ({ ...base, ...o });
  const NOW = 1_000_000;
  it("P1 没确认 = Unacknowledged,P2 没确认 = New;搁置到点自动回来", () => {
    expect(alarmState(mk({ level: "P1", acked_ms: null }), NOW)).toBe("unacked");
    expect(alarmState(mk({ level: "P2", acked_ms: null }), NOW)).toBe("new");
    expect(alarmState(mk({ level: "P2", acked_ms: 5 }), NOW)).toBe("acked");
    expect(alarmState(mk({ level: "P2", shelved_until_ms: NOW + 1 }), NOW)).toBe("shelved");
    expect(alarmState(mk({ level: "P2", shelved_until_ms: NOW }), NOW)).toBe("new");
    expect(alarmState(mk({ resolved_ms: 1 }), NOW)).toBe("resolved");
  });
  it("队列:P3 不进;搁置的另放;未确认在前,再 P1 在前,再新的在前", () => {
    const q = queue([
      mk({ key: "p2acked", level: "P2", acked_ms: 1, last_ms: 9 }),
      mk({ key: "p1acked", level: "P1", acked_ms: 1, last_ms: 1 }),
      mk({ key: "p2new", level: "P2", acked_ms: null, last_ms: 8 }),
      mk({ key: "p1old", level: "P1", acked_ms: null, last_ms: 2 }),
      mk({ key: "p1new", level: "P1", acked_ms: null, last_ms: 7 }),
      mk({ key: "p3", level: "P3", acked_ms: null, last_ms: 9 }),
      mk({ key: "shelf", level: "P2", acked_ms: null, last_ms: 9, shelved_until_ms: NOW + 5 }),
      mk({ key: "done", level: "P1", acked_ms: null, resolved_ms: 3 }),
    ], NOW);
    expect(q.active.map((a) => a.key)).toEqual(["p1new", "p1old", "p2new", "p1acked", "p2acked"]);
    expect(q.shelved.map((a) => a.key)).toEqual(["shelf"]);
  });
  it("视频墙按编号固定(数字按大小),有 P1 的置顶", () => {
    const r = (id: string) => ({ ...(fx("site_robots").robots[0] as Robot), robot_id: id });
    const w = wallOrder([r("D1-10"), r("D1-2"), r("D1-1")], new Set(["D1-10"]));
    expect(w.pinned.map((x) => x.robot_id)).toEqual(["D1-10"]);
    expect(w.rest.map((x) => x.robot_id)).toEqual(["D1-1", "D1-2"]);
  });
  it("正常状态不着色,异常的分 P1/P2", () => {
    for (const s of ["patrolling", "returning", "charging", "idle", "manual"] as const) expect(abnormal(s)).toBeNull();
    expect(abnormal("deterring")).toBe("p1");
    expect(abnormal("fell")).toBe("p1");
    expect(abnormal("offline")).toBe("p2");
  });
  it("翻倒压过任务;时长格式", () => {
    const robot: Robot = fx("site_robots").robots[0];
    const ok = { ...robot, status: { ...robot.status!, ready: { estop_clear: true } } };
    expect(robotState(ok, undefined, false, true)).toBe("fell");
    expect(duration(42_000)).toBe("0:42");
    expect(duration(725_000)).toBe("12:05");
    expect(duration(3_723_000)).toBe("1:02:03");
  });
});
