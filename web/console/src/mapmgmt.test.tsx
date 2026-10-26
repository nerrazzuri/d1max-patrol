// @vitest-environment happy-dom
// 地图管理页:真的把组件渲染出来点。后端是假的(按路径回数据)。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { activationText, MapManagePage } from "./mapmgmt";
import type { Activation, Dog, Overview, Version } from "./mapmgmt";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
function held() {
  let go!: (r: Response) => void;
  const p = new Promise<Response>((res) => (go = res));
  return { p, go };
}

const ver = (v: string, over: Partial<Version> = {}): Version => ({
  version: v, created_ms: Date.UTC(2026, 9, 13, 8, 0), source: "dog-A", note: "", size_bytes: 12_500_000, latest: false,
  build: null, counts: { zones: 0, nogo: 0, slow: 0, zones_rev: 0, zones_confirmed: true, standby: 0, intercepts: 0 },
  loaded_on: [], previous_on: [], thumbnail: `/api/maps/estate/${v}/preview.png`, has_raster: true, ...over,
});
const dog = (id: string, over: Partial<Dog> = {}): Dog => ({
  robot_id: id, online: true, can_activate: true, loaded: { map_id: "estate", version: "2" },
  running: { legacy: false, problems: [], map: { dog: "a", site: "a", match: true }, zones: { dog_rev: 1, site_rev: 1, match: true, basis: "digest" } },
  activation: null, history: [], rollback: null, ...over,
});
const act = (state: Activation["state"], over: Partial<Activation> = {}): Activation => ({
  map_id: "estate", version: "2", state, at_ms: Date.UTC(2026, 9, 13, 9, 0), by: "alice", reason: "", confirmed: null, ...over,
});

const OVERVIEW = (): Overview => ({
  now_ms: 0,
  maps: [{
    map_id: "estate", versions: [
      ver("3", { latest: true, note: "after loop closure", build: {
        source: "bag-1013", builder: "mola-lidar-odometry", frames: 812,
        loop_closure: { used: true, closures: 3, why: null }, handedness: { verdict: "ok", why: null } },
      counts: { zones: 3, nogo: 2, slow: 1, zones_rev: 4, zones_confirmed: true, standby: 2, intercepts: 1 } }),
      ver("2", { loaded_on: ["A"], previous_on: ["B"] }),
      ver("1", { build: null }),
    ],
  }],
  robots: [
    dog("A", { activation: act("confirmed", { confirmed: "localizer" }), rollback: { map_id: "estate", version: "1", local: true } }),
    dog("B", { loaded: { map_id: "estate", version: "1" }, online: true }),
    dog("C", { online: false, can_activate: true, loaded: null, running: null }),
  ],
});

interface Calls { method: string; path: string; body: unknown }
let calls: Calls[];
let overview: Overview;
let routes: Record<string, (body: unknown, url: URL) => Response | Promise<Response>>;
const say = vi.fn();

function mount() {
  calls = [];
  setFetch(((url: string, init?: RequestInit) => {
    const u = new URL(url, "https://site");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ method, path: u.pathname + u.search, body });
    const key = `${method} ${u.pathname}`;
    if (routes[key]) return Promise.resolve(routes[key](body, u));
    if (key === "GET /api/admin/maps") return Promise.resolve(json(overview));
    return Promise.resolve(json({ error: `no route ${key}` }, 404));
  }) as never);
  return render(<MapManagePage say={say} pollMs={0} />);
}

beforeEach(() => {
  lang.value = "en";
  overview = OVERVIEW();
  routes = {};
  say.mockReset();
});
afterEach(() => cleanup());

describe("总览", () => {
  it("每个版本:备注、构建信息、数量、哪几只狗在用;最新一版标出来", async () => {
    const { container: c, getByLabelText } = mount();
    await waitFor(() => expect(c.textContent).toContain("v3"));
    const v3 = getByLabelText("estate:3");
    expect(v3.textContent).toContain("latest");
    expect(v3.textContent).toContain("bag-1013");
    expect(v3.textContent).toContain("used (3)");
    expect(v3.textContent).toContain("Handedness check: ok");
    expect(v3.textContent).toContain("Zones 3 (2 no-go, 1 slow)");
    expect(v3.textContent).toContain("Standby points 2");
    expect((getByLabelText("Note for v3") as HTMLInputElement).value).toBe("after loop closure");
    expect(getByLabelText("estate:2").textContent).toContain("Loaded on");
    expect(getByLabelText("estate:2").textContent).toContain("Rollback target of");
    expect(getByLabelText("estate:1").textContent).toContain("No build record");
    expect((getByLabelText("estate:3").querySelector("img") as HTMLImageElement).getAttribute("src")).toBe("/api/maps/estate/3/preview.png");
  });

  it("镜像的版本用红字警告", async () => {
    overview.maps[0].versions[0].build!.handedness = { verdict: "mirrored", why: null };
    const { container: c } = mount();
    await waitFor(() => expect(c.textContent).toContain("MIRRORED"));
  });

  it("每只狗:加载的版本、最近一次下发的状态、实际配置核对;中文", async () => {
    lang.value = "zh";
    const { container: c } = mount();
    await waitFor(() => expect(c.textContent).toContain("定位器已确认"));
    expect(c.textContent).toContain("estate:2");
    expect(c.textContent).toContain("不在线");
  });
});

describe("备注", () => {
  it("改了才能存;存的时候带整段文字,存完重新拉", async () => {
    routes["POST /api/admin/maps/estate/2/note"] = () => json({ ok: true, note: "kept" });
    const { getByLabelText, getAllByText } = mount();
    await waitFor(() => getByLabelText("Note for v2"));
    const save = () => getAllByText("Save note")[1] as HTMLButtonElement;     // 第二张卡片是 v2
    expect(save().disabled).toBe(true);
    fireEvent.input(getByLabelText("Note for v2"), { target: { value: "after the storm" } });
    await waitFor(() => expect((getByLabelText("Note for v2") as HTMLInputElement).value).toBe("after the storm"));
    await waitFor(() => expect(save().disabled).toBe(false));
    fireEvent.click(save());
    await waitFor(() => expect(say).toHaveBeenCalledWith("Saved"));
    expect(calls.find((x) => x.method === "POST")?.body).toEqual({ note: "after the storm" });
    expect(calls.filter((x) => x.path === "/api/admin/maps").length).toBe(2);
  });
});

describe("发布", () => {
  it("只列在线又能换图的狗;选了才能发;结果一只一行(成功和拒绝分开)", async () => {
    routes["POST /api/admin/maps/estate/3/publish"] = (b) => json({ results: [
      { robot_id: "A", ok: true, state: "pending", reason: "" },
      { robot_id: "B", ok: false, state: "refused", reason: "estate:3 的禁行区还没确认", code: "zones_unconfirmed" },
    ], b });
    const { container: c, getByText, getByLabelText } = mount();
    await waitFor(() => getByLabelText("estate:3"));
    fireEvent.click(getByLabelText("estate:3").querySelector(".btn.primary") as HTMLElement);
    const dialog = await waitFor(() => {
      const d = c.querySelector("dialog");
      expect(d).not.toBeNull();
      return d as HTMLDialogElement;
    });
    const boxes = Array.from(dialog.querySelectorAll("fieldset input[type=checkbox]")) as HTMLInputElement[];
    expect(boxes.map((b) => b.disabled)).toEqual([false, false, true]);          // C 不在线
    const publish = Array.from(dialog.querySelectorAll("button")).find((b) => b.textContent === "Publish") as HTMLButtonElement;
    expect(publish.disabled).toBe(true);
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);
    fireEvent.click(dialog.querySelector("label.checks input[type=checkbox]") as HTMLElement);
    fireEvent.click(publish);
    await waitFor(() => expect(dialog.querySelectorAll(".mm-results li").length).toBe(2));
    expect(calls.find((x) => x.method === "POST")?.body).toEqual({ robots: ["A", "B"], without_home: true });
    const items = Array.from(dialog.querySelectorAll(".mm-results li"));
    expect(items[0].className).toBe("ok");
    expect(items[0].textContent).toContain("accepted");
    expect(items[1].className).toBe("bad");
    expect(items[1].textContent).toContain("禁行区还没确认");
    void getByText;
  });

  it("发布整个请求失败:对话框里显示错误,还能再试", async () => {
    routes["POST /api/admin/maps/estate/3/publish"] = () => json({ error: "boom" }, 500);
    const { container: c, getByLabelText } = mount();
    await waitFor(() => getByLabelText("estate:3"));
    fireEvent.click(getByLabelText("estate:3").querySelector(".btn.primary") as HTMLElement);
    const dialog = await waitFor(() => c.querySelector("dialog") as HTMLDialogElement);
    fireEvent.click(dialog.querySelector("fieldset input[type=checkbox]") as HTMLElement);
    fireEvent.click(Array.from(dialog.querySelectorAll("button")).find((b) => b.textContent === "Publish") as HTMLElement);
    await waitFor(() => expect(dialog.textContent).toContain("boom"));
  });
});

describe("回退", () => {
  it("按钮上写着退到哪一版;确认文字说明狗本地留着不用下载;点了发 rollback", async () => {
    routes["POST /api/admin/maps/rollback"] = () => json({ result: { robot_id: "A", ok: true, state: "pending", reason: "" } });
    const { container: c, getAllByText } = mount();
    await waitFor(() => getAllByText(/Roll back/));
    const buttons = Array.from(c.querySelectorAll(".mm-dogs tbody tr")).map((tr) => tr.querySelector("button") as HTMLButtonElement);
    expect(buttons.map((b) => b.disabled)).toEqual([false, true, true]);
    expect(buttons[0].textContent).toBe("Roll back → 1");
    fireEvent.click(buttons[0]);
    const dialog = await waitFor(() => c.querySelector("dialog") as HTMLDialogElement);
    expect(dialog.textContent).toContain("nothing is downloaded");
    fireEvent.click(Array.from(dialog.querySelectorAll("button")).find((b) => b.textContent === "Roll back") as HTMLElement);
    await waitFor(() => expect(say).toHaveBeenCalledWith("A: rolling back…"));
    expect(calls.find((x) => x.path === "/api/admin/maps/rollback")?.body).toEqual({ robot_id: "A", without_home: true });
  });

  it("狗上没留的上一版:确认文字说会重新下载", async () => {
    overview.robots[0].rollback = { map_id: "estate", version: "1", local: false };
    const { container: c, getAllByText } = mount();
    await waitFor(() => getAllByText(/Roll back/));
    fireEvent.click(c.querySelector(".mm-dogs tbody tr button") as HTMLElement);
    const dialog = await waitFor(() => c.querySelector("dialog") as HTMLDialogElement);
    expect(dialog.textContent).toContain("download it again");
  });
});

describe("删除", () => {
  it("有狗在用、最新一版:按钮灰着带原因;旧版本:确认后删,提示腾出多少", async () => {
    routes["POST /api/admin/maps/estate/1/delete"] = () => json({ deleted: "estate:1", freed_bytes: 12_500_000 });
    const { container: c, getByLabelText } = mount();
    await waitFor(() => getByLabelText("estate:3"));
    const del = (v: string) => Array.from(getByLabelText(`estate:${v}`).querySelectorAll("button")).find((b) => b.textContent === "Delete") as HTMLButtonElement;
    expect(del("3").disabled).toBe(true);
    expect(del("3").title).toContain("Latest version");
    expect(del("2").disabled).toBe(true);
    expect(del("2").title).toContain("A dog is using");
    expect(del("1").disabled).toBe(false);
    fireEvent.click(del("1"));
    const dialog = await waitFor(() => c.querySelector("dialog") as HTMLDialogElement);
    expect(dialog.textContent).toContain("cannot be undone");
    fireEvent.click(Array.from(dialog.querySelectorAll("button")).find((b) => b.textContent === "Delete") as HTMLElement);
    await waitFor(() => expect(say).toHaveBeenCalledWith("Deleted estate:1 (freed 13 MB)"));
  });

  it("站点拒了(409):把原因显示出来", async () => {
    routes["POST /api/admin/maps/estate/1/delete"] = () => json({ error: "estate:1 还有狗在用(A)" }, 409);
    const { container: c, getByLabelText } = mount();
    await waitFor(() => getByLabelText("estate:1"));
    fireEvent.click(Array.from(getByLabelText("estate:1").querySelectorAll("button")).find((b) => b.textContent === "Delete") as HTMLElement);
    const dialog = await waitFor(() => c.querySelector("dialog") as HTMLDialogElement);
    fireEvent.click(Array.from(dialog.querySelectorAll("button")).find((b) => b.textContent === "Delete") as HTMLElement);
    await waitFor(() => expect(say).toHaveBeenCalledWith("Failed: estate:1 还有狗在用(A)"));
  });
});

describe("对比", () => {
  const pickTwo = async (getByLabelText: (s: string) => HTMLElement, a = "3", b = "1") => {
    await waitFor(() => getByLabelText(`estate:${a}`));
    for (const v of [a, b]) fireEvent.click(getByLabelText(`estate:${v}`).querySelector("input[type=checkbox]") as HTMLElement);
  };
  const STATS = {
    a: "3", b: "1", width: 100, height: 80, caveat: "Separately built versions may not share a coordinate frame.", likely_misaligned: false,
    alignment: 0.93, frames_json_same: true, same_resolution: true,
    area_m2: { same_wall: 10, same_free: 90, wall_only_a: 1.5, wall_only_b: 0.5, free_only_a: 2, free_only_b: 3 },
    colors: { same_wall: "#464646", same_free: "#e1e1e1", wall_only_a: "#d62728", wall_only_b: "#1f77b4", free_only_a: "#ffa03c", free_only_b: "#3cbebe" },
  };

  it("勾两个才出对比按钮;打开后先看到提醒,再有图和图例", async () => {
    routes["GET /api/admin/maps/estate/3/diff"] = () => json(STATS);
    const { container: c, getByLabelText, queryByText, getByText } = mount();
    await pickTwo(getByLabelText as never);
    expect(queryByText("Compare the two ticked")).not.toBeNull();
    fireEvent.click(getByText("Compare the two ticked"));
    await waitFor(() => expect(c.querySelector(".mm-caveat")).not.toBeNull());
    expect(c.querySelector(".mm-caveat")!.textContent).toContain("may not share a coordinate frame");
    expect(c.querySelector(".mm-misaligned")).toBeNull();
    expect((c.querySelector(".mm-diffimg") as HTMLImageElement).getAttribute("src")).toBe("/api/admin/maps/estate/3/diff.png?with=1");
    expect(c.querySelectorAll(".mm-legend li").length).toBe(6);
    expect(c.querySelector(".mm-legend")!.textContent).toContain("Wall only in A");
    expect(calls.find((x) => x.path.includes("/diff?"))?.path).toBe("/api/admin/maps/estate/3/diff?with=1");
  });

  it("坐标系多半对不上:红框警告,说图没意义", async () => {
    routes["GET /api/admin/maps/estate/3/diff"] = () => json({ ...STATS, likely_misaligned: true, alignment: 0.04, frames_json_same: false });
    const { container: c, getByLabelText, getByText } = mount();
    await pickTwo(getByLabelText as never);
    fireEvent.click(getByText("Compare the two ticked"));
    await waitFor(() => expect(c.querySelector(".mm-misaligned")).not.toBeNull());
    expect(c.querySelector(".mm-misaligned")!.textContent).toContain("4%");
    expect(c.querySelector(".mm-misaligned")!.textContent).toContain("NOT in the same coordinate frame");
    expect(c.textContent).toContain("different frames.json");
  });

  it("只能在同一张图里勾两个;第三个顶掉最早勾的", async () => {
    routes["GET /api/admin/maps/estate/2/diff"] = () => json(STATS);
    const { getByLabelText, queryByText } = mount();
    await pickTwo(getByLabelText as never);
    const box = (v: string) => getByLabelText(`estate:${v}`).querySelector("input[type=checkbox]") as HTMLInputElement;
    fireEvent.click(box("2"));
    expect(box("3").checked).toBe(false);          // 3 被顶掉
    expect(box("1").checked && box("2").checked).toBe(true);
    expect(queryByText("Compare the two ticked")).not.toBeNull();
  });

  it("两次对比的回包乱序到:只认最后一次", async () => {
    const first = held();
    const second = held();
    let n = 0;
    routes["GET /api/admin/maps/estate/3/diff"] = () => { n += 1; return (n === 1 ? first : second).p as never; };
    routes["GET /api/admin/maps/estate/1/diff"] = () => second.p as never;
    const { container: c, getByLabelText, getByText } = mount();
    await pickTwo(getByLabelText as never, "3", "1");
    fireEvent.click(getByText("Compare the two ticked"));            // 3 vs 1(回包扣着)
    await waitFor(() => expect(n).toBe(1));
    fireEvent.click(Array.from(c.querySelectorAll("dialog .btn")).find((b) => b.textContent === "Close") as HTMLElement);
    fireEvent.click(getByLabelText("estate:3").querySelector("input[type=checkbox]") as HTMLElement);
    fireEvent.click(getByLabelText("estate:2").querySelector("input[type=checkbox]") as HTMLElement);
    fireEvent.click(getByText("Compare the two ticked"));            // 1 vs 2
    second.go(json({ ...STATS, a: "2", caveat: "SECOND" }));
    await waitFor(() => expect(c.querySelector(".mm-caveat")?.textContent).toBe("SECOND"));
    first.go(json({ ...STATS, caveat: "FIRST" }));
    await new Promise((r) => setTimeout(r, 20));
    expect(c.querySelector(".mm-caveat")?.textContent).toBe("SECOND");
  });
});

describe("换图状态和运行配置的文字", () => {
  it("各个状态的说法;老代理的成功标出没等定位器", () => {
    expect(activationText(act("pending"))).toBe("Waiting for the dog");
    expect(activationText(act("confirmed", { confirmed: "localizer" }))).toBe("Confirmed by the localizer");
    expect(activationText(act("confirmed", { confirmed: "load" }))).toContain("no localizer");
    expect(activationText(act("confirmed", { confirmed: "legacy" }))).toContain("did not wait for the localizer");
    expect(activationText(act("failed", { reason: "rejected the prior (rolled back)" }))).toBe("Failed: rejected the prior (rolled back)");
    expect(activationText(act("timeout"))).toContain("treat as not done");
  });

  it("运行配置对不上:红字和原因;老代理标「只比修订号」", async () => {
    overview.robots[1].running = {
      legacy: false, problems: ["estate:1 content differs from the site"],
      map: { dog: "a", site: "b", match: false }, zones: { dog_rev: 2, site_rev: 2, match: true, basis: "revision" },
      mission: { mission: "night", rev: "abc123" } };
    overview.robots[0].running = { legacy: true, problems: [], map: null, zones: null };
    const { container: c } = mount();
    await waitFor(() => expect(c.textContent).toContain("content differs"));
    const rows = Array.from(c.querySelectorAll(".mm-dogs tbody tr"));
    expect(rows[0].textContent).toContain("Old agent: only the zone revision is compared");
    expect(rows[1].textContent).toContain("Map ✗");
    expect(rows[1].textContent).toContain("old agent: revision only");
    expect(rows[1].textContent).toContain("night · abc123");
  });
});

describe("W41 清噪点", () => {
  it("是退回目标的版本:删除按钮灰着带原因", async () => {
    overview.maps[0].versions[2] = ver("1", { previous_on: ["A"] });
    const { getByLabelText } = mount();
    await waitFor(() => getByLabelText("estate:1"));
    const del = Array.from(getByLabelText("estate:1").querySelectorAll("button")).find((b) => b.textContent === "Delete") as HTMLButtonElement;
    expect(del.disabled).toBe(true);
    expect(del.title).toContain("roll back");
  });

  it("红圈画在图上;点一个留着它;清掉其余的、另存新版,带上看过的那份摘要", async () => {
    routes["GET /api/admin/maps/estate/1/noise"] = () => json({ digest: "d1", res: 0.05, max_m2: 0.05, ring_m: 0.2,
      specks: [{ id: 1, x: 1.0, y: 2.0, px: 3 }, { id: 2, x: -1.0, y: 0.5, px: 1 }, { id: 3, x: 2.0, y: -1.0, px: 2 }] });
    routes["GET /api/maps/estate/1/preview"] = () => json({ width: 400, height: 300, m_per_px: 0.05, left_x: -10, top_y: 8 });
    routes["POST /api/admin/maps/estate/1/denoise"] = () => json({ version: "4", cleaned: 2, cells: 5, homes_copied: 1 });
    const { container: c, getByLabelText } = mount();
    await waitFor(() => getByLabelText("estate:1"));
    fireEvent.click(Array.from(getByLabelText("estate:1").querySelectorAll("button")).find((b) => b.textContent === "Clean noise…") as HTMLElement);
    await waitFor(() => expect(c.querySelectorAll("circle[data-speck]").length).toBe(3));
    fireEvent.click(c.querySelector('circle[data-speck="2"]') as Element);
    expect(c.querySelector('circle[data-speck="2"]')!.getAttribute("data-kept")).toBe("1");
    const go = c.querySelector("[data-denoise]") as HTMLButtonElement;
    expect(go.textContent).toContain("Clean 2 spots");
    fireEvent.click(go);
    await waitFor(() => expect(say).toHaveBeenCalledWith(expect.stringContaining("Saved as v4")));
    const post = calls.find((x) => x.method === "POST" && x.path.endsWith("/denoise"))!;
    expect(post.body).toEqual({ digest: "d1", skip: [2] });
  });

  it("站点拒了(图换了):原因显示在对话框里", async () => {
    routes["GET /api/admin/maps/estate/1/noise"] = () => json({ digest: "d1", res: 0.05, max_m2: 0.05, ring_m: 0.2, specks: [{ id: 1, x: 1, y: 2, px: 3 }] });
    routes["GET /api/maps/estate/1/preview"] = () => json({ width: 400, height: 300, m_per_px: 0.05, left_x: -10, top_y: 8 });
    routes["POST /api/admin/maps/estate/1/denoise"] = () => json({ error: "这一版的栅格在你看过之后换了" }, 409);
    const { container: c, getByLabelText } = mount();
    await waitFor(() => getByLabelText("estate:1"));
    fireEvent.click(Array.from(getByLabelText("estate:1").querySelectorAll("button")).find((b) => b.textContent === "Clean noise…") as HTMLElement);
    await waitFor(() => expect(c.querySelector("[data-denoise]")).not.toBeNull());
    await waitFor(() => expect((c.querySelector("[data-denoise]") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(c.querySelector("[data-denoise]") as HTMLElement);
    await waitFor(() => expect(c.querySelector('[role="alert"]')?.textContent).toContain("换了"));
  });
});
