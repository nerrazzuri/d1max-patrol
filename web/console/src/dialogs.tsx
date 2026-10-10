import type { ComponentChildren } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { t } from "./i18n";
import { Icon } from "./icons";

export function Modal({ label, onClose, wide, children }: { label: string; onClose: () => void; wide?: boolean; children: ComponentChildren }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);
  return (
    <dialog ref={ref} class={wide ? "modal wide" : "modal"} aria-label={label}
      onCancel={(e) => { e.preventDefault(); onClose(); }}>
      <div class="modal-head">
        <h2>{label}</h2>
        <button type="button" class="btn icon" aria-label={t("close")} onClick={onClose}><Icon name="x" /></button>
      </div>
      {children}
    </dialog>
  );
}

export function Confirm(props: { text: string; yes: string; danger?: boolean; onYes: () => void; onNo: () => void }) {
  return (
    <Modal label={props.yes} onClose={props.onNo}>
      <p class="confirm-text">{props.text}</p>
      <div class="row end">
        <button type="button" class="btn" onClick={props.onNo}>{t("cancel")}</button>
        <button type="button" class={props.danger ? "btn stop" : "btn primary"} onClick={props.onYes}>{props.yes}</button>
      </div>
    </Modal>
  );
}

/** 搁置对话框(P2):1 小时 / 到 08:00 / 原因必填。回 until_ms 和原因。 */
export function ShelveDialog({ now, onShelve, onClose }: { now: number; onShelve: (untilMs: number, reason: string) => void; onClose: () => void }) {
  const [choice, setChoice] = useState<"1h" | "8am">("1h");
  const [reason, setReason] = useState("");
  const eight = new Date(now);
  eight.setHours(8, 0, 0, 0);
  if (eight.getTime() <= now) eight.setDate(eight.getDate() + 1);
  const until = choice === "1h" ? now + 3600_000 : eight.getTime();
  return (
    <Modal label={t("shelveTitle")} onClose={onClose}>
      <form class="shelve" onSubmit={(e) => { e.preventDefault(); if (reason.trim()) onShelve(until, reason.trim()); }}>
        <fieldset>
          <legend class="sr-only">{t("shelveTitle")}</legend>
          <label><input type="radio" name="until" checked={choice === "1h"} onChange={() => setChoice("1h")} /> {t("shelve1h")}</label>
          <label><input type="radio" name="until" checked={choice === "8am"} onChange={() => setChoice("8am")} /> {t("shelveTill8")}</label>
        </fieldset>
        <label class="field">
          {t("shelveReason")}
          <input value={reason} maxLength={200} required onInput={(e) => setReason(e.currentTarget.value)} />
        </label>
        <div class="row end">
          <button type="button" class="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="submit" class="btn primary" disabled={!reason.trim()}>{t("shelve").replace("…", "")}</button>
        </div>
      </form>
    </Modal>
  );
}
