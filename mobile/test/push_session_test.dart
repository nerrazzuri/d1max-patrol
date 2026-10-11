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
  String get provider => 'jpush';
  @override
  Future<String?> registrationId() async {
    calls++;
    return ids.length > 1 ? ids.removeAt(0) : ids.first;
  }
}

void main() {
  h9();
  test('F2：一开始拿不到推送号、第一次登记失败，退避重试，不用重新登录就登记上', () async {
    final log = <String>[];
    var fail = 1;
    final s = PushSession(_Reg([null, null, 'rid-1']),
        register: (id, p, prov) async {
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
        register: (id, p, prov) async {
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
        register: (id, p, prov) async {},
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
        register: (id, p, prov) async => fail('不该登记'), unregister: (id) async => fail('不该注销'))
      ..start();
    expect(s.state.value, PushState.off);
    await s.stop();
  });
}

// ---- RC 加固 H9：极光之外还有 FCM
class _Prov implements PushRegistrar {
  _Prov(this.provider, this.ids, {this.supported = true});
  final List<String?> ids;
  int calls = 0;
  @override
  final bool supported;
  @override
  final String provider;
  @override
  String get platform => 'android';
  @override
  Future<String?> registrationId() async {
    calls++;
    return ids.length > 1 ? ids.removeAt(0) : ids.first;
  }
}

Future<void> _settle(PushSession s, {int rounds = 80}) async {
  for (var i = 0; i < rounds; i++) {
    await Future<void>.delayed(Duration.zero);
  }
}

void h9() {
  test('H9：两个通道各登记各的，带着通道名；离开时两个都注销', () async {
    final log = <String>[];
    final s = PushSession(_Prov('jpush', ['jp-1']),
        also: [_Prov('fcm', [null, 'fcm-1'])],
        register: (id, p, prov) async => log.add('+$prov:$id'),
        unregister: (id) async => log.add('-$id'),
        backoff: (_) => Duration.zero)
      ..start();
    await _settle(s);
    expect(s.state.value, PushState.ready);
    expect(log, ['+jpush:jp-1', '+fcm:fcm-1']);
    await s.stop();
    expect(log.sublist(2), ['-jp-1', '-fcm-1']);
  });

  test('H9：有一个登记上就算登记上了；另一个一直拿不到号，再试几轮就不试了', () async {
    final log = <String>[];
    final jp = _Prov('jpush', [null]);                       // 没配极光的包：永远拿不到
    final s = PushSession(jp,
        also: [_Prov('fcm', ['fcm-1'])],
        extraTries: 3,
        register: (id, p, prov) async => log.add('+$prov:$id'),
        unregister: (id) async => log.add('-$id'),
        backoff: (_) => Duration.zero)
      ..start();
    await _settle(s);
    expect(s.state.value, PushState.ready);
    expect(s.reason, '');
    expect(log, ['+fcm:fcm-1']);
    expect(jp.calls, 4, reason: '第一轮加上再试 3 轮');
    await _settle(s);
    expect(jp.calls, 4, reason: '不再试了');
    await s.stop();
    expect(log, ['+fcm:fcm-1', '-fcm-1']);
  });

  test('H9：两个都没登记上：一直试，页面上说原因', () async {
    final a = _Prov('jpush', [null]);
    final s = PushSession(a,
        also: [_Prov('fcm', ['fcm-1'])],
        extraTries: 2,
        register: (id, p, prov) async => throw Exception('站点不通'),
        unregister: (id) async {},
        backoff: (_) => Duration.zero)
      ..start();
    await _settle(s, rounds: 40);
    expect(s.state.value, PushState.waiting);
    expect(s.reason, contains('站点不通'));
    expect(a.calls, greaterThan(5), reason: '没有一个登记上：不放弃');
    await s.stop();
  });

  test('H9：不支持的通道不碰；一个都不支持就不开', () async {
    final off = _Prov('fcm', ['x'], supported: false);
    final s = PushSession(_Prov('jpush', ['jp-1']),
        also: [off],
        register: (id, p, prov) async {},
        unregister: (id) async {},
        backoff: (_) => Duration.zero)
      ..start();
    await _settle(s);
    expect(off.calls, 0);
    expect(s.state.value, PushState.ready);
    await s.stop();
    final none = PushSession(_Prov('jpush', ['x'], supported: false),
        also: [off], register: (id, p, prov) async => fail('不该登记'), unregister: (id) async {})
      ..start();
    expect(none.state.value, PushState.off);
  });

  test('H9：FCM 没给全四个参数、不是安卓：不算配了，不去问原生', () async {
    const full = {'appId': 'a', 'apiKey': 'k', 'projectId': 'p', 'senderId': 's'};
    expect(FcmRegistrar(options: full, android: true).supported, isTrue);
    expect(FcmRegistrar(options: full, android: false).supported, isFalse);
    expect(FcmRegistrar(options: {...full, 'senderId': ''}, android: true).supported, isFalse);
    expect(FcmRegistrar(options: const {}, android: true).supported, isFalse);
    expect(await FcmRegistrar(options: const {}, android: true).registrationId(), isNull);
    expect(FcmRegistrar().supported, isFalse, reason: '这次构建没给 FCM 参数');
    expect(FcmRegistrar(options: full, android: true).provider, 'fcm');
  });
}
