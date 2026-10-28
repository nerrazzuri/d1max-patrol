// 地图管理页(管理员):每张图每个版本是什么、哪几只狗在用、发布到狗、回退、删旧版本、两版对比。
// 后端见 packages/site-node/src/d1max_site/maps_admin*.py;文字走本文件里的 tr(英, 中),不碰 i18n.ts。
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api, ApiError, enc } from "./api";
import { Confirm, Modal } from "./dialogs";
import { lang } from "./i18n";
import "./mapmgmt.css";
import { toPx, type PreviewMeta } from "./mapview";

type Say = (s: string) => void;

const tr = (en: string, zh: string): string => (lang.value === "zh" ? zh : en);

export interface Build {
  source: string | number | null;
  builder: string | null;
  frames: number | null;
  loop_closure: { used: boolean; closures: number | null; why: string | null };
  handedness: { verdict: string | null; why: string | null };
}
export interface Version {
  version: string;
  created_ms: number;
  source: string;
  note: string;
  size_bytes: number;
  latest: boolean;
  build: Build | null;
  counts: { zones: number; nogo: number; slow: number; zones_rev: number; zones_confirmed: boolean | null; standby: number; intercepts: number };
  loaded_on: string[];
  previous_on: string[];
  thumbnail: string;
  has_raster: boolean;
}
export interface Activation { map_id: string; version: string; state: "pending" | "confirmed" | "failed" | "timeout"; at_ms: number; by: string; reason: string; confirmed: string | null }
export interface Running {
  legacy: boolean;
  problems: string[];
  confirms?: string | null;
  map: { dog: string | null; site: string | null; match: boolean | null } | null;
  zones: { dog_rev: number | null; site_rev: number | null; match: boolean | null; basis: "digest" | "revision" | null } | null;
  mission?: { mission: string; rev: string } | null;
}
export interface Dog {
  robot_id: string;
  online: boolean;
  can_activate: boolean;
  loaded: { map_id: string; version: string } | null;
  running: Running | null;
  activation: Activation | null;
  history: Activation[];
  rollback: { map_id: string; version: string; local: boolean } | null;
}
export interface Overview { maps: { map_id: string; versions: Version[] }[]; robots: Dog[]; now_ms: number }
export interface PublishResult { robot_id: string; ok: boolean; state: string; reason: string; code?: string | null }
export interface DiffStats {
  a: string; b: string; width: number; height: number; caveat: string; likely_misaligned: boolean;
  alignment: number | null; frames_json_same: boolean | null; same_resolution: boolean;
  area_m2: Record<string, number>; colors: Record<string, string>; warning?: string;
}

function errText(e: unknown): string {
  return tr("Failed: ", "失败:") + (e instanceof ApiError ? e.message : String(e));
}

function when(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function mb(bytes: number): string {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${(bytes / 1e6).toFixed(bytes >= 1e7 ? 0 : 1)} MB`;
}

export function activationText(a: Activation): string {
  switch (a.state) {
    case "pending":
      return tr("Waiting for the robot", "等机器人换");
    case "confirmed":
      return a.confirmed === "localizer"
        ? tr("Confirmed by the localizer", "定位器已确认")
        : a.confirmed === "load"
          ? tr("Loaded (this robot has no localizer)", "已载入(这台机器人没有定位器)")
          : tr("Reported done (old agent: did not wait for the localizer)", "机器人报已换(老代理:没等定位器确认)");
    case "failed":
      return tr("Failed", "没成") + (a.reason ? `: ${a.reason}` : "");
    default:
      return tr("No answer for too long — treat as not done", "太久没动静,当作没换成");
  }
}

// ------------------------------------------------------------------ 页面

export function MapManagePage({ say, pollMs = 5000 }: { say: Say; pollMs?: number }) {
  const [data, setData] = useState<Overview | null>(null);
  const [err, setErr] = useState("");
  const [publish, setPublish] = useState<{ map_id: string; version: string } | null>(null);
  const [deleting, setDeleting] = useState<{ map_id: string; version: string } | null>(null);
  const [rolling, setRolling] = useState<Dog | null>(null);
  const [pick, setPick] = useState<string[]>([]);
  const [diff, setDiff] = useState<{ map_id: string; a: string; b: string } | null>(null);
  const [noise, setNoise] = useState<{ map_id: string; version: string } | null>(null);
  const gen = useRef(0);

  const load = useCallback(async () => {
    const my = ++gen.current;
    try {
      const d = await api<Overview>("GET", "/api/admin/maps");
      if (my === gen.current) {
        setData(d);
        setErr("");
      }
    } catch (e) {
      if (my === gen.current) setErr(errText(e));
    }
  }, []);
  useEffect(() => {
    void load();
    if (pollMs <= 0) return;
    const id = window.setInterval(() => void load(), pollMs);
    return () => window.clearInterval(id);
  }, [load, pollMs]);

  async function saveNote(map_id: string, version: string, note: string) {
    try {
      await api("POST", `/api/admin/maps/${enc(map_id)}/${enc(version)}/note`, { note });
      say(tr("Saved", "已保存"));
    } catch (e) {
      say(errText(e));
    }
    void load();
  }

  async function doDelete(t: { map_id: string; version: string }) {
    setDeleting(null);
    try {
      const r = await api<{ freed_bytes: number }>("POST", `/api/admin/maps/${enc(t.map_id)}/${enc(t.version)}/delete`, {});
      say(tr(`Deleted ${t.map_id}:${t.version} (freed ${mb(r.freed_bytes)})`, `已删除 ${t.map_id}:${t.version}(腾出 ${mb(r.freed_bytes)})`));
      setPick((p) => p.filter((v) => v !== t.version));
    } catch (e) {
      say(errText(e));
    }
    void load();
  }

  async function doRollback(d: Dog) {
    setRolling(null);
    try {
      const r = await api<{ result: PublishResult }>("POST", "/api/admin/maps/rollback", { robot_id: d.robot_id, without_home: true });
      say(r.result.ok ? tr(`${d.robot_id}: rolling back…`, `${d.robot_id}:正在退回…`) : `${d.robot_id}: ${r.result.reason}`);
    } catch (e) {
      say(errText(e));
    }
    void load();
  }

  const robots = data?.robots ?? [];
  return (
    <div class="admin mm">
      <section class="panel">
        <div class="panel-head">
          <h1>{tr("Maps", "地图管理")}</h1>
          {err && <span class="error" role="alert">{err}</span>}
          <span class="push" />
          {pick.length === 2 && (
            <button type="button" class="btn primary" onClick={() => {
              const [a, b] = pick.map((k) => k.split("\u0000"));
              setDiff({ map_id: a[0], a: a[1], b: b[1] });
            }}>{tr("Compare the two ticked", "对比勾选的两版")}</button>
          )}
        </div>
        {data && data.maps.length === 0 && <p class="pad muted">{tr("No map has been registered yet.", "站点里还没有登记任何地图。")}</p>}
        {(data?.maps ?? []).map((m) => (
          <div key={m.map_id} class="mm-map">
            <h2 class="mm-mapid mono">{m.map_id}</h2>
            <div class="mm-vers">
              {m.versions.map((v) => (
                <VersionCard key={v.version} map_id={m.map_id} v={v}
                  picked={pick.includes(`${m.map_id}\u0000${v.version}`)}
                  onPick={(on) => setPick((p) => {
                    const key = `${m.map_id}\u0000${v.version}`;
                    const rest = p.filter((k) => k !== key && k.split("\u0000")[0] === m.map_id);
                    return on ? [...rest, key].slice(-2) : rest;
                  })}
                  onNote={(n) => void saveNote(m.map_id, v.version, n)}
                  onPublish={() => setPublish({ map_id: m.map_id, version: v.version })}
                  onDelete={() => setDeleting({ map_id: m.map_id, version: v.version })}
                  onNoise={() => setNoise({ map_id: m.map_id, version: v.version })}
                />
              ))}
            </div>
          </div>
        ))}
        {pick.length === 1 && <p class="pad muted small">{tr("Tick a second version of the same map to compare.", "再勾选同一张图的另一个版本,就能对比。")}</p>}
      </section>

      <section class="panel">
        <div class="panel-head"><h2>{tr("Robots", "每台机器人")}</h2></div>
        {robots.length === 0 && <p class="pad muted">{tr("No robots enrolled.", "没有登记的机器人。")}</p>}
        {robots.length > 0 && (
          <table class="grid mm-dogs">
            <thead>
              <tr>
                <th>{tr("Robot", "机器人")}</th><th>{tr("Loaded", "加载的")}</th><th>{tr("Last publish", "最近一次下发")}</th>
                <th>{tr("Running config", "实际在跑的配置")}</th><th>{tr("Actions", "操作")}</th>
              </tr>
            </thead>
            <tbody>
              {robots.map((d) => (
                <tr key={d.robot_id} class={d.online ? undefined : "off"}>
                  <td class="mono">{d.robot_id}{!d.online && <span class="muted small"> · {tr("offline", "不在线")}</span>}</td>
                  <td class="mono">{d.loaded ? `${d.loaded.map_id}:${d.loaded.version}` : "—"}</td>
                  <td>{d.activation ? <ActivationCell a={d.activation} /> : <span class="muted">—</span>}</td>
                  <td><RunningCell r={d.running} /></td>
                  <td class="actions">
                    <button type="button" class="btn sm" disabled={!d.rollback} onClick={() => setRolling(d)}
                      title={d.rollback ? "" : tr("No previous version known for this robot", "不知道这台机器人的上一版")}>
                      {tr("Roll back", "退回上一版")}{d.rollback ? ` → ${d.rollback.version}` : ""}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {publish && (
        <PublishDialog target={publish} dogs={robots} onClose={() => { setPublish(null); void load(); }} />
      )}
      {deleting && (
        <Confirm danger yes={tr("Delete", "删除")} onNo={() => setDeleting(null)} onYes={() => void doDelete(deleting)}
          text={tr(`Delete ${deleting.map_id}:${deleting.version}? Its files, zones, standby and intercept points are removed. This cannot be undone. Do not reuse the version name.`,
            `删除 ${deleting.map_id}:${deleting.version}?它的文件、区域、待命点、拦截点都会删掉,不能恢复。以后别再用这个版本号。`)} />
      )}
      {rolling && rolling.rollback && (
        <Confirm yes={tr("Roll back", "退回")} onNo={() => setRolling(null)} onYes={() => void doRollback(rolling)}
          text={tr(`Roll ${rolling.robot_id} back to ${rolling.rollback.map_id}:${rolling.rollback.version}? `, `把 ${rolling.robot_id} 退回到 ${rolling.rollback.map_id}:${rolling.rollback.version}?`)
            + (rolling.rollback.local
              ? tr("The robot still has it locally — nothing is downloaded.", "机器人本地还留着这一版,不用重新下载。")
              : tr("The robot may not have it any more and will download it again.", "机器人上可能已经没有这一版,会重新下载。"))} />
      )}
      {diff && <DiffDialog target={diff} onClose={() => setDiff(null)} />}
      {noise && <NoiseDialog target={noise} onClose={() => setNoise(null)}
        onDone={(r) => { setNoise(null); say(tr(`Saved as v${r.version}: ${r.cleaned} spots cleaned. Confirm its zones before publishing.`, `另存成 v${r.version}:清了 ${r.cleaned} 处。发布前先确认它的区域。`)); void load(); }} />}
    </div>
  );
}

function ActivationCell({ a }: { a: Activation }) {
  return (
    <span class={`mm-act ${a.state}`}>
      <b>{a.map_id}:{a.version}</b> · <span data-state={a.state}>{activationText(a)}</span>
      <span class="muted small"> · {a.by} {when(a.at_ms)}</span>
    </span>
  );
}

function tick(ok: boolean | null): string {
  return ok === true ? "✓" : ok === false ? "✗" : "—";
}

function RunningCell({ r }: { r: Running | null }) {
  if (r === null) return <span class="muted">{tr("no report yet", "还没报")}</span>;
  if (r.legacy) return <span class="muted">{tr("Old agent: only the zone revision is compared", "老代理:只比区域修订号")}</span>;
  return (
    <div class="mm-run">
      <span class={r.map?.match === false ? "bad" : undefined}>
        {tr("Map", "图")} {tick(r.map?.match ?? null)}{r.map?.match == null ? <span class="muted small"> {tr("(not checkable)", "(查不了)")}</span> : null}
      </span>
      <span class={r.zones?.match === false ? "bad" : undefined}>
        {tr("Zones", "区域")} {tick(r.zones?.match ?? null)}
        {r.zones?.basis === "revision" && <span class="muted small"> {tr("(old agent: revision only)", "(旧版:只比修订号)")}</span>}
      </span>
      {r.mission && <span class="muted small mono">{tr("mission", "任务")} {r.mission.mission} · {r.mission.rev}</span>}
      {r.problems.map((p, i) => <span key={i} class="bad small">{p}</span>)}
    </div>
  );
}

function VersionCard(p: {
  map_id: string; v: Version; picked: boolean; onPick: (on: boolean) => void; onNote: (n: string) => void;
  onPublish: () => void; onDelete: () => void; onNoise?: () => void;
}) {
  const { v } = p;
  // 没改过就跟着站点的备注走;改过的是草稿(存了、站点那边的备注变成它,草稿就等于它,按钮自然灰掉)
  const [draft, setDraft] = useState<string | null>(null);
  const note = draft ?? v.note;
  const inUse = v.loaded_on.length > 0;
  const whyNot = inUse ? tr("A robot is using this version", "有机器人在用这一版") : v.latest ? tr("Latest version of this map", "这张图的最新一版")
    : v.previous_on.length > 0 ? tr("A robot would roll back to this version", "是机器人的退回目标") : "";
  const b = v.build;
  return (
    <article class="mm-ver" aria-label={`${p.map_id}:${v.version}`}>
      <img class="mm-thumb" src={v.thumbnail} alt="" loading="lazy" width={120} height={120} />
      <div class="mm-body">
        <h3>
          <span class="mono">v{v.version}</span>
          {v.latest && <span class="mm-tag">{tr("latest", "最新")}</span>}
          <span class="muted small"> · {when(v.created_ms)} · {v.source} · {mb(v.size_bytes)}</span>
        </h3>
        <div class="mm-notebox">
          <input type="text" aria-label={tr(`Note for v${v.version}`, `v${v.version} 的备注`)} value={note} maxLength={200}
            placeholder={tr("Note (what changed, why)", "备注(改了什么、为什么)")} onInput={(e) => setDraft(e.currentTarget.value)} />
          <button type="button" class="btn sm" disabled={note.trim() === v.note} onClick={() => p.onNote(note)}>{tr("Save note", "保存备注")}</button>
        </div>
        {b ? (
          <p class="mm-build small">
            <span>{tr("Source", "来源")}: <b>{b.source ?? "—"}</b></span>
            <span>{tr("Builder", "建图器")}: {b.builder ?? "—"}{b.frames != null ? ` · ${b.frames} ${tr("frames", "帧")}` : ""}</span>
            <span>{tr("Loop closure", "回环")}: {b.loop_closure.used ? tr(`used (${b.loop_closure.closures ?? "?"})`, `用了(${b.loop_closure.closures ?? "?"} 处)`) : tr("not used", "没用") + (b.loop_closure.why ? ` (${b.loop_closure.why})` : "")}</span>
            <span class={b.handedness.verdict === "mirrored" ? "bad" : undefined}>
              {tr("Handedness check", "手性核对")}: {b.handedness.verdict ?? "—"}{b.handedness.verdict === "mirrored" ? tr(" — MIRRORED, do not publish", " —— 镜像了,别发布") : ""}
            </span>
          </p>
        ) : <p class="muted small">{tr("No build record (imported version).", "没有建图记录(导入的版本)。")}</p>}
        <p class="small mm-counts">
          <span>{tr("Zones", "区域")} {v.counts.zones} <span class="muted">({v.counts.nogo} {tr("no-go", "禁行")}, {v.counts.slow} {tr("slow", "限速")})</span>
            {v.counts.zones_confirmed === false && <span class="bad"> · {tr("not confirmed", "没确认")}</span>}</span>
          <span>{tr("Standby points", "待命点")} {v.counts.standby}</span>
          <span>{tr("Intercept points", "拦截点")} {v.counts.intercepts}</span>
        </p>
        <p class="small mm-dogs-on">
          {v.loaded_on.length > 0 && <span>{tr("Loaded on", "正在用")}: {v.loaded_on.map((r) => <span key={r} class="mm-chip mono">{r}</span>)}</span>}
          {v.previous_on.length > 0 && <span class="muted">{tr("Rollback target of", "是退回目标")}: {v.previous_on.map((r) => <span key={r} class="mm-chip mono">{r}</span>)}</span>}
        </p>
        <div class="row mm-actions">
          <button type="button" class="btn sm primary" onClick={p.onPublish}>{tr("Publish to robots…", "发布到机器人…")}</button>
          <button type="button" class="btn sm" disabled={!!whyNot} title={whyNot} onClick={p.onDelete}>{tr("Delete", "删除")}</button>
          {p.onNoise && <button type="button" class="btn sm" disabled={!v.has_raster} onClick={p.onNoise}>{tr("Clean noise…", "清噪点…")}</button>}
          {/* W44 第四期:地图编辑器(橡皮擦、画障碍、区域名、特殊地面、摆正、底图),另存成新的一版 */}
          {v.has_raster && <a class="btn sm" href={`#/admin/mapedit?map=${enc(p.map_id)}&version=${enc(v.version)}`}>{tr("Edit…", "编辑…")}</a>}
          <label class="field-inline small">
            <input type="checkbox" checked={p.picked} disabled={!v.has_raster} onChange={(e) => p.onPick(e.currentTarget.checked)} />
            {tr("Compare", "对比")}
          </label>
        </div>
      </div>
    </article>
  );
}

// ------------------------------------------------------------------ 发布

export function PublishDialog({ target, dogs, onClose }: { target: { map_id: string; version: string }; dogs: Dog[]; onClose: () => void }) {
  const eligible = dogs.filter((d) => d.online && d.can_activate);
  const [chosen, setChosen] = useState<string[]>([]);
  const [noHome, setNoHome] = useState(false);
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<PublishResult[] | null>(null);
  const [err, setErr] = useState("");
  async function go() {
    setBusy(true);
    setErr("");
    try {
      const r = await api<{ results: PublishResult[] }>("POST", `/api/admin/maps/${enc(target.map_id)}/${enc(target.version)}/publish`, { robots: chosen, without_home: noHome, confirm_password: pw });
      setResults(r.results);
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 403 ? tr("Wrong password.", "密码不对。") : errText(e));
      setPw("");
    }
    setBusy(false);
  }
  return (
    <Modal label={tr(`Publish ${target.map_id}:${target.version}`, `发布 ${target.map_id}:${target.version}`)} onClose={onClose}>
      {results ? (
        <>
          <ul class="mm-results">
            {results.map((r) => (
              <li key={r.robot_id} class={r.ok ? "ok" : "bad"}>
                <b class="mono">{r.robot_id}</b>{" "}
                {r.ok ? tr("Robot accepted — it is downloading / loading. Watch its status in the Robots table.", "机器人收下了,正在下载、载入。结果看「每台机器人」那张表。") : r.reason}
              </li>
            ))}
          </ul>
          <div class="row end"><button type="button" class="btn primary" onClick={onClose}>{tr("Close", "关闭")}</button></div>
        </>
      ) : (
        <>
          <fieldset class="checks">
            <legend>{tr("Robots", "发到这几台机器人")}</legend>
            {dogs.length === 0 && <span class="muted">{tr("No robots enrolled.", "没有登记的机器人。")}</span>}
            {dogs.map((d) => {
              const ok = d.online && d.can_activate;
              return (
                <label key={d.robot_id}>
                  <input type="checkbox" disabled={!ok} checked={chosen.includes(d.robot_id)}
                    onChange={(e) => setChosen((c) => (e.currentTarget.checked ? [...c, d.robot_id] : c.filter((x) => x !== d.robot_id)))} />
                  <span class="mono"> {d.robot_id}</span>
                  <span class="muted small"> · {d.loaded ? `${tr("now", "现在")} ${d.loaded.map_id}:${d.loaded.version}` : tr("no map loaded", "没加载地图")}
                    {!d.online ? ` · ${tr("offline", "不在线")}` : !d.can_activate ? ` · ${tr("cannot switch maps", "不能换图")}` : ""}</span>
                </label>
              );
            })}
          </fieldset>
          {eligible.length === 0 && dogs.length > 0 && <p class="muted small">{tr("No robot is online and able to switch maps.", "没有在线、又能换图的机器人。")}</p>}
          <label class="checks">
            <span><input type="checkbox" checked={noHome} onChange={(e) => setNoHome(e.currentTarget.checked)} /> {tr("Publish even if a robot has no home origin on this version", "机器人在这一版上没有原点也照发")}</span>
          </label>
          <p class="muted small">{tr("Each robot switches on its own: it downloads, waits until idle, loads, and (if it has a localizer) waits for the localizer to accept the map. A robot the localizer rejects goes back to its previous map.", "每台机器人各自换:下载、等空闲、载入,有定位器的还要等定位器收下新图;定位器不收的,机器人会退回原来那张。")}</p>
          {/* W44 第四期(设计 §5.2 第 4 条):换图要再输一次密码 */}
          <label class="field">{tr("Your password (switching maps matters)", "你的密码(换图要紧,再输一次)")}
            <input type="password" name="confirm_pw" autoComplete="current-password" value={pw} onInput={(e) => setPw(e.currentTarget.value)} />
          </label>
          {err && <p class="error" role="alert">{err}</p>}
          <div class="row end">
            <button type="button" class="btn" onClick={onClose}>{tr("Cancel", "取消")}</button>
            <button type="button" class="btn primary" disabled={busy || chosen.length === 0 || !pw} onClick={() => void go()}>{tr("Publish", "发布")}</button>
          </div>
        </>
      )}
    </Modal>
  );
}

// ------------------------------------------------------------------ 对比

const LEGEND: [string, string, string][] = [
  ["same_wall", "Wall in both", "两版都有的墙"],
  ["same_free", "Free in both", "两版都是空地"],
  ["wall_only_a", "Wall only in A", "只有 A 有的墙"],
  ["wall_only_b", "Wall only in B", "只有 B 有的墙"],
  ["free_only_a", "Free in A, not mapped in B", "A 是空地、B 没扫到"],
  ["free_only_b", "Not mapped in A, free in B", "A 没扫到、B 是空地"],
];

export function DiffDialog({ target, onClose }: { target: { map_id: string; a: string; b: string }; onClose: () => void }) {
  const [stats, setStats] = useState<DiffStats | null>(null);
  const [err, setErr] = useState("");
  const gen = useRef(0);
  const q = `/api/admin/maps/${enc(target.map_id)}/${enc(target.a)}/diff`;
  useEffect(() => {
    const my = ++gen.current;
    setStats(null);
    setErr("");
    api<DiffStats>("GET", `${q}?with=${enc(target.b)}`)
      .then((d) => { if (my === gen.current) setStats(d); })
      .catch((e) => { if (my === gen.current) setErr(errText(e)); });
  }, [q, target.b]);
  return (
    <Modal wide label={tr(`Compare v${target.a} (A) and v${target.b} (B)`, `对比 v${target.a}(A)和 v${target.b}(B)`)} onClose={onClose}>
      {err && <p class="error" role="alert">{err}</p>}
      {!stats && !err && <p class="muted">{tr("Rendering…", "正在画…")}</p>}
      {stats && (
        <div class="mm-diff">
          <p class="mm-caveat" role="note">{stats.caveat}</p>
          {stats.likely_misaligned && (
            <p class="mm-misaligned bad" role="alert">
              {tr(`Only ${Math.round((stats.alignment ?? 0) * 100)}% of A's wall cells have a wall of B next to them: these two versions are very likely NOT in the same coordinate frame. The picture below is not meaningful.`,
                `A 的墙只有 ${Math.round((stats.alignment ?? 0) * 100)}% 在 B 里也有墙:这两版很可能不在同一个坐标系里,下面的图没有意义。`)}
            </p>
          )}
          {stats.frames_json_same === false && <p class="small bad">{tr("The two versions have different frames.json (lidar mounting / frame conventions).", "两版的 frames.json 不一样(雷达装法、坐标约定)。")}</p>}
          {stats.frames_json_same === true && <p class="small muted">{tr("Same frames.json in both (a good sign, not a proof).", "两版的 frames.json 一样(是好兆头,不是证明)。")}</p>}
          {!stats.same_resolution && <p class="small muted">{tr("Different resolutions: B was resampled to the finer grid.", "分辨率不同:按较细的重采。")}</p>}
          <img class="mm-diffimg" alt={tr("Difference image", "差异图")} src={`${q}.png?with=${enc(target.b)}`} />
          <ul class="mm-legend small">
            {LEGEND.map(([k, en, zh]) => (
              <li key={k}>
                <span class="mm-sw" style={{ background: stats.colors[k] }} aria-hidden="true" /> {tr(en, zh)}
                <span class="muted"> · {stats.area_m2[k]?.toFixed(1)} m²</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div class="row end"><button type="button" class="btn" onClick={onClose}>{tr("Close", "关闭")}</button></div>
    </Modal>
  );
}

// ------------------------------------------------------------------ 清噪点(W41)

interface Speck { id: number; x: number; y: number; px: number }
interface NoiseList { digest: string; res: number; max_m2: number; ring_m: number; specks: Speck[] }
export interface DenoiseResult { version: string; cleaned: number; cells: number; homes_copied: number }

/** 这一版里「零星噪点」的候选画在图上(红圈),点一下去掉、再点加回;确认后清掉、另存成新的一版。 */
export function NoiseDialog({ target, onClose, onDone }: {
  target: { map_id: string; version: string }; onClose: () => void; onDone: (r: DenoiseResult) => void;
}) {
  const base = `/api/admin/maps/${enc(target.map_id)}/${enc(target.version)}`;
  const view = `/api/maps/${enc(target.map_id)}/${enc(target.version)}`;
  const [list, setList] = useState<NoiseList | null>(null);
  const [meta, setMeta] = useState<PreviewMeta | null>(null);
  const [skip, setSkip] = useState<Set<number>>(new Set());
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // 外审 R6:预览(元数据、底图)拿不到就不许清 —— 看不到图就挑不了「细杆子留着」
  const [previewErr, setPreviewErr] = useState("");
  const [tries, setTries] = useState(0);
  // 复查 N3:底图(preview.png)真加载好了才算看得到;重试换个地址真的重拉
  const [imgOk, setImgOk] = useState(false);
  useEffect(() => {
    let live = true;
    api<NoiseList>("GET", `${base}/noise`).then((d) => live && setList(d)).catch((e) => live && setErr(errText(e)));
    return () => { live = false; };
  }, [base]);
  useEffect(() => {
    let live = true;
    setPreviewErr("");
    setImgOk(false);
    api<PreviewMeta>("GET", `${view}/preview`).then((m) => live && setMeta(m))
      .catch((e) => { if (live) { setMeta(null); setPreviewErr(errText(e)); } });
    return () => { live = false; };
  }, [view, tries]);
  const n = list ? list.specks.filter((s) => !skip.has(s.id)).length : 0;
  const toggle = (id: number) => setSkip((cur) => { const next = new Set(cur); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  async function go() {
    if (!list) return;
    setBusy(true);
    try {
      onDone(await api<DenoiseResult>("POST", `${base}/denoise`, { digest: list.digest, skip: [...skip] }));
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal wide label={tr(`Clean noise in v${target.version}`, `清 v${target.version} 的噪点`)} onClose={onClose}>
      {err && <p class="error" role="alert">{err}</p>}
      {previewErr && (
        <p class="error" role="alert">
          {tr(`Map preview unavailable (${previewErr}) — can't clean without seeing the spots.`, `地图预览拿不到(${previewErr}):看不到图就不能清。`)}{" "}
          <button type="button" class="btn" data-noise-retry="1" onClick={() => setTries((t) => t + 1)}>{tr("Retry", "重试")}</button>
        </p>
      )}
      {!list && !err && <p class="muted">{tr("Looking for noise…", "正在找…")}</p>}
      {list && (
        <div class="mm-noise">
          <p class="small">{tr(
            `Red circles: tiny isolated obstacle specks (≤ ${list.max_m2} m², open floor all around within ${list.ring_m} m) — usually people walking while the map was built. They block the planner. Click a circle to keep that one (e.g. a thin post).`,
            `红圈:很小、四周 ${list.ring_m} 米都是空地的占用格(每处不超过 ${list.max_m2} 平方米)—— 多半是建图时有人走动留下的,会把规划的路封住。是真东西(比如细杆子)就点一下那个圈,留着它。`)}</p>
          {list.specks.length === 0 ? <p class="muted">{tr("No noise found in this version.", "这一版没找到噪点。")}</p> : meta && (
            <svg class="mm-noiseimg" viewBox={`0 0 ${meta.width} ${meta.height}`} role="img" aria-label={tr("Noise on the map", "图上的噪点")}>
              <image href={`${view}/preview.png${tries ? `?try=${tries}` : ""}`} x={0} y={0} width={meta.width} height={meta.height}
                onLoad={() => setImgOk(true)}
                onError={() => { setImgOk(false); setPreviewErr(tr("map image failed to load", "底图加载失败")); }} />
              {list.specks.map((s) => {
                const p = toPx(meta, s.x, s.y);
                const kept = skip.has(s.id);
                return <circle key={s.id} data-speck={s.id} data-kept={kept ? "1" : "0"} cx={p.px} cy={p.py} r={6}
                  fill="none" stroke={kept ? "#888" : "#e53935"} stroke-width={2} stroke-dasharray={kept ? "3 2" : undefined}
                  style={{ cursor: "pointer" }} onClick={() => toggle(s.id)}><title>{`#${s.id} (${s.x}, ${s.y}) · ${s.px}`}</title></circle>;
              })}
            </svg>
          )}
        </div>
      )}
      <div class="row end">
        <button type="button" class="btn" onClick={onClose}>{tr("Cancel", "取消")}</button>
        <button type="button" class="btn primary" data-denoise="1" disabled={!list || n === 0 || busy || !meta || !imgOk || !!previewErr} onClick={() => void go()}>
          {tr(`Clean ${n} spots → save as a new version`, `清掉 ${n} 处,另存成新的一版`)}
        </button>
      </div>
    </Modal>
  );
}
