// W44 第二期 布防时间页的时间段运算(不碰 DOM):加一段会跟相接、相交的合并;拖边吸到 30 分钟、不越过邻段、
// 不短于 30 分钟;复制一天会盖掉目标那天原来的段;删只删含那一刻的那一段。
import { describe, expect, it } from "vitest";
import { addBlock, copyDay, fromWire, moveEdge, removeBlock, snap, toWire, type Slot } from "./armgrid";

const s = (day: number, start: number, end: number, zone = ""): Slot => ({ zone, day, start, end });
const H = 60;

describe("时间段运算", () => {
  it("吸到 30 分钟", () => {
    expect(snap(14)).toBe(0);
    expect(snap(16)).toBe(30);
    expect(snap(-5)).toBe(0);
    expect(snap(1500)).toBe(1440);
  });

  it("加一段:相接、相交的合并,别的防区、别的天不动", () => {
    let x = [s(0, 20 * H, 22 * H), s(1, 20 * H, 22 * H), s(0, 0, 6 * H, "后花园")];
    x = addBlock(x, "", 0, 22 * H, 24 * H);
    expect(x).toContainEqual(s(0, 20 * H, 24 * H));
    x = addBlock(x, "", 0, 19 * H, 21 * H);
    expect(x.filter((y) => y.zone === "" && y.day === 0)).toEqual([s(0, 19 * H, 24 * H)]);
    expect(x).toContainEqual(s(1, 20 * H, 22 * H));
    expect(x).toContainEqual(s(0, 0, 6 * H, "后花园"));
  });

  it("加一段:倒着拖也行,太短的不加", () => {
    expect(addBlock([], "", 2, 8 * H, 6 * H)).toEqual([s(2, 6 * H, 8 * H)]);
    expect(addBlock([], "", 2, 8 * H, 8 * H + 10)).toEqual([]);
  });

  it("删只删含那一刻的那一段", () => {
    const x = [s(0, 0, 6 * H), s(0, 20 * H, 24 * H), s(1, 0, 6 * H)];
    expect(removeBlock(x, "", 0, 21 * H)).toEqual([s(0, 0, 6 * H), s(1, 0, 6 * H)]);
    expect(removeBlock(x, "", 0, 12 * H)).toEqual(x);
  });

  it("拖边:吸到 30 分钟、不越过邻段、不短于 30 分钟", () => {
    const x = [s(0, 0, 6 * H), s(0, 20 * H, 22 * H)];
    expect(moveEdge(x, "", 0, 20 * H, "start", 19 * H + 10)).toContainEqual(s(0, 19 * H, 22 * H));
    expect(moveEdge(x, "", 0, 20 * H, "start", 3 * H)).toContainEqual(s(0, 6 * H, 22 * H));
    expect(moveEdge(x, "", 0, 20 * H, "start", 23 * H)).toContainEqual(s(0, 21 * H + 30, 22 * H));
    expect(moveEdge(x, "", 0, 20 * H, "end", 30 * H)).toContainEqual(s(0, 20 * H, 24 * H));
    expect(moveEdge(x, "", 0, 0, "end", 21 * H)).toContainEqual(s(0, 0, 20 * H));
  });

  it("复制一天盖掉目标那天原来的段", () => {
    const x = [s(0, 20 * H, 24 * H), s(0, 0, 6 * H), s(2, 8 * H, 9 * H), s(2, 8 * H, 9 * H, "后花园")];
    const y = copyDay(x, "", 0, [1, 2]);
    expect(y.filter((v) => v.zone === "" && v.day === 2)).toEqual([s(2, 0, 6 * H), s(2, 20 * H, 24 * H)]);
    expect(y.filter((v) => v.zone === "" && v.day === 1)).toHaveLength(2);
    expect(y).toContainEqual(s(2, 8 * H, 9 * H, "后花园"));
  });

  it("跟站点来回:HH:MM,24:00", () => {
    const w = [{ zone: "", day: 0, start: "22:00", end: "24:00" }];
    expect(fromWire(w)).toEqual([s(0, 22 * H, 24 * H)]);
    expect(toWire(fromWire(w))).toEqual(w);
  });
});
