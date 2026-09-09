/// 盘况屏（`GET /api/storage`）。**只读，不清盘。**
///
/// `POST /api/storage/sweep` 那条路的另一头是 `shutil.rmtree`（狗那头自己的
/// docstring 说它是全仓最危险的一行），它的安全前提是「人先看名单，再点第二
/// 下」。在手机上、戴着手套、站在现场，那第二下点得太容易了。所以**这一屏
/// 从头到尾不向狗发一个 POST** —— `storage_page_test.dart` 里有一条盯着
/// 网络层的计数，不是盯按钮的类型：将来谁把清盘做成 `IconButton`，盯按钮
/// 类型的那条照样绿。
///
/// **不轮询。** 盘况是分钟级变化的东西，轮询它只是在热点上跟视频抢带宽。
/// 一次 `GET /api/storage`，另给一个刷新按钮。
///
/// **狗拼好的三句话一律原样上屏**（`notice_detail` / `backup.detail` /
/// `forecast.detail`）。`engine/retention.py` 里 `Forecast` 的 docstring 明写
/// 「文案在这里拼，不在 app 里拼 —— 免得手机端和网页端各说各的，而这是要给
/// 客户看的一句话」。手机这头重新组织的版本迟早跟狗分家。
///
/// **所有 URL 都从 `client.baseUrl` 取，`robot` 只用来显示名字**（跟
/// `teleop_page.dart` 一个规矩）：`Robot.host/port` 的默认值是真狗的
/// `192.168.168.100:8095`，拿 `robot` 去拼 URL，测试会去敲真狗的地址。
library;

import 'dart:developer' as developer;

import 'package:flutter/material.dart';

import '../model/robot.dart';
import '../net/patrol_client.dart';
import '../net/wire.dart';

/// 读不到盘况时整屏那一句。**异常原文不上屏**，只进 `developer.log`。
///
/// 屏上写 `$e` 的话，人看到的是 `SocketException: ... errno = 111` 这种东西 ——
/// 它既不告诉人下一步该干什么，又会把 URL 之类的内部细节推到客户面前。
const String storageLoadFailed = '读不到盘况。看看还连着这只狗的热点吗 ——'
    '连上了就点右上角再问一次。';

/// 备份盘那一句挂在哪个槽里，见 [backupColorOf]。
const String backupSectionTitle = '备份盘';

/// 盘要满了该去哪清。
///
/// **这一屏刻意不给清盘入口**（见文件头）：`POST /api/storage/sweep` 的另一头
/// 是 `shutil.rmtree`，在手机上、戴着手套、站在现场，那一下点得太容易了。
/// 但只挡不指路的话，人看完这块屏只是更急 —— 他知道盘要满了，不知道下一步
/// 该走哪，于是现场就会打电话问。
///
/// **这句话里不许出现端口号、IP、密码，也不许出现任何厂商私有协议的细节。**
/// 它是给站在狗旁边的人看的一句指路，不是给人照着敲的一条命令；写进去的地址
/// 会跟着截图、跟着工单一路流到客户手上。
const String sweepElsewhereHint = '这一屏只看不删。要清盘，请连上这只狗自己的'
    '网页端，在那边照着名单一趟一趟地清。';

/// 字节换成人读的样子。
///
/// **按 10 的幂（1 GB = 1e9 字节）**，跟盘商标称的容量、也跟
/// `shutil.disk_usage` 报的原始字节对得上：一块标着 1 TB 的盘在这儿显示
/// `1000.0 GB`。换成 2 的幂会显示成 `931.3 GB`，现场的人会以为盘不对。
String formatGb(int bytes) => '${(bytes / 1e9).toStringAsFixed(1)} GB';

/// 生戳换成人读的样子。
///
/// 狗那头写的是 `engine/archive.py` 的 `STAMP_FMT`（`%Y%m%dT%H%M%SZ`，UTC），
/// 形如 `20260830T041500Z`。**这种东西不许原样上屏** —— 它是给目录名排序用
/// 的，不是给人读的。
///
/// **带上 `UTC` 三个字母。** 狗写的就是 UTC（跨时区看历史报告时本地时间没有
/// 意义），不标的话现场的人会按自己的时区读，差出来的那几个小时正好落在
/// 「这一趟是今天早上跑的还是昨天下午跑的」上。
///
/// 认不出的戳原样退回：编不出更好的说法时，如实给出狗发来的那串，好过编一个
/// 看着像模像样的日期。
String formatStamp(String raw) {
  final RegExp shape =
      RegExp(r'^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$');
  final RegExpMatch? m = shape.firstMatch(raw);
  if (m == null) return raw;
  return '${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]} UTC';
}

/// 名单每一行右边那句话。
///
/// **不许写成「还剩 N 天」或者「N 天后删除」。** wire 上压根没有「还剩几天」
/// 这个数（`RunInfo.days_left()` 没进 `to_wire()`），这里拿到的
/// [retentionDays] 是**保留期策略值**。在一份标着「预告名单」的表里写
/// 「保留 30 天」，百分之百会被读成「还剩 30 天」—— 而真正动手的时刻是
/// `delete_starts_at()` 算的 `max(到期日, 首次预告 + 预告期)`，能差出十几天。
///
/// 「还剩几天 / 即将删除」那句话只准来自 `forecast.detail`（狗那头拼好的原话）。
String retentionLabel(int days) => '保留期 $days 天';

/// 备份盘那句话用什么颜色。
///
/// **`neutral` 不许用警示色。** `engine/backup.py` 的 `backup_notice` 写着：
/// 没配镜像盘不许常年报红（spec §7.6）—— 常年报警的东西等于没报警，现场的人
/// 会先学会忽略它，然后连真的那次也一起忽略。`NEUTRAL` 单独立一档就是为了
/// 这件事：画成黄的或红的，每一台没插镜像盘的狗从此常年顶着警示色。
///
/// 认不出的档（狗以后加了新的）**按中性处理**：宁可少喊一次，也不要凭一个
/// 读不懂的字符串把屏染红。
Color backupColorOf(BuildContext context, String level) {
  final ColorScheme scheme = Theme.of(context).colorScheme;
  return level == backupLevelPush ? scheme.error : scheme.onSurfaceVariant;
}

class StoragePage extends StatefulWidget {
  const StoragePage({super.key, required this.client, required this.robot});

  final PatrolClient client;

  /// **只用来显示名字**（[Robot.label]）。见文件头。
  final Robot robot;

  /// 右上角那个刷新。**这一屏唯一的按钮，而且它只读。**
  static const Key refreshKey = ValueKey<String>('storage-refresh');

  /// 预告没落盘时顶在最上面那块卡片的正文。
  static const Key noticeKey = ValueKey<String>('storage-notice');

  /// 「已用 42%（420.0 GB / 1000.0 GB）」那一行。
  static const Key capacityKey = ValueKey<String>('storage-capacity');

  /// 备份盘那句话。测试读它的颜色 —— 见 [backupColorOf]。
  static const Key backupKey = ValueKey<String>('storage-backup');

  /// 预告那句话（`forecast.detail`）。
  static const Key forecastKey = ValueKey<String>('storage-forecast');

  /// 「其中 N 即将到期」那一行。`bytes_at_risk` 为 0 时压根不挂。
  static const Key atRiskKey = ValueKey<String>('storage-at-risk');

  /// 指路那一句（[sweepElsewhereHint]）。**静态文案，不随报文变。**
  static const Key sweepHintKey = ValueKey<String>('storage-sweep-hint');

  /// 读不到盘况时那一句。
  static const Key errorKey = ValueKey<String>('storage-error');

  /// 名单里的一行。拿归档路径当身份 —— `mission` 会重名。
  static Key runKey(String path) => ValueKey<String>('storage-run-$path');

  @override
  State<StoragePage> createState() => _StoragePageState();
}

class _StoragePageState extends State<StoragePage> {
  late Future<StorageView> _pending;

  @override
  void initState() {
    super.initState();
    _pending = _ask();
  }

  /// 问一次狗。**只有 GET。**
  Future<StorageView> _ask() async =>
      StorageView.fromJson(await widget.client.get('/api/storage'));

  /// 再问一次。
  ///
  /// **`setState` 的闭包不许返回 future。** 写成 `setState(() => _pending =
  /// _ask())` 的话，箭头函数的值就是那个 `Future`，Flutter 当场抛
  /// 「setState() callback argument returned a Future」—— 而它抛在手势回调里，
  /// 屏上什么也不会变，看起来就是「刷新按钮点了没反应」。
  void _refresh() {
    setState(() {
      _pending = _ask();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('盘况 · ${widget.robot.label}'),
        actions: <Widget>[
          IconButton(
            key: StoragePage.refreshKey,
            icon: const Icon(Icons.refresh),
            tooltip: '再问一次',
            onPressed: _refresh,
          ),
        ],
      ),
      body: FutureBuilder<StorageView>(
        future: _pending,
        builder: (BuildContext context, AsyncSnapshot<StorageView> snap) {
          if (snap.hasError) {
            // **异常原文只进日志。** 见 [storageLoadFailed]。
            developer.log('读盘况失败：${snap.error}', name: 'storage_page');
            return _failed(context);
          }
          final StorageView? view = snap.data;
          if (view == null) {
            return const Center(child: CircularProgressIndicator());
          }
          return _content(context, view);
        },
      ),
    );
  }

  Widget _failed(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Text(
            storageLoadFailed,
            key: StoragePage.errorKey,
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodyMedium,
          ),
        ),
      );

  Widget _content(BuildContext context, StorageView v) {
    final ThemeData theme = Theme.of(context);
    return ListView(
      padding: const EdgeInsets.all(16),
      children: <Widget>[
        // **预告没落盘那块卡片顶在最上面。** 这是这块屏存在的理由：
        // `notice_written == false` 的意思是盘满了或者被挂成只读，「再过几天
        // 开始删除」那句话说了不算 —— 钟没开始走。排在后面（或者干脆不画）
        // 的话，人看到的是一块一切正常的屏，而归档会无声地堆到盘炸。
        if (!v.noticeWritten)
          Card(
            color: theme.colorScheme.errorContainer,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Icon(Icons.report_outlined,
                      color: theme.colorScheme.onErrorContainer),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      // 狗那头已经把话写全了，手机不重新组织。
                      v.noticeDetail,
                      key: StoragePage.noticeKey,
                      style: TextStyle(
                          color: theme.colorScheme.onErrorContainer),
                    ),
                  ),
                ],
              ),
            ),
          ),
        Text(
          '已用 ${(v.usedRatio * 100).round()}%'
          '（${formatGb(v.usedBytes)} / ${formatGb(v.totalBytes)}）',
          key: StoragePage.capacityKey,
          style: theme.textTheme.titleMedium,
        ),
        const SizedBox(height: 8),
        LinearProgressIndicator(value: v.usedRatio.clamp(0.0, 1.0)),
        const SizedBox(height: 8),
        Text('归档 ${v.runsTotal} 趟 · ${formatGb(v.runsBytes)}；'
            '基线 ${formatGb(v.baselinesBytes)}；'
            '导出 ${formatGb(v.exportsBytes)}'),
        const Divider(height: 32),
        Text(backupSectionTitle, style: theme.textTheme.titleSmall),
        const SizedBox(height: 4),
        Text(
          // 原样上屏。
          v.backupDetail,
          key: StoragePage.backupKey,
          style: TextStyle(color: backupColorOf(context, v.backupLevel)),
        ),
        const Divider(height: 32),
        Text(
          // 「还剩几天 / 即将删除」那句话只出自这里。见 [retentionLabel]。
          v.forecastDetail,
          key: StoragePage.forecastKey,
          style: theme.textTheme.bodyMedium,
        ),
        if (v.bytesAtRisk > 0) ...<Widget>[
          const SizedBox(height: 4),
          Text('其中 ${formatGb(v.bytesAtRisk)} 即将到期',
              key: StoragePage.atRiskKey,
              style: theme.textTheme.bodyMedium),
        ],
        // **挡了就要指路。** 见 [sweepElsewhereHint]。挂在预告这一段下面：
        // 人是在这儿看见「盘 42%、再过几天开始删」才想起要清盘的。
        const SizedBox(height: 12),
        Text(
          sweepElsewhereHint,
          key: StoragePage.sweepHintKey,
          style: theme.textTheme.bodySmall
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
        ),
        if (v.runs.isNotEmpty) ...<Widget>[
          const SizedBox(height: 12),
          for (final StorageRun r in v.runs)
            Padding(
              key: StoragePage.runKey(r.path),
              padding: const EdgeInsets.symmetric(vertical: 6),
              child: Row(
                children: <Widget>[
                  Expanded(
                    child: Text('${formatStamp(r.startedAt)} ${r.mission}',
                        overflow: TextOverflow.ellipsis),
                  ),
                  const SizedBox(width: 8),
                  Text(retentionLabel(r.retentionDays),
                      style: theme.textTheme.bodySmall),
                ],
              ),
            ),
        ],
      ],
    );
  }
}
