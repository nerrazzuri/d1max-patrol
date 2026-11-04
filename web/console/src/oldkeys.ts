// 改名(d1max → nexwar,2026-11)兼容一个版本:浏览器里存过的老键(语言、地图图层、外观)搬到新键。
// 要在读这些键的模块之前跑:main.tsx 第一个导入它。新键已经有的不动;读写不了(隐私模式)就算了。
export const MOVED = ["lang", "maplayers", "look"];

export function moveOldKeys(store: Storage | undefined = globalThis.localStorage): number {
  let n = 0;
  try {
    for (const k of MOVED) {
      const old = store?.getItem(`d1max.${k}`);
      if (old != null && store?.getItem(`nexwar.${k}`) == null) {
        store!.setItem(`nexwar.${k}`, old);
        n++;
      }
      if (old != null) store!.removeItem(`d1max.${k}`);
    }
  } catch { /* 存不了:按没存过 */ }
  return n;
}

moveOldKeys();
