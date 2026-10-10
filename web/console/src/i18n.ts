// 中英文(B0 规范第十节):英文为主,中文可选。界面文字都走词条;缺中文显示英文,从不显示 key。
// 告警标题按种类(kind)取 design/i18n/alerts.json,不认识的种类才用站点给的原文。
import { signal } from "@preact/signals";
import alertCatalog from "../../../design/i18n/alerts.json";

export type Lang = "en" | "zh";

const en = {
  appName: "D1 Max",
  dutyConsole: "Duty console",
  signIn: "Sign in",
  signingIn: "Signing in…",
  account: "Account",
  password: "Password",
  signOut: "Sign out",
  badLogin: "Wrong account or password",
  lockedOut: "Too many attempts. Try again later",
  sessionExpired: "Your session has expired. Please sign in again",
  siteUnreachable: "Can't reach the site",
  mainNav: "Main",
  tabLive: "Live",
  tabMap: "Map",
  tabAlarms: "Alarms",
  tabRecordings: "Recordings",
  tabReports: "Reports",
  tabAdmin: "Admin",
  comingSoon: "Coming soon",
  modeArmed: "Armed",
  modeHome: "Home",
  modeVisitor: "Visitor",
  modeLabel: "Mode",
  modeHomeConfirm: "Switch to Home? Robots stop raising person alarms until you arm again.",
  modeVisitorConfirm: "Switch to Visitor? Visitor zones stop raising person alarms for 4 hours.",
  modeArmConfirm: "Arm the site? Robots raise P1 when they see a person.",
  stopAll: "Stop all",
  stopAllConfirm: "Stop all {n} robots now? Each one halts where it is until you resume it.",
  stopAllDone: "Stopped {n} robots.",
  stopFailed: "{id} didn't stop: {why}. Use its handheld remote.",
  confirm: "Confirm",
  cancel: "Cancel",
  close: "Close",
  noActiveAlarms: "No active alarms",
  alarmsActive: "Alarms ({n} active)",
  qActive: "Active",
  qShelved: "Shelved ({n})",
  qHistory: "History",
  collapse: "Collapse",
  expand: "Expand",
  colPriority: "Priority",
  colState: "State",
  colTime: "Time",
  colSource: "Source",
  colEvent: "Event",
  colLocation: "Location",
  colHandledBy: "Handled by",
  colActions: "Actions",
  stUnacked: "Unacknowledged",
  stNew: "New",
  stAcked: "Acknowledged",
  stShelved: "Shelved",
  stResolved: "Resolved",
  take: "I'll handle it",
  acknowledge: "Acknowledge",
  ackedBy: "Acknowledged by {who} at {t}",
  unackedFor: "Unacknowledged for {d}",
  shelve: "Shelve…",
  shelveTitle: "Shelve this alarm",
  shelve1h: "For 1 hour",
  shelveTill8: "Until 08:00",
  shelveReason: "Reason (required)",
  shelvedUntil: "Shelved until {t} by {who}: {why}",
  unshelve: "Unshelve",
  resolve: "Resolve",
  goTo: "Go to",
  goToVideo: "Go to video",
  showOnMap: "Show on map",
  siren: "Sound siren (level 2)",
  sirenOn: "Siren on (level 2)",
  falseAlarm: "False alarm",
  falseAlarmConfirm: "Mark as false alarm? Deterrence stops and the alarm is resolved.",
  falseAlarmYes: "Mark as false alarm",
  talkNA: "Talk: not available yet",
  deterOn: "Deterrence level {n} on.",
  persons: "{n} person(s), nearest {m} m",
  aheadOf: "{m} m ahead of {id}",
  st_patrolling: "Patrolling",
  st_deterring: "Deterring",
  st_returning: "Returning",
  st_charging: "Charging",
  st_idle: "Idle",
  st_manual: "Manual",
  st_estop: "E-stopped",
  st_fell: "Fell over",
  st_offline: "Offline",
  offlineSince: "Offline since {t}.",
  notStreaming: "Not streaming.",
  battery: "Battery {n}%",
  front: "Front",
  back: "Back",
  live: "Live",
  pausedAt: "Paused at {t}",
  pausedHint: "Scroll this camera into view to watch live.",
  reconnecting: "Reconnecting…",
  noFeed: "No video",
  watching: "Watching {n} of {total} cameras",
  map: "Map",
  fullScreen: "Full screen",
  p1On: "P1 on {id}. Go to it",
  noRobots: "No robots yet. Add one on the Robots page.",
  noMap: "No map for this site yet. Build one from the Robots page.",
  mapAria: "Site map with robot positions and alarms",
  legendRoute: "Patrol route",
  legendNoGo: "No-go zone",
  legendRobot: "Robot",
  failed: "Failed: {why}",
  language: "Language",
};

type Key = keyof typeof en;

const zh: Partial<Record<Key, string>> = {
  appName: "D1 Max",
  dutyConsole: "值班台",
  signIn: "登录",
  signingIn: "正在登录…",
  account: "账号",
  password: "口令",
  signOut: "退出",
  badLogin: "账号或口令不对",
  lockedOut: "试得太多了，过一会儿再试",
  sessionExpired: "登录过期了，请重新登录",
  siteUnreachable: "连不上站点",
  mainNav: "主菜单",
  tabLive: "实时",
  tabMap: "地图",
  tabAlarms: "告警",
  tabRecordings: "录像",
  tabReports: "报表",
  tabAdmin: "管理",
  comingSoon: "即将推出",
  modeArmed: "布防",
  modeHome: "在家",
  modeVisitor: "访客",
  modeLabel: "模式",
  modeHomeConfirm: "切到「在家」？重新布防前，机器狗不再报「发现人员」。",
  modeVisitorConfirm: "切到「访客」？访客防区 4 小时内不报「发现人员」。",
  modeArmConfirm: "布防？机器狗看见人就报 P1。",
  stopAll: "全部急停",
  stopAllConfirm: "现在让 {n} 只机器狗全部急停？每只原地停下，直到你恢复。",
  stopAllDone: "已急停 {n} 只机器狗。",
  stopFailed: "{id} 没停下：{why}。请用它的遥控器。",
  confirm: "确定",
  cancel: "取消",
  close: "关闭",
  noActiveAlarms: "没有待处理的报警",
  alarmsActive: "报警（{n} 条待处理）",
  qActive: "待处理",
  qShelved: "已搁置（{n}）",
  qHistory: "历史",
  collapse: "收起",
  expand: "展开",
  colPriority: "优先级",
  colState: "状态",
  colTime: "时间",
  colSource: "来源",
  colEvent: "事件",
  colLocation: "位置",
  colHandledBy: "处理人",
  colActions: "操作",
  stUnacked: "未确认",
  stNew: "新",
  stAcked: "已确认",
  stShelved: "已搁置",
  stResolved: "已解除",
  take: "我来处理",
  acknowledge: "确认",
  ackedBy: "{who} {t} 已确认",
  unackedFor: "{d} 未确认",
  shelve: "搁置…",
  shelveTitle: "搁置这条报警",
  shelve1h: "搁置 1 小时",
  shelveTill8: "到 08:00",
  shelveReason: "原因（必填）",
  shelvedUntil: "{who}搁置到 {t}：{why}",
  unshelve: "取消搁置",
  resolve: "解除",
  goTo: "去看",
  goToVideo: "去看画面",
  showOnMap: "在地图上看",
  siren: "拉响警笛（2 级）",
  sirenOn: "警笛已开（2 级）",
  falseAlarm: "误报",
  falseAlarmConfirm: "标为误报？驱离停止，报警解除。",
  falseAlarmYes: "标为误报",
  talkNA: "喊话：暂未开通",
  deterOn: "{n} 级驱离中。",
  persons: "{n} 个人，最近 {m} m",
  aheadOf: "{id} 前方 {m} m",
  st_patrolling: "巡逻中",
  st_deterring: "驱离中",
  st_returning: "返回中",
  st_charging: "充电中",
  st_idle: "待命",
  st_manual: "手动",
  st_estop: "急停",
  st_fell: "翻倒",
  st_offline: "离线",
  offlineSince: "{t} 起离线。",
  notStreaming: "不拉画面。",
  battery: "电量 {n}%",
  front: "前",
  back: "后",
  live: "实时",
  pausedAt: "{t} 暂停",
  pausedHint: "滚到可见位置即恢复实时。",
  reconnecting: "正在重连…",
  noFeed: "没有画面",
  watching: "正在看 {n} / {total} 路画面",
  map: "地图",
  fullScreen: "全屏",
  p1On: "{id} 有 P1，去看",
  noRobots: "还没有机器狗。到「机器狗」页添加。",
  noMap: "这个站点还没有地图。到「机器狗」页建图。",
  mapAria: "站点地图：机器狗的位置与报警",
  legendRoute: "巡逻路线",
  legendNoGo: "禁行区",
  legendRobot: "机器狗",
  failed: "没成：{why}",
  language: "语言",
};

export const DICTS: Record<Lang, Partial<Record<Key, string>>> = { en, zh };
export type { Key };

/** 第一次打开:中文系统选中文,别的都选英文;选过就记住(B0 规范十.1)。 */
export function initialLang(stored: string | null, navLangs: readonly string[]): Lang {
  if (stored === "en" || stored === "zh") return stored;
  return navLangs.some((l) => l.toLowerCase().startsWith("zh")) ? "zh" : "en";
}

function readStored(): string | null {
  try {
    return localStorage.getItem("d1max.lang");
  } catch {
    return null;
  }
}

export const lang = signal<Lang>(
  initialLang(readStored(), typeof navigator === "undefined" ? [] : navigator.languages ?? []),
);

export function setLang(l: Lang): void {
  lang.value = l;
  try {
    localStorage.setItem("d1max.lang", l);
  } catch {
    /* 隐私模式:不记也行 */
  }
  if (typeof document !== "undefined") document.documentElement.lang = l === "zh" ? "zh-CN" : "en";
}

export function tr(l: Lang, key: Key, vars?: Record<string, string | number>): string {
  let s = DICTS[l][key] ?? en[key];
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
  return s;
}

export function t(key: Key, vars?: Record<string, string | number>): string {
  return tr(lang.value, key, vars);
}

const CATALOG = alertCatalog as unknown as Record<string, { en: string; zh: string }>;

/** 告警标题:认识的种类用目录里的;不认识的用站点给的原文。 */
export function alertTitle(l: Lang, kind: string, siteTitle: string): string {
  const e = CATALOG[kind];
  return e && !kind.startsWith("$") ? e[l] : siteTitle;
}

export function knownKinds(): string[] {
  return Object.keys(CATALOG).filter((k) => !k.startsWith("$"));
}
