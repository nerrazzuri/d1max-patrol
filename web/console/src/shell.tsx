// 值班台外壳(W44 第一期,设计 §4.0):路由、顶栏、用户菜单、按页面挂深色 / 浅色。
// 顶栏只有:品牌和站点名、页面导航、报警数、布防状态(只显示)、时钟、用户名下拉(语言、外观、改密码、退出)、全部急停。
import { useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError } from "./api";
import type { Me } from "./app";
import { Modal } from "./dialogs";
import { hm } from "./data";
import { lang, setLang, t, type Key } from "./i18n";
import { Icon } from "./icons";
import { canDispatch, type Mode } from "./model";

export type GuardPage = "duty" | "live" | "map" | "recordings" | "alarms";
export type AdminPage =
  | "admin/notices" | "admin/robots" | "admin/zones" | "admin/maps" | "admin/routes" | "admin/users"
  | "admin/lists" | "admin/system" | "admin/localization" | "admin/reports";
export type Page = GuardPage | AdminPage;

export const GUARD_PAGES: GuardPage[] = ["duty", "live", "map", "recordings", "alarms"];
const ADMIN_PAGES: AdminPage[] = ["admin/notices", "admin/robots", "admin/zones", "admin/maps", "admin/routes",
  "admin/users", "admin/lists", "admin/system", "admin/localization", "admin/reports"];
/** 改版前的地址(书签、手机分享的链接)。 */
const LEGACY: Record<string, Page> = { localization: "admin/localization", reports: "admin/reports" };

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

/** 角色的界面名字(设计 §3):admin 叫业主,现在的 owner 叫家人 / 只看。 */
export function roleLabel(role: string): Key {
  return role === "admin" ? "roleOwner" : role === "guard" ? "roleGuard" : "roleFamily";
}

/** 页面默认的明暗(设计 §2.2):保安页深色,管理页浅色。 */
export function pageScheme(p: Page): "dark" | "light" {
  return isAdminPage(p) ? "light" : "dark";
}

/** 个人在「外观」里的选择:按页面(默认)、跟随系统、一直深色、一直浅色。 */
export type ThemePref = "auto" | "system" | "dark" | "light";
const PREF_KEY = "d1max.look";

export function schemeFor(p: Page, pref: ThemePref, systemDark: boolean): "dark" | "light" {
  if (pref === "dark" || pref === "light") return pref;
  if (pref === "system") return systemDark ? "dark" : "light";
  return pageScheme(p);
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

const NAV: { id: GuardPage; key: Key }[] = [
  { id: "duty", key: "tabDuty" },
  { id: "live", key: "tabLive" },
  { id: "map", key: "tabMap" },
  { id: "recordings", key: "tabRecordings" },
  { id: "alarms", key: "tabAlarms" },
];

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
}) {
  return (
    <header class="topbar">
      <a class="skip" href="#main">Skip to content</a>
      <span class="wordmark">Nexus Warden</span>
      <span class="site muted" title={props.me.site_name}>{props.me.site_name}</span>
      <nav class="tabsnav" aria-label={t("mainNav")}>
        {NAV.map((x) => (
          <a key={x.id} href={`#/${x.id}`} aria-current={props.page === x.id ? "page" : undefined}>{t(x.key)}</a>
        ))}
        {props.me.role === "admin" && (
          <>
            <span class="navsep" aria-hidden="true" />
            <a href="#/admin/notices" aria-current={isAdminPage(props.page) ? "page" : undefined}>{t("adminGroup")}</a>
          </>
        )}
      </nav>
      <span class={props.alarms ? "alarmcount on push" : "alarmcount push"} aria-live="polite">
        {t("alarmCount", { n: props.alarms == null ? "?" : props.alarms })}
      </span>
      {props.mode && <ArmState mode={props.mode} />}
      <span class="clock mono">{new Date(props.now).toLocaleTimeString("en-GB", { hour12: false })}</span>
      <UserMenu me={props.me} pref={props.pref} onPref={props.onPref} onSignOut={props.onSignOut} onSay={props.onSay} />
      {(canDispatch(props.me.role) || props.me.role === "owner") && (
        <button type="button" class="btn stop" onClick={props.onStop}><Icon name="octagon" />{t("stopAll")}</button>
      )}
    </header>
  );
}

/** 布防状态:只显示(设计 §4.0,用户说的),切模式在别处。 */
function ArmState({ mode }: { mode: Mode }) {
  const text = mode.mode === "armed" ? t("armArmed")
    : mode.mode === "home" ? t("armHome")
    : t("armVisitor", { t: mode.until_ms ? hm(mode.until_ms) : "—" });
  return <span class={`armstate ${mode.mode}`}><i class="dot" aria-hidden="true" />{text}</span>;
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

const ADMIN_NAV: { id: AdminPage; key: Key }[] = [
  { id: "admin/notices", key: "tabNotices" },
  { id: "admin/robots", key: "tabRobotsAdmin" },
  { id: "admin/zones", key: "tabMapsZones" },
  { id: "admin/maps", key: "tabMapMgmt" },
  { id: "admin/routes", key: "tabRoutes" },
  { id: "admin/users", key: "tabUsers" },
  { id: "admin/lists", key: "tabLists" },
  { id: "admin/localization", key: "tabLocalization" },
  { id: "admin/reports", key: "tabReports" },
  { id: "admin/system", key: "tabSystem" },
];

export function AdminNav({ page, notices }: { page: Page; notices: number }) {
  return (
    <nav class="subnav" aria-label={t("adminGroup")}>
      {ADMIN_NAV.map((x) => (
        <a key={x.id} href={`#/${x.id}`} aria-current={page === x.id ? "page" : undefined}>
          {t(x.key)}{x.id === "admin/notices" && notices > 0 ? <span class="badge">{notices}</span> : null}
        </a>
      ))}
    </nav>
  );
}
