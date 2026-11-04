// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";
import { moveOldKeys } from "./oldkeys";

describe("改名兼容", () => {
  it("老键搬到新键,新键已有的不动,老键删掉", () => {
    localStorage.clear();
    localStorage.setItem("d1max.lang", "en");
    localStorage.setItem("d1max.look", "pro");
    localStorage.setItem("nexwar.look", "classic");
    expect(moveOldKeys()).toBe(1);
    expect(localStorage.getItem("nexwar.lang")).toBe("en");
    expect(localStorage.getItem("nexwar.look")).toBe("classic");
    expect(localStorage.getItem("d1max.lang")).toBeNull();
    expect(localStorage.getItem("d1max.look")).toBeNull();
  });
});
