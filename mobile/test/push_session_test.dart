// 一次登录的推送登记（商业化 A6 外审 F2、F3）：拿不到号、登记失败退避重试；离开时在路上的登记落定后注销。
import 'dart:async';

import 'package:d1max_patrol/net/push.dart';
import 'package:flutter_test/flutter_test.dart';

class _Reg implements PushRegistrar {
  _Reg(this.ids);
  final List<String?> ids;
  int calls = 0;
  @override
  bool get supported => true;
  @override
  String get platform => 'android';
  @override
  Future<String?> registrationId() async {
    calls++;
    return ids.length > 1 ? ids.removeAt(0) : ids.first;
  }
}

void main() {
  test('F2：一开始拿不到推送号、第一次登记失败，退避重试，不用重新登录就登记上', () async {
    final log = <String>[];
    var fail = 1;
    final s = PushSession(_Reg([null, null, 'rid-1']),
        register: (id, p) async {
          if (fail-- > 0) throw Exception('网络抖了');
          log.add('+$id');
        },
        unregister: (id) async => log.add('-$id'),
        backoff: (_) => Duration.zero)
      ..start();
    expect(s.state.value, PushState.waiting);
    for (var i = 0; i < 50 && s.state.value != PushState.ready; i++) {
      await Future<void>.delayed(Duration.zero);
    }
    expect(s.state.value, PushState.ready);
    expect(log, ['+rid-1']);
    await s.stop();
    expect(log, ['+rid-1', '-rid-1']);
    expect(s.state.value, PushState.off);
  });

  test('F3：登记还在路上就离开了，等它落定，登记上了就马上注销', () async {
    final log = <String>[];
    final gate = Completer<void>();
    final s = PushSession(_Reg(['rid-1']),
        register: (id, p) async {
          await gate.future;                                 // 回执迟迟不来
          log.add('+$id');
        },
        unregister: (id) async => log.add('-$id'),
        backoff: (_) => Duration.zero)
      ..start();
    await Future<void>.delayed(Duration.zero);
    final stopping = s.stop();                             // 先离开
    gate.complete();                                       // 站点那头随后才登记上
    await stopping;
    expect(log, ['+rid-1', '-rid-1'], reason: '在路上的登记落定以后注销，不留在推送名单里');
  });

  test('离开以后不再重试', () async {
    final reg = _Reg([null]);
    final s = PushSession(reg,
        register: (id, p) async {},
        unregister: (id) async {},
        backoff: (_) => const Duration(milliseconds: 10))
      ..start();
    await Future<void>.delayed(const Duration(milliseconds: 35));
    expect(reg.calls, greaterThan(1), reason: '拿不到号：一直在重试');
    await s.stop();
    final after = reg.calls;
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(reg.calls, after);
    expect(s.state.value, PushState.off);
  });

  test('不支持推送的平台：什么都不做', () async {
    final s = PushSession(const NoPush(),
        register: (id, p) async => fail('不该登记'), unregister: (id) async => fail('不该注销'))
      ..start();
    expect(s.state.value, PushState.off);
    await s.stop();
  });
}
