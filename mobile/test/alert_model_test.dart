/// 告警和值守汇总在手机这头的解析（`model/alert.dart`）。
///
/// **狗那侧的字段会长。** 手机不许因为多一个字段就白屏 —— 这一卷里狗那侧
/// 已经加过 `channel`（裁决十一），下一卷还会加。所以每个字段显式给缺省，
/// 遇到 `null` 不许崩，遇到不认识的键当没看见。
///
/// **`null` 和 0 不许合并。** `acked_ms`/`resolved_ms`/`upload_backlog` 这几个
/// 是这条纪律最锋利的地方：塌成 0 之后，一条没人确认过的告警在屏上写着
/// 「1970-01-01 已确认」，而升级链那一头正指着「没人看见」这个事实。
///
/// **标识符全是 ASCII**：这台机器上的 Dart SDK 不接受非 ASCII 标识符。
library;

import 'package:d1max_patrol/model/alert.dart';
import 'package:flutter_test/flutter_test.dart';

/// 一条告警在 wire 上完整的 14 个键（`engine/alerts.py` 的 `Alert.to_wire()`）。
Map<String, dynamic> fullWire() => <String, dynamic>{
      'key': 'C40221/stuck#1',
      'level': 'P1',
      'kind': 'stuck',
      'robot': 'C40221',
      'title': '狗卡住了',
      'detail': '在阀门间原地打转 3 分钟',
      'first_ms': 1757400000000,
      'last_ms': 1757400180000,
      'count': 4,
      'acked_by': '老王',
      'acked_ms': 1757400200000,
      'resolved_ms': null,
      'escalated': 1,
      'channel': 'push',
    };

void main() {
  test('十四个键一个不落地解出来', () {
    final Alert a = Alert.fromWire(fullWire());
    expect(a.key, 'C40221/stuck#1');
    expect(a.level, 'P1');
    expect(a.kind, 'stuck');
    expect(a.robot, 'C40221');
    expect(a.title, '狗卡住了');
    expect(a.detail, '在阀门间原地打转 3 分钟');
    expect(a.firstMs, 1757400000000);
    expect(a.lastMs, 1757400180000);
    expect(a.count, 4);
    expect(a.ackedBy, '老王');
    expect(a.ackedMs, 1757400200000);
    expect(a.resolvedMs, isNull);
    expect(a.escalated, 1);
    expect(a.channel, 'push');
  });

  test('多出三个没见过的字段照样解得出来,不许白屏', () {
    // 狗那侧字段会长。手机因为多一个字段就崩的话，一次狗那头的小升级
    // 就是现场一块黑屏。
    final Map<String, dynamic> m = fullWire()
      ..['ack_note'] = '在路上了'
      ..['snoozed_ms'] = 1757400300000
      ..['tags'] = <String>['夜班'];
    final Alert a = Alert.fromWire(m);
    expect(a.key, 'C40221/stuck#1');
    expect(a.level, 'P1');
    expect(a.count, 4);
  });

  test('字段全缺也不崩,给得出缺省', () {
    final Alert a = Alert.fromWire(const <String, dynamic>{});
    expect(a.key, '');
    expect(a.level, '');
    expect(a.ackedMs, isNull);
    expect(a.resolvedMs, isNull);
    expect(a.acked, isFalse);
    expect(a.resolved, isFalse);
  });

  test('没确认过的时候 acked_ms 是 null,不许塌成 0', () {
    // 塌成 0 之后，一条没人确认过的告警在屏上写着「1970-01-01 已确认」，
    // 而升级链那一头（§5.3）正指着「没人看见」这个事实。
    final Alert a =
        Alert.fromWire(fullWire()..['acked_ms'] = null..['acked_by'] = '');
    expect(a.ackedMs, isNull);
    expect(a.acked, isFalse);
  });

  test('认不出的级别不许自己升成 P1', () {
    // 狗那侧以后加了第四档，手机不许凭一个读不懂的字符串把人半夜叫起来，
    // 也不许把它悄悄吃掉 —— 原样留着，屏上照登。
    final Alert a = Alert.fromWire(fullWire()..['level'] = 'P0');
    expect(a.level, 'P0');
    expect(a.isP1, isFalse);
  });

  test('值守汇总六项解得出来,量纲不动', () {
    final WatchSummary s = WatchSummary.fromWire(<String, dynamic>{
      'bundle_lag': <String>['b-0912'],
      'clock_skew_s': 12.5,
      'upload_backlog': null,
      'disk_pct': 0.83,
      'battery_pct': 36.0,
      'battery_as_of_ms': 1757400000000,
      'alerts_open': 2,
      'mirror': <String, dynamic>{
        'disks': 2,
        'usable': 1,
        'measured': 1,
        'behind': 7,
        'behind_bytes': 9000000000,
        'full': false,
        'last_sync_ms': 1757300000000,
      },
      'detail': <String, dynamic>{'upload_backlog': '这台狗还没装回传功能'},
    });
    expect(s.bundleLag, <String>['b-0912']);
    expect(s.clockSkewS, 12.5);
    // **这一卷恒为 null。** 报 0 等于告诉人「证据都传上去了」，而它们全在
    // 狗上堆着。
    expect(s.uploadBacklog, isNull);
    // 已用**比例** 0-1，不是百分数 —— 模型这一层不换算。
    expect(s.diskPct, 0.83);
    // **百分数** 0-100，跟上面那一行量纲不同。
    expect(s.batteryPct, 36.0);
    expect(s.batteryAsOfMs, 1757400000000);
    expect(s.alertsOpen, 2);
    expect(s.mirror?.disks, 2);
    expect(s.mirror?.usable, 1);
    expect(s.mirror?.measured, 1);
    expect(s.mirror?.behind, 7);
    expect(s.mirror?.full, isFalse);
    expect(s.mirror?.lastSyncMs, 1757300000000);
    expect(s.detailFor('upload_backlog'), '这台狗还没装回传功能');
  });

  test('汇总里 null 的那几项留着 null,不许塌成 0', () {
    final WatchSummary s = WatchSummary.fromWire(const <String, dynamic>{
      'bundle_lag': null,
      'clock_skew_s': null,
      'upload_backlog': null,
      'disk_pct': null,
      'battery_pct': null,
      'battery_as_of_ms': null,
      'mirror': null,
    });
    expect(s.bundleLag, isNull);
    expect(s.clockSkewS, isNull);
    expect(s.uploadBacklog, isNull);
    expect(s.diskPct, isNull);
    expect(s.batteryPct, isNull);
    expect(s.batteryAsOfMs, isNull);
    expect(s.mirror, isNull);
    expect(s.detailFor('upload_backlog'), '');
  });

  test('空落差和读不出落差是两件事', () {
    // 空列表是「翻过盘了，没有落差」，`null` 是「盘上那份局面读不出来」。
    // 合并之后，一台读不出任务包局面的狗在屏上写着「没有落差」。
    expect(
        WatchSummary.fromWire(const <String, dynamic>{'bundle_lag': <String>[]})
            .bundleLag,
        <String>[]);
    expect(
        WatchSummary.fromWire(const <String, dynamic>{'bundle_lag': null})
            .bundleLag,
        isNull);
  });

  test('汇总多出没见过的字段照样解得出来', () {
    final WatchSummary s = WatchSummary.fromWire(<String, dynamic>{
      'disk_pct': 0.5,
      'net_backlog': 3,
      'gpu_temp_c': 61.5,
    });
    expect(s.diskPct, 0.5);
  });

  test('整数报成整数,浮点报成浮点,两边都收得下', () {
    // 狗那头 `disk_pct` 是 `used / total`（浮点），可 `clock_skew_s` 在钟正好
    // 对得上时是 `0`（JSON 里就是个整数）。只认 `double` 的话那一拍会崩。
    final WatchSummary s = WatchSummary.fromWire(
        const <String, dynamic>{'clock_skew_s': 0, 'battery_pct': 100});
    expect(s.clockSkewS, 0.0);
    expect(s.batteryPct, 100.0);
  });

  // ---------------------------------------------------- 名单：读不懂就抛

  test('读得懂的名单顺序原样留着', () {
    final List<Alert> got = alertsFromWire(<String, dynamic>{
      'alerts': <Map<String, dynamic>>[
        <String, dynamic>{'key': 'a', 'level': 'P2'},
        <String, dynamic>{'key': 'b', 'level': 'P1'},
      ],
    });
    expect(got.map((Alert a) => a.key).toList(), <String>['a', 'b']);
  });

  test('体里没有名单那一段的时候抛,不许退回一份空名单', () {
    // 空名单在值守屏上不是「没数据」——它会被翻译成一句「现在没有需要立刻
    // 动身的事」，而那一刻狗上可能正挂着一条 P1。
    expect(() => alertsFromWire(const <String, dynamic>{}),
        throwsFormatException);
  });

  test('名单那一段不是名单的时候也抛', () {
    // 将来狗那侧改成 `{"items": [...]}`、或者某一拍回 `{"alerts": null}`。
    expect(() => alertsFromWire(const <String, dynamic>{'alerts': null}),
        throwsFormatException);
    expect(() => alertsFromWire(const <String, dynamic>{'alerts': 'nope'}),
        throwsFormatException);
  });

  test('名单里有一条不是对象的时候整份都不算数,不许悄悄少一条', () {
    // 少掉的那条正好是 P1 的那天，屏上剩下的几条看着一切正常。
    expect(
        () => alertsFromWire(<String, dynamic>{
              'alerts': <Object>[fullWire(), 'not-an-alert'],
            }),
        throwsFormatException);
  });
}
