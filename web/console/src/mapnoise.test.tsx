// @vitest-environment happy-dom
// W40/W41 外审 R6(2026-10-28):清噪点时预览(元数据)拿不到,图和红圈都没有,「清掉 N 处」照样能点,
// 一点就把默认选中的全清掉 —— 人工看图挑「细杆子留着」那一步被绕过。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
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
  expect((container.querySelector("[data-denoise]") as HTMLButtonElement).disabled).toBe(false);
});

it("预览正常:图和红圈都在,能清", async () => {
  setFetch((async (url: string) => (url.endsWith("/noise") ? json(LIST) : json(META))) as typeof fetch);
  const { container } = render(<NoiseDialog target={{ map_id: "m", version: "1" }} onClose={() => {}} onDone={() => {}} />);
  await waitFor(() => expect(container.querySelectorAll("[data-speck]").length).toBe(2));
  expect((container.querySelector("[data-denoise]") as HTMLButtonElement).disabled).toBe(false);
});
