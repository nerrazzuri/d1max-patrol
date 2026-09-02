"""巡检报告:Markdown 与**自包含** HTML。

自包含是硬要求:变电站现场经常没网,报告要能拷到 U 盘上、在别人笔记本里
双击打开就能看,包括每一张照片。所以 HTML 里的图片一律 base64 内嵌,
外链一个都不许有。

**没有模板引擎。** 纯字符串拼接 + ``html.escape``。引 jinja2 就多一个
第三方运行时依赖(全局约束),而这里要拼的东西就这么点。

**报告只读归档目录。** 它不认识引擎、不认识后端,给一个跑完的 run 目录就
出得来 —— 所以历史运行随时能重新出报告。
"""

from __future__ import annotations

import base64
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from d1max_patrol.engine.archive import read_manifest, read_state

#: 判读结论的中文说法。``pending`` 是"还没判",不是"判了但没结论"。
VERDICT_CN = {
    "normal": "正常",
    "abnormal": "异常",
    "unclear": "存疑",
    "pending": "待判读",
}

#: 按扩展名给 data URI 用的类型。报告里只会出现照片。
_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


@dataclass(frozen=True, slots=True)
class PhotoRow:
    """报告里的一张照片,连同它的两层结论。"""

    file: str
    waypoint: str
    camera: str
    model_verdict: str = "pending"
    model_confidence: float = 0.0
    model_reason: str = ""
    human_verdict: str = ""
    human_note: str = ""

    @property
    def final_verdict(self) -> str:
        """人改过就以人的为准 —— 复核的意义就在这里。"""
        return self.human_verdict or self.model_verdict


def _split_photo_name(name: str) -> tuple[str, str]:
    """从 ``<点位>__<相机>__<时间戳>.jpg`` 里拆出点位和相机。

    拆不开就把整个文件名当点位名。报告宁可难看一点,也不能因为多了一个
    不认识的文件就出不来。
    """
    stem = Path(name).stem
    parts = stem.split("__")
    if len(parts) >= 3:
        return parts[0], parts[1]
    return stem, ""


def _load_json(path: Path) -> tuple[Any, str]:
    """读一个可选的 JSON 文件。返回 (内容, 出错说明)。

    读不了不静默当成空:把出错说明摆进报告里。悄悄少一段判读结果,比
    报告里写一行"findings.json 读不了"危险得多。
    """
    if not path.exists():
        return None, ""
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path.name} 读不了: {exc}"


def collect_photos(run_dir: Path) -> tuple[list[PhotoRow], list[str]]:
    """把照片、模型结论、人工复核合成一张表。返回 (行, 出错说明)。"""
    run_dir = Path(run_dir)
    photos_dir = run_dir / "photos"
    files = sorted(p.name for p in photos_dir.iterdir()
                   if p.is_file()) if photos_dir.is_dir() else []

    notes: list[str] = []
    findings, err = _load_json(run_dir / "findings.json")
    if err:
        notes.append(err)
    reviews, err = _load_json(run_dir / "review.json")
    if err:
        notes.append(err)

    by_photo: dict[str, dict[str, Any]] = {}
    for item in findings or []:
        if isinstance(item, dict) and item.get("photo"):
            by_photo[str(item["photo"])] = item

    rows: list[PhotoRow] = []
    for name in files:
        waypoint, camera = _split_photo_name(name)
        f = by_photo.get(name, {})
        r = (reviews or {}).get(name, {}) if isinstance(reviews, dict) else {}
        rows.append(PhotoRow(
            file=name, waypoint=waypoint, camera=camera,
            model_verdict=str(f.get("verdict", "pending")),
            model_confidence=float(f.get("confidence", 0.0) or 0.0),
            model_reason=str(f.get("reason", "")),
            human_verdict=str(r.get("verdict", "")),
            human_note=str(r.get("note", "")),
        ))
    return rows, notes


def _cn(verdict: str) -> str:
    return VERDICT_CN.get(verdict, verdict or "待判读")


def _summary(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """从 manifest / state 里凑出汇总。跑到一半崩掉的运行也要出得了报告。"""
    manifest = read_manifest(run_dir) or {}
    summary = dict(manifest.get("summary") or {})
    results = list(summary.get("results") or [])
    if not results:
        # manifest 里没有汇总,说明这趟没走到收尾 —— 退回 state.json。
        state = read_state(run_dir) or {}
        results = list(state.get("results") or [])
        summary.setdefault("state", state.get("state", "未知"))
        summary.setdefault("reason", state.get("reason", ""))
    return manifest, results, str(summary.get("state", "未知"))


# --------------------------------------------------------------------- Markdown


def build_markdown(run_dir: Path) -> str:
    run_dir = Path(run_dir)
    manifest, results, state = _summary(run_dir)
    mission = (manifest.get("mission") or {}).get("mission", run_dir.parent.name)
    rows, notes = collect_photos(run_dir)

    succeeded = sum(1 for r in results if r.get("ok"))
    failed = len(results) - succeeded
    total_s = sum(float(r.get("elapsed_s", 0.0) or 0.0) for r in results)

    out: list[str] = [f"# 巡检报告: {mission}", ""]
    out += [
        f"- 开始时间: {manifest.get('started_at', '未知')}",
        f"- 结束状态: {state}",
        f"- 成功 {succeeded} 个,失败 {failed} 个,共 {len(results)} 个点位",
        f"- 总时长: {total_s:.1f}s",
    ]
    reason = (manifest.get("summary") or {}).get("reason", "")
    if reason:
        out.append(f"- 结束原因: {reason}")
    fingerprint = manifest.get("fingerprint") or {}
    if fingerprint:
        joined = ", ".join(f"{k}={v}" for k, v in sorted(fingerprint.items()))
        out.append(f"- 环境: {joined}")
    out.append("")

    for note in notes:
        out += [f"> **{note}**", ""]

    out += ["## 点位", "",
            "| 点位 | 结果 | 用时 | 照片 | 说明 |",
            "| --- | --- | --- | --- | --- |"]
    if not results:
        out.append("| (没有点位记录) | | | | |")
    for r in results:
        out.append("| {} | {} | {:.1f}s | {} | {} |".format(
            r.get("name", "?"), "成功" if r.get("ok") else "失败",
            float(r.get("elapsed_s", 0.0) or 0.0),
            len(r.get("photos") or []), r.get("note", "")))
    out.append("")

    out += ["## 判读", ""]
    if not rows:
        out += ["(这趟没有照片)", ""]
    for row in rows:
        title = f"{row.waypoint} / {row.camera}" if row.camera else row.waypoint
        out += [f"### {title}", "", f"![{row.file}](photos/{row.file})", ""]
        model = _cn(row.model_verdict)
        if row.model_verdict != "pending":
            model += f"(置信度 {row.model_confidence:.2f})"
        if row.model_reason:
            model += f" —— {row.model_reason}"
        out.append(f"- 模型判读: {model}")
        if row.human_verdict:
            human = _cn(row.human_verdict)
            if row.human_note:
                human += f" —— {row.human_note}"
            out.append(f"- 人工复核: {human}")
        else:
            out.append("- 人工复核: 尚未复核")
        out += [f"- 最终结论: **{_cn(row.final_verdict)}**", ""]
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------------ HTML

_CSS = """
body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;
padding:0 1rem;line-height:1.6;color:#222}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ccc;padding:.4rem .6rem;text-align:left}
th{background:#f2f2f2}
img{max-width:100%;border:1px solid #ddd}
.bad{color:#b00}.ok{color:#070}.pending{color:#888}
.card{border:1px solid #ddd;border-radius:6px;padding:1rem;margin:1rem 0}
"""

_CLASS = {"正常": "ok", "异常": "bad", "存疑": "bad", "待判读": "pending"}


def _e(text: Any) -> str:
    """一切进 HTML 的东西都过这里。

    点位名在 Task 1 已经挡过路径分隔符,这是第二道 —— 照片文件也可能是
    别人手工塞进 photos/ 的。
    """
    return html.escape(str(text), quote=True)


def _data_uri(path: Path) -> str:
    mime = _MIME.get(path.suffix.lower(), "application/octet-stream")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_html(run_dir: Path) -> str:
    run_dir = Path(run_dir)
    manifest, results, state = _summary(run_dir)
    mission = (manifest.get("mission") or {}).get("mission", run_dir.parent.name)
    rows, notes = collect_photos(run_dir)

    succeeded = sum(1 for r in results if r.get("ok"))
    failed = len(results) - succeeded
    total_s = sum(float(r.get("elapsed_s", 0.0) or 0.0) for r in results)

    out: list[str] = [
        "<!doctype html>", '<html lang="zh-CN">', "<head>",
        # 断网也要能看,所以 charset 必须写死在文件里,不能靠服务端响应头。
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>巡检报告 {_e(mission)}</title>",
        f"<style>{_CSS}</style>", "</head>", "<body>",
        f"<h1>巡检报告: {_e(mission)}</h1>", "<ul>",
        f"<li>开始时间: {_e(manifest.get('started_at', '未知'))}</li>",
        f"<li>结束状态: {_e(state)}</li>",
        f"<li>成功 {succeeded} 个,失败 {failed} 个,共 {len(results)} 个点位</li>",
        f"<li>总时长: {total_s:.1f}s</li>",
    ]
    reason = (manifest.get("summary") or {}).get("reason", "")
    if reason:
        out.append(f"<li>结束原因: {_e(reason)}</li>")
    for key, value in sorted((manifest.get("fingerprint") or {}).items()):
        out.append(f"<li>{_e(key)}: {_e(value)}</li>")
    out.append("</ul>")

    for note in notes:
        out.append(f'<p class="bad"><strong>{_e(note)}</strong></p>')

    out += ["<h2>点位</h2>", "<table>",
            "<tr><th>点位</th><th>结果</th><th>用时</th><th>照片</th>"
            "<th>说明</th></tr>"]
    for r in results:
        cls = "ok" if r.get("ok") else "bad"
        out.append(
            f"<tr><td>{_e(r.get('name', '?'))}</td>"
            f'<td class="{cls}">{"成功" if r.get("ok") else "失败"}</td>'
            f"<td>{float(r.get('elapsed_s', 0.0) or 0.0):.1f}s</td>"
            f"<td>{len(r.get('photos') or [])}</td>"
            f"<td>{_e(r.get('note', ''))}</td></tr>")
    if not results:
        out.append('<tr><td colspan="5">(没有点位记录)</td></tr>')
    out.append("</table>")

    out.append("<h2>判读</h2>")
    if not rows:
        out.append("<p>(这趟没有照片)</p>")
    for row in rows:
        title = f"{row.waypoint} / {row.camera}" if row.camera else row.waypoint
        final = _cn(row.final_verdict)
        out += ['<div class="card">', f"<h3>{_e(title)}</h3>"]
        photo = run_dir / "photos" / row.file
        try:
            out.append(f'<img alt="{_e(row.file)}" src="{_data_uri(photo)}">')
        except OSError as exc:
            out.append(f'<p class="bad">{_e(f"照片读不了: {exc}")}</p>')
        model = _cn(row.model_verdict)
        if row.model_verdict != "pending":
            model += f"(置信度 {row.model_confidence:.2f})"
        if row.model_reason:
            model += f" —— {row.model_reason}"
        out.append(f"<p>模型判读: {_e(model)}</p>")
        if row.human_verdict:
            human = _cn(row.human_verdict)
            if row.human_note:
                human += f" —— {row.human_note}"
            out.append(f"<p>人工复核: {_e(human)}</p>")
        else:
            out.append('<p class="pending">人工复核: 尚未复核</p>')
        out += [f'<p class="{_CLASS.get(final, "pending")}">最终结论: '
                f"<strong>{_e(final)}</strong></p>", "</div>"]

    out += ["</body>", "</html>"]
    return "\n".join(out) + "\n"


def write_reports(run_dir: Path) -> tuple[Path, Path]:
    """出两份:``report.md`` 给 git 和终端看,``report.html`` 给人看。"""
    run_dir = Path(run_dir)
    md = run_dir / "report.md"
    htm = run_dir / "report.html"
    md.write_text(build_markdown(run_dir), encoding="utf-8")
    htm.write_text(build_html(run_dir), encoding="utf-8")
    return md, htm
