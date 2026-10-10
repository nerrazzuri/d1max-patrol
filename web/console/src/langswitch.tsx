import { lang, setLang, t } from "./i18n";

export function LangSwitch() {
  return (
    <div class="seg" role="group" aria-label={t("language")}>
      <button type="button" aria-pressed={lang.value === "en"} onClick={() => setLang("en")}>EN</button>
      <button type="button" aria-pressed={lang.value === "zh"} onClick={() => setLang("zh")}>中文</button>
    </div>
  );
}
