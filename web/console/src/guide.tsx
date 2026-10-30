// 上手向导(W53 §8,用户 2026-10-31:只买设备、没买交付服务的客户要能自己学会)网页这一份:
// - 开站清单:管理首页最上面,站点按数据判断每一项做完没有,带「去做」;能「不用了」、能收起(记在账号上)。
// - 使用教程:按角色几步文字说明(网页值班台怎么用);第一次登录自动打开,以后顶栏「?」再看。
// - 常见问题:按角色挑。
import { useEffect, useState } from "preact/hooks";
import { api } from "./api";
import { Modal } from "./dialogs";
import { t, type Key } from "./i18n";

export interface Guide { tour: number; web_tour?: number; setup_hidden: boolean; skip: string[] }
interface SetupItem { id: string; done: boolean; skipped: boolean }

/** 网页导览的版本:内容大改了加一。跟 App 的分开记(``web_tour``)。 */
export const WEB_TOUR = 1;

const ITEM: Record<string, [Key, Key, string]> = {
  robots: ["suRobots", "suRobotsHint", "#/admin/robots"],
  map: ["suMap", "suMapHint", "#/admin/maps"],
  zones: ["suZones", "suZonesHint", "#/admin/layout"],
  standby: ["suStandby", "suStandbyHint", "#/admin/layout"],
  routes: ["suRoutes", "suRoutesHint", "#/admin/layout"],
  schedule: ["suSchedule", "suScheduleHint", "#/admin/layout"],
  arming: ["suArming", "suArmingHint", "#/admin/arming"],
  accounts: ["suAccounts", "suAccountsHint", "#/admin/users"],
  push: ["suPush", "suPushHint", ""],
};

export function SetupChecklist() {
  const [items, setItems] = useState<SetupItem[] | null>(null);
  const [guide, setGuide] = useState<Guide | null>(null);
  const [open, setOpen] = useState(true);
  const load = () => {
    api<{ items: SetupItem[] }>("GET", "/api/setup").then((d) => setItems(d.items ?? [])).catch(() => setItems(null));
    api<{ guide?: Guide }>("GET", "/api/me").then((d) => {
      if (d.guide) { setGuide(d.guide); setOpen(!d.guide.setup_hidden); }
    }).catch(() => {});
  };
  useEffect(load, []);
  if (!items) return null;
  const patch = async (p: Partial<Guide>) => {
    try { setGuide(await api<Guide>("POST", "/api/me/guide", p)); } catch { /* 记不上:下次还是展开的 */ }
    load();
  };
  const done = items.filter((x) => x.done || x.skipped).length;
  return (
    <section class="panel setup" aria-label={t("suTitle")}>
      <div class="row">
        <h2>{done === items.length ? t("suAllDone", { n: done, total: items.length }) : t("suCount", { n: done, total: items.length })}</h2>
        <button type="button" class="btn sm push" onClick={() => {
          const next = !open;
          setOpen(next);
          if (!next) void patch({ setup_hidden: true });
        }}>{open ? t("suHide") : t("suShow")}</button>
      </div>
      {open && (
        <ul class="plain setup-list">
          {items.map((x) => {
            const [name, hint, href] = ITEM[x.id] ?? [x.id as Key, x.id as Key, ""];
            return (
              <li key={x.id} data-item={x.id} class={x.done ? "done" : x.skipped ? "skipped" : undefined}>
                <span class="setup-mark" aria-hidden="true">{x.done ? "✓" : x.skipped ? "–" : "○"}</span>
                <span class="setup-text"><b>{t(name)}</b>{!x.done && <span class="muted small">{t(hint)}</span>}</span>
                {!x.done && !x.skipped && (
                  <>
                    <button type="button" class="btn sm" onClick={() => void patch({ skip: [...(guide?.skip ?? []), x.id] })}>{t("suSkip")}</button>
                    {href && <a class="btn sm primary" href={href}>{t("suGo")}</a>}
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

const STEPS: Record<string, Key[]> = {
  guard: ["wtDuty", "wtBanner", "wtLive", "wtMap", "wtStop"],
  owner: ["wtDuty", "wtBanner", "wtMapOwner", "wtArming", "wtStop"],
  viewer: ["wtDuty", "wtLive", "wtRecords"],
  admin: ["wtDuty", "wtBanner", "wtAdmin", "wtSetup", "wtUsers"],
  engineer: ["wtDuty", "wtAdmin", "wtSetup"],
};

const FAQ: [Key, Key, string[] | null][] = [
  ["fqAlarm", "fqAlarmA", null],
  ["fqStop", "fqStopA", ["guard", "owner", "admin", "engineer"]],
  ["fqRefused", "fqRefusedA", ["guard", "owner", "admin", "engineer"]],
  ["fqQueued", "fqQueuedA", ["guard", "owner"]],
  ["fqOffline", "fqOfflineA", null],
  ["fqUsers", "fqUsersA", ["admin"]],
  ["fqStart", "fqStartA", ["admin", "engineer"]],
];

/** 使用教程 + 常见问题(一个对话框,两段)。第一次登录自动打开,关的时候记下看过了。 */
export function HelpDialog({ role, onClose }: { role: string; onClose: () => void }) {
  const [tab, setTab] = useState<"tour" | "faq">("tour");
  const [i, setI] = useState(0);
  const steps = STEPS[role] ?? STEPS.viewer;
  const close = () => {
    void api("POST", "/api/me/guide", { web_tour: WEB_TOUR }).catch(() => {});
    onClose();
  };
  return (
    <Modal label={t("helpTitle")} onClose={close}>
      <div class="help">
        <div class="seg" role="group">
          <button type="button" aria-pressed={tab === "tour"} onClick={() => setTab("tour")}>{t("helpTour")}</button>
          <button type="button" aria-pressed={tab === "faq"} onClick={() => setTab("faq")}>{t("helpFaq")}</button>
        </div>
        {tab === "tour" ? (
          <div class="help-step">
            <p class="muted small">{i + 1} / {steps.length}</p>
            <p>{t(steps[i])}</p>
            <div class="row end">
              {i > 0 && <button type="button" class="btn" onClick={() => setI(i - 1)}>{t("helpPrev")}</button>}
              {i + 1 < steps.length
                ? <button type="button" class="btn primary" onClick={() => setI(i + 1)}>{t("helpNext")}</button>
                : <button type="button" class="btn primary" onClick={close}>{t("helpDone")}</button>}
            </div>
          </div>
        ) : (
          <div class="help-faq">
            {FAQ.filter(([, , roles]) => roles == null || roles.includes(role)).map(([q, a]) => (
              <details key={q}><summary>{t(q)}</summary><p>{t(a)}</p></details>
            ))}
          </div>
        )}
      </div>
    </Modal>
  );
}
