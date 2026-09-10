/// 上线形状的契约夹具(挂账 11)。
///
/// **这些 JSON 不是手写的**，是 `tests/app/test_wire_fixtures.py` 从真的
/// 路由输出里生成的。狗那头改了形状，这里就该红 —— 而不是等到真机上点开
/// 那一屏才发现，那时候狗在客户现场。
///
/// 所以这个文件里**不许出现手写的 JSON 字面量**：那等于在这头又抄了一份
/// 形状，抄的那份跟狗那头不一样的那天，两边都是绿的。
library;

import 'dart:convert';
import 'dart:io';

import 'package:d1max_patrol/model/alert.dart';
import 'package:d1max_patrol/net/wire.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> _load(String name) => jsonDecode(
        File('test/fixtures/$name.json').readAsStringSync())
    as Map<String, dynamic>;

void main() {
  test('租约：倒计时是相对量', () {
    final m = _load('lease_state');
    expect(m['expires_in_ms'], isA<int>());
    expect(m['ttl_ms'], isA<int>());
    expect(m['mine'], isA<bool>());
    expect(m['max_sessions'], isA<int>());
    expect(m['remote_sessions'], isA<int>());
    expect(m['notice'], isA<String>());
  });

  test('运行快照：挂起点位解析得出来', () {
    final m = _load('run_snapshot');
    expect(m['state'], 'SUSPENDED');
    final s = m['suspended_at'] as Map<String, dynamic>;
    expect(s['waypoint_name'], isA<String>());
    expect(s['at_ms'], isA<int>());
    // pose 这一份夹具必须非空 —— 否则这条断言测不出 position/orientation
    // 到底有哪些键，Dart 那头解析错了也不会红。
    final pose = s['pose'] as Map<String, dynamic>;
    final position = pose['position'] as Map<String, dynamic>;
    expect(position['x'], isA<num>());
    expect(position['y'], isA<num>());
    final orientation = pose['orientation'] as Map<String, dynamic>;
    expect(orientation['w'], isA<num>());
  });

  test('视频健康：每一路都有 online', () {
    final cams = _load('video_health')['cameras'] as Map<String, dynamic>;
    expect(cams.keys, containsAll(<String>['front', 'back']));
    for (final v in cams.values) {
      final cam = v as Map<String, dynamic>;
      expect(cam['online'], isA<bool>());
      expect(cam['viewers'], isA<int>());
    }
  });

  test('有一路真在出画面时：anyLive 为真，since_frame_s 是个数', () {
    // `video_health` 那一份两台相机都是 false、`since_frame_s` 都是 null，
    // 于是「有画面」这条路和 `since_frame_s` 的类型在这头从来没被夹具驱动
    // 过。Task 11 的遥控屏拿 `anyLive` 决定摇杆灰不灰 —— 它恒为假的话，
    // 那块判断在真机之前不会有人发现。
    final live = _load('video_health_live');
    final cams = live['cameras'] as Map<String, dynamic>;
    final front = cams['front'] as Map<String, dynamic>;
    expect(front['online'], isTrue);
    expect(front['since_frame_s'], isA<num>(),
        reason: 'null 不带类型信息，这头就学不到它是个数');
    expect((cams['back'] as Map<String, dynamic>)['since_frame_s'], isNull,
        reason: '没起过泵的相机没有「多久之前」可言，null 才是如实描述');
    expect(VideoHealth.fromJson(live).anyLive, isTrue);
  });

  test('留痕：切远程那条认得出来', () {
    final audit = _load('control_audit')['audit'] as List<dynamic>;
    expect(audit.map((e) => (e as Map<String, dynamic>)['kind']),
        contains('mode_switched'));
    expect(_load('control_audit')['max'], isA<int>());
  });

  test('盘况和身份都解析得出来', () {
    final storage = _load('storage');
    expect(storage, isNotEmpty);
    expect(storage['used_bytes'], isA<int>());
    expect(storage['total_bytes'], isA<int>());
    final identity = _load('identity');
    expect(identity, isNotEmpty);
    expect(identity['sn'], isA<String>());
  });

  test('租约：面板要的那几样都解析得出来', () {
    // 面板上每一格都对着这里的一个字段：谁拿着、是不是我、几个席位、
    // 那句「不核实」。少解一个，屏上就少一格，而少的那一格在真机之前
    // 没人会发现。
    final v = LeaseView.fromJson(_load('lease_state'));
    expect(v.mine, isTrue);
    expect(v.holderOperator, isNotEmpty);
    expect(v.holderRef, isNotNull);
    expect(v.maxSessions, isPositive);
    expect(v.remoteSessions, isNotNull);
    expect(v.notice, isNotEmpty, reason: '§6.3 那句话要原样上屏，解不出来就没得上');
  });

  test('租约：没人拿着的时候 expires_in_ms 是 null,不是 0', () {
    // 夹具里是「张三拿着」，`holder: null` 这条路夹具驱动不到 —— 所以照着
    // 夹具改这两个字段（狗那头 `LeaseState.to_wire()` 就是这么发的：没有
    // 持有者时 `holder` 和 `expires_in_ms` 一起是 null）。
    //
    // **`null` 和 0 不许合并**（`engine/lease.py:126-128`）：合并掉的话，
    // 一台谁也没在开的狗在屏上写着「已过期」。
    final m = <String, dynamic>{
      ..._load('lease_state'),
      'holder': null,
      'expires_in_ms': null,
      'mine': false,
    };
    final v = LeaseView.fromJson(m);
    expect(v.expiresInMs, isNull,
        reason: '塌成 0 的话「没人拿着」会被画成「已过期」');
    expect(v.held, isFalse);
    expect(v.mine, isFalse);
  });

  test('租约：challenger 拆成 ref 和 operator,跟 holder 一个样子', () {
    // 夹具里没人在抢（`challenger: null`），所以这条也照着夹具改一个字段。
    // 形状抄的是 `Holder.to_wire()` —— holder 和 challenger 在狗那头是同一
    // 个类型，这头也该是同一种拆法。
    final m = <String, dynamic>{
      ..._load('lease_state'),
      'challenger': <String, dynamic>{'ref': 'cc00cc00', 'operator': '王五'},
      'grace_in_ms': 15000,
    };
    final v = LeaseView.fromJson(m);
    expect(v.challenged, isTrue);
    expect(v.challengerRef, 'cc00cc00');
    expect(v.challengerOperator, '王五');
    expect(v.graceInMs, 15000);
    expect(LeaseView.fromJson(_load('lease_state')).challenged, isFalse,
        reason: '没人在抢的时候不许凭空长出一个挑战者');
  });

  test('租约：heartbeat_ms 解得出来,缺了是 null 不是 0', () {
    // 面板的校准周期是**从这个字段算出来的**（`engine/lease.py` 的
    // `LeaseState` 明写客户端不该硬编心跳间隔）。解不出来的话，面板会以为
    // 狗没说，一直按缺省的 3 秒走 —— 狗那头把 `LEASE_HEARTBEAT_MS` 调了，
    // 手机不跟，而两头的代码各自看都是对的。
    expect(LeaseView.fromJson(_load('lease_state')).heartbeatMs, 10000,
        reason: '夹具是狗那头生成的，出厂常量 LEASE_HEARTBEAT_MS = 10_000');
    // 老版本的狗压根没这一项。**缺了是 `null` 不是 0**：塌成 0 的话，
    // 「狗没说」跟「狗说 0」就分不开了，而后者是个不合法的数。
    final Map<String, dynamic> without = <String, dynamic>{
      ..._load('lease_state')
    }..remove('heartbeat_ms');
    expect(LeaseView.fromJson(without).heartbeatMs, isNull);
    expect(
        LeaseView.fromJson(<String, dynamic>{
          ..._load('lease_state'),
          'heartbeat_ms': null,
        }).heartbeatMs,
        isNull,
        reason: '发了个 JSON null 跟压根没发，在这头是同一个答案');
  });

  test('盘况：StorageView 吃得下狗发的那份,而且往 forecast 里伸得到手', () {
    // 这份夹具在仓库里躺了好几卷，**从来没有一处在读它** —— 于是盘况这一块
    // 是全仓唯一一个绕开契约机制的界面：狗那头改了字段名，Dart 这头照旧绿，
    // 直到有人在客户现场点开那一屏。
    final v = StorageView.fromJson(_load('storage'));
    expect(v.usedBytes, 420000000000);
    expect(v.totalBytes, 1000000000000);
    expect(v.usedRatio, closeTo(0.42, 1e-9));
    // **`runs` / `bytes_at_risk` / `detail` 在 `forecast` 段里，不在顶层。**
    // 写 `m['runs']` 的话恒为 null，屏上那份名单永远是空的 —— 而拿自己造的
    // 报文喂进去的测试照样绿。
    expect(v.forecastDetail, isNotEmpty,
        reason: 'detail 在 forecast 段里，往顶层伸手拿到的是 null');
    expect(v.forecastDetail, _load('storage')['forecast']['detail']);
    expect(v.bytesAtRisk, 0);
    expect(v.runs, isEmpty, reason: '生成这份夹具的那台狗上一趟归档也没有');
    // **`backup.level` 只有三档，没有 `warn`。** 夹具里是 `neutral`：
    // 没配备份盘是出厂常态，而它**不是警示态**（`engine/backup.py` 的
    // `backup_notice`：常年报警的东西等于没报警）。
    expect(v.backupLevel, backupLevelNeutral);
    expect(v.backupLevel,
        isIn(<String>[backupLevelOk, backupLevelNeutral, backupLevelPush]));
    expect(v.backupDetail, _load('storage')['backup']['detail']);
    expect(v.backupDetail, isNotEmpty);
    // 预告落没落盘。夹具里那台狗是落了盘的（true）。
    expect(v.noticeWritten, isTrue);
  });

  test('盘况：预告没落盘那一档解得出来,缺了这一项当成落了盘', () {
    // 夹具里 `notice_written` 恒为 true，**「没落盘」那一支夹具驱动不到** ——
    // 而那一支正是这块屏存在的理由。照着夹具改这两个字段（狗那头盘满或者
    // 只读文件系统时发的就是这个）。
    final m = <String, dynamic>{
      ..._load('storage'),
      'notice_written': false,
      'notice_detail': '预告没能记到盘上(只读文件系统)',
    };
    final v = StorageView.fromJson(m);
    expect(v.noticeWritten, isFalse);
    expect(v.noticeDetail, '预告没能记到盘上(只读文件系统)');
    // 老版本的狗压根没这一项。**缺了按「落了盘」处理**：默认为假的话，
    // 每一台老狗上那块屏都常年顶着一句吓人的红话，而它其实什么也没发生。
    final without = <String, dynamic>{..._load('storage')}
      ..remove('notice_written');
    expect(StorageView.fromJson(without).noticeWritten, isTrue);
  });

  test('租约：challenging 在报文里,没人在抢的时候是 false 不是缺席', () {
    // 挂账 61。**键必须在**：狗那头无条件发这一位（跟 `mine` 一样），缺席
    // 只会出现在老版本的狗上。夹具是「张三拿着、没人在抢」那一份，所以这里
    // 是 false —— 但 false 和「压根没这个键」在这头是两件事，前者是狗说的，
    // 后者是我们自己兜的底。
    final m = _load('lease_state');
    expect(m['challenging'], isA<bool>(),
        reason: '缺了这一位，两台同名的手机在屏上就分不开');
    expect(m['challenging'], isFalse);
    expect(LeaseView.fromJson(m).challenging, isFalse);
    // 照着夹具摆一个「有人在抢，而且那个人是我」。**`challenger` 那一段跟
    // 「别人在抢」的那一份可以一个字不差** —— 判据只有 `challenging`。
    final mine = <String, dynamic>{
      ...m,
      'challenger': <String, dynamic>{'ref': 'cc00cc00', 'operator': '张三'},
      'challenging': true,
    };
    final other = <String, dynamic>{...mine, 'challenging': false};
    expect(LeaseView.fromJson(mine).challenging, isTrue);
    expect(LeaseView.fromJson(other).challenging, isFalse);
    expect(LeaseView.fromJson(mine).challengerOperator,
        LeaseView.fromJson(other).challengerOperator,
        reason: '同名两台手机：名字比不出来，这条断言就是那个事实本身');
    // 老版本的狗压根没这一位。**缺了是 false**：宁可退回到不指名的那句话，
    // 也不许指错人。
    final without = <String, dynamic>{...mine}..remove('challenging');
    expect(LeaseView.fromJson(without).challenging, isFalse);
  });

  test('告警：三种局面的形状都在这一份里', () {
    // 这一份取的是 `/api/alerts/all` —— 只有它里头同时有没人管的、有人确认
    // 过的、已经解决的。`acked_ms`/`resolved_ms` 恒为 null 的那种夹具，教不
    // 会这头它们非空时长什么样，而 `model/alert.dart` 上那段注释说的正是解析
    // 错的后果：屏上写着「1970-01-01 已确认」。
    final alerts = alertsFromWire(_load('alerts'));
    expect(alerts, hasLength(4));
    final byKind = <String, Alert>{for (final a in alerts) a.kind: a};
    final fallen = byKind['fallen']!;
    expect(fallen.isP1, isTrue);
    expect(fallen.acked, isFalse, reason: '没人确认过，acked_ms 是 null');
    expect(fallen.resolved, isFalse);
    // **`channel` 是狗那头算好发过来的，不是这头再算一遍**（裁决十一）。
    // 这一条已经升到顶了，所以它同时钉住了 escalated 和 channel 两个键。
    expect(fallen.escalated, 2);
    expect(fallen.channel, 'sound');
    final estop = byKind['estop_pressed']!;
    expect(estop.acked, isTrue);
    expect(estop.ackedBy, isNotEmpty);
    expect(estop.channel, 'screen');
    final lag = byKind['bundle_lag']!;
    // 同一个 kind 报了两次，聚合成一条。**first_ms 和 last_ms 是两个不同的
    // 数** —— 一样的话，这两个键读串了也没人发现。
    expect(lag.count, 2);
    expect(lag.lastMs, greaterThan(lag.firstMs));
    expect(lag.resolved, isTrue);
    expect(lag.acked, isFalse,
        reason: '解决不许冒充确认：狗那头 acked_* 原样不动（§5.3）');
    expect(byKind['run_done']!.isP1, isFalse);
    // 键里同时带 `/` 和 `#`，拼进 URL 之前必须整段转义。
    expect(fallen.key, contains('/'));
    expect(fallen.key, contains('#'));
  });

  test('值守汇总：六项都是非缺省值,量纲是 0-1 不是 0-100', () {
    final m = _load('watch_summary');
    // 裁决三十一：这个键叫 `disk_used_ratio`，**旧名字 `disk_pct` 不许再出现**
    // —— 名字里写着 pct 而值是比例，是一次「把 83% 满的盘画成 0.83%」的手滑
    // 的全部由来。
    expect(m.containsKey('disk_pct'), isFalse);
    expect(m['disk_used_ratio'], closeTo(0.83, 1e-9));
    final v = WatchSummary.fromWire(m);
    expect(v.diskUsedRatio, closeTo(0.83, 1e-9));
    // **两个量纲不一样,这一条就是那件事本身**：盘是比例 0-1，电量是百分数
    // 0-100。共用一个格式化函数的那天，其中一格会反着报。
    expect(v.batteryPct, 36.0);
    expect(v.diskUsedRatio!, lessThan(1.0));
    expect(v.batteryPct!, greaterThan(1.0));
    expect(v.clockSkewS, closeTo(12.5, 1e-9));
    expect(v.batteryAsOfMs, isNotNull);
    // **`null` 不许塌成 0。** 这一卷回传功能还没有，报 0 等于告诉人「证据都
    // 传上去了」，而它们全在狗上堆着。
    expect(v.uploadBacklog, isNull);
    expect(v.detailFor('upload_backlog'), isNotEmpty,
        reason: '一个光秃秃的「不知道」摆在屏上，本身就是一次新的静默失败');
    // 嵌套那两块（列表、对象）都得真的走一遍：缺省值摆出来的夹具走不到。
    expect(v.bundleLag, <String>['site-kl-4']);
    final mirror = v.mirror!;
    expect(mirror.disks, 1);
    expect(mirror.usable, 1);
    expect(mirror.measured, 1);
    expect(mirror.behind, 1);
    expect(mirror.behindBytes, isPositive);
    expect(mirror.full, isFalse);
    expect(mirror.lastSyncMs, isNotNull);
    // 屏上那一格要跟告警栏对得上：同一份事实喂给两处，这一份夹具里盘水位
    // 过了 disk_80 的线、盘上还有个没生效的槽，于是有两条未解决的告警。
    expect(v.alertsOpen, 2);
  });

  test('三个视图都吃得下狗发的那份', () {
    expect(LeaseView.fromJson(_load('lease_state')).expiresInMs, isPositive);
    expect(VideoHealth.fromJson(_load('video_health')).online, isNotEmpty);
    expect(RunView.fromJson(_load('run_snapshot')).suspended, isTrue);
  });
}
