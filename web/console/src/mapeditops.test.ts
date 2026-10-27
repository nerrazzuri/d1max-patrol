// W44 第四期 地图编辑器的操作栈:撤销重做、面积估算、特殊地面默认放慢。
import { describe, expect, it } from "vitest";
import { apply, areaOf, newTerrain, redo, start, summary, undo, type GridOp } from "./mapeditops";

const D = { rotate_deg: null, basemap: null, regions: [], terrain: [] };
const erase: GridOp = { op: "erase", shape: "stroke", points: [[0, 0], [2, 0]], width_m: 0.2 };
const box: GridOp = { op: "obstacle", shape: "rect", points: [[0, 0], [1, 2]], width_m: 0.2 };

describe("操作栈", () => {
  it("撤销重做", () => {
    let h = start(D);
    h = apply(h, (s) => ({ ...s, ops: [...s.ops, erase] }));
    h = apply(h, (s) => ({ ...s, ops: [...s.ops, box] }));
    expect(h.now.ops.length).toBe(2);
    h = undo(h);
    expect(h.now.ops).toEqual([erase]);
    h = redo(h);
    expect(h.now.ops.length).toBe(2);
    h = undo(undo(h));
    expect(h.now.ops).toEqual([]);
    expect(undo(h)).toBe(h);
    h = apply(h, (s) => ({ ...s, display: { ...s.display, rotate_deg: 10 } }));
    expect(h.future).toEqual([]);
  });
  it("面积估算", () => {
    expect(areaOf(box)).toBe(2);
    expect(areaOf(erase)).toBeCloseTo(2 * 0.2 + Math.PI * 0.01, 5);
    const s = summary({ ops: [erase, box], display: D });
    expect(s).toEqual({ erased: 0.4, obstacle: 2 });
  });
  it("特殊地面:楼梯默认放慢,草地不放慢,id 不重", () => {
    const a = newTerrain([], "stairs", [[0, 0], [1, 0], [1, 1]]);
    expect(a.slow_mps).toBe(0.3);
    const b = newTerrain([a], "grass", [[0, 0], [1, 0], [1, 1]]);
    expect(b.slow_mps).toBeNull();
    expect(b.id).not.toBe(a.id);
  });
});
