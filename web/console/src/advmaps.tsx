// 高级 → 地图(W44 第四期,设计 §5.3「地图」):什么时候用 —— 家具挪动很多,或者机器人老是迷路。
// 列出所有地图和版本、在用的是哪张、哪几台在用、发布(再输密码)、退回、对比、删、清噪点、编辑(mapmgmt.tsx,外审过的那一页);
// 上面加「重新建图」的步骤(不自动动机器人:第 2 步在手机上遥控)。
import { useState } from "preact/hooks";
import { t, type Key } from "./i18n";
import { MapManagePage } from "./mapmgmt";

const STEPS: Key[] = ["rbStep1", "rbStep2", "rbStep3", "rbStep4", "rbStep5"];

export function AdvMapsPage({ say }: { say: (s: string) => void }) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(0);
  return (
    <section class="advmaps">
      <p class="muted lead">{t("mapsWhen")}</p>
      <section class="panel rebuild">
        <button type="button" class="linklike" aria-expanded={open} onClick={() => setOpen(!open)}>{t("rbTitle")} {open ? "▾" : "▸"}</button>
        {open && (
          <ol class="rb-steps">
            {STEPS.map((k, i) => (
              <li key={k} class={i < done ? "done" : i === done ? "now" : undefined}>
                <span>{t(k)}</span>
                {i === done && i < STEPS.length - 1 && <button type="button" class="btn sm" onClick={() => setDone(i + 1)}>{t("rbDone")}</button>}
              </li>
            ))}
          </ol>
        )}
      </section>
      <MapManagePage say={say} />
    </section>
  );
}
