// @vitest-environment happy-dom
// W44 第四期 高级 → 定位:三种颜色(加离线的灰)、帮它找位置进选点、详细默认收着。
import { cleanup, fireEvent, render } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { LocPage, locLevel } from "./advloc";
import { lang } from "./i18n";
import type { UnitInfo } from "./live";
import type { Alert, Robot } from "./model";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const robot = (o: Partial<Robot>): Robot => ({ robot_id: "A", active: true, fresh: true, revoked: false, held: null,
  status: { online: true, last_seen: 0 }, loc: { anchored: true, source: "map", sigma_m: 0.05, pose: { x: 1, y: 1, yaw: 0 } }, ...o } as Robot);
const alert = (o: Partial<Alert>): Alert => ({ key: "k", kind: "loc_lost_paused", level: "P1", robot: "A", title: "", detail: "", first_ms: 1, last_ms: 1,
  count: 1, acked_by: "", acked_ms: null, resolved_ms: null, ...o });

beforeEach(() => {
  lang.value = "zh";
  vi.stubGlobal("IntersectionObserver", undefined);
  setFetch((() => Promise.resolve(json({}, 404))) as typeof fetch);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("定位的颜色", () => {
  it("绿、黄、红、灰", () => {
    expect(locLevel(robot({}), [])).toBe("ok");
    expect(locLevel(robot({ loc: { anchored: true, sigma_m: 0.8 } }), [])).toBe("unsure");
    expect(locLevel(robot({}), [alert({ kind: "loc_degraded" })])).toBe("unsure");
    expect(locLevel(robot({}), [alert({})])).toBe("lost");
    expect(locLevel(robot({ loc: { anchored: false } }), [])).toBe("lost");
    expect(locLevel(robot({ fresh: false }), [])).toBe("unknown");
    expect(locLevel(robot({}), [alert({ resolved_ms: 2 })])).toBe("ok");
  });
});

describe("定位页", () => {
  const units: UnitInfo[] = [{ robot: robot({}), state: "idle", battery: 50, task: "" },
    { robot: robot({ robot_id: "B", fresh: false }), state: "offline", battery: null, task: "" }];
  it("每台一行写人话,离线的不给找位置", () => {
    const { container: c } = render(<LocPage units={units} alerts={[alert({})]} now={0} role="admin" />);
    expect(c.querySelector('.loc-row[data-robot="A"]')!.textContent).toContain("迷路了");
    expect(c.querySelector('.loc-row[data-robot="B"] button')).toBeNull();
    expect(c.querySelector('.loc-row[data-robot="B"]')!.textContent).toContain("不知道");
  });
  it("帮它找位置进选点,详细默认收着", () => {
    const { container: c } = render(<LocPage units={units} alerts={[]} now={0} role="admin" />);
    fireEvent.click(c.querySelector('.loc-row[data-robot="A"] button')!);
    expect(c.textContent).toContain("在地图上");
    expect((c.querySelector("details.loc-detail") as HTMLDetailsElement).open).toBe(false);
  });
});
