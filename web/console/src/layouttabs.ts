// 「布置」五个标签的实现(W44 第三期)。外框在 layoutpage.tsx;各标签做好一个接一个。
import type { FunctionComponent } from "preact";
import type { LayoutTab, TabProps } from "./layoutpage";

export const LAYOUT_TABS: Partial<Record<LayoutTab, FunctionComponent<TabProps>>> = {};
