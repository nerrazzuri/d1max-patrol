import { useState } from "preact/hooks";
import { api, ApiError } from "./api";
import type { Me } from "./app";
import { t } from "./i18n";
import { LangSwitch } from "./langswitch";

export function Login({ onIn, expired }: { onIn: (m: Me) => void; expired: boolean }) {
  const [name, setName] = useState("");
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit(e: Event) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await api("POST", "/api/login", { name, password: pw, web: true });
      onIn(await api<Me>("GET", "/api/me"));
    } catch (x) {
      const s = x instanceof ApiError ? x.status : 0;
      setErr(s === 401 ? t("badLogin") : s === 429 ? t("lockedOut") : t("siteUnreachable"));
    } finally {
      setBusy(false);
      setPw("");
    }
  }

  return (
    <main class="login">
      <div class="login-top"><LangSwitch /></div>
      <form class="login-card" onSubmit={submit}>
        <div class="brand"><span class="mark">D1</span><span>{t("appName")}</span></div>
        <h1>{t("dutyConsole")}</h1>
        {expired && <p class="note" role="status">{t("sessionExpired")}</p>}
        <label>
          {t("account")}
          <input autocomplete="username" value={name} onInput={(e) => setName(e.currentTarget.value)} required />
        </label>
        <label>
          {t("password")}
          <input type="password" autocomplete="current-password" value={pw}
            onInput={(e) => setPw(e.currentTarget.value)} required />
        </label>
        {err && <p class="error" role="alert">{err}</p>}
        <button class="btn primary" type="submit" disabled={busy}>{busy ? t("signingIn") : t("signIn")}</button>
      </form>
    </main>
  );
}
