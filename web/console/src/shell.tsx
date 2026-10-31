// 值班台外壳(W44 第一期,设计 §4.0;W60 改版):路由、左侧导航、顶栏、用户菜单、主题。
// 左侧导航:品牌、站点名,按角色分「值班 / 管理 / 系统」三组(高级默认收着)。
// 顶栏只有:页面名、布防状态(只显示)、报警数、时钟、帮助、用户名下拉(语言、外观、改密码、退出)、全部急停。
// W60:全站默认深色(不再按页面换深浅色),浅色在「外观」里个人选。
import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "./api";
import type { Me } from "./app";
import { modeLabel } from "./armingpage";
import { Modal } from "./dialogs";
import { hm } from "./data";
import { lang, setLang, t, type Key } from "./i18n";
import { Icon } from "./icons";
import { canDispatch, isOwnerLike, type Mode } from "./model";

export type GuardPage = "duty" | "live" | "map" | "recordings" | "alarms";
/** 「管理」各页(W44 第二期,设计 §5):常用 = 现场概况、布防时间、授权和临时撤防、用户、机器人、报表;
 *  布置、高级两组先挂现有页面(第三、四期重做)。 */
export type AdminPage =
  | "admin/home" | "admin/arming" | "admin/access" | "admin/users" | "admin/robots" | "admin/reports"
  | "admin/layout" | "admin/maps" | "admin/localization" | "admin/diag" | "admin/upgrade" | "admin/system"
  | "admin/mapedit";
export type Page = GuardPage | AdminPage;

export const GUARD_PAGES: GuardPage[] = ["duty", "live", "map", "recordings", "alarms"];

/** 左侧导航的一组(W60)。``fold``:默认收着,在组里的页时展开。``badge``:右边的数(报警、技术提醒)。 */
export interface NavSection {
  id: "duty" | "manage" | "system" | "advanced";
  key: Key;
  fold?: boolean;
  items: { id: Page; key: Key; badge?: "alarms" | "notices" }[];
}
/** 一级菜单要短(W60 约束):地图管理、定位、诊断、升级、系统这些工程向的收进「高级」。 */
export const NAV_SECTIONS: NavSection[] = [
  { id: "duty", key: "navDuty", items: [
    { id: "duty", key: "tabOverview" }, { id: "live", key: "tabLive" }, { id: "map", key: "tabMap" },
    { id: "alarms", key: "tabAlarms", badge: "alarms" }, { id: "recordings", key: "tabRecordings" },
  ] },
  { id: "manage", key: "navManage", items: [
    { id: "admin/robots", key: "tabRobotsAdmin" }, { id: "admin/layout", key: "lyTitle" }, { id: "admin/arming", key: "tabArming" },
    { id: "admin/access", key: "tabAccess" }, { id: "admin/users", key: "tabUsers" },
  ] },
  { id: "system", key: "navSystem", items: [
    { id: "admin/reports", key: "tabReports" }, { id: "admin/home", key: "tabHome", badge: "notices" },
  ] },
  { id: "advanced", key: "navAdvanced", fold: true, items: [
    { id: "admin/maps", key: "tabMapMgmt" }, { id: "admin/localization", key: "tabLocalization" }, { id: "admin/diag", key: "tabDiag" },
    { id: "admin/upgrade", key: "tabUpgrade" }, { id: "admin/system", key: "tabSystem" },
  ] },
];
/** 不在导航里、从别的页打开的(地图编辑器从「高级 → 地图」每一版的「编辑…」进)。 */
const ADMIN_PAGES: AdminPage[] = [
  ...NAV_SECTIONS.flatMap((g) => g.items.map((p) => p.id)).filter((p): p is AdminPage => p.startsWith("admin/")),
  "admin/mapedit",
];

/** 这个角色在左侧导航里看得到哪几组、每组哪几页(没权限的页不列,空的组不列)。 */
export function navFor(role: string): NavSection[] {
  return NAV_SECTIONS.map((g) => ({ ...g, items: g.items.filter((x) => pageAllowed(x.id, role)) }))
    .filter((g) => g.items.length > 0);
}

/** 页面在导航里的名字(顶栏左边的标题用)和所在组。 */
export function navLabel(page: Page): { section: Key; page: Key } | null {
  for (const g of NAV_SECTIONS) {
    const x = g.items.find((i) => i.id === page);
    if (x) return { section: g.id === "advanced" ? "navSystem" : g.key, page: x.key };
  }
  return page === "admin/mapedit" ? { section: "navSystem", page: "tabMapMgmt" } : null;
}
/** 改版前的地址(书签、手机分享的链接)。 */
const LEGACY: Record<string, Page> = {
  localization: "admin/localization", reports: "admin/reports", "admin/notices": "admin/home", "admin/lists": "admin/access",
  "admin/zones": "admin/layout", "admin/routes": "admin/layout",
};

export interface Route {
  page: Page;
  robot: string | null;
  at: number | null;
}

export function readRoute(hash: string = typeof location === "undefined" ? "" : location.hash): Route {
  const [p, q] = hash.replace(/^#\/?/, "").split("?");
  const qs = new URLSearchParams(q ?? "");
  const at = Number(qs.get("t") ?? "");
  const page: Page = (GUARD_PAGES as string[]).includes(p) || (ADMIN_PAGES as string[]).includes(p)
    ? (p as Page) : LEGACY[p] ?? "duty";
  return { page, robot: qs.get("robot"), at: at > 0 ? at : null };
}

export function isAdminPage(p: Page): p is AdminPage {
  return p.startsWith("admin/");
}

/** 角色的界面名字(设计 §3):admin 叫管理员,owner 叫经理,engineer 是工程师(临时)。 */
export function roleLabel(role: string): Key {
  return role === "admin" ? "roleOwner" : role === "guard" ? "roleGuard" : role === "engineer" ? "roleEngineer"
    : role === "viewer" ? "roleViewer" : "roleFamily";
}

/** 页面默认的明暗:W60 起全站深色(原来管理页浅色,切页面像进了另一个系统)。 */
export function pageScheme(_p: Page): "dark" | "light" {
  return "dark";
}

/** 个人在「外观」里的选择:默认(深色)、跟随系统、一直深色、一直浅色。 */
export type ThemePref = "auto" | "system" | "dark" | "light";
const PREF_KEY = "d1max.look";

export function schemeFor(p: Page, pref: ThemePref, systemDark: boolean): "dark" | "light" {
  if (pref === "dark" || pref === "light") return pref;
  if (pref === "system") return systemDark ? "dark" : "light";
  return pageScheme(p);
}

/** 外壳要把主题定成哪一套:选了「跟随系统」就不定(``null``),交给系统换颜色的事件 —— 定成「当时」那一套的话,
 *  系统后来再换就不跟了(真浏览器实测查出来的)。 */
export function pinFor(p: Page, pref: ThemePref, systemDark: boolean): "dark" | "light" | null {
  return pref === "system" ? null : schemeFor(p, pref, systemDark);
}

/** 这个角色能不能开这一页(管理那几页要管理员、经理、工程师;「用户」只有管理员)。没权限的写清楚,不给空白页(演示站点实测查出)。 */
export function pageAllowed(p: Page, role: string): boolean {
  if (!isAdminPage(p)) return true;
  if (p === "admin/users") return role === "admin";
  // W53:经理能排布防时间(App 里排巡逻日程,布防时间在这里)
  if (p === "admin/arming") return isOwnerLike(role) || role === "owner";
  return isOwnerLike(role);
}

export function loadThemePref(): ThemePref {
  try {
    const v = localStorage.getItem(PREF_KEY);
    return v === "system" || v === "dark" || v === "light" ? v : "auto";
  } catch {
    return "auto";
  }
}

export function saveThemePref(v: ThemePref): void {
  try {
    localStorage.setItem(PREF_KEY, v);
  } catch {
    /* 存不下就只管这一次 */
  }
}

export function TopBar(props: {
  me: Me;
  page: Page;
  mode: Mode | null;
  /** 给保安看的未解决 P1、P2 有几条;数据过期时是 null(说不清)。 */
  alarms: number | null;
  now: number;
  pref: ThemePref;
  onPref: (p: ThemePref) => void;
  onStop: () => void;
  onSignOut: () => void;
  onSay: (s: string) => void;
  /** 「?」:使用教程、常见问题(W53)。 */
  onHelp?: () => void;
}) {
  return (
    <header class="topbar">
      <a class="skip" href="#main">Skip to content</a>
      <PageTitle page={props.page} />
      {props.mode && <ArmState mode={props.mode} />}
      <a href="#/alarms" class={props.alarms ? "alarmcount on" : "alarmcount"} aria-live="polite">
        {t("alarmCount", { n: props.alarms == null ? "?" : props.alarms })}
      </a>
      <span class="clock mono">{new Date(props.now).toLocaleTimeString("en-GB", { hour12: false })}</span>
      {props.onHelp && (
        <button type="button" class="btn sm help-btn" aria-label={t("helpTitle")} title={t("helpTitle")} onClick={props.onHelp}>?</button>
      )}
      <UserMenu me={props.me} pref={props.pref} onPref={props.onPref} onSignOut={props.onSignOut} onSay={props.onSay} />
      {(canDispatch(props.me.role) || props.me.role === "owner") && (
        <button type="button" class="btn stop" onClick={props.onStop}><Icon name="octagon" />{t("stopAll")}</button>
      )}
    </header>
  );
}

/** 顶栏左边:组名(小字)+ 页名。 */
function PageTitle({ page }: { page: Page }) {
  const l = navLabel(page);
  return (
    <div class="pagetitle">
      {l && <span class="pagetitle-sec">{t(l.section)}</span>}
      <span class="pagetitle-name">{l ? t(l.page) : ""}</span>
    </div>
  );
}

/** 品牌标识(W60):外部设计的标志到了以后只换这一个组件。现在是文字。 */
export function BrandMark() {
  return (
    <span class="brandmark" aria-label="Nexus Warden">
      <span class="brandmark-a">NEXUS</span>
      <span class="brandmark-b">WARDEN</span>
    </span>
  );
}

/** 左侧导航(W60):品牌、站点名,按角色分组;高级默认收着(在高级的页里就展开)。 */
export function SideNav(props: { me: Me; page: Page; alarms: number | null; notices: number }) {
  const groups = navFor(props.me.role);
  const inAdvanced = NAV_SECTIONS.find((g) => g.id === "advanced")!.items.some((p) => p.id === props.page)
    || props.page === "admin/mapedit";
  const [open, setOpen] = useState(inAdvanced);
  useEffect(() => { if (inAdvanced) setOpen(true); }, [inAdvanced]);
  const badge = (b: "alarms" | "notices" | undefined) => {
    const n = b === "alarms" ? props.alarms : b === "notices" ? props.notices : null;
    return n ? <span class={b === "alarms" ? "badge alarm mono" : "badge mono"}>{n}</span> : null;
  };
  return (
    <aside class="sidenav">
      <div class="sidenav-brand">
        <BrandMark />
        <span class="sidenav-site" title={props.me.site_name}>{props.me.site_name}</span>
      </div>
      <nav class="sidenav-nav" aria-label={t("mainNav")}>
        {groups.map((g) => {
          const shown = !g.fold || open;
          return (
            <div key={g.id} class="navgroup">
              {g.fold
                ? <button type="button" class="navgroup-title" aria-expanded={open} onClick={() => setOpen(!open)}>{t(g.key)}</button>
                : <div class="navgroup-title">{t(g.key)}</div>}
              {shown && g.items.map((x) => (
                <a key={x.id} href={`#/${x.id}`} aria-current={props.page === x.id ? "page" : undefined}>
                  <span>{t(x.key)}</span>{badge(x.badge)}
                </a>
              ))}
            </div>
          );
        })}
      </nav>
    </aside>
  );
}

/** 布防状态:只显示(设计 §4.0,用户说的),切模式在别处。 */
function ArmState({ mode }: { mode: Mode }) {
  const text = mode.source === "schedule" ? modeLabel(mode) : mode.mode === "armed" ? t("armArmed")
    : mode.mode === "home" ? t("armHome")
    : t("armVisitor", { t: mode.until_ms ? hm(mode.until_ms) : "—" });
  return <span class={`armstate ${mode.source === "schedule" ? (mode.state === "armed" ? "armed" : "home") : mode.mode}`}><i class="dot" aria-hidden="true" />{text}</span>;
}

function UserMenu(props: { me: Me; pref: ThemePref; onPref: (p: ThemePref) => void; onSignOut: () => void; onSay: (s: string) => void }) {
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const off = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", off);
    return () => document.removeEventListener("mousedown", off);
  }, [open]);
  const looks: { v: ThemePref; key: Key }[] = [
    { v: "auto", key: "lookAuto" }, { v: "system", key: "lookSystem" }, { v: "dark", key: "lookDark" }, { v: "light", key: "lookLight" },
  ];
  return (
    <div class="usermenu" ref={box}>
      <button type="button" class="who" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>
        {props.me.display_name || props.me.name} ▾
      </button>
      {open && (
        <div class="menu" role="menu">
          <div class="menu-head">
            <b>{props.me.display_name || props.me.name}</b>
            <span class="muted small">{t(roleLabel(props.me.role))}</span>
          </div>
          <div class="menu-row">
            <span class="muted small">{t("language")}</span>
            <div class="seg" role="group" aria-label={t("language")}>
              <button type="button" aria-pressed={lang.value === "en"} onClick={() => setLang("en")}>EN</button>
              <button type="button" aria-pressed={lang.value === "zh"} onClick={() => setLang("zh")}>中文</button>
            </div>
          </div>
          <div class="menu-row">
            <span class="muted small">{t("menuLook")}</span>
            <div class="seg" role="group" aria-label={t("menuLook")}>
              {looks.map((l) => (
                <button key={l.v} type="button" aria-pressed={props.pref === l.v} onClick={() => props.onPref(l.v)}>{t(l.key)}</button>
              ))}
            </div>
          </div>
          <button type="button" class="menu-item" role="menuitem" onClick={() => { setOpen(false); setPw(true); }}>{t("changePassword")}</button>
          <a class="menu-item" role="menuitem" href="/assets/third-party-notices.txt" target="_blank" rel="noopener noreferrer" onClick={() => setOpen(false)}>{t("thirdParty")}</a>
          <button type="button" class="menu-item" role="menuitem" onClick={props.onSignOut}><Icon name="logout" />{t("signOut")}</button>
        </div>
      )}
      {pw && <OwnPassword onClose={() => setPw(false)} onDone={() => { setPw(false); props.onSay(t("pwDone")); }} />}
    </div>
  );
}

function OwnPassword({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [old, setOld] = useState("");
  const [neu, setNeu] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(e: Event) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await api("POST", "/api/me/password", { old, new: neu });
      onDone();
    } catch (x) {
      setErr(x instanceof ApiError ? x.message : String(x));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal label={t("changePassword")} onClose={onClose}>
      <form class="field" onSubmit={save}>
        <label>{t("pwOld")}<input type="password" autoComplete="current-password" value={old} onInput={(e) => setOld((e.target as HTMLInputElement).value)} /></label>
        <label>{t("pwNew")}<input type="password" autoComplete="new-password" value={neu} onInput={(e) => setNeu((e.target as HTMLInputElement).value)} /></label>
        {err && <p class="error">{err}</p>}
        <div class="row end"><button type="submit" class="btn primary" disabled={busy || !old || !neu}>{t("pwSave")}</button></div>
      </form>
    </Modal>
  );
}
