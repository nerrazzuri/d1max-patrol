// 二维码(App V2:手机扫码添加站点)。只做「文字 → 黑白格子」,画成一条 SVG 路径;纠错等级 M。
import qrcode from "qrcode-generator";

export interface QrGrid {
  /** 每边多少格(不含静区)。 */
  n: number;
  /** 黑格子的 SVG 路径,一格 = 1 个单位,已经留出 4 格静区。 */
  d: string;
  /** 连静区的边长。 */
  size: number;
}

export const QUIET = 4;

export function qrGrid(text: string): QrGrid {
  const qr = qrcode(0, "M");
  qr.addData(text, "Byte");
  qr.make();
  const n = qr.getModuleCount();
  let d = "";
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (!qr.isDark(r, c)) continue;
      // 同一行连着的黑格合成一条,路径短一半
      let w = 1;
      while (c + w < n && qr.isDark(r, c + w)) w++;
      d += `M${c + QUIET} ${r + QUIET}h${w}v1h-${w}z`;
      c += w - 1;
    }
  }
  return { n, d, size: n + 2 * QUIET };
}
