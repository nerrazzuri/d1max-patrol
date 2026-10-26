// @vitest-environment happy-dom
// 地图页「去这里 / 在这里设位置」跟底图、选中的狗绑在同一张图上(外审 W39-I2),只有狗收下了才说成功(W39-I3)。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, expect, it } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { MapPage } from "./mappage";
import { notAccepted, usePosePick } from "./posepick";

const meta = { width: 400, height: 300, m_per_px: 0.05, left_x: -10, top_y: 8 };
const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
const MA = { map_id: "map-A", version: "1" };
const MB = { map_id: "map-B", version: "1" };

beforeEach(() => { lang.value = "en"; });
afterEach(cleanup);

const unit = (id: string, map: string | null) => ({
  robot: {
    robot_id: id, active: true, fresh: true, revoked: false, held: null, status: { online: true, last_seen: 0 },
    loc: { pose: { x: 1, y: 1, yaw: 0 } },
    capabilities: { tasks: map ? { patrol: { map_id: map, map_version: "1" } } : {} },
  },
  state: "idle", battery: 80, task: "",
});

function mapFetch(log?: string[]) {
  setFetch((async (url: unknown) => {
    const u = String(url);
    log?.push(u);
    if (u.endsWith("/preview")) return json(meta);
    if (u.endsWith("/areas")) return json({ areas: [] });
    if (u.includes("/runviz")) return json({ robots: {} });
    return json({ episodes: [] });
  }) as never);
}

type Ctx = { robot: string; map: typeof MA; robotMap?: typeof MA | null };

function Harness(p: Ctx) {
  const pick = usePosePick({ meta, map: p.map, robotId: p.robot, robotMap: p.robotMap === undefined ? p.map : p.robotMap, role: "admin" });
  return <><button onClick={() => pick.onPick?.(300, 100)}>pick</button>{pick.panel}</>;
}

async function pickAndConfirmSet(r: ReturnType<typeof render>) {
  fireEvent.click(r.getByText("pick"));
  await waitFor(() => expect(r.container.querySelector("[data-pose-text]")).not.toBeNull());
  fireEvent.click(r.getByText("Set position here"));
  await waitFor(() => expect(document.querySelector("dialog")).not.toBeNull());
  fireEvent.click([...document.querySelectorAll("dialog button")].find((b) => b.textContent === "Set position here")!);
}

it("选中 B(在 map-B 上):底图是 map-B,只画 B,不画 map-A 上的 A", async () => {
  mapFetch();
  const r = render(<MapPage units={[unit("A", "map-A"), unit("B", "map-B")] as never} p1={[]} now={0}
    selected="B" role="admin" onSelect={() => {}} onVideo={() => {}} />);
  await waitFor(() => expect(r.container.querySelector("g.map-world")).not.toBeNull());
  expect(r.container.querySelector("image")?.getAttribute("href")).toContain("/map-B/1/");
  const bots = [...r.container.querySelectorAll("g.bot")].map((g) => g.textContent);
  expect(bots).toEqual(["B"]);
});

it("选中的狗没报图:底图退回第一只有图的狗的,没报图的狗不画在上面", async () => {
  mapFetch();
  const r = render(<MapPage units={[unit("A", "map-A"), unit("C", null)] as never} p1={[]} now={0}
    selected="C" role="admin" onSelect={() => {}} onVideo={() => {}} />);
  await waitFor(() => expect(r.container.querySelector("g.map-world")).not.toBeNull());
  expect(r.container.querySelector("image")?.getAttribute("href")).toContain("/map-A/1/");
  expect([...r.container.querySelectorAll("g.bot")].map((g) => g.textContent)).toEqual(["A"]);
});

it("换图以后才回来的旧预览不认", async () => {
  const pending: Array<() => void> = [];
  setFetch((async (url: unknown) => {
    const u = String(url);
    if (u.includes("/map-A/") && u.endsWith("/preview")) {
      await new Promise<void>((res) => pending.push(res));        // map-A 的预览晚到
      return json({ ...meta, width: 999 });
    }
    if (u.endsWith("/preview")) return json(meta);
    if (u.endsWith("/areas")) return json({ areas: [] });
    if (u.includes("/runviz")) return json({ robots: {} });
    return json({ episodes: [] });
  }) as never);
  const units = [unit("A", "map-A"), unit("B", "map-B")] as never;
  const r = render(<MapPage units={units} p1={[]} now={0} selected="A" role="admin" onSelect={() => {}} onVideo={() => {}} />);
  await waitFor(() => expect(pending.length).toBe(1));
  r.rerender(<MapPage units={units} p1={[]} now={0} selected="B" role="admin" onSelect={() => {}} onVideo={() => {}} />);
  await waitFor(() => expect(r.container.querySelector("image")?.getAttribute("href")).toContain("/map-B/1/"));
  pending[0]();
  await new Promise((res) => setTimeout(res, 20));
  expect(r.container.querySelector("image")?.getAttribute("href")).toContain("/map-B/1/");
  expect(r.container.querySelector("svg")?.innerHTML).not.toContain("999");
});

it("在 map-A 给 A 选的点,换成 map-B / B 以后作废,发不出去", async () => {
  const posted: unknown[] = [];
  setFetch((async (url: unknown, init: { body: string }) => { posted.push([String(url), JSON.parse(init.body)]); return json({ ack: { result: "accepted" } }); }) as never);
  const r = render(<Harness robot="A" map={MA} />);
  fireEvent.click(r.getByText("pick"));
  await waitFor(() => expect(r.container.querySelector("[data-pose-text]")?.textContent).toContain("x 5 m, y 3 m"));
  r.rerender(<Harness robot="B" map={MB} />);
  expect(r.container.querySelector("[data-pose-text]")).toBeNull();
  expect(r.queryByText("Set position here")).toBeNull();
  expect(posted).toEqual([]);
});

it("确认框开着时换了狗:框跟着作废", async () => {
  setFetch((async () => json({ ack: { result: "accepted" } })) as never);
  const r = render(<Harness robot="A" map={MA} />);
  fireEvent.click(r.getByText("pick"));
  await waitFor(() => expect(r.container.querySelector("[data-pose-text]")).not.toBeNull());
  fireEvent.click(r.getByText("Set position here"));
  await waitFor(() => expect(document.querySelector("dialog")).not.toBeNull());
  r.rerender(<Harness robot="B" map={MA} />);
  expect(document.querySelector("dialog")).toBeNull();
});

it("目标狗加载的不是底图那张:按钮灰、写明原因", async () => {
  setFetch((async () => json({ ack: { result: "accepted" } })) as never);
  const r = render(<Harness robot="B" map={MA} robotMap={MB} />);
  fireEvent.click(r.getByText("pick"));
  await waitFor(() => expect(r.container.querySelector("[data-pose-wrongmap]")).not.toBeNull());
  expect((r.getByText("Set position here") as HTMLButtonElement).disabled).toBe(true);
  expect((r.getByText("Go here") as HTMLButtonElement).disabled).toBe(true);
});

it("设位置带上底图的图号、版本(站点核对)", async () => {
  const posted: unknown[] = [];
  setFetch((async (url: unknown, init: { body: string }) => { posted.push([String(url), JSON.parse(init.body)]); return json({ command_id: "c1", ack: { command_id: "c1", result: "accepted" } }); }) as never);
  const r = render(<Harness robot="A" map={MA} />);
  await pickAndConfirmSet(r);
  await waitFor(() => expect(posted.length).toBe(1));
  expect(posted[0]).toEqual(["/api/robots/A/relocalize", { x: 5, y: 3, yaw: 0, map_id: "map-A", map_version: "1" }]);
  await waitFor(() => expect(r.container.querySelector("[data-pose-msg]")?.textContent).toBe("Position set for A"));
});

it("狗拒了(HTTP 200 里 rejected):不说已设,写原因", async () => {
  setFetch((async () => json({ command_id: "c1", ack: { command_id: "c1", result: "rejected", reason: "moving" } })) as never);
  const r = render(<Harness robot="A" map={MA} />);
  await pickAndConfirmSet(r);
  await waitFor(() => expect(r.container.querySelector("[data-pose-msg]")).not.toBeNull());
  const text = r.container.querySelector("[data-pose-msg]")?.textContent ?? "";
  expect(text).not.toContain("Position set");
  expect(text).toContain("rejected: moving");
});

it("去这里被拒(rejected)也不说已让去", async () => {
  setFetch((async () => json({ ack: { result: "rejected", reason: "zone" } })) as never);
  const r = render(<Harness robot="A" map={MA} />);
  fireEvent.click(r.getByText("pick"));
  await waitFor(() => expect(r.container.querySelector("[data-pose-text]")).not.toBeNull());
  fireEvent.click(r.getByText("Go here"));
  await waitFor(() => expect(r.container.querySelector("[data-pose-msg]")).not.toBeNull());
  expect(r.container.querySelector("[data-pose-msg]")?.textContent).not.toContain("Go-to sent");
});

it("回包回来时已经换了狗:不把结果说成新狗的", async () => {
  let reply: (v: Response) => void = () => {};
  setFetch((() => new Promise<Response>((res) => { reply = res; })) as never);
  const r = render(<Harness robot="A" map={MA} />);
  await pickAndConfirmSet(r);
  r.rerender(<Harness robot="B" map={MA} />);
  reply(json({ ack: { result: "accepted" } }));
  await new Promise((res) => setTimeout(res, 20));
  expect(r.container.querySelector("[data-pose-msg]")).toBeNull();
  expect(r.container.textContent).not.toContain("Position set for");
});

it("回执判定:只有 accepted 且命令号对得上才算", () => {
  expect(notAccepted({ ack: { result: "accepted" } })).toBeNull();
  expect(notAccepted({ command_id: "a", ack: { command_id: "a", result: "accepted" } })).toBeNull();
  expect(notAccepted({ command_id: "a", ack: { command_id: "b", result: "accepted" } })).not.toBeNull();
  for (const result of ["rejected", "expired", "duplicate"]) expect(notAccepted({ ack: { result } })).toContain(result);
  expect(notAccepted({})).not.toBeNull();
  expect(notAccepted(null)).not.toBeNull();
});

it("W41 自动找位置:狗报了能力才有按钮;确认后发 global、带底图;狗收下才说找到了", async () => {
  const sent: { url: string; body: unknown }[] = [];
  setFetch((async (url: unknown, init?: RequestInit) => {
    sent.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : null });
    return json({ command_id: "c1", ack: { command_id: "c1", result: "accepted", reason: "" } });
  }) as never);
  function H(p: { canFind: boolean }) {
    const pick = usePosePick({ meta, map: MA, robotId: "A", robotMap: MA, role: "admin", canFind: p.canFind });
    return <>{pick.panel}</>;
  }
  const no = render(<H canFind={false} />);
  expect(no.container.querySelector("[data-pose-find]")).toBeNull();
  cleanup();
  const r = render(<H canFind />);
  fireEvent.click(r.container.querySelector("[data-pose-find]")!);
  await waitFor(() => expect(document.querySelector("dialog")).not.toBeNull());
  expect(sent).toEqual([]);
  fireEvent.click([...document.querySelectorAll("dialog button")].find((b) => b.textContent === "Find position automatically")!);
  await waitFor(() => expect(r.container.querySelector("[data-pose-msg]")?.textContent).toContain("found its position"));
  expect(sent[0].url).toBe("/api/robots/A/relocalize");
  expect(sent[0].body).toEqual({ global: true, map_id: "map-A", map_version: "1" });
});

it("W41 自动找位置:狗认不出就把原因给人看", async () => {
  setFetch((async () => json({ command_id: "c1", ack: { command_id: "c1", result: "rejected", reason: "not_found: 有两处差不多像" } })) as never);
  function H() {
    const pick = usePosePick({ meta, map: MA, robotId: "A", robotMap: MA, role: "admin", canFind: true });
    return <>{pick.panel}</>;
  }
  const r = render(<H />);
  fireEvent.click(r.container.querySelector("[data-pose-find]")!);
  await waitFor(() => expect(document.querySelector("dialog")).not.toBeNull());
  fireEvent.click([...document.querySelectorAll("dialog button")].find((b) => b.textContent === "Find position automatically")!);
  await waitFor(() => expect(r.container.querySelector("[data-pose-msg]")?.textContent).toContain("差不多像"));
});
