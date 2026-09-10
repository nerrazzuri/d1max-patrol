/// 值守屏（§5.1）。**把静默失败摆到玻璃上。**
///
/// 布局就是 §5.1 那三个问题，按优先级从上往下：
///
/// 1. **现在有没有需要我动身的事？** —— P1 告警，最上面，最大。
///    **一条都没有的时候要明确说「没有」**，不是留白 —— 留白和「还没加载
///    出来」长得一模一样，值班的人分不出「今晚太平」和「这一屏挂了」。
/// 2. **每只狗此刻在哪、在干什么？** —— 引擎状态 + 位姿（这一卷单机档只有
///    一只狗）。
/// 3. **有没有什么正在悄悄坏掉？** —— `app/watch.py` 那六项。
///
/// **告警的顺序原样照登，手机不再排一遍。** 排法（先级别再 `last_ms` 倒序）
/// 在 `AlertBook.open()` 里，判据只许在 `engine/` 说一次。理由见
/// `net/patrol_client.dart` 的 `alertsOpen`。
///
/// **三条路各读各的，一条断了不许把另外两段带走。** 狗那侧的 `_watch_summary`
/// 写着「探针卡住不许把另外五项一起带走 —— 值守屏是最后一块必须还能亮的
/// 玻璃」。手机这头是同一条纪律：告警读不到的时候，盘水位和电量正是人要看的。
///
/// **操作员姓名是构造参数**（[operatorName]，裁决十六）。`roster_page.dart`
/// 的 `_open` 里那个 `who` 一路传下来 —— 这一屏不新造存储、也不去问狗：
/// 名字按计划就不在狗那侧。任务 11 会把 `_operatorName()` 的来源换成落盘 +
/// 一步切换的 chip，这个参数原样不动。
///
/// **不许把这个名字说成「已登录」「已认证」。** `Session.operatorVerified`
/// 那条注释写着：狗记下名字但**不核实**（§6.3）。它就是一个自报的署名，而
/// 现场判断「屏上这个名字能信到什么程度」全靠这一句话不说错。
///
/// **不轮询。** 一次问完三条路，另给一个刷新按钮 —— 跟 `storage_page.dart`
/// 一个规矩。值守屏该不该自己刷是任务 12（告警声光）那一档的事，在这儿
/// 起一个定时器只是先在热点上跟视频抢带宽。
///
/// **所有 URL 都从 `client` 走，[robot] 只用来显示名字**（跟
/// `storage_page.dart` / `teleop_page.dart` 一个规矩）。
library;

import 'dart:async';
import 'dart:developer' as developer;
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../model/alert.dart';
import '../model/robot.dart';
import '../net/patrol_client.dart';
import 'widget/operator_chip.dart';

/// 墙上钟，UTC 毫秒。**这一屏读时刻只走这一个口子**（见 [WatchPage.nowMs]）。
int wallClockMs() => DateTime.now().toUtc().millisecondsSinceEpoch;

/// 一条 P1 都没有的时候屏上那一句。**不许换成留白。**
const String noP1Hint = '现在没有需要立刻动身的事。';

/// 那一句底下的小字：说清楚「没有」是查过的结果，不是还没查。
const String noP1Detail = '这一屏刚问过狗，未解决的告警里没有 P1。';

/// 自报署名那一句。**这句话的措辞是硬约束**，见文件头。
///
/// 狗那侧每次应答都跟着发一句 `auth.OPERATOR_NOTICE`（`Session.notice`），
/// 那是给「这个名字能信到什么程度」用的原话。这里这一句只说这一屏上的确认
/// 会记成谁 —— 两句话是两件事，不合并。
String signedAs(String who) => '这一屏上的确认会记成：$who（狗记下这个名字，但不核实）';

/// 回传积压是 `null` 时屏上那一句。**不是 `0`、不是空白、不是 `--`。**
///
/// `0` 会让人以为证据都传上去了，而它们全在狗上堆着（§4.3）。
const String noUploaderText = '这一档没有回传';

/// 报不出来的那一档统一这么说。
const String unknownText = '不知道';

/// 值守屏。
class WatchPage extends StatefulWidget {
  const WatchPage({
    super.key,
    required this.client,
    required this.robot,
    required this.operatorName,
    this.onOperatorChanged,
    this.nowMs = wallClockMs,
  });

  final PatrolClient client;

  /// **只用来显示名字**（`Robot.label`）。见文件头。
  final Robot robot;

  /// 这一趟是谁在值守。**确认那一下带上去的就是它**（§5.3 记名确认）。
  ///
  /// 见文件头：来源是 `roster_page.dart::_open` 里那个 `who`，不是狗，也不是
  /// 会话上那个 `operator`。
  final String operatorName;

  /// 在这一屏上点 chip 换了人。**落盘是名册那一屏的事**，这儿只往上报。
  final ValueChanged<String>? onOperatorChanged;

  /// 现在几点（UTC 毫秒）。**注入的，不许在 widget 里 `DateTime.now()`。**
  ///
  /// 这一屏**不轮询**（见文件头）：屏上这堆数全是某一次点刷新时问回来的，
  /// 之后再没变过。「这堆数是什么时候的」于是不是装饰，而是这一屏能不能被
  /// 信的前提 —— 值班的人半夜看一眼，得知道自己看的是三秒前的还是四十分钟
  /// 前的（电量那一格的「收到于」是同一条理由，只是它只管一格）。
  ///
  /// 走参数是为了这句话能被**证**：真时钟每次跑都不一样，断言只能写成
  /// 「有个 `watch-as-of` 在」—— 那是恒真的，把渲染整段删掉也照样绿。喂两个
  /// 不同的时刻、断出两个不同的显示，这一格才算承重。
  final int Function() nowMs;

  /// 右上角那个刷新。
  static const Key refreshKey = ValueKey<String>('watch-refresh');

  /// 自报署名那一行。
  static const Key operatorKey = ValueKey<String>('watch-operator');

  /// 屏上这堆数是什么时候问回来的。见 [nowMs]。
  static const Key asOfKey = ValueKey<String>('watch-as-of');

  /// 告警那一整段画出来了没有。**测试拿它当「屏真的加载完了」的锚。**
  static const Key alertsSectionKey = ValueKey<String>('watch-alerts');

  /// 一条 P1 都没有时那一句。
  static const Key p1NoneKey = ValueKey<String>('watch-p1-none');

  /// 现在有几条未解决的告警。
  static const Key alertsOpenKey = ValueKey<String>('watch-alerts-open');

  /// 名单里第 i 条。**按下标，不按 key** —— 要证的是「拿到什么顺序就渲染
  /// 什么顺序」，按 key 挂的话顺序就再也钉不住了。
  static Key alertKeyFor(int i) => ValueKey<String>('watch-alert-$i');
  static Key ackKeyFor(int i) => ValueKey<String>('watch-ack-$i');
  static Key resolveKeyFor(int i) => ValueKey<String>('watch-resolve-$i');

  /// 某一档底下那句「为什么」。
  ///
  /// **这一行也是会变的话槽**：整句话来自狗（`app/watch.py`），所以也得有
  /// 自己的 `Key`。没有 Key 的时候唯一能覆盖它的写法是
  /// `find.text('一整句中文')` —— 狗那头改个标点测试就红，而红的原因跟被测
  /// 的事无关。
  static Key whyKeyFor(String field) => ValueKey<String>('watch-why-$field');

  /// 引擎状态、位姿。
  static const Key engineKey = ValueKey<String>('watch-engine');
  static const Key poseKey = ValueKey<String>('watch-pose');

  /// §5.1 那六项，一格一个。
  static const Key bundleLagKey = ValueKey<String>('watch-bundle-lag');
  static const Key clockSkewKey = ValueKey<String>('watch-clock-skew');
  static const Key backlogKey = ValueKey<String>('watch-backlog');
  static const Key diskKey = ValueKey<String>('watch-disk');
  static const Key batteryKey = ValueKey<String>('watch-battery');
  static const Key mirrorKey = ValueKey<String>('watch-mirror');

  /// 六项一个不少 —— 名单在这儿只列一次，测试照着它数。
  static const List<Key> sixSlotKeys = <Key>[
    bundleLagKey,
    clockSkewKey,
    backlogKey,
    diskKey,
    batteryKey,
    mirrorKey,
  ];

  /// 三条路各自读不到时那一句。**三个分开** —— 见文件头。
  static const Key alertsErrorKey = ValueKey<String>('watch-alerts-error');
  static const Key stateErrorKey = ValueKey<String>('watch-state-error');
  static const Key summaryErrorKey = ValueKey<String>('watch-summary-error');

  @override
  State<WatchPage> createState() => _WatchPageState();
}

/// 六项里某一格的一行：一个名、一句值，外加狗附的那句为什么。
class _Slot extends StatelessWidget {
  const _Slot({
    required this.slotKey,
    required this.whyKey,
    required this.label,
    required this.value,
    required this.why,
  });

  final Key slotKey;
  final Key whyKey;
  final String label;
  final String value;

  /// 狗那头拼好的那句话。**原样上屏**，空串就不挂。
  final String why;

  @override
  Widget build(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text('$label：$value',
              key: slotKey, style: theme.textTheme.bodyMedium),
          if (why.isNotEmpty)
            Text(why,
                key: whyKey,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        ],
      ),
    );
  }
}

/// 名单里的一条。
///
/// **是个公开的类，[alert] 也是公开字段** —— 测试要读第一条到底是哪一级，
/// 靠的是 `tester.widget<AlertTile>(...).alert.level`，不是去扫屏上的文字。
class AlertTile extends StatelessWidget {
  const AlertTile({
    super.key,
    required this.alert,
    required this.ackKey,
    required this.resolveKey,
    required this.onAck,
    required this.onResolve,
  });

  final Alert alert;
  final Key ackKey;
  final Key resolveKey;
  final VoidCallback onAck;
  final VoidCallback onResolve;

  @override
  Widget build(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    // **P1 才用警示色。** 每一条都染红的话，现场的人会先学会忽略这块屏，
    // 然后连真该动身的那次也一起忽略（跟盘况屏 `neutral` 那条同一根）。
    final Color ink = alert.isP1
        ? theme.colorScheme.error
        : theme.colorScheme.onSurfaceVariant;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Text(alert.level,
                    style: theme.textTheme.titleMedium?.copyWith(color: ink)),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(alert.title,
                      style: theme.textTheme.titleMedium,
                      overflow: TextOverflow.ellipsis),
                ),
                if (alert.count > 1) Text('×${alert.count}'),
              ],
            ),
            const SizedBox(height: 4),
            // 狗那头把话写全了，手机不重新组织。
            Text(alert.detail, style: theme.textTheme.bodyMedium),
            const SizedBox(height: 4),
            Text(
              // **确认和解决是两件事，分开写**（§5.3）：「我看见了在处理」
              // 和「这事没了」不是同一个状态机上的两档。
              <String>[
                alert.acked ? '${alert.ackedBy} 确认过' : '还没有人确认',
                if (alert.resolved) '已解决',
                if (alert.escalated > 0) '已升级到 ${alert.channel}',
              ].join(' · '),
              style: theme.textTheme.bodySmall?.copyWith(color: ink),
            ),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: <Widget>[
                TextButton(
                  key: ackKey,
                  onPressed: onAck,
                  child: const Text('我看见了'),
                ),
                TextButton(
                  key: resolveKey,
                  onPressed: onResolve,
                  child: const Text('这事没了'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _WatchPageState extends State<WatchPage> {
  List<Alert>? _alerts;
  WatchSummary? _summary;
  Map<String, dynamic>? _state;
  bool _alertsFailed = false;
  bool _summaryFailed = false;
  bool _stateFailed = false;

  /// 屏上此刻挂着谁的名字。初值来自 [WatchPage.operatorName]，点 chip 就换。
  late String _operator = widget.operatorName;

  /// 这一屏上这堆数是什么时候问回来的（UTC 毫秒）。见 [WatchPage.nowMs]。
  ///
  /// **这一次问出去的时刻，不是回来的时刻。** 三条路各读各的、快慢不一，
  /// 拿最后一条回来的时刻当「这屏是什么时候的」会把断掉的那条路藏起来：
  /// 告警路挂着重试到超时的那 10 秒，屏上写的反而是更新的时刻。
  int _asOfMs = 0;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  /// 问一遍。**三条路各读各的。** 见文件头。
  Future<void> _load() async {
    setState(() => _asOfMs = widget.nowMs());
    await Future.wait<void>(<Future<void>>[
      _loadAlerts(),
      _loadSummary(),
      _loadState(),
    ]);
  }

  /// 点 chip 换了人。**屏上先换，再往上报（落盘在名册那一屏）。**
  void _onOperatorChanged(String name) {
    if (name == _operator) return;
    setState(() => _operator = name);
    widget.onOperatorChanged?.call(name);
  }

  Future<void> _loadAlerts() async {
    try {
      final List<Alert> got = await widget.client.alertsOpen();
      if (mounted) {
        setState(() {
          _alerts = got;
          _alertsFailed = false;
        });
      }
    } catch (e) {
      // **异常原文只进日志。** 屏上写 `$e` 给不出下一步该干什么，还会把内部
      // 地址推到客户面前。
      developer.log('读不到告警：$e', name: 'watch_page');
      if (mounted) setState(() => _alertsFailed = true);
    }
  }

  Future<void> _loadSummary() async {
    try {
      final WatchSummary got = await widget.client.watchSummary();
      if (mounted) {
        setState(() {
          _summary = got;
          _summaryFailed = false;
        });
      }
    } catch (e) {
      developer.log('读不到值守汇总：$e', name: 'watch_page');
      if (mounted) setState(() => _summaryFailed = true);
    }
  }

  Future<void> _loadState() async {
    try {
      final Map<String, dynamic> got = await widget.client.get('/api/state');
      // **读不懂就当读不到，不许留着一份空壳往下画。** 空壳会让这一段一半
      // 说「不知道」、一半报几个凭空捏的数（见 `_whereSection`）。这条路
      // 今天就走得到：`_send` 吞掉 `FormatException` 之后，一页强制门户的
      // HTML 就是一个空的 `{}`。
      if (got['run'] is! Map<String, dynamic>) {
        throw const FormatException('狗回的状态里没有 run 那一段，读不出它在干什么');
      }
      if (mounted) {
        setState(() {
          _state = got;
          _stateFailed = false;
        });
      }
    } catch (e) {
      developer.log('读不到狗的状态：$e', name: 'watch_page');
      if (mounted) setState(() => _stateFailed = true);
    }
  }

  /// 记名确认。**送的是 [_operator]，屏上此刻挂着的那个名字。**
  ///
  /// **不许写成 `widget.operatorName`。** 那个值是进这一屏那一刻取的，点 chip
  /// 换人不会动它 —— 张三开着屏、李四接班点 chip 改成李四、屏上三处都写着
  /// 李四，而狗的告警上 `acked_by` 记的还是张三。§5.3 记名确认存在的全部理由
  /// 就是交接班时查得出谁在盯屏幕，这一屏偏偏会在交接班这唯一场景上记错人。
  Future<void> _ack(Alert a) async {
    try {
      await widget.client.ackAlert(a.key, who: _operator);
    } catch (e) {
      developer.log('确认 ${a.key} 失败：$e', name: 'watch_page');
      if (mounted) _toast('确认没发出去。热点还连着吗');
      return;
    }
    // **确认完了要再问一次。** 不重问的话屏上那条还挂着「还没有人确认」，
    // 人会再点一次、再点一次 —— 而每一次都真的发出去了。
    await _loadAlerts();
  }

  Future<void> _resolve(Alert a) async {
    try {
      await widget.client.resolveAlert(a.key);
    } catch (e) {
      developer.log('解决 ${a.key} 失败：$e', name: 'watch_page');
      if (mounted) _toast('没发出去。热点还连着吗');
      return;
    }
    await _loadAlerts();
  }

  void _toast(String text) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  @override
  Widget build(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text('值守 · ${widget.robot.label}'),
        actions: <Widget>[
          IconButton(
            key: WatchPage.refreshKey,
            icon: const Icon(Icons.refresh),
            tooltip: '再问一次',
            onPressed: () => unawaited(_load()),
          ),
        ],
      ),
      // **`SingleChildScrollView` + `Column`，不是 `ListView`。**
      // `ListView` 只建可见的那几个孩子：这一屏往下滚才看得见的那几格
      // （六项里靠后的、名单里靠后的）压根不在树上。对人没差别，对**测试**
      // 差别很大 —— 「六项一个不少」那条会因为第四格还没建出来而红，而红的
      // 原因跟被测的那件事毫无关系；更坏的是反过来：把断言改松成「滚到那儿
      // 再看」之后，一格真的被删掉也照样绿。这一屏就是几十个话槽，全建出来
      // 的代价可以忽略。
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: <Widget>[
            // **常显的署名 chip，就在这一屏最上头**（人拍的板 3 的第一条
            // 补偿）。**不许挪进任何要点开才看得见的地方** —— 夜班接班的人
            // 走到这块屏前，不做任何动作就该看见账记在谁头上；看不见的那
            // 一版，代价（乙的操作签在甲名下）照旧，补偿没了。
            Align(
              alignment: Alignment.centerLeft,
              child: OperatorChip(
                name: _operator,
                client: widget.client,
                onChanged: _onOperatorChanged,
              ),
            ),
            // **不许写成「已登录」。** 见文件头。
            Text(
              signedAs(_operator),
              key: WatchPage.operatorKey,
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
            // **这堆数是什么时候的。** 这一屏不轮询（见文件头），所以屏上
            // 每一格都是上一次问回来的样子 —— 不写出来的话，四十分钟前的
            // 一屏和三秒前的一屏在玻璃上长得一模一样。
            Text(
              '这一屏的数据读于 ${_stampText(_asOfMs)}（不会自己刷新，点右上角再问一次）',
              key: WatchPage.asOfKey,
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
            const SizedBox(height: 12),
            ..._alertsSection(context),
            const Divider(height: 32),
            ..._whereSection(context),
            const Divider(height: 32),
            ..._quietlyBreakingSection(context),
          ],
        ),
      ),
    );
  }

  // ------------------------------------------- 问题一：有没有需要我动身的事

  List<Widget> _alertsSection(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    if (_alertsFailed) {
      return <Widget>[
        Text('读不到告警。看看还连着这只狗的热点吗 —— 连上了就点右上角再问一次。',
            key: WatchPage.alertsErrorKey, style: theme.textTheme.bodyMedium),
      ];
    }
    final List<Alert>? list = _alerts;
    if (list == null) {
      return const <Widget>[Center(child: CircularProgressIndicator())];
    }
    final bool anyP1 = list.any((Alert a) => a.isP1);
    return <Widget>[
      Text('现在有没有需要我动身的事',
          key: WatchPage.alertsSectionKey, style: theme.textTheme.titleLarge),
      const SizedBox(height: 8),
      // **一条 P1 都没有的时候要明确说「没有」。** 留白和「还没加载出来」
      // 长得一模一样。
      if (!anyP1) ...<Widget>[
        Text(noP1Hint,
            key: WatchPage.p1NoneKey,
            style: theme.textTheme.titleMedium
                ?.copyWith(color: theme.colorScheme.primary)),
        Text(noP1Detail,
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
        const SizedBox(height: 8),
      ],
      Text('未解决 ${list.length} 条',
          key: WatchPage.alertsOpenKey, style: theme.textTheme.bodySmall),
      // **原样照登。** 狗那侧已经排好了（P1 在最上面），这儿不再排一遍。
      for (int i = 0; i < list.length; i++)
        AlertTile(
          key: WatchPage.alertKeyFor(i),
          alert: list[i],
          ackKey: WatchPage.ackKeyFor(i),
          resolveKey: WatchPage.resolveKeyFor(i),
          onAck: () => unawaited(_ack(list[i])),
          onResolve: () => unawaited(_resolve(list[i])),
        ),
    ];
  }

  // ------------------------------------------------- 问题二：狗在哪、在干什么

  List<Widget> _whereSection(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    final List<Widget> out = <Widget>[
      Text('${widget.robot.label} 此刻在哪、在干什么',
          style: theme.textTheme.titleMedium),
      const SizedBox(height: 8),
    ];
    if (_stateFailed) {
      out.add(Text('读不到这只狗的状态。连上热点之后点右上角再问一次。',
          key: WatchPage.stateErrorKey, style: theme.textTheme.bodyMedium));
      return out;
    }
    final Map<String, dynamic>? s = _state;
    if (s == null) {
      // **还没问回来的时候画转圈，不画一句「不知道」。** 那两件事在屏上要
      // 分得开：「问过了，狗答不出位姿」和「还没问回来」是两回事，这一屏
      // 存在的全部理由就是不许再多一个这样的混淆。
      out.add(const Center(child: CircularProgressIndicator()));
      return out;
    }
    final Map<String, dynamic> run =
        (s['run'] as Map<String, dynamic>?) ?? const <String, dynamic>{};
    final Map<String, dynamic> device =
        (s['device'] as Map<String, dynamic>?) ?? const <String, dynamic>{};
    final String state = (run['state'] as String?) ?? unknownText;
    final String wpName = (run['waypoint_name'] as String?) ?? '';
    // **`null` 不许塌成 `0`。** `0` 是「查过了，就是第 0 个」，`null` 是
    // 「这一拍答不上来」。塌成 `0` 之后屏上是「第 0/0 个点位」—— 值班的人
    // 读到的是「这条线一共 0 个点位」或者「还没出发」，而事实是这一屏没读懂
    // 回复。跟下面 `_poseText` 里那条「不许画成 (0.00, 0.00)」是同一条规矩：
    // 一个看着像真事的假数比一句「不知道」更坏。
    final int? wpIndex = (run['waypoint_index'] as num?)?.toInt();
    final int? total = (run['total'] as num?)?.toInt();
    final String progress = (wpIndex == null || total == null)
        ? '点位进度$unknownText'
        : '第 $wpIndex/$total 个点位${wpName.isEmpty ? '' : '「$wpName」'}';
    out.add(Text(
      '引擎：$state · $progress',
      key: WatchPage.engineKey,
      style: theme.textTheme.bodyMedium,
    ));
    out.add(Text(_poseText(device['pose']),
        key: WatchPage.poseKey, style: theme.textTheme.bodyMedium));
    return out;
  }

  /// 位姿那一行。
  ///
  /// **没收到过位姿的时候说「不知道」，不许画成 (0.00, 0.00)。** 一个看着像
  /// 真事的假坐标比一句「不知道」更坏：屏上写着原点，人会以为狗在原点。
  ///
  /// `yaw` 狗那头是**弧度**（`nav_types.orientation_to_yaw`）。换成度是为了
  /// 给人读的，所以单位符号必须跟着 —— 一个没有单位的 `0.79` 会被当成度。
  String _poseText(Object? raw) {
    if (raw is! Map<String, dynamic>) {
      return '位姿：$unknownText（还没收到过位姿）';
    }
    final double? x = (raw['x'] as num?)?.toDouble();
    final double? y = (raw['y'] as num?)?.toDouble();
    final double? yaw = (raw['yaw'] as num?)?.toDouble();
    if (x == null || y == null) return '位姿：$unknownText（这一拍没有坐标）';
    final String heading = yaw == null
        ? ''
        : '，朝向 ${(yaw * 180 / math.pi).toStringAsFixed(0)}°';
    return '位姿：x ${x.toStringAsFixed(2)} m，y ${y.toStringAsFixed(2)} m$heading';
  }

  // ------------------------------------------ 问题三：有没有什么正在悄悄坏掉

  List<Widget> _quietlyBreakingSection(BuildContext context) {
    final ThemeData theme = Theme.of(context);
    final List<Widget> out = <Widget>[
      Text('有没有什么正在悄悄坏掉', style: theme.textTheme.titleMedium),
      const SizedBox(height: 4),
    ];
    if (_summaryFailed) {
      out.add(Text('读不到这六项。连上热点之后点右上角再问一次。',
          key: WatchPage.summaryErrorKey, style: theme.textTheme.bodyMedium));
      return out;
    }
    final WatchSummary? s = _summary;
    if (s == null) {
      out.add(const Center(child: CircularProgressIndicator()));
      return out;
    }
    out.addAll(<Widget>[
      _Slot(
        slotKey: WatchPage.bundleLagKey,
        whyKey: WatchPage.whyKeyFor('bundle_lag'),
        label: '任务包落差',
        value: _bundleLagText(s.bundleLag),
        why: s.detailFor('bundle_lag'),
      ),
      _Slot(
        slotKey: WatchPage.clockSkewKey,
        whyKey: WatchPage.whyKeyFor('clock_skew_s'),
        label: '钟偏',
        value: _clockSkewText(s.clockSkewS),
        why: s.detailFor('clock_skew_s'),
      ),
      _Slot(
        slotKey: WatchPage.backlogKey,
        whyKey: WatchPage.whyKeyFor('upload_backlog'),
        label: '回传积压',
        // **`null` 这一支说「这一档没有回传」，不是 `0`。** 见 [noUploaderText]。
        value: s.uploadBacklog == null ? noUploaderText : '${s.uploadBacklog} 条',
        why: s.detailFor('upload_backlog'),
      ),
      _Slot(
        slotKey: WatchPage.diskKey,
        whyKey: WatchPage.whyKeyFor('disk_used_ratio'),
        label: '盘水位',
        value: _diskText(s.diskUsedRatio),
        why: s.detailFor('disk_used_ratio'),
      ),
      _Slot(
        slotKey: WatchPage.batteryKey,
        whyKey: WatchPage.whyKeyFor('battery_pct'),
        label: '电量',
        value: _batteryText(s.batteryPct, s.batteryAsOfMs),
        why: s.detailFor('battery_pct'),
      ),
      _Slot(
        slotKey: WatchPage.mirrorKey,
        whyKey: WatchPage.whyKeyFor('mirror'),
        label: '镜像盘',
        value: _mirrorText(s.mirror),
        why: s.detailFor('mirror'),
      ),
    ]);
    return out;
  }

  /// **空列表和 `null` 是两件事。** 空列表是「翻过盘了，没有落差」，`null`
  /// 是「盘上那份局面读不出来」。合并之后，一台读不出局面的狗在屏上写着
  /// 「没有落差」。
  String _bundleLagText(List<String>? lag) {
    if (lag == null) return unknownText;
    if (lag.isEmpty) return '没有落差';
    return '有 ${lag.length} 个槽比 current 新（${lag.join('、')}）';
  }

  /// **没有外部参照就是「不知道」，不是 0**（§3.3 第 4 条）：报 0 等于说
  /// 「钟是准的」，而狗会一脸认真地在错误的时间巡逻。
  String _clockSkewText(double? s) {
    if (s == null) return unknownText;
    final String n = s.abs().toStringAsFixed(1);
    if (s == 0) return '本地钟对得上（0.0 秒）';
    return s > 0 ? '本地钟快了 $n 秒' : '本地钟慢了 $n 秒';
  }

  /// **盘水位是已用比例 0-1**，屏上要写成百分数。
  ///
  /// **千万别跟 [_batteryText] 共用一个格式化函数**（`app/watch.py` 里那段
  /// 注释专门写了这件事）：`${diskUsedRatio}%` 会把一块 83% 满的盘画成「0.83%」，
  /// 而这一格存在的全部理由就是「盘快满了要看得见」—— 那一手滑会让它反着报，
  /// 而且是以最安静的方式。
  String _diskText(double? ratio) =>
      ratio == null ? unknownText : '已用 ${(ratio * 100).round()}%';

  /// **电量已经是百分数 0-100**，不许再乘一次 100。见 [_diskText]。
  ///
  /// **收到的时刻一起摆出来。** 电量是事件推出来的，链路断了它不会自己变回
  /// `null`，只会一直停在最后一个读数上：狗在地下室断链 40 分钟，屏上稳稳
  /// 写着 31%。一个不再更新的数比 `null` 更危险，因为它看起来像在更新。
  /// **这儿不设阈值、不替人判「多久算旧」** —— 把时刻摆出来，让看的人自己算。
  String _batteryText(double? pct, int? asOfMs) {
    if (pct == null) return unknownText;
    final String when = asOfMs == null
        ? '（收到时刻$unknownText）'
        : '（收到于 ${_stampText(asOfMs)}）';
    return '${pct.round()}%$when';
  }

  String _mirrorText(MirrorSummary? m) {
    if (m == null) return unknownText;
    // **一块盘都没有的时候 behind 是 `null` 而不是 0**：「落后 0 趟」是一句
    // 听着让人放心的假话。
    final String behind = m.behind == null ? unknownText : '落后 ${m.behind} 趟';
    return <String>[
      '认到 ${m.disks} 块',
      '能用 ${m.usable} 块',
      '读到 ${m.measured} 块',
      behind,
      if (m.full == true) '有盘满了',
      if (m.lastSyncMs == null)
        '上次同步$unknownText'
      else
        '上次同步 ${_stampText(m.lastSyncMs!)}',
    ].join(' · ');
  }

  /// UTC 毫秒换成人读的样子。**带上 `UTC` 三个字母**（跟盘况屏一个规矩）：
  /// 不标的话现场的人会按自己的时区读。
  String _stampText(int ms) {
    final DateTime t = DateTime.fromMillisecondsSinceEpoch(ms, isUtc: true);
    String two(int n) => n.toString().padLeft(2, '0');
    return '${t.year}-${two(t.month)}-${two(t.day)} '
        '${two(t.hour)}:${two(t.minute)} UTC';
  }
}
