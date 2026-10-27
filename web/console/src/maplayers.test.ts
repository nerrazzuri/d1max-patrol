// W44 地图页的图层规则(设计 §4.3):多久没巡过、今晚的事、本班从几点算、下一个巡检点几分钟到、防区标签。
import { beforeEach, describe, expect, it } from "vitest";
import { lang } from "./i18n";
import { DEFAULT_LAYERS, etaNext, shiftStart, staleAfter, staleCells, STALE_AFTER_MS, tonightEvents, zoneDecor } from "./maplayers";
import type { Alert } from "./model";

beforeEach(() => {
  lang.value = "zh";
});

const alert = (o: Partial<Alert>): Alert => ({
  key: "k", kind: "stuck", level: "P1", robot: "A", title: "", detail: "", first_ms: 0, last_ms: 0, count: 1,
  acked_by: "", acked_ms: null, resolved_ms: null, audience: "help", ...o,
});

describe("图层", () => {
  it("禁行区默认关,别的默认开", () => {
    expect(DEFAULT_LAYERS.has("nogo")).toBe(false);
    for (const l of ["zones", "route", "standby", "events", "stale"] as const) expect(DEFAULT_LAYERS.has(l)).toBe(true);
  });
});

describe("多久没巡过", () => {
  it("超过两小时的格子才算没巡过", () => {
    expect(STALE_AFTER_MS).toBe(2 * 3_600_000);
    const now = 10 * 3_600_000;
    const cells = [{ i: 0, j: 0, t: now - STALE_AFTER_MS - 1 }, { i: 1, j: 0, t: now - STALE_AFTER_MS + 1 }];
    expect(staleCells(cells, now, STALE_AFTER_MS)).toEqual([{ i: 0, j: 0, age_ms: STALE_AFTER_MS + 1 }]);
  });
});

describe("今晚的事", () => {
  it("今晚的事只要给保安的,新的在前", () => {
    const a = [
      alert({ key: "a", first_ms: 100 }),
      alert({ key: "b", first_ms: 300, audience: "intrusion" }),
      alert({ key: "t", first_ms: 200, audience: "owner" }),
      alert({ key: "old", first_ms: 10 }),
    ];
    expect(tonightEvents(a, 50).map((x) => x.key)).toEqual(["b", "a"]);
  });
});

describe("本班从几点算", () => {
  it("交班时间_晚上8点算19点那一班_早上6点算前一天19点那一班_中午算7点那一班", () => {
    const at = (d: number, h: number, m = 0) => new Date(2026, 9, d, h, m).getTime();
    expect(shiftStart(at(27, 20))).toBe(at(27, 19));
    expect(shiftStart(at(28, 6))).toBe(at(27, 19));
    expect(shiftStart(at(27, 12))).toBe(at(27, 7));
    expect(shiftStart(at(27, 7))).toBe(at(27, 7));
    expect(shiftStart(at(27, 6, 59))).toBe(at(26, 19));
  });
});

describe("下一个巡检点几分钟到", () => {
  it("etaNext_沿路径量不按直线", () => {
    expect(etaNext([[0, 0], [0, 10], [10, 10]], [0, 0])).toBe(40);
  });
  it("机器人还没走到路径起点:先算到起点那一段", () => {
    expect(etaNext([[0, 0], [0, 10]], [0, -2])).toBe(24);
  });
  it("没有路径就不说", () => {
    expect(etaNext(null, [0, 0])).toBeNull();
    expect(etaNext([], [0, 0])).toBeNull();
    expect(etaNext([[0, 0], [1, 1]], null)).toBeNull();
  });
});

describe("防区的标签", () => {
  const authz = [{ name: "园丁", zones: ["后花园"], end: "18:00", active_now: true }, { name: "保洁", zones: ["主楼"], end: "11:00", active_now: false }];
  it("布防的写布防、带正在生效的授权", () => {
    expect(zoneDecor("后花园", { mode: "armed", until_ms: null, zones: [{ zone: "后花园", armed: true }] }, authz))
      .toEqual({ label: "后花园 · 布防 · 园丁授权到 18:00", tone: "armed" });
  });
  it("没生效的授权不写", () => {
    expect(zoneDecor("主楼", { mode: "armed", until_ms: null, zones: [{ zone: "主楼", armed: true }] }, authz).label).toBe("主楼 · 布防");
  });
  it("访客模式撤掉的写到几点", () => {
    const until = new Date(2026, 9, 27, 23, 30).getTime();
    expect(zoneDecor("车库", { mode: "visitor", until_ms: until, zones: [{ zone: "车库", armed: false }] }, []))
      .toEqual({ label: "车库 · 访客模式（到 23:30）", tone: "off" });
  });
  it("在家撤掉的写撤防", () => {
    expect(zoneDecor("院子", { mode: "home", until_ms: null, zones: [{ zone: "院子", armed: false }] }, []))
      .toEqual({ label: "院子 · 撤防", tone: "off" });
  });
  it("模式里没有这个防区:没配过的一律布防(跟站点一样)", () => {
    expect(zoneDecor("新防区", { mode: "armed", until_ms: null, zones: [] }, []).tone).toBe("armed");
  });
});

describe("多久没巡过按站点设置", () => {
  it("小时换成毫秒,拿不到用默认", () => {
    expect(staleAfter(3)).toBe(3 * 3_600_000);
    expect(staleAfter(undefined)).toBe(STALE_AFTER_MS);
  });
});
