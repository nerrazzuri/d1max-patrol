"""真机一致性巡检 —— 对着真机把只读接口全跑一遍,录帧,并跟 golden fixture 逐字段对拍。

**为什么要有这个模块,而不是现场手敲命令行:**

真机在客户那边,窗口只有一到两天。手敲二十几条 CLI、肉眼比对返回值,既慢
又必然漏 —— 而漏掉的那条恰好就是回来才发现不对的那条。这里把整个只读扫描
做成一条命令: 一次跑完、全程录原始帧、自动出字段级差异报告。

**分级与安全:**

阶段 0/1 是**纯只读**,不会让机器动一下,任何时候都可以跑。
阶段 4/5 会让机器建图和走路,必须显式开 `allow_motion`,默认不跑。
这个分界是硬的: 现场的机器旁边站着人,误触发一次移动的代价不是"重跑"。

**产出**(缺一不可):

* ``frames-*.jsonl``        原始帧录像 —— 见 recorder.py。**这是核心产出。**
  报告和差异表以后随时能从录像重算,录像本身错过就没了。
* ``conformance.json``      机器可读的结果,供离线脚本消费。
* ``conformance-report.md`` 人读的报告,字段级差异逐条列出。
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from d1max_patrol.backends.base import NavBackend, NavBackendError
from d1max_patrol.recorder import FrameRecorder

#: golden fixture 相对仓库根的位置。真机现场跑的是安装好的包,fixture 却在
#: tests/ 里 —— 所以路径要能被调用方覆盖(见 load_fixtures 的 root 参数)。
FIXTURE_DIR = Path("tests/protocol/fixtures")

#: 阶段 0 要探的两个网段。SDK 那条本卷没有实现,但**可达性本身就是要带回去的
#: 数据** —— 明天验不了,至少要知道网通不通、以后还要不要为它单独接线。
DEFAULT_PROBES: tuple[tuple[str, str, int], ...] = (
    ("nav", "192.168.144.100", 10010),
    ("sdk", "192.168.234.1", 8081),
)


# --------------------------------------------------------------- 结果模型


@dataclass
class Probe:
    """一次 TCP 可达性探测的结果。"""

    name: str
    host: str
    port: int
    ok: bool
    detail: str


@dataclass
class Call:
    """一次只读接口调用的结果。"""

    #: 我们这一侧的接口名(NavBackend 的方法名),不是厂商的 req_func。
    op: str
    ok: bool
    #: 成功时是返回值的 repr;失败时是异常信息。
    detail: str
    elapsed_s: float


@dataclass
class SchemaDiff:
    """一个 resp_func 的真机报文 vs golden fixture 的字段级差异。"""

    resp_func: str
    fixture_files: list[str]
    #: fixture 里有、真机没有的字段路径
    missing_in_real: list[str] = field(default_factory=list)
    #: 真机有、fixture 里没有的字段路径(厂商加了字段,或文档没写全)
    extra_in_real: list[str] = field(default_factory=list)
    #: 两边都有但类型不同: path -> "fixture=<t> real=<t>"
    type_mismatch: dict[str, str] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (self.missing_in_real or self.extra_in_real or self.type_mismatch)


@dataclass
class ConformanceResult:
    """一次巡检的全部结果。"""

    started_at: str
    nav_url: str
    allow_motion: bool
    probes: list[Probe] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    diffs: list[SchemaDiff] = field(default_factory=list)
    #: 巡检器自己遇到的、不属于任何单条调用的问题
    notes: list[str] = field(default_factory=list)
    recording_path: str | None = None
    recorded_frames: int = 0

    def to_json(self) -> dict[str, Any]:
        def conv(x: Any) -> Any:
            if hasattr(x, "__dataclass_fields__"):
                return {k: conv(getattr(x, k)) for k in x.__dataclass_fields__}
            if isinstance(x, list):
                return [conv(i) for i in x]
            if isinstance(x, dict):
                return {k: conv(v) for k, v in x.items()}
            return x

        return conv(self)


# ------------------------------------------------------------- 结构对拍


def json_shape(value: Any, prefix: str = "") -> dict[str, str]:
    """把一个 JSON 值摊平成 ``字段路径 -> 类型名``。

    数组压成一个 ``array`` 节点加上第一个元素的形状 —— 把每个元素的路径都
    写一遍的话,"地图列表返回了 3 张而不是 1 张"会被当成 schema 差异刷出
    一屏,而它根本不是 schema 差异。差异表要报的是**形状**不同,不是**长度**
    不同。

    **已知局限**: 厂商有几个接口拿**地图名当字典键**(get_all_pgm_map 的
    ``data`` 就是 ``{<地图名>: [...]}``)。这种动态键会原样出现在字段路径里,
    看报告的人会看到 ``data.req_result.data.一号楼.info.width`` 这样的路径。
    那是地图名,不是 schema 差异。目前这几个接口在文档里根本没有响应样本
    (对拍时走的是"文档缺样本"分支),所以还构不成噪声;等文档补齐了再处理。
    """
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(json_shape(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(value, list):
        out[prefix or "$"] = "array"
        if value:
            out.update(json_shape(value[0], f"{prefix}[]"))
    else:
        out[prefix or "$"] = type(value).__name__
    return out


#: 每次会话都变、比对时必须忽略的字段。留着它们的话每一条报文都会报差异,
#: 真正的 schema 差异就被淹在噪声里了。
VOLATILE_PATHS = frozenset({"head.time_stamp", "head.frame_count"})


def diff_shapes(
    fixture: dict[str, str], real: dict[str, str]
) -> tuple[list[str], list[str], dict[str, str]]:
    """比两个形状。返回 (fixture 独有, 真机独有, 类型冲突)。"""
    fx = {k: v for k, v in fixture.items() if k not in VOLATILE_PATHS}
    rl = {k: v for k, v in real.items() if k not in VOLATILE_PATHS}
    missing = sorted(set(fx) - set(rl))
    extra = sorted(set(rl) - set(fx))
    mismatch = {
        k: f"fixture={fx[k]} real={rl[k]}"
        for k in sorted(set(fx) & set(rl))
        # NoneType 在两边都可能出现,而 NoneType vs str 只说明"这一次没值",
        # 不是 schema 冲突。报它会把差异表变成噪声。
        if fx[k] != rl[k] and "NoneType" not in (fx[k], rl[k])
    }
    return missing, extra, mismatch


def load_fixtures(root: Path | None = None) -> dict[str, list[tuple[str, Any]]]:
    """按 ``expected_req_func`` 归组 golden fixture 里的 **app_resp** 样本。

    只取 app_resp: 请求那一侧是我们自己发的,拿我们自己的报文跟我们自己的
    编码器对拍毫无信息量。要对的是**厂商发回来的东西**。
    """
    base = (root or Path.cwd()) / FIXTURE_DIR
    index_path = base / "index.json"
    if not index_path.exists():
        return {}
    index = json.loads(index_path.read_text(encoding="utf-8"))
    grouped: dict[str, list[tuple[str, Any]]] = {}
    for entry in index:
        if entry.get("type") != "app_resp":
            continue
        func = entry.get("expected_req_func")
        payload_path = base / entry["file"]
        if not func or not payload_path.exists():
            continue
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        grouped.setdefault(func, []).append((entry["file"], payload))
    return grouped


#: 厂商在 ``data.req_result`` 底下有时还会再套一层壳,而且**两处拼写不一样**:
#: §7 之前是 ``AppReponseObjectData``(厂商把 Response 拼成了 Reponse),
#: §7 里又变成 ``AppResponse``。这是协议地雷 1 和 2。
#: 这两个名字里哪个才是真机实际发的、还是两个都有 —— 正是本次巡检要验的。
RESPONSE_SHELLS = ("AppReponseObjectData", "AppResponse")


def resp_func_of(payload: Any) -> str | None:
    """从一条厂商响应里取出 ``req_func``,自动穿透那两层可选的怪壳。

    响应的真实形状是::

        {"head": {...}, "data": {"req_result": {"req_func": "...", ...}}}

    但速度类接口(以及 §7 的若干接口)会在 ``req_result`` 底下再套一层壳,
    壳名见 RESPONSE_SHELLS。这里三种形状都认 —— 认不出来就返回 None,
    由调用方当成"没法对拍的帧"跳过,绝不猜。

    注意函数名要从**响应自己报的 req_func 字段**里取,不能拿我们发出去的
    请求名去推: 协议地雷 3 —— loc_load_map 的响应报的是 load_localization_map。
    """
    try:
        inner = payload["data"]["req_result"]
    except (KeyError, TypeError):
        return None
    if not isinstance(inner, dict):
        return None
    for shell in RESPONSE_SHELLS:
        shelled = inner.get(shell)
        if isinstance(shelled, dict):
            inner = shelled
            break
    func = inner.get("req_func")
    return func if isinstance(func, str) else None


def diff_recording(
    recording: Path, fixtures: dict[str, list[tuple[str, Any]]]
) -> list[SchemaDiff]:
    """读回一份录像,把里面每一种 app_resp 跟 fixture 对拍。

    **对拍的输入是录像文件,不是内存里的对象** —— 这样同一份录像事后在别的
    机器上可以原样重算出同一张差异表,不需要真机在场。
    """
    seen: dict[str, dict[str, str]] = {}
    for line in recording.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("dir") != "rx" or rec.get("enc") == "base64":
            continue
        try:
            payload = json.loads(rec["raw"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        func = resp_func_of(payload)
        if func is None:
            continue
        # 同一个 func 收到多条时取并集: 厂商有的字段是可选的,只看第一条会把
        # "这次没带"误报成"真机根本没有这个字段"。
        seen.setdefault(func, {}).update(json_shape(payload))

    diffs: list[SchemaDiff] = []
    for func, real_shape in sorted(seen.items()):
        samples = fixtures.get(func, [])
        if not samples:
            diffs.append(
                SchemaDiff(
                    resp_func=func,
                    fixture_files=[],
                    extra_in_real=sorted(real_shape),
                )
            )
            continue
        fixture_shape: dict[str, str] = {}
        for _, payload in samples:
            fixture_shape.update(json_shape(payload))
        missing, extra, mismatch = diff_shapes(fixture_shape, real_shape)
        diffs.append(
            SchemaDiff(
                resp_func=func,
                fixture_files=[f for f, _ in samples],
                missing_in_real=missing,
                extra_in_real=extra,
                type_mismatch=mismatch,
            )
        )
    return diffs


# ------------------------------------------------------------- 阶段 0


def probe_tcp(host: str, port: int, timeout_s: float = 3.0) -> tuple[bool, str]:
    """探一个 TCP 端口通不通。

    用 TCP 而不是 ICMP ping: 现场真正要回答的问题是"我这台笔记本能不能跟这个
    服务说上话",而不是"这个 IP 有没有回应" —— 防火墙放 ping 拦端口是常态,
    反过来也有。而且 ping 在 Windows 上还要另起一个进程去解析中文输出。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, "TCP 可达"
    except TimeoutError:
        return False, f"连接 {host}:{port} 超时({timeout_s}s) —— 网段不通或 IP 没配对"
    except OSError as exc:
        return False, f"连接 {host}:{port} 失败: {exc}"


def run_probes(probes=DEFAULT_PROBES, timeout_s: float = 3.0) -> list[Probe]:
    """把 probes 里的每一条链路都探一遍。任何一条不通都不影响其它条。"""
    return [
        Probe(name, host, port, *probe_tcp(host, port, timeout_s))
        for name, host, port in probes
    ]


# ------------------------------------------------------------- 阶段 1


async def _call(result: ConformanceResult, op: str, coro) -> Any:
    """跑一次只读调用并记账。**失败绝不中断扫描。**

    现场最不划算的失败模式是"第三条接口报错,后面二十条一条没跑" —— 机器
    时间是稀缺的,一次扫描要尽量把能拿的都拿回来。
    """
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        value = await coro
    except (NavBackendError, asyncio.TimeoutError, OSError) as exc:
        result.calls.append(
            Call(op, False, f"{type(exc).__name__}: {exc}", round(loop.time() - t0, 3))
        )
        return None
    result.calls.append(Call(op, True, repr(value)[:400], round(loop.time() - t0, 3)))
    return value


async def readonly_sweep(
    backend: NavBackend,
    result: ConformanceResult,
    recorder: FrameRecorder | None = None,
) -> None:
    """把每一个只读导航接口都跑一遍。不写任何状态、不让机器动。"""
    if recorder is not None:
        recorder.note("stage_begin", stage=1, name="只读扫描")

    await _call(result, "nav_status", backend.nav_status())
    await _call(result, "loc_status", backend.loc_status())
    await _call(result, "mapping_status", backend.mapping_status())
    await _call(result, "get_speed", backend.get_speed())

    maps = await _call(result, "list_maps", backend.list_maps())

    # 地图相关的接口要有地图才跑得动。没有地图不是错误 —— 是一条要记下来的
    # 现场事实(说明这台机器还没建过图),报告里会写清楚。
    if not maps:
        result.notes.append(
            "机器上没有任何地图: list_paths / get_map_grid 未能执行。"
            "若需要覆盖它们,先按手册手动建一张小图再重跑本命令。"
        )
    else:
        for m in maps[:2]:  # 只取前两张: 再多也只是重复同一个 schema
            map_id = getattr(m, "map_id", None) or getattr(m, "id", None) or str(m)
            await _call(result, f"list_paths({map_id})", backend.list_paths(map_id))
            await _call(result, f"get_map_grid({map_id})", backend.get_map_grid(map_id))

    if recorder is not None:
        recorder.note("stage_end", stage=1)


# ------------------------------------------------------------- 报告


def render_report(result: ConformanceResult) -> str:
    """出人读的 markdown 报告。"""
    lines: list[str] = []
    a = lines.append
    a("# D1 Max 真机一致性巡检报告")
    a("")
    a(f"- 开始时间(UTC): `{result.started_at}`")
    a(f"- 导航地址: `{result.nav_url}`")
    a(f"- 允许运动阶段: {'是' if result.allow_motion else '否(仅只读)'}")
    if result.recording_path:
        a(
            f"- 原始帧录像: `{result.recording_path}` "
            f"(共 {result.recorded_frames} 行) **← 最重要的产出,务必带回**"
        )
    a("")

    a("## 阶段 0 · 网络可达性")
    a("")
    a("| 链路 | 地址 | 结果 | 说明 |")
    a("|---|---|---|---|")
    for p in result.probes:
        verdict = "通" if p.ok else "**不通**"
        a(f"| {p.name} | `{p.host}:{p.port}` | {verdict} | {p.detail} |")
    a("")

    a("## 阶段 1 · 只读接口扫描")
    a("")
    ok_n = sum(1 for c in result.calls if c.ok)
    a(f"共 {len(result.calls)} 条调用,成功 {ok_n},失败 {len(result.calls) - ok_n}。")
    a("")
    a("| 接口 | 结果 | 耗时(s) | 返回/错误 |")
    a("|---|---|---|---|")
    for c in result.calls:
        detail = c.detail.replace("|", "/").replace("\n", " ")
        if len(detail) > 200:
            detail = detail[:200] + " …"
        a(f"| `{c.op}` | {'ok' if c.ok else '**失败**'} | {c.elapsed_s} | `{detail}` |")
    a("")

    if result.notes:
        a("### 现场事实")
        a("")
        for n in result.notes:
            a(f"- {n}")
        a("")

    a("## 字段级对拍(真机响应 vs golden fixture)")
    a("")
    if not result.diffs:
        a("_没有采到任何可对拍的响应 —— 多半是链路就没通,先看阶段 0。_")
    for dfx in result.diffs:
        a(f"### `{dfx.resp_func}`")
        a("")
        if not dfx.fixture_files:
            a(
                "**文档里没有这个响应的样本。** 真机发回了文档没写的报文 —— "
                "这条要单独记下来带回去。字段:"
            )
            a("")
            for p in dfx.extra_in_real:
                a(f"- `{p}`")
            a("")
            continue
        joined = ", ".join("`" + f + "`" for f in dfx.fixture_files)
        a(f"对拍样本: {joined}")
        a("")
        if dfx.clean:
            a("**一致。**")
            a("")
            continue
        if dfx.missing_in_real:
            a("文档有、真机没有:")
            a("")
            for p in dfx.missing_in_real:
                a(f"- `{p}`")
            a("")
        if dfx.extra_in_real:
            a("真机有、文档没写:")
            a("")
            for p in dfx.extra_in_real:
                a(f"- `{p}`")
            a("")
        if dfx.type_mismatch:
            a("类型不一致:")
            a("")
            for p, v in dfx.type_mismatch.items():
                a(f"- `{p}` — {v}")
            a("")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------- 编排


async def run_conformance(
    backend: NavBackend,
    nav_url: str,
    outdir: Path,
    recorder: FrameRecorder | None = None,
    *,
    allow_motion: bool = False,
    fixtures_root: Path | None = None,
    probes=DEFAULT_PROBES,
) -> ConformanceResult:
    """跑阶段 0 + 1,落三份产出。

    `backend` 必须**已经连上**。阶段 0 的可达性探测本身不需要 backend,但它
    跑在这里是有意的: 报告要能回答"扫描失败的时候网到底通不通"这个问题 ——
    否则现场看到一片红,分不清是网线没插还是协议对不上。
    """
    result = ConformanceResult(
        started_at=datetime.now(timezone.utc).isoformat(),
        nav_url=nav_url,
        allow_motion=allow_motion,
    )
    result.probes = run_probes(probes)
    await readonly_sweep(backend, result, recorder)

    if allow_motion:
        # 阶段 4/5 是本卷之外的工作。这里不静默跳过 —— 现场的人开了这个开关
        # 就是指望它跑,不说话会让人以为跑过了。
        result.notes.append(
            "已开启 allow_motion,但阶段 4(建图)/5(短距导航)尚未做成自动流程;"
            "请按手册用 map-start / map-stop / goto 手动执行,录像会照常记录。"
        )

    outdir.mkdir(parents=True, exist_ok=True)
    if recorder is not None:
        result.recording_path = str(recorder.path)
        result.recorded_frames = recorder.count
        # 对拍前必须先收尾落盘 —— diff_recording 是**从文件读**的,而不是
        # 从内存里的对象读。这样同一份录像回去后能原样重算出同一张表。
        recorder.close()
        fixtures = load_fixtures(fixtures_root)
        if not fixtures:
            result.notes.append(
                "未找到 golden fixture(tests/protocol/fixtures/),跳过字段对拍。"
                "录像已保存,回去后可离线重算 —— 不影响本次现场产出的价值。"
            )
        else:
            result.diffs = diff_recording(recorder.path, fixtures)

    (outdir / "conformance.json").write_text(
        json.dumps(result.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / "conformance-report.md").write_text(
        render_report(result), encoding="utf-8"
    )
    return result


def nav_host_port(url: str) -> tuple[str, int]:
    """从 ws:// 地址里拆出 host/port,给阶段 0 的探测用。"""
    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1", parsed.port or 10010
