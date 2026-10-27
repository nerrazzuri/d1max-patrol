// 「再输一次密码」(W44 第二期,设计 §5.2 第 4 条):开工程师账号、解绑机器人这类会出大事的操作。
// 站点判密码(跟登录同一个失败计数);不对写「密码不对」、锁了写「错太多次」,对话框不关,能再输。
import { useState } from "preact/hooks";
import { ApiError } from "./api";
import { Modal } from "./dialogs";
import { t } from "./i18n";

export function ConfirmPassword({ title, onConfirm, onClose }: { title: string; onConfirm: (pw: string) => Promise<void>; onClose: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: Event) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await onConfirm(pw);
    } catch (x) {
      setErr(x instanceof ApiError && x.status === 403 ? t("pwWrong")
        : x instanceof ApiError && x.status === 429 ? t("pwLocked")
        : t("failed", { why: x instanceof ApiError ? x.message : String(x) }));
      setPw("");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal label={title} onClose={onClose}>
      <form class="form confirm-pw" onSubmit={submit}>
        <p class="muted small">{t("pwAgainHint")}</p>
        <label class="field">{t("pwAgain")}<input type="password" autoComplete="current-password" value={pw} required
          onInput={(e) => setPw(e.currentTarget.value)} /></label>
        {err && <p class="error" role="alert">{err}</p>}
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={busy || !pw}>{t("pwConfirm")}</button>
        </div>
      </form>
    </Modal>
  );
}
