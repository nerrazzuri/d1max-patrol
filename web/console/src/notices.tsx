// 业主的「提醒」(W44 第一期的过渡页):保安页面不显示的技术类告警(证书、硬盘、备份……)在这里看、在这里处理;
// 布防模式也先在这里切。第二期换成「家里的状况」(设计 §5.0)。
import { hm } from "./data";
import { alertTitle, lang, t, type Key } from "./i18n";
import { forGuard, type Alert, type Mode } from "./model";

export function NoticesPage(props: {
  alerts: Alert[];
  mode: Mode | null;
  onAck: (a: Alert) => void;
  onDone: (a: Alert) => void;
  onMode: (m: Mode["mode"]) => void;
}) {
  const rows = props.alerts.filter((a) => a.resolved_ms == null && !forGuard(a))
    .sort((a, b) => (a.level === b.level ? b.last_ms - a.last_ms : a.level < b.level ? -1 : 1));
  const modes: { m: Mode["mode"]; key: Key }[] = [
    { m: "armed", key: "modeArmed" }, { m: "home", key: "modeHome" }, { m: "visitor", key: "modeVisitor" },
  ];
  return (
    <section class="notices">
      <div class="row">
        <h2>{t("noticesTitle")}</h2>
        {props.mode && (
          <div class="seg push" role="group" aria-label={t("noticesMode")}>
            {modes.map((x) => (
              <button key={x.m} type="button" aria-pressed={props.mode?.mode === x.m} disabled={props.mode?.mode === x.m}
                onClick={() => props.onMode(x.m)}>{t(x.key)}</button>
            ))}
          </div>
        )}
      </div>
      {rows.length === 0 ? <p class="empty">{t("noticesEmpty")}</p> : (
        <ul class="notice-list">
          {rows.map((a) => (
            <li key={a.key} class={`notice ${a.level}`}>
              <time class="mono">{hm(a.last_ms)}</time>
              <div class="notice-body">
                <b>{alertTitle(lang.value, a.kind, a.title)}</b>
                {a.detail && <span class="muted small">{a.detail}</span>}
              </div>
              {a.acked_ms == null && <button type="button" class="btn sm" onClick={() => props.onAck(a)}>{t("noticeAck")}</button>}
              <button type="button" class="btn sm primary" onClick={() => props.onDone(a)}>{t("noticeDone")}</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
