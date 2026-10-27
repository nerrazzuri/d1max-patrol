// 布置 → 巡逻路线(W44 第三期,设计 §5.2):用现有路线编辑(routepage.tsx,外审过的点运算、拖点、转朝向、晚到回包、排程)
// 嵌进布置页 —— 图、保存按钮、「刚才有人改过」都走外框;不显示坐标数字;站点逐段查走不通的那一段画红;热像两项灰着。
import type { TabProps } from "./layoutpage";
import { RoutesPage } from "./routepage";

export function RoutesTab(p: TabProps) {
  return (
    <RoutesPage say={p.say} embed={{
      map: p.map, meta: p.meta, Map: p.Map, setDirty: p.setDirty,
      registerSave: (fn) => p.registerSave(fn ? async () => { await fn(); await p.reload("routes"); } : null),
    }} />
  );
}
