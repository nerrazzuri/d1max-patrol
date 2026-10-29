// 「布置」五个标签的实现(W44 第三期)。外框在 layoutpage.tsx;各标签做好一个接一个。
import type { FunctionComponent } from "preact";
import type { LayoutTab, TabProps } from "./layoutpage";
import { CamerasTab } from "./layoutcams";
import { NogoTab } from "./layoutnogo";
import { RoadnetTab } from "./layoutroadnet";
import { RoutesTab } from "./layoutroutes";
import { StandbyTab } from "./layoutstandby";
import { ZonesTab } from "./layoutzones";

export const LAYOUT_TABS: Partial<Record<LayoutTab, FunctionComponent<TabProps>>> = { zones: ZonesTab, nogo: NogoTab, routes: RoutesTab, roadnet: RoadnetTab, standby: StandbyTab, cameras: CamerasTab };
