/// 告警和值守汇总在手机这头的样子（§5.1、§5.2、§5.3）。
///
/// **全是只读的**，跟 `net/wire.dart` 一个规矩：真理源在狗身上（告警簿活在
/// `engine/alerts.py` 里，有自己的生命周期），手机改了也不算数；能改的模型
/// 只会诱使人在界面上先改再发，而那两步之间的窗口里，屏幕上显示的是一件
/// 没发生的事。
///
/// **不判级、不排序、不换算量纲。** 三件事各有各的理由：
///
/// * **判级**（哪个 kind 是 P1）只在 `engine/alerts.py` 的那张表里说一次。
/// * **排序**（先级别再 `last_ms` 倒序）在 `AlertBook.open()` 里。狗那侧的
///   `_alerts_open` 处理器自己都写着「这儿不再排一遍」—— 同一件事有两个出处，
///   对不上的那天，屏幕第一行显示的不是该起身的那件事。
/// * **量纲**：`disk_used_ratio` 是已用**比例** 0-1，`battery_pct` 是**百分数**
///   0-100（`app/watch.py` 里那两段注释专门写了这件事）。模型这一层原样收着，
///   换算只在渲染那一处做，而且两处不共用一个格式化函数。
///
/// **`null` 一律不许塌成 0。** `0` 是「查过了，没有」，`null` 是「这一档还没有
/// 这个能力 / 这一拍不知道」—— 两句话在屏幕上长得一样，在现场差着一次事故。
/// `upload_backlog` 是这条纪律最锋利的一处：报 `0` 等于告诉人「证据都传上去
/// 了」，而它们全在狗上堆着。
///
/// **每个字段显式给缺省，遇到 `null` 不许崩，遇到不认识的键当没看见。**
/// 狗那侧字段会长（这一卷已经加过 `channel`，见裁决十一），手机因为多一个
/// 字段就崩的话，一次狗那头的小升级就是现场一块黑屏。
library;

/// 一条告警（`engine/alerts.py` 的 `Alert.to_wire()`，14 个键）。
class Alert {
  /// 聚合键，形如 `robot/kind#seq`。
  ///
  /// **里面同时带着 `/` 和 `#`。** 拼进 URL 之前必须整段
  /// `Uri.encodeComponent` —— 不转义的话 `#` 会被 `Uri.parse` 当 fragment
  /// 从请求里整段切掉，狗收到的路径连 `/ack` 都没有了。见
  /// `net/patrol_client.dart` 的 `ackAlert`。
  final String key;

  /// `"P1"` / `"P2"` / `"P3"`。**原样留着字符串。**
  ///
  /// 翻成本地枚举的话，狗那侧加了第四档时手机这头只有两种下场：崩，或者
  /// 悄悄归到某一档里去。原样留着，认不出的档照登不误，[isP1] 说不是就
  /// 不是 —— 不许凭一个读不懂的字符串把人半夜叫起来。
  final String level;

  final String kind;
  final String robot;
  final String title;
  final String detail;

  /// 第一次触发、最近一次触发（UTC 毫秒），以及重复了多少次。
  final int firstMs;
  final int lastMs;
  final int count;

  /// 谁确认的。没人确认过就是空串（狗那侧的缺省值也是空串）。
  final String ackedBy;

  /// 什么时候确认的。**没人确认过是 `null`，不是 0。**
  ///
  /// 塌成 0 之后，一条没人确认过的告警在屏上写着「1970-01-01 已确认」，而
  /// 升级链（§5.3）那一头正指着「没人看见」这个事实。
  final int? ackedMs;

  /// 什么时候解决的。没解决就是 `null`。
  ///
  /// **跟 [ackedMs] 是两个独立字段，不是同一个状态机上的两档**：「我看见了
  /// 在处理」和「这事没了」是两回事（§5.3）。
  final int? resolvedMs;

  /// 升到第几档通道了（0 = 只在大屏上）。
  final int escalated;

  /// 当前该用哪个通道播（`"screen"` / `"push"` / `"sound"`）。
  ///
  /// **狗那侧把它和 [escalated] 一起发过来了**（裁决十一），手机不自己再算
  /// 一遍那张表 —— 同一份判据落在两处早晚分叉，而分叉的那天正好是该出声的
  /// 那次没出声。
  final String channel;

  const Alert({
    required this.key,
    required this.level,
    required this.kind,
    required this.robot,
    required this.title,
    required this.detail,
    required this.firstMs,
    required this.lastMs,
    required this.count,
    required this.ackedBy,
    required this.ackedMs,
    required this.resolvedMs,
    required this.escalated,
    required this.channel,
  });

  factory Alert.fromWire(Map<String, dynamic> m) => Alert(
        key: (m['key'] as String?) ?? '',
        level: (m['level'] as String?) ?? '',
        kind: (m['kind'] as String?) ?? '',
        robot: (m['robot'] as String?) ?? '',
        title: (m['title'] as String?) ?? '',
        detail: (m['detail'] as String?) ?? '',
        firstMs: (m['first_ms'] as num?)?.toInt() ?? 0,
        lastMs: (m['last_ms'] as num?)?.toInt() ?? 0,
        // 一条画得出来的告警至少发生过一次；`0 次` 是句没有意义的话。
        count: (m['count'] as num?)?.toInt() ?? 1,
        ackedBy: (m['acked_by'] as String?) ?? '',
        // **不给缺省 0。** 见 [ackedMs]。
        ackedMs: (m['acked_ms'] as num?)?.toInt(),
        resolvedMs: (m['resolved_ms'] as num?)?.toInt(),
        escalated: (m['escalated'] as num?)?.toInt() ?? 0,
        channel: (m['channel'] as String?) ?? '',
      );

  /// 要人立刻动身的那一档（§5.2）。
  bool get isP1 => level == levelP1;

  /// 有人说过「我看见了」吗。**升级只看这个，不看解没解决。**
  bool get acked => ackedMs != null;

  bool get resolved => resolvedMs != null;
}

/// 立刻动身的那一档。**字符串常量，不是枚举** —— 见 [Alert.level]。
const String levelP1 = 'P1';

/// 镜像盘那一档（`app/watch.py` 的 `_镜像`）。
///
/// **整档是悲观聚合：最差的那块盘代表这台狗。** 想知道具体哪块盘落后多少，
/// 看 `/api/backup`，那里是一块盘一行。
class MirrorSummary {
  /// 认到几块、其中声称能用几块、这一拍真的读出来了几块。
  ///
  /// [usable] 和 [measured] 不等的时候狗那侧的 `detail` 里必有一句话 ——
  /// 在一块以消灭静默失败为职责的屏上，不许自己造一个静默失败。
  final int disks;
  final int usable;
  final int measured;

  /// 落后多少趟 / 多少字节。**一块盘都没有的时候是 `null` 而不是 0**：
  /// 一块镜像盘都没有的时候，「落后 0 趟」是一句听着让人放心的假话。
  final int? behind;
  final int? behindBytes;

  /// 有没有哪块镜像盘满了。量不出来就是 `null`。
  final bool? full;

  /// 最近一次同步（取几块盘里**最旧**的那个）。一趟也没同步过是 `null`，
  /// 不是 0 —— 画出来是 1970-01-01，一个看着像真事的假时刻。
  final int? lastSyncMs;

  const MirrorSummary({
    required this.disks,
    required this.usable,
    required this.measured,
    required this.behind,
    required this.behindBytes,
    required this.full,
    required this.lastSyncMs,
  });

  factory MirrorSummary.fromWire(Map<String, dynamic> m) => MirrorSummary(
        disks: (m['disks'] as num?)?.toInt() ?? 0,
        usable: (m['usable'] as num?)?.toInt() ?? 0,
        measured: (m['measured'] as num?)?.toInt() ?? 0,
        behind: (m['behind'] as num?)?.toInt(),
        behindBytes: (m['behind_bytes'] as num?)?.toInt(),
        full: m['full'] as bool?,
        lastSyncMs: (m['last_sync_ms'] as num?)?.toInt(),
      );
}

/// 值守屏那六项（`GET /api/watch/summary`，`app/watch.py` 的 `watch_summary`）。
///
/// 六项的共同点是**它们的失败都是静默的**：盘满了、包落好了没生效、钟漂了、
/// 证据在狗上堆着、备份盘早就坏了 —— 每一件都不会自己冒出来喊一声。
class WatchSummary {
  /// 比 `current` 新的槽名。**空列表和 `null` 是两件事**：空列表是「翻过盘
  /// 了，没有落差」，`null` 是「盘上那份局面读不出来」。
  final List<String>? bundleLag;

  /// 本地钟快了多少**秒**（正数 = 走快了）。没有外部参照就是 `null` ——
  /// 报 0 等于说「钟是准的」（§3.3）。
  final double? clockSkewS;

  /// 还有多少条证据没传上去。**这一卷恒为 `null`**（这台狗还没装回传功能）。
  final int? uploadBacklog;

  /// 盘已用**比例 0-1**，不是 0-100。见文件头那段量纲。
  final double? diskUsedRatio;

  /// 电量**百分数 0-100**，不是 0-1。
  final double? batteryPct;

  /// 上面那个电量是**什么时候**收到的（UTC 毫秒）。
  ///
  /// **这一档不设阈值、不替人判「多久算旧」。** 电量是事件推出来的，链路断
  /// 了它不会自己变回 `null`，只会一直停在最后一个读数上。一个不再更新的数
  /// 比 `null` 更危险，因为它看起来像在更新。
  final int? batteryAsOfMs;

  /// 现在有几条未解决的告警。
  final int alertsOpen;

  final MirrorSummary? mirror;

  /// 每一档报不出来时狗附的那句人话（键就是上面那几个字段的 wire 名）。
  ///
  /// **原样上屏，手机不重新组织。** 一个光秃秃的「不知道」摆在屏上，人分不
  /// 清是「没查」还是「查了没事」—— 那本身就是一次新的静默失败。措辞归狗
  /// 那头：手机这头硬编一句的话，狗改了措辞手机不跟。
  final Map<String, String> detail;

  const WatchSummary({
    required this.bundleLag,
    required this.clockSkewS,
    required this.uploadBacklog,
    required this.diskUsedRatio,
    required this.batteryPct,
    required this.batteryAsOfMs,
    required this.alertsOpen,
    required this.mirror,
    required this.detail,
  });

  factory WatchSummary.fromWire(Map<String, dynamic> m) {
    final Object? lag = m['bundle_lag'];
    final Object? mir = m['mirror'];
    final Object? why = m['detail'];
    return WatchSummary(
      bundleLag: lag is List
          ? <String>[for (final Object? s in lag) '$s']
          : null,
      // `(x as num?)?.toDouble()`：钟正好对得上时狗那头发的是整数 `0`，
      // 只认 `double` 的话那一拍会崩。
      clockSkewS: (m['clock_skew_s'] as num?)?.toDouble(),
      uploadBacklog: (m['upload_backlog'] as num?)?.toInt(),
      diskUsedRatio: (m['disk_used_ratio'] as num?)?.toDouble(),
      batteryPct: (m['battery_pct'] as num?)?.toDouble(),
      batteryAsOfMs: (m['battery_as_of_ms'] as num?)?.toInt(),
      alertsOpen: (m['alerts_open'] as num?)?.toInt() ?? 0,
      mirror: mir is Map<String, dynamic> ? MirrorSummary.fromWire(mir) : null,
      detail: why is Map
          ? <String, String>{
              for (final MapEntry<Object?, Object?> e in why.entries)
                '${e.key}': '${e.value}',
            }
          : const <String, String>{},
    );
  }

  /// 某一档那句为什么。没有就是空串。
  String detailFor(String field) => detail[field] ?? '';
}

/// 一份告警名单（`{"alerts": [...]}`）。
///
/// **顺序原样留着**，见文件头。
///
/// **读不懂就抛，绝不退回一份空名单。** 空名单在这一屏上不是「没数据」，
/// 它会被翻译成一句主动的、带着「这一屏刚问过狗」的假保证 ——
/// 「现在没有需要立刻动身的事」—— 而那一刻狗上可能正挂着一条 P1。
/// 留白只是分不清「太平」和「没加载」；这一句是**说了假话**，是这一屏
/// 能犯的最坏的一种错。
///
/// 这条路今天就走得到：`net/patrol_client.dart` 的 `_send` 吞掉
/// `FormatException`（S-2，为了让状态码报得对），于是一份 200 + 一页强制
/// 门户 HTML 会原样变成一个空的 `{}` 交到这儿。
///
/// **单条读不懂也是整份不算数**，不是悄悄少一条：少掉的那条正好是 P1 的
/// 那天，屏上剩下的几条看着一切正常。跟 `store/registry_store.dart` 里
/// 「名册里有条目不是对象」一个规矩 —— 那儿也是整份读不出，不是少一只狗。
///
/// 抛的是 `FormatException`，跟 `model/robot.dart`、`store/registry_store.dart`
/// 同一个类型：调用点（`ui/watch_page.dart` 的 `_loadAlerts`）本来就把
/// 一切异常收进「读不到告警」那一支。
List<Alert> alertsFromWire(Map<String, dynamic> m) {
  final Object? raw = m['alerts'];
  if (raw is! List) {
    throw FormatException(m.containsKey('alerts')
        ? '狗回的告警名单不是个名单：${raw.runtimeType}'
        : '狗回的这份东西里没有 alerts 那一段，读不出告警');
  }
  return <Alert>[
    for (final Object? a in raw)
      if (a is Map<String, dynamic>)
        Alert.fromWire(a)
      else
        throw FormatException('告警名单里有一条不是对象：${a.runtimeType}'),
  ];
}
