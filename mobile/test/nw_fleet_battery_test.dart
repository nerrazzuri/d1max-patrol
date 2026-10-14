// 狗列表的电量从值守汇总里取（2026-10-14 重做，四批合到一起时补的）：狗的视图里没有电量，汇总里有。
import 'dart:async';

import 'package:d1max_patrol/ui/nw/nw_cards.dart';
import 'package:d1max_patrol/ui/nw/nw_p3_robots.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> _robot(String id) => {
      'robot_id': id,
      'status': {'online': true, 'ready': {'estop_clear': true}, 'task': null},
    };

/// [age]：电量是多久以前读的（缺省刚读的）；给 [noAsOf] 就不带读数时刻。
Map<String, dynamic> _summary(Map<String, num?> battery, {Duration age = Duration.zero, bool noAsOf = false}) => {
      'robots': [
        for (final e in battery.entries)
          {
            'robot_id': e.key, 'online': true, 'fresh': true, 'battery_pct': e.value, 'why': <String, String>{},
            if (!noAsOf) 'battery_as_of_ms': _clock.subtract(age).millisecondsSinceEpoch,
          },
      ],
      'site': <String, dynamic>{},
    };

DateTime _clock = DateTime(2026, 10, 14, 2);

Widget _fleet(List<Map<String, dynamic>> robots, Future<Map<String, dynamic>> Function()? load) => MaterialApp(
    home: Scaffold(
        body: SingleChildScrollView(
            child: NwFleet(
                robots: robots, activityOf: (_) => 'x', onOpen: (_) {}, loadSummary: load, now: () => _clock))));

num? _batteryOf(WidgetTester t, String id) => t.widget<NwDogCard>(find.byKey(Key('robot-$id'))).battery;

void main() {
  testWidgets('汇总里有电量：贴到对应的狗卡上；汇总里是「不知道」的那只不写数', (t) async {
    await t.pumpWidget(_fleet([_robot('A'), _robot('B')], () async => _summary({'A': 82, 'B': null})));
    expect(_batteryOf(t, 'A'), isNull, reason: '还没读到：不写数');
    await t.pump();
    expect(_batteryOf(t, 'A'), 82);
    expect(_batteryOf(t, 'B'), isNull);
  });

  testWidgets('汇总读不到：电量不写数，不报错；没给汇总也一样', (t) async {
    await t.pumpWidget(_fleet([_robot('A')], () async => throw StateError('没连上')));
    await t.pump();
    expect(_batteryOf(t, 'A'), isNull);
    expect(t.takeException(), isNull);
    await t.pumpWidget(_fleet([_robot('A')], null));
    await t.pump();
    expect(_batteryOf(t, 'A'), isNull);
  });

  testWidgets('上一次读到过、这一次读不到：不留着旧的数当现在的', (t) async {
    var fail = false;
    Future<Map<String, dynamic>> load() async => fail ? throw StateError('断了') : _summary({'A': 82});
    await t.pumpWidget(_fleet([_robot('A')], load));
    await t.pump();
    expect(_batteryOf(t, 'A'), 82);
    fail = true;
    _clock = _clock.add(NwFleet.batteryEvery);
    await t.pumpWidget(_fleet([_robot('A')], load));          // 列表换了一份、也过了间隔：重读
    await t.pump();
    expect(_batteryOf(t, 'A'), isNull);
  });

  testWidgets('先发的那次回得晚：不盖掉后发的那次读到的', (t) async {
    final first = Completer<Map<String, dynamic>>();
    var n = 0;
    Future<Map<String, dynamic>> load() => ++n == 1 ? first.future : Future.value(_summary({'A': 40}));
    await t.pumpWidget(_fleet([_robot('A')], load));          // 第一次：卡在路上
    _clock = _clock.add(NwFleet.batteryEvery);
    await t.pumpWidget(_fleet([_robot('A')], load));          // 第二次：马上回 40
    await t.pump();
    expect(_batteryOf(t, 'A'), 40);
    first.complete(_summary({'A': 99}));                      // 第一次这才回来
    await t.pump();
    expect(_batteryOf(t, 'A'), 40, reason: '迟到的旧回复不算');
  });

  testWidgets('刷新得勤：间隔没到不跟着每次都问汇总', (t) async {
    var n = 0;
    Future<Map<String, dynamic>> load() async {
      n++;
      return _summary({'A': 82});
    }
    await t.pumpWidget(_fleet([_robot('A')], load));
    await t.pump();
    for (var i = 0; i < 5; i++) {
      await t.pumpWidget(_fleet([_robot('A')], load));        // 每次都是新的一份列表
      await t.pump();
    }
    expect(n, 1);
  });

  testWidgets('狗的视图自己带了电量：用它自己的，不用汇总的', (t) async {
    final r = {..._robot('A'), 'battery_pct': 15};
    await t.pumpWidget(_fleet([r], () async => _summary({'A': 82})));
    await t.pump();
    expect(_batteryOf(t, 'A'), 15);
  });

  group('电量多旧：超过 10 分钟的不写数', () {
    testWidgets('刚读的：写数', (t) async {
      await t.pumpWidget(_fleet([_robot('A')], () async => _summary({'A': 82}, age: const Duration(minutes: 9))));
      await t.pump();
      expect(_batteryOf(t, 'A'), 82);
    });

    testWidgets('11 分钟前读的：不写数（当不知道）', (t) async {
      await t.pumpWidget(_fleet([_robot('A')], () async => _summary({'A': 82}, age: const Duration(minutes: 11))));
      await t.pump();
      expect(_batteryOf(t, 'A'), isNull);
    });

    testWidgets('没说什么时候读的：不写数', (t) async {
      await t.pumpWidget(_fleet([_robot('A')], () async => _summary({'A': 82}, noAsOf: true)));
      await t.pump();
      expect(_batteryOf(t, 'A'), isNull);
    });

    testWidgets('读的时候是新的、后来放旧了：到时候也不写了', (t) async {
      final robots = [_robot('A')];
      await t.pumpWidget(_fleet(robots, () async => _summary({'A': 82})));
      await t.pump();
      expect(_batteryOf(t, 'A'), 82);
      _clock = _clock.add(const Duration(minutes: 11));
      await t.pumpWidget(_fleet(robots, () async => throw StateError('不该重读')));  // 同一份列表：不重读
      expect(_batteryOf(t, 'A'), isNull);
    });
  });
}
