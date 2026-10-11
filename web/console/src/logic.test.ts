// 值班台的逻辑(商业化 B1):词条齐不齐、狗的状态、告警排序、坐标换算、请求头。
// 站点数据用手机那边的夹具(mobile/test/fixtures,站点测试真跑出来的),两边对同一份契约。
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { api, ApiError, onUnauthorized, setFetch } from "./api";
import { alertTitle, DICTS, initialLang, knownKinds, lang, tr } from "./i18n";
import { heading, onMap, toPx } from "./mapview";
import { daysText, toMap } from "./zonesadmin";
import { applyKeep, ascending, belongs, clockSeconds, covered, dateInput, dayRange, firstAfter, minuteOf, nextSegment, prevSegment, recordingLink, segmentAt, spans, type Recording } from "./recordings";
import { canReview } from "./recpage";
import { alarmsCsv, csv, csvCell, fileTag, incidentsCsv, matches, minutes, patrolsCsv, period, secondsText, stamp, type AlarmRow } from "./reports";
import { outcomeText } from "./reportpage";
import { abnormal, alarmState, canDispatch, duration, openAlerts, queue, robotMap, robotState, SIREN_LEVEL, sirenState, wallOrder, type Alert, type Robot } from "./model";

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

describe("recordings", () => {
  const rec = (id: number, start_ms: number, keep = 0): Recording => ({ id, robot_id: "A", camera: "front", start_ms, bytes: 1, keep });
  const day = dayRange("2026-10-11")!;
  it("一天的范围按当地时间;日期框的写法来回一致;看不懂的、不存在的日子不收", () => {
    expect(new Date(day.since).getHours()).toBe(0);
    expect(day.until - day.since).toBe(86_400_000);
    expect(dateInput(day.since)).toBe("2026-10-11");
    expect(dateInput(day.until - 1)).toBe("2026-10-11");
    expect(dateInput(day.until)).toBe("2026-10-12");
    expect(dayRange("2026-1-1")).toBeNull();
    expect(dayRange("2026-02-30")).toBeNull();
    expect(dayRange("2026-13-01")).toBeNull();
    expect(dayRange("2026-00-10")).toBeNull();
    expect(dayRange("")).toBeNull();
  });
  it("站点给的是新的在前:排成从早到晚;同一刻的按编号", () => {
    expect(ascending([rec(3, 300), rec(2, 100), rec(1, 100)]).map((r) => r.id)).toEqual([1, 2, 3]);
  });
  it("挨着的段并成一段,隔开 2 秒以上的分开;一共录了多久只算这一天里的", () => {
    const s = day.since;
    const rows = [rec(1, s - 30_000), rec(2, s + 30_000), rec(3, s + 91_999), rec(4, s + 300_000)];
    const sp = spans(rows, 60);
    expect(sp).toEqual([{ from: s - 30_000, to: s + 151_999 }, { from: s + 300_000, to: s + 360_000 }]);
    expect(spans([rec(1, 0), rec(2, 62_000)], 60).length).toBe(2);
    expect(covered(sp, day.since, day.until)).toBe(151_999 + 60_000);
    expect(covered([{ from: day.until - 1000, to: day.until + 5000 }], day.since, day.until)).toBe(1000);
    expect(spans([], 60)).toEqual([]);
  });
  it("哪一段盖住那一刻:含头不含尾,回从头算第几秒;空档上回 null,往后找最近的一段", () => {
    const rows = [rec(1, 0), rec(2, 60_000), rec(3, 300_000)];
    expect(segmentAt(rows, 0, 60)).toEqual({ rec: rows[0], offsetS: 0 });
    expect(segmentAt(rows, 59_999, 60)!.rec.id).toBe(1);
    expect(segmentAt(rows, 60_000, 60)).toEqual({ rec: rows[1], offsetS: 0 });
    expect(segmentAt(rows, 75_500, 60)).toEqual({ rec: rows[1], offsetS: 15.5 });
    expect(segmentAt(rows, 120_000, 60)).toBeNull();
    expect(segmentAt(rows, -1, 60)).toBeNull();
    expect(firstAfter(rows, 120_000)!.id).toBe(3);
    expect(firstAfter(rows, 300_000)!.id).toBe(3);
    expect(firstAfter(rows, 300_001)).toBeNull();
  });
  it("上一段、下一段:到头了是 null;不在列表里的也是 null", () => {
    const rows = [rec(1, 0), rec(2, 60_000)];
    expect(nextSegment(rows, rows[0])!.id).toBe(2);
    expect(nextSegment(rows, rows[1])).toBeNull();
    expect(prevSegment(rows, rows[1])!.id).toBe(1);
    expect(prevSegment(rows, rows[0])).toBeNull();
    expect(nextSegment(rows, rec(9, 5))).toBeNull();
    expect(prevSegment(rows, rec(9, 5))).toBeNull();
  });
  it("时间轴的横坐标是这一天的第几分钟,出界夹住;从告警去录像页带狗和前 10 秒", () => {
    expect(minuteOf(day.since, day.since)).toBe(0);
    expect(minuteOf(day.since + 90_000, day.since)).toBe(1.5);
    expect(minuteOf(day.since - 5, day.since)).toBe(0);
    expect(minuteOf(day.until + 5, day.since)).toBe(1440);
    expect(recordingLink({ robot: "D1 01", first_ms: 50_000 })).toBe("#/recordings?robot=D1%2001&t=40000");
    expect(recordingLink({ robot: "A", first_ms: 5 })).toBe("#/recordings?robot=A&t=1");
  });
  it("跳到几点:24 小时制 HH:MM[:SS];出界、看不懂回 null", () => {
    expect([clockSeconds("00:00"), clockSeconds("2:14"), clockSeconds("02:14:36"), clockSeconds(" 23:59:59 ")]).toEqual([0, 8040, 8076, 86399]);
    expect([clockSeconds("24:00"), clockSeconds("12:60"), clockSeconds("12:00:60"), clockSeconds("1214"), clockSeconds("2 PM"), clockSeconds("")]).toEqual([null, null, null, null, null, null]);
  });
  it("谁能标留着:管理员、保安;业主不能(跟站点的 review 权限一致)", () => {
    expect([canReview("admin"), canReview("guard"), canReview("owner"), canReview("")]).toEqual([true, true, false, false]);
  });
});

describe("reports", () => {
  it("几点几分:24:00 是一天的尽头;看不懂的不收", () => {
    expect([minutes("00:00"), minutes("7:05"), minutes("23:59"), minutes("24:00")]).toEqual([0, 425, 1439, 1440]);
    expect([minutes("24:01"), minutes("25:00"), minutes("12:60"), minutes("12"), minutes(""), minutes("ab:cd")]).toEqual([null, null, null, null, null, null]);
  });
  it("时间段:同一天里的;结束不比开始晚 = 到第二天(一整天、跨夜的班);日期不对回 null", () => {
    const day = dayRange("2026-10-11")!;
    expect(period("2026-10-11", "00:00", "00:00")).toEqual({ since: day.since, until: day.until });
    expect(period("2026-10-11", "08:00", "20:00")).toEqual({ since: day.since + 8 * 3_600_000, until: day.since + 20 * 3_600_000 });
    expect(period("2026-10-11", "20:00", "08:00")).toEqual({ since: day.since + 20 * 3_600_000, until: day.until + 8 * 3_600_000 });
    expect(period("2026-10-11", "09:30", "09:30")).toEqual({ since: day.since + 9.5 * 3_600_000, until: day.until + 9.5 * 3_600_000 });
    expect(period("2026-10-11", "00:00", "24:00")).toEqual({ since: day.since, until: day.until });
    expect(period("2026-13-01", "00:00", "00:00")).toBeNull();
    expect(period("2026-10-11", "", "00:00")).toBeNull();
    expect(period("2026-10-11", "00:00", "x")).toBeNull();
  });
  it("多少秒写成人看的", () => {
    expect([secondsText(null), secondsText(0), secondsText(41.6), secondsText(59.4)]).toEqual(["—", "0 s", "42 s", "59 s"]);
    expect([secondsText(60), secondsText(110), secondsText(3599)]).toEqual(["1 min 00 s", "1 min 50 s", "59 min 59 s"]);
    expect([secondsText(3600), secondsText(7500)]).toEqual(["1 h 00 min", "2 h 05 min"]);
  });
  it("CSV:逗号引号换行加引号;公式开头垫撇号(数字不垫);带 BOM、CRLF", () => {
    expect(csvCell("plain")).toBe("plain");
    expect(csvCell('a,"b"')).toBe('"a,""b"""');
    expect(csvCell("line1\nline2")).toBe('"line1\nline2"');
    expect([csvCell("=1+1"), csvCell("+x"), csvCell("-x"), csvCell("@x")]).toEqual(["'=1+1", "'+x", "'-x", "'@x"]);
    expect(csvCell("=A1,B")).toBe(`"'=A1,B"`);
    expect([csvCell(-5), csvCell(0), csvCell(null), csvCell(undefined)]).toEqual(["-5", "0", "", ""]);
    expect(csv(["a", "b"], [[1, "x,y"], ["狗", null]])).toBe('\uFEFFa,b\r\n1,"x,y"\r\n狗,\r\n');
    expect(csv(["a"], [])).toBe("\uFEFFa\r\n");
  });
  it("时刻、文件名", () => {
    const ms = new Date(2026, 9, 11, 2, 4, 9).getTime();
    expect(stamp(ms)).toBe("2026-10-11 02:04:09");
    expect(stamp(null)).toBe("");
    const night = period("2026-10-11", "20:00", "08:00")!;
    expect(fileTag(night.since, night.until)).toBe("2026-10-11_2000-0800");
    const whole = period("2026-10-11", "00:00", "00:00")!;
    expect(fileTag(whole.since, whole.until)).toBe("2026-10-11_0000-0000");
  });
  it("读到的报表是不是现在选的这一段:头尾都得对上;没读到、没选好都不算", () => {
    const d = { since_ms: 100, until_ms: 200 };
    expect(matches(d, { since: 100, until: 200 })).toBe(true);
    expect(matches(d, { since: 100, until: 201 })).toBe(false);
    expect(matches(d, { since: 99, until: 200 })).toBe(false);
    expect(matches(null, { since: 100, until: 200 })).toBe(false);
    expect(matches(d, null)).toBe(false);
  });
  it("「留着」的回包:只改问的那一段;选择换了、回的不是那一段都不动;正在放别的段不换回去", () => {
    const r = (id: number, keep = 0): Recording => ({ id, robot_id: "A", camera: "front", start_ms: id, bytes: 1, keep });
    const s = { rows: [r(1), r(2)], cur: r(2) };
    expect(applyKeep(s, 1, r(1, 1), true)).toEqual({ rows: [r(1, 1), r(2)], cur: r(2) });
    expect(applyKeep(s, 2, r(2, 1), true)).toEqual({ rows: [r(1), r(2, 1)], cur: r(2, 1) });
    expect(applyKeep(s, 1, r(1, 1), false)).toBe(s);
    expect(applyKeep(s, 1, r(2, 1), true)).toBe(s);
    expect(applyKeep({ rows: null, cur: null }, 1, r(1, 1), true)).toEqual({ rows: null, cur: null });
    expect([belongs(r(1), "A", "front"), belongs(r(1), "B", "front"), belongs(r(1), "A", "back"), belongs(null, "A", "front")]).toEqual([true, false, false, false]);
  });
  it("三张 CSV 的列:告警带多久确认(钟差当 0、没确认留空);入侵事件、巡逻的空值留空", () => {
    const t0 = new Date(2026, 9, 11, 2, 0, 0).getTime();
    const base: AlarmRow = { key: "k", level: "P1", kind: "dog_sees_person", robot: "A", title: "t", first_ms: t0, count: 2, acked_by: "gina", acked_ms: t0 + 41_600, resolved_by: "", resolved_ms: null, zone: "East, wall" };
    const out = alarmsCsv([base, { ...base, acked_ms: t0 - 500 }, { ...base, acked_ms: null, acked_by: "", zone: null }], (r) => `T:${r.kind}`).split("\r\n");
    expect(out[0]).toBe("\uFEFFfirst_raised,priority,kind,event,robot,zone,times_raised,acknowledged_by,acknowledged_at,seconds_to_acknowledge,closed_by,closed_at");
    expect(out[1]).toBe('2026-10-11 02:00:00,P1,dog_sees_person,T:dog_sees_person,A,"East, wall",2,gina,2026-10-11 02:00:41,42,,');
    expect(out[2].split(",").slice(-3)).toEqual(["0", "", ""]);
    expect(out[3]).toBe("2026-10-11 02:00:00,P1,dog_sees_person,T:dog_sees_person,A,,2,,,,,");
    expect(incidentsCsv([{ id: 1, received_at: t0, source: "nvr", type: "intrusion", zone: "front", intercept: null, outcome: "dispatched", robot_id: null, result: null, note: "=x" }]).split("\r\n")[1])
      .toBe("2026-10-11 02:00:00,nvr,intrusion,front,,dispatched,,,'=x");
    expect(patrolsCsv([{ id: 1, entry_id: "night", scheduled_ms: t0, outcome: "started", robot_id: "A", result: null, note: "" }]).split("\r\n")[1])
      .toBe("2026-10-11 02:00:00,night,started,A,,");
  });
  it("处置、结果:认得的翻成人话(中英都有),不认得的原样,没有的是 —", () => {
    for (const k of ["dispatched", "dispatching", "dispatch_failed", "merged", "duplicate", "disarmed", "authorized", "unmapped", "unreachable", "no_robot", "ignored_type", "started", "skip", "done", "failed", "aborted", "preempted", "running"]) {
      expect(DICTS.en[`out_${k}` as never], k).toBeTruthy();
      expect(DICTS.zh[`out_${k}` as never], k).toBeTruthy();
    }
    expect(outcomeText("some_new_outcome")).toBe("some_new_outcome");
    expect(outcomeText(null)).toBe("—");
    expect(outcomeText("")).toBe("—");
    expect(outcomeText("done")).toBe(tr(lang.value, "out_done"));
  });
});

describe("model", () => {
  const robot: Robot = fx("site_robots").robots[0];
  it("夹具里的狗:在线、急停没解除 → 急停", () => {
    expect(robotState(robot, undefined, false)).toBe("estop");
  });
  it("警笛在第 3 级;狗接受了才算开着,级别到了只算要求了;没装的说得出来(C-B2 外审 I1)", () => {
    expect(SIREN_LEVEL).toBe(3);
    const all = ["siren", "speaker", "spotlight", "strobe"];
    expect(sirenState({ robot_id: "A", level: 1, active: ["strobe"], outputs: all })).toBe("off");
    expect(sirenState({ robot_id: "A", level: 2, active: ["speaker", "strobe"], outputs: all })).toBe("off");
    expect(sirenState({ robot_id: "A", level: 3, active: ["strobe"], outputs: all })).toBe("requested");
    expect(sirenState({ robot_id: "A", level: 3, active: ["siren", "strobe"], outputs: all })).toBe("on");
    expect(sirenState({ robot_id: "A", level: 3, active: ["siren"], outputs: ["strobe"] })).toBe("notFitted");
    expect(sirenState({ robot_id: "A", level: 3, active: [], outputs: null })).toBe("requested");
    expect(sirenState({ robot_id: "A", level: 3 })).toBe("requested"); // 老站点不说哪几路开着:不猜
    expect(sirenState({ robot_id: "A", level: 2 })).toBe("off");
    expect(DICTS.en.siren).toContain("level 3");
    expect(DICTS.zh.siren).toContain("3 级");
    expect(DICTS.en.nearestTo).not.toMatch(/ahead/);
    expect(DICTS.zh.nearestTo).not.toContain("前方");
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

describe("防区与名单", () => {
  it("预览像素 ↔ 地图坐标来回一样", () => {
    const meta = { width: 200, height: 100, m_per_px: 0.5, left_x: -10, top_y: 20 };
    const [x, y] = toMap(meta, 20, 40);
    expect([x, y]).toEqual([0, 0]);
    expect(toPx(meta, x, y)).toEqual({ px: 20, py: 40 });
  });
  it("星期写成人话", () => {
    expect(daysText(127)).toBe("Every day");
    expect(daysText(0b0011111)).toBe("Weekdays");
    expect(daysText(0b1100000)).toBe("Weekends");
    expect(daysText(0b0000101)).toBe("Mon, Wed");
  });
});

describe("外审 I5:跟站点断了不能把旧的当现在", () => {
  it("拿不到、或者太久没拿到新的,都算过期", async () => {
    const { isStale, STALE_AFTER_MS } = await import("./data");
    expect(isStale({ okAt: null, failingSince: null }, 1000)).toBe(true);
    expect(isStale({ okAt: 1000, failingSince: null }, 1000 + STALE_AFTER_MS)).toBe(false);
    expect(isStale({ okAt: 1000, failingSince: null }, 1001 + STALE_AFTER_MS)).toBe(true);
    expect(isStale({ okAt: 1000, failingSince: 1500 }, 1600)).toBe(true);
  });
  it("核心接口失败 → 整个失败;驱离接口失败 → 记成说不清(null),不是「没有驱离」", async () => {
    const { fetchSite } = await import("./data");
    const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
    const routes: Record<string, () => Response> = {
      "/api/robots": () => ok({ robots: [] }),
      "/api/alerts": () => ok({ alerts: [] }),
      "/api/mode": () => ok({ mode: "armed" }),
      "/api/deterrence": () => new Response("{}", { status: 502 }),
      "/api/watch/summary": () => ok({ robots: [] }),
    };
    setFetch(async (p) => routes[String(p)]());
    const d = await fetchSite();
    expect(d.deter).toBeNull();
    expect(d.mode).toEqual({ mode: "armed" });
    routes["/api/alerts"] = () => { throw new TypeError("offline"); };
    await expect(fetchSite()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("App V2:手机扫码添加站点的二维码", () => {
  it("格子数是合法的二维码大小,留 4 格静区,三个角上有定位块", async () => {
    const { qrGrid, QUIET } = await import("./qr");
    const g = qrGrid("D1MAXSITE1." + "A".repeat(150));
    expect((g.n - 17) % 4).toBe(0);
    expect(g.size).toBe(g.n + 2 * QUIET);
    // 定位块的最上一行是连着 7 格黑:左上、右上
    expect(g.d.startsWith(`M${QUIET} ${QUIET}h7v1h-7z`)).toBe(true);
    expect(g.d).toContain(`M${QUIET + g.n - 7} ${QUIET}h7v1h-7z`);
    expect(g.d).toContain(`M${QUIET} ${QUIET + g.n - 7}h7v1h-7z`);
  });
  it("内容不同码就不同;同样的内容码一样(能复查)", async () => {
    const { qrGrid } = await import("./qr");
    expect(qrGrid("a").d).toBe(qrGrid("a").d);
    expect(qrGrid("a").d).not.toBe(qrGrid("b").d);
  });
});
