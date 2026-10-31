// @vitest-environment happy-dom
// W44 第四期 高级 → 地图:开头写什么时候用;重新建图五步,一步步往下走;每版有「编辑…」。
import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AdvMapsPage } from "./advmaps";
import { setFetch } from "./api";
import { lang } from "./i18n";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
beforeEach(() => {
  lang.value = "zh";
  setFetch(((url: string) => {
    if (url === "/api/admin/maps") {
      return Promise.resolve(json({ now_ms: 0, maps: [{ map_id: "home", versions: [{ version: "3", note: "", created_ms: 1, source: "", has_raster: true, latest: true, previous_on: [], thumbnail: "", build: null,
        loaded_on: [], counts: { zones: 0, nogo: 0, slow: 0, zones_rev: 0, zones_confirmed: null, standby: 0, intercepts: 0 } }] }], robots: [] }));
    }
    return Promise.resolve(json({}, 404));
  }) as typeof fetch);
});
afterEach(cleanup);

describe("高级 → 地图", () => {
  it("重新建图五步,一步步往下走", () => {
    const { container: c } = render(<AdvMapsPage say={() => {}} />);
    expect(c.textContent).toContain("家具大量移动");
    fireEvent.click(Array.from(c.querySelectorAll("button")).find((b) => b.textContent?.startsWith("重新建图"))!);
    expect(c.querySelectorAll(".rb-steps li").length).toBe(5);
    expect(c.querySelector(".rb-steps li.now")!.textContent).toContain("起点");
    fireEvent.click(c.querySelector(".rb-steps li.now button")!);
    expect(c.querySelector(".rb-steps li.now")!.textContent).toContain("手机");
  });
  it("每版有编辑", async () => {
    const { container: c } = render(<AdvMapsPage say={() => {}} />);
    await waitFor(() => expect(c.querySelector('a[href="#/admin/mapedit?map=home&version=3"]')).not.toBeNull());
  });
});
