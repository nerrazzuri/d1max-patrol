// @vitest-environment happy-dom
// W40/W41 外审 R6(2026-10-28):清噪点时预览(元数据)拿不到,图和红圈都没有,「清掉 N 处」照样能点,
// 一点就把默认选中的全清掉 —— 人工看图挑「细杆子留着」那一步被绕过。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { h, render as rawRender } from "preact";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setFetch } from "./api";
import { lang } from "./i18n";
import { NoiseDialog } from "./mapmgmt";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const LIST = { digest: "d1", res: 0.05, max_m2: 0.05, ring_m: 0.2, specks: [{ id: 1, x: 1, y: 2, px: 3 }, { id: 2, x: 2, y: 3, px: 1 }] };
const META = { width: 100, height: 80, resolution: 0.05, origin: [0, 0, 0] };

beforeEach(() => { lang.value = "zh"; });
afterEach(cleanup);

it("预览拿不到:按钮不许点、写清楚原因;重试拿到了才许清", async () => {
  let previewOk = false;
  const posts: unknown[] = [];
  setFetch((async (url: string, init?: RequestInit) => {
    if (url.endsWith("/noise")) return json(LIST);
    if (url.endsWith("/preview")) return previewOk ? json(META) : json({ error: "preview unavailable" }, 503);
    if (url.endsWith("/denoise")) { posts.push(JSON.parse(String(init?.body))); return json({ version: "2", cleaned: 2, cells: 4, homes_copied: 0 }); }
    throw new Error(url);
  }) as typeof fetch);
  const done = vi.fn();
  const { container } = render(<NoiseDialog target={{ map_id: "m", version: "1" }} onClose={() => {}} onDone={done} />);
  await waitFor(() => expect(container.querySelector("[role=alert]")).not.toBeNull());
  const btn = container.querySelector("[data-denoise]") as HTMLButtonElement;
  expect(btn.disabled).toBe(true);
  fireEvent.click(btn);
  expect(posts).toEqual([]);
  previewOk = true;
  fireEvent.click(container.querySelector("[data-noise-retry]") as HTMLButtonElement);
  await waitFor(() => expect(container.querySelector(".mm-noiseimg")).not.toBeNull());
  fireEvent.load(container.querySelector(".mm-noiseimg image")!);
  await waitFor(() => expect((container.querySelector("[data-denoise]") as HTMLButtonElement).disabled).toBe(false));
});

it("预览正常:图和红圈都在,能清", async () => {
  setFetch((async (url: string) => (url.endsWith("/noise") ? json(LIST) : json(META))) as typeof fetch);
  const { container } = render(<NoiseDialog target={{ map_id: "m", version: "1" }} onClose={() => {}} onDone={() => {}} />);
  await waitFor(() => expect(container.querySelectorAll("[data-speck]").length).toBe(2));
  fireEvent.load(container.querySelector(".mm-noiseimg image")!);
  await waitFor(() => expect((container.querySelector("[data-denoise]") as HTMLButtonElement).disabled).toBe(false));
});

it("复查 N3:底图还没加载好不许清;底图加载失败、点重试要真的重新拉图,拉到之前照样不许清", async () => {
  const posts: unknown[] = [];
  setFetch((async (url: string, init?: RequestInit) => {
    if (url.endsWith("/noise")) return json(LIST);
    if (url.endsWith("/preview")) return json(META);
    if (url.endsWith("/denoise")) { posts.push(JSON.parse(String(init?.body))); return json({ version: "2", cleaned: 2, cells: 4, homes_copied: 0 }); }
    throw new Error(url);
  }) as typeof fetch);
  const { container } = render(<NoiseDialog target={{ map_id: "m", version: "1" }} onClose={() => {}} onDone={() => {}} />);
  await waitFor(() => expect(container.querySelector(".mm-noiseimg image")).not.toBeNull());
  const btn = () => container.querySelector("[data-denoise]") as HTMLButtonElement;
  expect(btn().disabled).toBe(true);                          // 元数据有了、底图还没好
  const img = container.querySelector(".mm-noiseimg image")!;
  const href0 = img.getAttribute("href");
  fireEvent.error(img);
  await waitFor(() => expect(container.querySelector("[data-noise-retry]")).not.toBeNull());
  expect(btn().disabled).toBe(true);
  fireEvent.click(container.querySelector("[data-noise-retry]") as HTMLButtonElement);
  await waitFor(() => expect(container.querySelector(".mm-noiseimg image")!.getAttribute("href")).not.toBe(href0));
  expect(btn().disabled).toBe(true);                          // 重拉的图还没到
  fireEvent.click(btn());
  expect(posts).toEqual([]);
  fireEvent.load(container.querySelector(".mm-noiseimg image")!);
  await waitFor(() => expect(btn().disabled).toBe(false));
});

it.each(["load", "error"])("复查 N6:点重试后新底图马上 %s(重置的 effect 还没跑),结果不被盖掉", async (ev) => {
  // 用 Preact 原生 render(testing-library 的 render 会把 effect 立刻冲刷掉,复现不了这个时序)
  setFetch((async (url: string) => (url.endsWith("/noise") ? json(LIST) : json(META))) as typeof fetch);
  const host = document.createElement("div");
  document.body.append(host);
  try {
    rawRender(h(NoiseDialog, { target: { map_id: "m", version: "1" }, onClose: () => {}, onDone: () => {} }), host);
    for (let i = 0; !host.querySelector(".mm-noiseimg image") && i < 100; i++) await new Promise((r) => setTimeout(r, 0));
    host.querySelector(".mm-noiseimg image")!.dispatchEvent(new Event("error"));
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    host.querySelector("[data-noise-retry]")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(host.querySelector(".mm-noiseimg image")!.getAttribute("href")).toContain("?try=1");
    host.querySelector(".mm-noiseimg image")!.dispatchEvent(new Event(ev));
    await new Promise((r) => setTimeout(r, 100));
    const btn = host.querySelector("[data-denoise]") as HTMLButtonElement;
    if (ev === "load") expect(btn.disabled).toBe(false);
    else {
      expect(btn.disabled).toBe(true);
      expect(host.querySelector("[data-noise-retry]")).not.toBeNull();   // 还能再试
    }
  } finally {
    rawRender(null, host);
    host.remove();
  }
});

it("复查 N7:连着重试两次,前一次的预览请求最后才回来、还是失败,不许把最新一次的成功盖掉", async () => {
  let calls = 0;
  let finishOld!: (v: Response) => void;
  setFetch((async (url: string) => {
    if (url.endsWith("/noise")) return json(LIST);
    calls++;
    if (calls === 2) return await new Promise<Response>((r) => { finishOld = r; });   // 第一次重试的那份:挂着
    return json(META);
  }) as typeof fetch);
  const micro = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
  const tick = () => new Promise((r) => setTimeout(r, 0));
  const host = document.createElement("div");
  document.body.append(host);
  try {
    rawRender(h(NoiseDialog, { target: { map_id: "m", version: "1" }, onClose: () => {}, onDone: () => {} }), host);
    for (let i = 0; !host.querySelector(".mm-noiseimg image") && i < 100; i++) await tick();
    host.querySelector(".mm-noiseimg image")!.dispatchEvent(new Event("error"));
    await micro();
    host.querySelector("[data-noise-retry]")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await micro();
    for (let i = 0; calls < 2 && i < 100; i++) await tick();
    host.querySelector(".mm-noiseimg image")!.dispatchEvent(new Event("error"));
    await micro();
    host.querySelector("[data-noise-retry]")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await micro();
    finishOld(json({ error: "前一次的失败" }, 503));                                  // 迟到的旧失败
    await micro();
    for (let i = 0; (calls < 3 || !host.querySelector(".mm-noiseimg image")) && i < 100; i++) await tick();
    host.querySelector(".mm-noiseimg image")!.dispatchEvent(new Event("load"));
    await new Promise((r) => setTimeout(r, 100));
    expect((host.querySelector("[data-denoise]") as HTMLButtonElement).disabled).toBe(false);
    expect(host.querySelector("[role=alert]")).toBeNull();
  } finally {
    rawRender(null, host);
    host.remove();
  }
});
